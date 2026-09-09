"""The part that wakes people up.

Every test here is about restraint rather than detection. Noticing a wet float
is the easy half; the half that decides whether this thing is worth running is
whether it can notice one wet float and send one message about it, and then say
nothing for the six hours it stays wet.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pitwatch.domain.engine import AlertEngine
from pitwatch.schemas import (
    AlertsSettings,
    ClampSource,
    ContactInput,
    HealthSource,
    MqttSettings,
    PumpsSettings,
    Severity,
    SiteSettings,
    SmsSettings,
    SmtpSettings,
)


class _Contacts:
    """Stands in for the live view of the panel."""

    def __init__(self, **states: bool) -> None:
        self._roles = states
        self.by_channel: dict[int, bool] = {}

    def state_of(self, channel: int | None):
        return self.by_channel.get(channel) if channel else None


ROLES = ("high_water", "system_alert", "pump1_run", "pump2_run", "pump1_fault", "pump2_fault")


def _store(alerts: AlertsSettings | None = None, **contact_states):
    """Settings shaped the way the reference panel is wired."""
    channels = [
        ContactInput(channel=number, role=role, topic=f"pit/in/{number}")
        for number, role in enumerate(ROLES, start=1)
    ]
    return SimpleNamespace(
        alerts=alerts or AlertsSettings(),
        site=SiteSettings(name="A pit"),
        mqtt=MqttSettings(
            inputs=channels,
            clamps=[
                ClampSource(pump=1, topic="a"),
                ClampSource(pump=2, topic="b"),
            ],
        ),
        pumps=PumpsSettings(),
        smtp=SmtpSettings(),
        sms=SmsSettings(),
    )


def _engine(pool, store, contacts):
    return AlertEngine(pool, store, SimpleNamespace(samples={}), contacts, None, None)


def _wire(contacts, **states):
    """Point the live view at the same channels the settings use."""
    for number, role in enumerate(ROLES, start=1):
        if role in states:
            contacts.by_channel[number] = states[role]
    return contacts


@pytest.fixture
def sent(monkeypatch):
    """Every message the engine tried to send, instead of sending it."""
    posted: list[tuple[str, str, str]] = []

    async def fake_email(settings, to, subject, body):
        posted.append(("email", to, body))
        return "queued"

    async def fake_sms(sms_settings, to, message):
        posted.append(("sms", to, message))

    monkeypatch.setattr("pitwatch.notify.dispatch.email_sender.send", fake_email)
    monkeypatch.setattr("pitwatch.notify.dispatch.sms_sender.send", fake_sms)
    return posted


async def _a_person(pool, **columns):
    fields = {
        "username": "david",
        "name": "David",
        "email": "david@example.com",
        "phone": None,
        "notify_email": True,
        "notify_sms": False,
        "min_severity": "warning",
        "is_admin": True,
        "enabled": True,
    }
    fields.update(columns)
    await pool.execute(
        """
        INSERT INTO app_user (username, name, email, phone, notify_email, notify_sms,
                              min_severity, is_admin, enabled)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        *fields.values(),
    )


# -- what an alert says ------------------------------------------------------


async def test_an_alert_is_stamped_on_the_buildings_clock(pool, sent):
    """The time in a message is the time where the pit is, in the format it
    would be read out in.

    It used to be `datetime.now(UTC).astimezone()`, which is the *server's*
    zone with no argument. The server is a container and its zone is UTC, so
    an alert reaching somebody in New York at ten to eleven in the morning
    said "Time 14:47" and asked them to work out whether that was now or in
    the middle of the night.
    """
    await _a_person(pool)
    store = _store()
    store.site = SiteSettings(name="A pit", timezone="America/New_York")
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, store, contacts).sweep()

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'high_water'")
    when = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p")
    assert f"Time {when}." in detail, detail
    assert "AM" in detail or "PM" in detail


# -- which devices count as a device -----------------------------------------


async def _device(pool, name, online):
    await pool.execute(
        """
        INSERT INTO device_status (device, online) VALUES ($1, $2)
        ON CONFLICT (device) DO UPDATE SET online = excluded.online
        """,
        name,
        online,
    )


async def test_a_health_check_going_quiet_is_the_alert(pool, sent):
    """Named from the settings, so it says which one rather than "a device".

    This could not fire at all for a while. It held a hardcoded map of device
    names, and when the names changed the lookup matched nothing, found nothing
    offline, and reported all clear on every sweep.
    """
    await _a_person(pool)
    store = _store()
    store.mqtt = MqttSettings(
        host="broker",
        enabled=True,
        health=[HealthSource(name="Meter", topic="meter/tick", expect_s=60)],
    )
    await _device(pool, "health0", False)

    await _engine(pool, store, _Contacts()).sweep()

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'device_offline'")
    assert "Meter" in detail, detail


async def test_a_clamp_row_is_not_a_second_voice_for_the_same_thing(pool, sent):
    """A clamp's row only changes state when the broker connection does, and
    that reports every source at once, health checks included. Watching the
    clamps as well would say it twice: two alerts' worth of names in one
    message, and on the dashboard two more lamps repeating the two beside
    them."""
    await _a_person(pool)
    store = _store()
    store.mqtt = MqttSettings(
        host="broker",
        enabled=True,
        clamps=[ClampSource(pump=1, topic="meter/em1:0")],
        health=[HealthSource(name="Meter", topic="meter/tick", expect_s=60)],
    )
    await _device(pool, "clamp1", False)
    await _device(pool, "health0", True)

    await _engine(pool, store, _Contacts()).sweep()

    assert await pool.fetchval("SELECT count(*) FROM alert WHERE rule = 'device_offline'") == 0


# -- saying it once ----------------------------------------------------------


async def test_a_condition_that_stays_true_is_said_once(pool, sent):
    """The whole game. A float that is wet for six hours is one message, not
    seven hundred, and the restraint is the database's rather than a timer's:
    the unique index refuses a second open alert for the same rule, so the
    insert returns nothing and nothing is sent."""
    await _a_person(pool)
    contacts = _wire(_Contacts(), high_water=True)
    engine = _engine(pool, _store(), contacts)

    await engine.sweep()
    await engine.sweep()
    await engine.sweep()

    assert len(sent) == 1, sent
    assert await pool.fetchval("SELECT count(*) FROM alert WHERE rule = 'high_water'") == 1
    assert await pool.fetchval("SELECT count(*) FROM notification") == 1


async def test_it_clears_and_says_so_once(pool, sent):
    await _a_person(pool)
    contacts = _wire(_Contacts(), high_water=True)
    engine = _engine(pool, _store(), contacts)
    await engine.sweep()

    contacts.by_channel[1] = False
    await engine.sweep()
    await engine.sweep()

    assert [kind for kind, _, _ in sent] == ["email", "email"], "raised, then cleared"
    assert "Cleared" in sent[1][2]
    assert await pool.fetchval("SELECT cleared_at IS NOT NULL FROM alert LIMIT 1") is True


async def test_nothing_is_cleared_that_was_never_raised(pool, sent):
    """The update returns a row only when something was open, so a clear cannot
    be announced for an alert nobody was told about."""
    await _a_person(pool)
    contacts = _wire(_Contacts(), high_water=False)

    await _engine(pool, _store(), contacts).sweep()

    assert sent == []
    assert await pool.fetchval("SELECT count(*) FROM alert") == 0


async def test_a_rule_with_nothing_to_read_says_nothing(pool, sent):
    """No contact assigned means no opinion, which is not the same as the
    opinion that all is well. A rule that fires because a wire is missing is
    one somebody learns to ignore."""
    await _a_person(pool)
    store = _store()
    store.mqtt = MqttSettings(inputs=[])  # nothing wired at all

    await _engine(pool, store, _Contacts()).sweep()

    assert sent == []
    assert await pool.fetchval("SELECT count(*) FROM alert") == 0


# -- who hears it ------------------------------------------------------------


async def test_severity_decides_who_is_told(pool, sent):
    """Everybody picks a floor. A warning does not reach somebody who asked for
    critical only, and the same alert reaches somebody who asked for
    everything."""
    await _a_person(pool, username="picky", email="picky@example.com", min_severity="critical")
    await _a_person(pool, username="keen", email="keen@example.com", min_severity="info")

    rules = AlertsSettings()
    rules.high_water.severity = Severity.WARNING
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, _store(rules), contacts).sweep()

    assert [to for _, to, _ in sent] == ["keen@example.com"]


async def test_admins_only_keeps_it_off_everybody_elses_phone(pool, sent):
    """A device going quiet is worth waking somebody who can do something about
    it, and is noise to everybody else."""
    await _a_person(pool, username="boss", email="boss@example.com", is_admin=True)
    await _a_person(pool, username="tenant", email="tenant@example.com", is_admin=False)

    rules = AlertsSettings()
    rules.high_water.admins_only = True
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, _store(rules), contacts).sweep()

    assert [to for _, to, _ in sent] == ["boss@example.com"]


async def test_a_disabled_account_hears_nothing(pool, sent):
    await _a_person(pool, enabled=False)
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, _store(), contacts).sweep()

    assert sent == []
    assert await pool.fetchval("SELECT count(*) FROM alert") == 1, "still recorded"


async def test_email_only_means_email_only(pool, sent):
    """The shape the reference installation is actually in: one person, email
    on, texts off."""
    await _a_person(pool, notify_email=True, notify_sms=False, phone="+12125550142")
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, _store(), contacts).sweep()

    assert [kind for kind, _, _ in sent] == ["email"]


# -- when the sending itself fails -------------------------------------------


async def test_a_failed_send_is_written_down_rather_than_raised(pool, monkeypatch):
    """That we tried to tell somebody and could not is the thing they need to
    see afterwards, and a record written only on success cannot show it. A
    broken mail server must not stop the alert being recorded either."""

    async def explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("pitwatch.notify.dispatch.email_sender.send", explode)
    await _a_person(pool)
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, _store(), contacts).sweep()

    assert await pool.fetchval("SELECT count(*) FROM alert") == 1
    row = await pool.fetchrow("SELECT status, error FROM notification")
    assert row["status"] == "failed"
    assert "connection refused" in row["error"]


# -- the rules that need a clamp ---------------------------------------------


async def test_a_held_rule_asks_to_be_looked_at_again(pool, sent):
    """The panel alarm was tripped by hand on 2026-09-08 and held for fourteen
    seconds. Nothing was raised and nothing was sent.

    The contact change nudged a sweep, the sweep started the five second hold
    and returned nothing, and the next look was the thirty second tick, which
    arrived after the alarm had already cleared. The hold was not a delay, it
    was a filter that dropped every alarm shorter than a sweep, on the contact
    that carries the controller's own alarm.

    A rule that defers now says when it wants asking again, and the run loop
    waits that long instead of a whole tick.
    """
    await _a_person(pool)
    store = _store()
    engine = _engine(pool, store, _wire(_Contacts(), system_alert=True))

    # The first look, the one the contact change triggers. It starts the hold
    # and says nothing, which is correct.
    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "panel_alert" not in raised, "held back to see whether anything explains it"
    assert engine._recheck_at is not None, "and it asked to be looked at again"

    hold = store.alerts.panel_alert.hold_s
    assert engine._recheck_at - asyncio.get_running_loop().time() <= hold + 0.1


async def test_a_panel_alarm_shorter_than_a_sweep_is_still_raised(pool, sent):
    """The whole point of the recheck: the alarm goes up, the hold elapses, and
    it is raised without waiting for the tick that would have missed it."""
    await _a_person(pool)
    store = _store()
    engine = _engine(pool, store, _wire(_Contacts(), system_alert=True))

    await engine.sweep()
    # The hold elapsing, without a thirty second tick and without the contact
    # changing again.
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "panel_alert" in raised


async def test_a_pump_with_no_runs_on_record_is_not_reported_idle(pool, sent):
    """This one shipped and sent a text at three in the morning.

    The readings were wiped on 2026-09-08. Twenty two minutes later pump 1 ran,
    and the rule announced that pump 2 had not run in twenty five hours: with
    no rows for pump 2 it invented a number one hour past the threshold, so
    "nothing recorded" read as "definitely broken". A fresh installation would
    have done the same on its first ever call for water.

    A pump with no runs on record has not been idle, it has not been watched.
    """
    await _a_person(pool)
    engine = _engine(pool, _store(), _wire(_Contacts()))

    # The whole record is one run of pump 1, a minute old. Pump 2 has none.
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, ended_at, duration_s, started_by) "
        "VALUES (1, now() - interval '60 seconds', now() - interval '48 seconds', 12, 'contact')"
    )

    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "pump_idle" not in raised, "a minute of watching cannot support a claim about a day"


async def test_a_pump_that_really_has_sat_out_is_still_reported(pool, sent):
    """The guard must not turn the rule off. Once the record reaches back
    further than the threshold, never having run is exactly what it is for: an
    overload nobody saw, a failed contactor coil, a seized motor."""
    await _a_person(pool)
    engine = _engine(pool, _store(), _wire(_Contacts()))

    # Two days of pump 1 doing all the work, and pump 2 never once starting.
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, ended_at, duration_s, started_by) "
        "VALUES (1, now() - interval '2 days', now() - interval '2 days' + interval '12 seconds',"
        "        12, 'contact'),"
        "       (1, now() - interval '60 seconds', now() - interval '48 seconds', 12, 'contact')"
    )

    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "pump_idle" in raised


async def test_a_contactor_with_no_current_stays_quiet_on_an_unfitted_clamp(pool, sent):
    """The reference installation has one CT and two pumps, so pump 2's channel
    reads a perfectly convincing 0.00 A on every run.

    Without this guard the rule fires on every single run of a pump that is
    working fine, which is how somebody learns to ignore the one alert that
    means a motor is not turning.
    """
    await _a_person(pool)
    contacts = _wire(_Contacts(), pump2_run=True)
    store = _store()
    engine = _engine(pool, store, contacts)
    # A run open long enough to be judged, and a clamp that has never in its
    # life seen current.
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) "
        "VALUES (2, now() - interval '60 seconds', 'contact')"
    )

    await engine.sweep()

    raised = await pool.fetch("SELECT rule FROM alert")
    assert "contactor_no_current" not in [row["rule"] for row in raised]


async def test_a_contactor_with_no_current_fires_once_the_clamp_has_proved_itself(pool, sent):
    """The same rule on a channel that has read current before, which is the
    difference between a CT nobody fitted and a motor that has stopped
    turning."""
    await _a_person(pool)
    contacts = _wire(_Contacts(), pump1_run=True)
    store = _store()
    engine = _engine(pool, store, contacts)
    engine._live = SimpleNamespace(samples={0: SimpleNamespace(current=0.0)})

    await pool.execute(
        "INSERT INTO em_sample (ts, channel, current) VALUES (now() - interval '1 day', 0, 15.4)"
    )
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) "
        "VALUES (1, now() - interval '60 seconds', 'contact')"
    )

    await engine.sweep()

    rules = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "contactor_no_current" in rules
