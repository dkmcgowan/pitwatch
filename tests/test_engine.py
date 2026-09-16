"""The part that wakes people up.

Every test here is about restraint rather than detection. Noticing a wet float
is the easy half; the half that decides whether this thing is worth running is
whether it can notice one wet float and send one message about it, and then say
nothing for the six hours it stays wet.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
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
    PanelButtonSettings,
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
        panel_button=PanelButtonSettings(),
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
        "role": "owner",
        "enabled": True,
    }
    fields.update(columns)
    await pool.execute(
        """
        INSERT INTO app_user (username, name, email, phone, notify_email, notify_sms,
                              min_severity, role, enabled)
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
    # The all clear says what happened rather than opening with the word
    # Cleared and repeating the alarm's own title. See the test that reads
    # every rule's all clear for why that mattered.
    assert "The top float" in sent[1][2] and "has dropped" in sent[1][2], sent[1][2]
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
    await _a_person(pool, username="boss", email="boss@example.com", role="owner")
    await _a_person(pool, username="tenant", email="tenant@example.com", role="viewer")

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


async def test_a_pulsing_panel_alarm_is_raised_rather_than_filtered_out(pool, sent):
    """The alarm output pulses. The hold used to require an unbroken stretch,
    so it could never reach the end against a signal that keeps stopping.

    An overload was tripped by hand on 2026-09-12 and the contact came back a
    one hertz square wave: up for half a second, down for half a second, for
    three and a half seconds, until the silence button was pressed and it went
    steady. The alarm was reported ten seconds late and only because somebody
    was standing in front of the panel. Left alone it would have pulsed all
    night and said nothing.
    """
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), system_alert=True)
    engine = _engine(pool, store, contacts)

    # Up, and the hold starts.
    await engine.sweep()
    started = engine._panel_alert_since
    assert started is not None

    # The dark half of the pulse. It is not the alarm ending, so the hold is
    # not thrown away and nothing is reported either way.
    contacts.by_channel[2] = False
    await engine.sweep()
    assert engine._panel_alert_since == started, "the gap did not restart the hold"
    assert engine._recheck_at is not None, "and it asked to be looked at again"
    assert not await pool.fetch("SELECT 1 FROM alert")

    # Up again, and the hold is now old enough to mean something.
    contacts.by_channel[2] = True
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "panel_alert" in raised


async def test_one_blip_and_nothing_after_it_is_still_filtered(pool, sent):
    """The other half of the same change. Tolerating the gaps in a pulse must
    not turn into raising a critical alert for a single flicker, which is what
    the hold was there to stop."""
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), system_alert=True)
    engine = _engine(pool, store, contacts)

    await engine.sweep()
    contacts.by_channel[2] = False
    await engine.sweep()

    # The gap outlasting a pulse, without the contact ever coming back up.
    engine._panel_alert_quiet_since -= timedelta(seconds=store.alerts.panel_alert.pulse_gap_s + 1)
    await engine.sweep()

    assert not await pool.fetch("SELECT 1 FROM alert"), "one flicker is not an alarm"
    assert engine._panel_alert_since is None, "and the hold was thrown away"


async def test_a_panel_alarm_clears_once_the_quiet_outlasts_a_pulse(pool, sent):
    """Raised, and then genuinely over. The gap costs a few seconds on the
    clear, which is the price of not reading every pulse as the end."""
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), system_alert=True)
    engine = _engine(pool, store, contacts)

    await engine.sweep()
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()
    assert await pool.fetchval("SELECT count(*) FROM alert WHERE cleared_at IS NULL") == 1

    contacts.by_channel[2] = False
    await engine.sweep()
    still_open = await pool.fetchval("SELECT count(*) FROM alert WHERE cleared_at IS NULL")
    assert still_open == 1, "a gap this short might still be a pulse"

    engine._panel_alert_quiet_since -= timedelta(seconds=store.alerts.panel_alert.pulse_gap_s + 1)
    await engine.sweep()
    assert await pool.fetchval("SELECT count(*) FROM alert WHERE cleared_at IS NULL") == 0


async def _open(pool, rule: str) -> int:
    """How many of one rule's alerts are open."""
    return await pool.fetchval(
        "SELECT count(*) FROM alert WHERE rule = $1 AND cleared_at IS NULL", rule
    )


async def test_an_explained_panel_alarm_stays_explained_after_it_is_explained_away(pool, sent):
    """The overload is reset, the panel alarm is still latched, and nothing is
    open to account for it any more. That must not become a second alert.

    On 2026-09-12 two real trips left exactly this window open, 6.6 s and 8.4 s
    wide: the overload contact went healthy and the controller held its alarm
    until somebody pressed the button. Nothing raised, but only because no
    sweep happened to land in either window. The message it would have sent is
    "nothing here explains it", to somebody who had just finished dealing with
    the overload that explained it.
    """
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), system_alert=True, pump1_fault=True)
    engine = _engine(pool, store, contacts)

    await engine.sweep()
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()

    rules = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "overload" in rules
    assert "panel_alert" not in rules, "the overload explains it"

    # The overload is reset. Its alert clears; the panel alarm does not, which
    # is what the panel actually does.
    contacts.by_channel[5] = False
    await engine.sweep()
    assert await _open(pool, "overload") == 0
    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "panel_alert" not in raised, "still the same alarm, still explained"


async def test_an_unexplained_panel_alarm_is_not_silenced_by_a_later_alert(pool, sent):
    """The other direction. Nothing explained it when it came up, so it was
    raised, and something arriving afterwards does not retract that."""
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), system_alert=True)
    engine = _engine(pool, store, contacts)

    await engine.sweep()
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()
    assert await _open(pool, "panel_alert") == 1

    contacts.by_channel[1] = True  # high water, which speaks for itself
    await engine.sweep()

    assert await _open(pool, "panel_alert") == 1, "one alarm, one alert"


async def test_the_explanation_is_decided_again_for_the_next_alarm(pool, sent):
    """Latched for the life of one alarm, not for the life of the process. The
    alarm ending is what throws the answer away."""
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), system_alert=True, pump1_fault=True)
    engine = _engine(pool, store, contacts)

    await engine.sweep()
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()
    assert engine._panel_alert_explained is True

    # Everything goes away.
    contacts.by_channel[2] = False
    contacts.by_channel[5] = False
    engine._panel_alert_quiet_since = datetime.now(UTC) - timedelta(
        seconds=store.alerts.panel_alert.pulse_gap_s + 1
    )
    await engine.sweep()
    assert engine._panel_alert_explained is None

    # A second alarm, this one with nothing behind it.
    contacts.by_channel[2] = True
    await engine.sweep()
    engine._panel_alert_since -= timedelta(seconds=store.alerts.panel_alert.hold_s + 1)
    await engine.sweep()
    assert engine._panel_alert_explained is False
    assert await _open(pool, "panel_alert") == 1


async def test_no_alert_goes_out_with_its_placeholders_still_in_it(pool, sent):
    """The one that should have existed from the start.

    Every rule advertises the placeholders its message can use, and for four of
    them nothing ever supplied the value. `fill` leaves what it does not know
    alone, on the argument that a slightly odd alert beats a silent one, so it
    failed quietly: the high water rule, which is the most important sentence
    this thing can send, went out reading "The top float is wet,
    {pumps_state}." Nothing noticed because nothing asserted on the finished
    sentence, only on which rule fired.

    Found on 2026-09-12 by reading a real overload text.
    """
    await _a_person(pool)
    alerts = AlertsSettings()
    # Built off the defaults rather than a bare rule, because the point of this
    # test is the wording the defaults carry.
    alerts.run_too_long.longer_than_ms = 1000
    alerts.run_too_long.enabled = True
    store = _store(alerts)
    contacts = _wire(
        _Contacts(),
        high_water=True,
        pump1_fault=True,
        pump2_run=True,
    )
    engine = _engine(pool, store, contacts)
    # An open run, so the rule that talks about a duration has one to talk about.
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) "
        "VALUES (2, now() - interval '90 seconds', 'contact')"
    )

    await engine.sweep()

    rows = await pool.fetch("SELECT rule, detail FROM alert")
    assert rows, "nothing fired, so this proved nothing"
    for row in rows:
        assert "{" not in row["detail"], f"{row['rule']} sent a raw placeholder: {row['detail']}"
        assert "}" not in row["detail"], row["rule"]

    said = {row["rule"]: row["detail"] for row in rows}
    assert "high_water" in said and "neither pump is running" not in said["high_water"]
    assert "run_too_long" in said and "1 min 30 s" in said["run_too_long"]


async def test_an_overload_names_the_relay_to_go_and_reset(pool, sent):
    """Anybody writing their own wording can ask which relay it was, and used
    to get the braces back."""
    await _a_person(pool)
    alerts = AlertsSettings()
    alerts.overload.message = "Go and reset {overload} at {site}."
    store = _store(alerts)
    engine = _engine(pool, store, _wire(_Contacts(), pump1_fault=True))

    await engine.sweep()

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'overload'")
    assert "{overload}" not in detail
    assert "Pump 1 overload" in detail


async def test_clearing_an_overload_says_the_pump_is_not_back_yet(pool, sent):
    """The fault going away is not the pump coming back.

    Measured on 2026-09-12: the relay was reset at 17:18:52 and the pump did
    not run again until 17:29:56, through five calls on the other one, because
    the controller holds it out until the panel alarm is cleared by hand. An
    all clear that does not say so reads as "nothing to do".
    """
    await _a_person(pool)
    store = _store()
    store.alerts.overload.tell_when_it_clears = True
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    await engine.sweep()

    contacts.by_channel[5] = False
    await engine.sweep()

    body = [body for _, _, body in sent][-1]
    assert not body.startswith("Cleared"), "it is not cleared, the pump is out"
    assert "Clear the alarm at the panel" in body, "nothing is going to do it"
    assert "Pump 2 is covering on its own" in body
    assert "  " not in body, "an empty value left a gap in the sentence"


async def test_an_overload_clearing_while_the_other_is_out_says_so(pool, sent):
    """The same sentence with the frightening half. One pump back from an
    overload while the other is still tripped is not cover, it is nothing."""
    await _a_person(pool)
    store = _store()
    store.alerts.overload.tell_when_it_clears = True
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=True, system_alert=True)
    engine = _engine(pool, store, contacts)
    await engine.sweep()

    contacts.by_channel[5] = False
    await engine.sweep()

    # Both rules clear on the same sweep, so look for the one under test
    # rather than whichever went out last.
    assert any("nothing is pumping at all" in body for _, _, body in sent)


async def test_both_overloads_out_is_its_own_alert(pool, sent):
    """Two pumps out is not two faults, it is no pumping."""
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=True, system_alert=True)
    engine = _engine(pool, store, contacts)

    await engine.sweep()

    raised = [row["rule"] for row in await pool.fetch("SELECT rule FROM alert")]
    assert "both_overloads" in raised
    assert raised.count("overload") == 2, "and each relay still says which one it is"

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'both_overloads'")
    assert "{" not in detail
    assert "Nothing is pumping" in detail

    # One coming back ends it, and the other pump's own alert stays open.
    contacts.by_channel[5] = False
    await engine.sweep()
    assert await _open(pool, "both_overloads") == 0
    assert await _open(pool, "overload") == 1

    # Nothing is sent about half of it being over. These rules do not announce
    # their own clears any more: one message speaks for the whole incident and
    # it waits until there is nothing left out, because eight true sentences
    # about pumps and relays never added up to "it is finished".
    assert not [body for _, _, body in sent if body.startswith("All clear")], (
        "one pump is still out, so it is not all clear"
    )
    assert not [body for _, _, body in sent if "is still out" in body]


def _wired(**over):
    """Settings with a panel button wired and, unless told otherwise, set to
    press it by itself."""
    from pitwatch.schemas import PanelButtonSettings

    fields = {"enabled": True, "topic": "shellyemg3/rpc", "auto_recover": True}
    fields.update(over)
    return PanelButtonSettings(**fields)


async def _pressed_everything(engine, timeout: float = 2.0) -> None:
    """Wait for any recovery to finish.

    A recovery runs off the sweep on purpose, because it holds a contact closed
    for seconds and the sweep is what notices the other pump. So a test that
    checks what it did has to let it happen first.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while engine._recovering and loop.time() < end:
        await asyncio.sleep(0.01)
    if engine._hushing is not None:
        with contextlib.suppress(Exception):
            await engine._hushing
    await asyncio.sleep(0.01)


async def _flash(engine, contacts, times: int = 3) -> None:
    """Make the alarm contact flash, the way the panel does.

    The controller pulses that output while nobody has acknowledged it, half a
    second each way, and goes steady once somebody has. So a test about
    silencing has to flash: a contact held true is an alarm that has already
    been dealt with, and the point is to not press at one of those.
    """
    alarm = engine._store.mqtt.channel_for("system_alert")
    for _ in range(times):
        for state in (True, False):
            contacts.by_channel[alarm] = state
            await engine.on_events([SimpleNamespace(channel=alarm, state=state, label="alarm")])
    contacts.by_channel[alarm] = True


def _watching(engine):
    """Record every press instead of making one, and do not wait about."""
    pressed: list[str] = []

    async def press(action: str) -> str | None:
        pressed.append(action)
        return None

    engine.press = press
    return pressed


async def test_an_overload_silences_the_alarm_and_then_puts_the_pump_back(pool, sent):
    """The two presses, at the two moments that matter.

    Silence when the fault arrives, because the horn is the least useful part
    of it. Reset when the relay clears, because that is the press that actually
    returns the pump to the rotation: the fault going away does not. Measured
    on 2026-09-12, eleven minutes and five calls on one pump.
    """
    await _a_person(pool)
    store = _store()
    store.panel_button = _wired()
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    pressed = _watching(engine)

    # The panel starts flashing, which is it asking to be acknowledged.
    await engine.sweep()
    await _flash(engine, contacts)
    await _pressed_everything(engine)
    assert pressed == ["silence"], "the horn first, and nothing else yet"

    # The relay is pushed back in and the panel drops its alarm, which is
    # what clearing it looks like from here. Both together, because a fault
    # going while the alarm stays up is a different case and has its own test.
    contacts.by_channel[5] = False
    contacts.by_channel[2] = False
    await engine.sweep()
    await _pressed_everything(engine)
    assert pressed == ["silence", "reset"], "nothing left to silence"

    # One message for the whole thing, once there is nothing out and nothing
    # sounding. Not a line per pump: the point is somebody knowing it is over.
    #
    # The alarm has to have been quiet for the pulse gap first, which is what
    # stops half a flash being read as the end of one.
    engine._panel_alert_quiet_since -= timedelta(seconds=store.alerts.panel_alert.pulse_gap_s + 1)
    await engine.sweep()
    said = [body for _, _, body in sent if body.startswith("All clear")]
    assert said, "somebody has to be told it is finished"
    assert "Both pumps are in the rotation and the alarm is off" in said[-1]
    # One overload, so no lecture about having them looked at.
    assert "worth having" not in said[-1]

    # Two messages for one pump: it tripped, and then it was over. The relay
    # coming back is not news of its own, because the all clear covers it.
    posted = [body for _, _, body in sent]
    assert len(posted) == 2, posted
    assert "Pump 1 overload tripped" in posted[0]
    assert posted[1].startswith("All clear")


async def test_recovery_gives_up_on_a_pump_that_keeps_tripping(pool, sent):
    """The limit, and the reason for it.

    A relay on auto reset comes back once the bimetal has cooled, so clearing
    the alarm for it every time is a loop. Around a motor that overloads
    because something is wrong with it, that loop is how one pump out of
    service becomes two burned out overnight. Past the limit it stops, leaves
    the alarm up, and says how many times.
    """

    await _a_person(pool)
    store = _store()
    store.panel_button = _wired(max_trips=2, within_minutes=60)
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    pressed = _watching(engine)

    # Three earlier trips already on the record, inside the window.
    for minutes in (5, 10, 15):
        await pool.execute(
            """
            INSERT INTO alert (rule, severity, pump, title, detail, raised_at, cleared_at)
            VALUES ('overload', 'critical', 1, 'Overload tripped', 'x',
                    now() - ($1::int * interval '1 minute'),
                    now() - ($1::int * interval '1 minute'))
            """,
            minutes,
        )

    await engine.sweep()
    await _flash(engine, contacts)
    await _pressed_everything(engine)

    # The horn still stops. The limit is about putting a pump back into
    # service, not about leaving a beacon sounding in a basement: the thing
    # that fetches somebody is the message, and that still says how many times
    # this has happened and that nothing is going to fix it.
    assert "reset" not in pressed, "it stopped putting the pump back"
    assert pressed == ["silence"]

    detail = await pool.fetchval(
        "SELECT detail FROM alert WHERE rule = 'overload' AND cleared_at IS NULL"
    )
    assert "automatic recovery has stopped" in detail
    assert "4 trips in 60 minutes" in detail


async def test_nothing_is_pressed_when_nobody_asked_for_it(pool, sent):
    """Off is off, both halves of it. A contact wired across a button on a
    live panel does not get pressed because a setting defaulted to on."""

    await _a_person(pool)
    store = _store()
    store.panel_button = _wired(auto_recover=False, auto_silence=False)
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    pressed = _watching(engine)

    await engine.sweep()
    await _pressed_everything(engine)
    assert pressed == []

    # And the message tells somebody to go and do it themselves.
    detail = await pool.fetchval(
        "SELECT detail FROM alert WHERE rule = 'overload' AND cleared_at IS NULL"
    )
    assert "Reset the overload" in detail and "clear the alarm at the panel" in detail


async def test_the_horn_can_be_stopped_without_the_pump_being_put_back(pool, sent):
    """Two questions, two settings.

    Silencing costs nothing and hides nothing: the alarm stays raised, the
    alert stays open, the message still goes out. All it stops is a beacon
    sounding in a basement at nobody. Deciding a pump is fit to run again is a
    different question, and wanting the first without the second is reasonable.
    """

    await _a_person(pool)
    store = _store()
    store.panel_button = _wired(auto_recover=False, auto_silence=True)
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    pressed = _watching(engine)

    await engine.sweep()
    await _flash(engine, contacts)
    await _pressed_everything(engine)
    assert pressed == ["silence"]

    detail = await pool.fetchval(
        "SELECT detail FROM alert WHERE rule = 'overload' AND cleared_at IS NULL"
    )
    assert "The alarm has been silenced" in detail
    assert "clear the alarm at the panel" in detail, "and somebody has to do the rest"

    contacts.by_channel[5] = False
    await engine.sweep()
    await _pressed_everything(engine)
    assert "reset" not in pressed


async def test_a_limit_of_zero_never_gives_up(pool, sent):
    """No limit, rather than no resetting.

    Zero briefly meant the opposite, which was a second way of saying what
    turning the reset off already said. Two settings for one behavior is worse
    than either, so zero now reads the way a zero usually does.
    """

    await _a_person(pool)
    store = _store()
    store.panel_button = _wired(max_trips=0)
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    pressed = _watching(engine)

    # Plenty of trips already on the record, well past any sane limit.
    for minutes in range(1, 9):
        await pool.execute(
            """
            INSERT INTO alert (rule, severity, pump, title, detail, raised_at, cleared_at)
            VALUES ('overload', 'critical', 1, 'Overload tripped', 'x',
                    now() - ($1::int * interval '1 minute'),
                    now() - ($1::int * interval '1 minute'))
            """,
            minutes,
        )

    await engine.sweep()
    await _pressed_everything(engine)
    contacts.by_channel[5] = False
    await engine.sweep()
    await _pressed_everything(engine)

    assert "reset" in pressed, "nine trips and it still puts the pump back"

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'overload'")
    assert "recovery has stopped" not in detail


async def test_one_incident_is_four_messages_and_the_last_one_says_it_is_over(pool, sent):
    """What the chain reads like on a phone.

    On 2026-09-13 a dual trip sent eight: two trips, both pumps out, two
    relays clearing, both pumps out clearing, and two pumps rejoining. Every
    one true, and to know it was finished you had to hold all eight in your
    head and notice both pumps had appeared in the "back" list. The last one
    was about one pump.

    Four now. It started, it got worse, it is over, and the last one says so.
    """
    await _a_person(pool)
    store = _store()
    store.panel_button = _wired()
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    _watching(engine)

    await engine.sweep()
    await _flash(engine, contacts)
    await _pressed_everything(engine)

    contacts.by_channel[6] = True
    await engine.sweep()
    await _pressed_everything(engine)

    # Both relays pushed back in and the panel drops its alarm.
    contacts.by_channel[5] = False
    contacts.by_channel[6] = False
    contacts.by_channel[2] = False
    await engine.sweep()
    await _pressed_everything(engine)
    engine._panel_alert_quiet_since -= timedelta(seconds=store.alerts.panel_alert.pulse_gap_s + 1)
    await engine.sweep()

    posted = [body for _, _, body in sent]
    assert len(posted) == 4, posted

    assert "Pump 1 overload tripped" in posted[0]
    assert "Pump 2 overload tripped" in posted[1]
    assert "nothing is pumping at all" in posted[1]
    assert "BOTH pumps" in posted[2]

    # The one somebody is actually looking for.
    assert posted[3].startswith("All clear")
    assert "Both pumps are in the rotation and the alarm is off" in posted[3]
    assert "2 overloads" in posted[3]
    assert "worth having the pumps looked at" in posted[3]


async def test_it_says_where_things_stand_while_they_still_do_not(pool, sent):
    """The middle of a long incident is silent, and silence reads as fixed.

    The rules speak when things change, so twenty minutes of a pump sitting
    out and an alarm sounding produced nothing at all on 2026-09-13: not being
    over is not an event, and it is the thing somebody wants to know about.
    """
    await _a_person(pool)
    store = _store()
    store.alerts.unresolved_every_minutes = 10
    store.panel_button = _wired()
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    _watching(engine)

    await engine.sweep()
    await _pressed_everything(engine)
    assert not [body for _, _, body in sent if body.startswith("Still not right")]

    # Ten minutes on, with nothing having changed.
    engine._incident_since -= timedelta(minutes=10)
    await engine.sweep()

    said = [body for _, _, body in sent if body.startswith("Still not right")]
    assert said, "somebody has to be told it is still going"
    assert "10 minutes on" in said[-1]
    assert "Pump 1 is out on overload and Pump 2 is covering" in said[-1]
    assert "The panel alarm is still up" in said[-1]

    # And not again straight away.
    await engine.sweep()
    assert len([b for _, _, b in sent if b.startswith("Still not right")]) == 1

    # It stops when it is over, and the all clear is the last word.
    contacts.by_channel[5] = False
    contacts.by_channel[2] = False
    await engine.sweep()
    await _pressed_everything(engine)
    engine._panel_alert_quiet_since -= timedelta(seconds=store.alerts.panel_alert.pulse_gap_s + 1)
    await engine.sweep()
    engine._nagged_at = None
    await engine.sweep()

    assert len([b for _, _, b in sent if b.startswith("Still not right")]) == 1
    assert [b for _, _, b in sent if b.startswith("All clear")]


async def test_the_reminder_can_be_turned_off(pool, sent):
    """Zero means never. A nag nobody asked for is the thing that teaches
    somebody to stop reading these."""
    await _a_person(pool)
    store = _store()
    store.alerts.unresolved_every_minutes = 0
    store.panel_button = _wired()
    engine = _engine(
        pool, store, _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    )
    _watching(engine)

    await engine.sweep()
    await _pressed_everything(engine)
    engine._incident_since -= timedelta(hours=3)
    await engine.sweep()

    assert not [body for _, _, body in sent if body.startswith("Still not right")]


async def test_every_fresh_alarm_is_silenced_and_a_dealt_with_one_is_not(pool, sent):
    """Walked on the real panel on 2026-09-13, four times over.

    This controller raises a fresh alarm whenever one is cleared while a pump
    is still out, so a single incident produces several: one for the first
    trip, one for the second, and one after each relay is pushed back in.
    Every attempt to work out from the faults which of those was new got some
    of them wrong, and each wrong one left the horn sounding at nobody.

    The flashing says it directly. This panel flashes while nobody has
    acknowledged it and holds steady once somebody has, so the question is
    never which alarm it is, only whether it is still asking.
    """
    await _a_person(pool)
    store = _store()
    store.panel_button = _wired()
    contacts = _wire(_Contacts(), pump1_fault=True, pump2_fault=False, system_alert=True)
    engine = _engine(pool, store, contacts)
    pressed = _watching(engine)

    await engine.sweep()
    await _flash(engine, contacts)
    await _pressed_everything(engine)
    assert pressed == ["silence"]

    # Steady now, which is what a silenced alarm looks like. Sweeping at it
    # again must not press: a tap into a panel with nothing to silence is how
    # the controller runs its lamp test, so it would raise one, not end one.
    for _ in range(3):
        await engine.sweep()
        await _pressed_everything(engine)
    assert pressed == ["silence"], "an alarm already dealt with is left alone"

    # The second pump goes and the panel starts asking again.
    engine._hushed_at = None
    contacts.by_channel[6] = True
    await engine.sweep()
    await _flash(engine, contacts)
    await _pressed_everything(engine)
    assert pressed.count("silence") == 2, "a fresh alarm is a fresh press"

    # A relay pushed back in. The panel clears the alarm and raises it again
    # for the pump still out, and the count of pumps out has gone down rather
    # than up: the version that counted faults missed this one twice.
    contacts.by_channel[5] = False
    await engine.sweep()
    await _pressed_everything(engine)
    assert "reset" in pressed, "one pump back is worth clearing the alarm for"

    # And then the panel raises it again for the pump still out. That happens
    # after the reset, not during it: silencing into a reset would cut a three
    # second press down to a tap.
    engine._hushed_at = None
    await _flash(engine, contacts)
    await _pressed_everything(engine)
    assert pressed.count("silence") == 3, "one pump back is a fresh alarm too"


async def test_two_presses_never_share_the_contact(pool):
    """One contact, and the panel reads how long it is held.

    The danger is a short press cutting a long one short. Two silences
    overlapping are harmless: the first release opens the contact and the
    second finds it open already. A silence landing in the middle of a reset
    is not, because it releases at four hundred milliseconds and turns a three
    second press into a tap. The alarm stays up, the pump stays out, and
    nothing reports a failure: both messages were sent and both were accepted.

    Pump 1's relay clearing at the moment pump 2 trips is exactly that pair.

    Tested on the supervisor because that is where the one connection lives
    and therefore where the queue has to be. The engine is handed this same
    method, so ordering it here covers the automatic presses and the ones a
    person makes from the page alike.
    """
    from pitwatch.ingest.supervisor import Supervisor

    store = _store()
    store.panel_button = _wired()
    boss = Supervisor(pool, store, None, None)

    holding = 0
    overlapped = False
    sent_topics: list[str] = []

    async def send(topic: str, payload: str) -> str | None:
        nonlocal holding, overlapped
        sent_topics.append(topic)
        if '"on": true' in payload or '"on":true' in payload:
            holding += 1
            overlapped = overlapped or holding > 1
        else:
            holding -= 1
        await asyncio.sleep(0)
        return None

    boss.send = send

    await asyncio.gather(boss.press("reset"), boss.press("silence"))

    assert not overlapped, "a silence was on the contact during a reset"
    assert len(sent_topics) == 4, "two presses, each held and released"


async def test_a_person_is_told_to_wait_rather_than_queued_behind_a_press(pool):
    """A recovery waits its turn. A person does not.

    Queueing is right for the automatic presses: a silence that arrives during
    a reset still has to happen, and dropping it leaves a horn sounding. It is
    wrong for somebody at the page, because a button that appears to do nothing
    and then fires three seconds later, after whatever it was queued behind, is
    a button that gets pressed twice.
    """
    from pitwatch.ingest.supervisor import Supervisor

    store = _store()
    store.panel_button = _wired()
    boss = Supervisor(pool, store, None, None)

    async def send(topic: str, payload: str) -> str | None:
        await asyncio.sleep(0)
        return None

    boss.send = send

    held = asyncio.create_task(boss.press("reset"))
    await asyncio.sleep(0)

    turned_away = await boss.press("silence", wait=False)
    assert turned_away and "already being pressed" in turned_away

    # And the one that waits still happens.
    assert await held is None


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
    # Reading zero, and reading it now: the clamp is answering during the run,
    # which is what separates a stopped motor from a stopped meter.
    engine._live = SimpleNamespace(samples={0: SimpleNamespace(current=0.0, ts=datetime.now(UTC))})

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


async def _a_reader(pool, store, contacts, *, history=None, recent=None, samples=None):
    """The engine with the two history readers a drift rule needs, and a live
    meter reading for the rule that compares the contact against the clamp."""
    return AlertEngine(
        pool, store, SimpleNamespace(samples=samples or {}), contacts, history, recent
    )


async def test_every_rule_that_can_fire_sends_a_finished_sentence(pool, sent):
    """The wording test widened to the whole list.

    The one that came before it built a single world, and that world only ever
    fired three rules: high water, overload and ran too long. The other
    thirteen had their finished sentence asserted by nothing at all, which is
    the same hole that let "The top float is wet, {pumps_state}." reach a
    phone. A rule nobody has ever read the output of is a rule nobody knows
    the wording of.

    So this walks scenarios rather than one arrangement, and every alert any
    of them writes has to read as English.
    """
    await _a_person(pool)

    fired: set[str] = set()

    async def run(scenario, alerts, contacts_states, setup=None, **engine_bits):
        await pool.execute("DELETE FROM notification")
        await pool.execute("DELETE FROM alert")
        await pool.execute("DELETE FROM pump_run")
        await pool.execute("DELETE FROM pump_cycle")
        await pool.execute("DELETE FROM em_sample")
        await pool.execute("DELETE FROM device_status")
        store = _store(alerts)
        contacts = _wire(_Contacts(), **contacts_states)
        if setup is not None:
            await setup(pool, store)
        engine = await _a_reader(pool, store, contacts, **engine_bits)
        await engine.sweep()
        rows = await pool.fetch("SELECT rule, detail FROM alert")
        for row in rows:
            assert "{" not in row["detail"], (
                f"{scenario}: {row['rule']} sent a raw placeholder: {row['detail']}"
            )
            assert "}" not in row["detail"], f"{scenario}: {row['rule']}: {row['detail']}"
            fired.add(row["rule"])
        return {row["rule"]: row["detail"] for row in rows}

    # The panel, everything wrong at once.
    alerts = AlertsSettings()
    alerts.run_too_long.longer_than_ms = 1000
    await run(
        "the panel in trouble",
        alerts,
        {
            "high_water": True,
            "pump1_fault": True,
            "pump2_fault": True,
            "pump1_run": True,
            "pump2_run": True,
        },
        setup=lambda pool, store: pool.execute(
            "INSERT INTO pump_run (pump, started_at, started_by) "
            "VALUES (2, now() - interval '90 seconds', 'contact')"
        ),
    )

    # The alarm with nothing to explain it.
    alerts = AlertsSettings()
    alerts.panel_alert.hold_s = 0
    await run("an unexplained alarm", alerts, {"system_alert": True})

    # A contactor closed on a motor that is not turning, which needs the clamp
    # to have proved it can see current at all.
    # Pump 1's readings are filed under channel 0: the clamp's channel is the
    # pump number less one, which is the kind of off by one that makes a rule
    # look broken when the test is what is wrong.
    async def a_dead_motor(pool, store):
        await pool.execute(
            "INSERT INTO em_sample (ts, channel, current) VALUES (now() - interval '1 day', 0, 12.0)"
        )
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, started_by) "
            "VALUES (1, now() - interval '30 seconds', 'contact')"
        )

    alerts = AlertsSettings()
    await run(
        "a motor that is not turning",
        alerts,
        {"pump1_run": True},
        setup=a_dead_motor,
        # Heard during this run, which is what gives the rule an opinion.
        samples={0: SimpleNamespace(current=0.2, ts=datetime.now(UTC))},
    )

    # Drawing more than its limit.
    async def drawing_too_much(pool, store):
        for step in range(4):
            await pool.execute(
                "INSERT INTO em_sample (ts, channel, current) VALUES (now() - $1::interval, 0, 30.0)",
                timedelta(seconds=step),
            )

    alerts = AlertsSettings()
    alerts.over_current.enabled = True
    alerts.over_current.pump1_amps = 20.0
    await run("drawing too much", alerts, {}, setup=drawing_too_much)

    # A check valve letting the discharge back in.
    async def restarting_constantly(pool, store):
        for step in range(6):
            await pool.execute(
                "INSERT INTO pump_run (pump, started_at, ended_at, started_by) VALUES "
                "(1, now() - $1::interval, now() - $1::interval + interval '12 seconds', 'contact')",
                timedelta(seconds=(6 - step) * 20),
            )

    alerts = AlertsSettings()
    alerts.short_cycling.restart_within_ms = 30_000
    alerts.short_cycling.times_in_a_row = 2
    await run("a check valve passing", alerts, {}, setup=restarting_constantly)

    # Silence.
    async def long_quiet(pool, store):
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, ended_at, started_by) "
            "VALUES (1, now() - interval '20 hours', now() - interval '20 hours', 'contact')"
        )

    alerts = AlertsSettings()
    await run("nothing has run", alerts, {}, setup=long_quiet)

    # One pump not taking its turn.
    async def one_pump_sitting_out(pool, store):
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, ended_at, started_by) "
            "VALUES (1, now() - interval '40 hours', now() - interval '40 hours', 'contact')"
        )
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, ended_at, started_by) "
            "VALUES (2, now() - interval '5 minutes', now() - interval '5 minutes', 'contact')"
        )

    alerts = AlertsSettings()
    await run("a pump not taking its turn", alerts, {}, setup=one_pump_sitting_out)

    # The two drift rules, which need their history readers to answer.
    alerts = AlertsSettings()
    await run(
        "wearing out",
        alerts,
        {},
        history=SimpleNamespace(
            typical=lambda *a, **k: _answer(
                SimpleNamespace(drift=2.0, median=9.5, earlier_median=7.5)
            )
        ),
        recent=SimpleNamespace(
            from_contacts=lambda *a, **k: _answer(
                SimpleNamespace(duration_drift_s=9.0, typical_duration_s=21.0)
            )
        ),
    )

    # A box that stopped answering.
    async def a_quiet_device(pool, store):
        # The rule reads nothing at all unless the broker is configured, which
        # is deliberate: there is no such thing as a device gone quiet on an
        # installation that was never listening.
        store.mqtt.enabled = True
        store.mqtt.health[0].topic = "pit/health"
        store.mqtt.health[0].name = "the I/O module"
        await pool.execute("INSERT INTO device_status (device, online) VALUES ('health0', false)")

    alerts = AlertsSettings()
    await run("a device gone quiet", alerts, {}, setup=a_quiet_device)

    # Every rule that is swept and can be made to fire should have been read.
    expected = {
        "high_water",
        "overload",
        "both_overloads",
        "both_pumps",
        "run_too_long",
        "panel_alert",
        "contactor_no_current",
        "over_current",
        "short_cycling",
        "nothing_has_run",
        "pump_idle",
        "run_drift",
        "load_drift",
        "device_offline",
    }
    assert expected <= fired, f"never read the wording of: {sorted(expected - fired)}"


async def _answer(value):
    return value


async def test_a_run_that_goes_long_is_noticed_when_it_does_not_a_tick_later(pool, sent):
    """The rule reads open runs, and a sweep happens when a contact changes or
    every thirty seconds otherwise. Both ends of a run are contact changes, so
    a run shorter than a tick was looked at once when it started and once when
    it finished, and never in between.

    On the reference pit, which runs for twelve seconds, that meant no
    threshold under thirty seconds could ever fire, and the sixty second one
    was reported up to a tick late. The rule now asks to be looked at again at
    the moment the run crosses its own threshold.
    """
    await _a_person(pool)
    alerts = AlertsSettings()
    alerts.run_too_long.longer_than_ms = 4_000
    engine = _engine(pool, _store(alerts), _wire(_Contacts(), pump1_run=True))
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) "
        "VALUES (1, now() - interval '1 second', 'contact')"
    )

    await engine.sweep()

    assert await pool.fetchval("SELECT count(*) FROM alert WHERE rule = 'run_too_long'") == 0
    # Roughly the three seconds it has left, rather than the thirty second tick.
    waiting = engine._recheck_at - asyncio.get_running_loop().time()
    assert 1.5 < waiting < 3.5, waiting


async def test_short_cycling_is_the_pits_rhythm_and_not_one_pumps(pool, sent):
    """The fault is a check valve letting the discharge back into the pit, and
    a duplex panel answers the refill by calling the *other* pump.

    So the pit restarts every few seconds while neither pump restarts quickly
    at all: each one's own gap spans the other one's entire run plus both
    intervals. Measured per pump this arrangement reads as 32 s gaps and says
    nothing; measured across the pit it is 8 s, which is the number somebody
    would read off the panel and the number the message claims.
    """
    await _a_person(pool)
    alerts = AlertsSettings()
    alerts.short_cycling.restart_within_ms = 15_000
    alerts.short_cycling.times_in_a_row = 3
    # Alternating calls: twelve seconds of running, eight seconds of quiet.
    for step in range(6):
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, ended_at, started_by) VALUES "
            "($1, now() - $2::interval, now() - $2::interval + interval '12 seconds', 'contact')",
            1 if step % 2 == 0 else 2,
            timedelta(seconds=(6 - step) * 20),
        )

    await _engine(pool, _store(alerts), _Contacts()).sweep()

    rows = await pool.fetch("SELECT pump, detail FROM alert WHERE rule = 'short_cycling'")
    assert len(rows) == 1, f"one alert about the pit, not one per pump: {rows}"
    assert rows[0]["pump"] is None, "the pit is not a pump"
    assert "8 s" in rows[0]["detail"], rows[0]["detail"]


async def test_every_all_clear_says_the_good_news_rather_than_the_bad(pool, sent):
    """The other half of the wording, which nothing read either.

    A rule with no all clear of its own fell back to "Cleared at the pit: " and
    its own title, and a title that is a negation then says the opposite of
    what happened. Six minutes of quiet raised "No pump has run", a pump ran,
    and the good news went out as "Cleared at 822 Greenwich St: Nothing has
    run." Read on a phone that is the alarm again, not the end of it.

    Found on 2026-09-15 by reading a real one.
    """
    await _a_person(pool)
    from pitwatch.domain import alerts as specs

    for spec in specs.SPECS:
        if spec.key in ("float_activity", "pump_running"):
            continue  # Raised and cleared in one breath; there is no all clear.
        assert spec.cleared_message, f"{spec.key} falls back to its own title"
        said = spec.cleared_message.lower()
        assert not said.startswith("cleared"), spec.key

    # And the finished sentences, filled the way the engine fills them.
    alerts = AlertsSettings()
    alerts.run_too_long.longer_than_ms = 1000
    store = _store(alerts)
    contacts = _wire(_Contacts(), high_water=True, pump1_fault=True, pump2_run=True)
    engine = _engine(pool, store, contacts)
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) "
        "VALUES (2, now() - interval '90 seconds', 'contact')"
    )
    await engine.sweep()
    raised = await pool.fetchval("SELECT count(*) FROM alert WHERE cleared_at IS NULL")
    assert raised, "nothing was raised, so nothing can clear"

    # Everything goes away at once.
    await pool.execute("UPDATE pump_run SET ended_at = now()")
    for channel in contacts.by_channel:
        contacts.by_channel[channel] = False
    await engine.sweep()

    cleared = [body for _, _, body in sent if "Cleared at" in body]
    assert not cleared, f"an all clear fell back to the generic sentence: {cleared}"
    for _, _, body in sent:
        assert "{" not in body and "}" not in body, body


async def test_a_meter_that_stopped_talking_is_not_a_motor_that_stopped_turning(pool, sent):
    """The live reading is a cache of the last thing the meter said, and it
    does not expire.

    When the meter fell off the broker on 2026-09-16 that cache held its final
    between-runs zero, and every pump call for the next hour looked like a
    motor sitting dead on a closed contactor. Fifteen critical alerts about two
    pumps that were running perfectly.

    A monitoring failure reported as a hardware failure is worse than no alert,
    because somebody acts on it. The clamp has to have spoken during this run
    to have an opinion about it.
    """
    await _a_person(pool)
    store = _store()
    contacts = _wire(_Contacts(), pump1_run=True)
    # The clamp has proved itself in the past, which is what arms the rule.
    await pool.execute(
        "INSERT INTO em_sample (ts, channel, current) VALUES (now() - interval '2 days', 0, 14.0)"
    )
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) "
        "VALUES (1, now() - interval '30 seconds', 'contact')"
    )

    # A stale zero: the last thing the meter said, well before this run began.
    stale = SimpleNamespace(current=0.0, ts=datetime.now(UTC) - timedelta(minutes=40))
    engine = AlertEngine(pool, store, SimpleNamespace(samples={0: stale}), contacts, None, None)
    await engine.sweep()

    assert (
        await pool.fetchval("SELECT count(*) FROM alert WHERE rule = 'contactor_no_current'") == 0
    ), "a dead meter was reported as a dead motor"

    # And the real fault still fires: the clamp answering during the run, with
    # nothing on it.
    live = SimpleNamespace(current=0.0, ts=datetime.now(UTC))
    engine = AlertEngine(pool, store, SimpleNamespace(samples={0: live}), contacts, None, None)
    await engine.sweep()

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'contactor_no_current'")
    assert detail and "drawing nothing" in detail, detail


def test_the_two_pulses_this_panel_uses_are_told_apart():
    """One contact, more than one meaning, distinguished by rate.

    An unacknowledged alarm is symmetric at about a hertz. The yearly service
    reminder is 2.00 s closed and 3.00 s open, measured over 6,314 cycles on
    2026-09-16 without varying by more than a hundredth. The manufacturer
    confirmed the slow one is a maintenance prompt and not a fault.

    Before this the alert offered the same guess either way, a power failure or
    an open door, and sent somebody looking for a fault that did not exist.
    """
    import asyncio as _asyncio

    async def cadence(period_s, count):
        loop = _asyncio.get_running_loop()
        now = loop.time()
        e = _engine(None, _store(), _Contacts())
        # Newest last, the order on_events appends them in.
        for step in range(count, 0, -1):
            e._alarm_edges.append(now - step * period_s)
        return e._alarm_cadence()

    async def go():
        assert await cadence(0.5, 12) == "fast", "a one hertz alarm"
        assert await cadence(2.5, 6) == "slow", "the five second service reminder"
        assert await cadence(9.0, 6) == "steady", "changes too far apart to be a pulse"
        assert await cadence(0.5, 2) == "steady", "two edges is a blip, not a rhythm"

    _asyncio.run(go())


async def test_the_panel_alert_says_which_pulse_it_is_seeing(pool, sent):
    """The sentence is the whole value of this alert. The contact carries no
    detail, so the one thing worth saying is what it is doing."""
    await _a_person(pool)
    alerts = AlertsSettings()
    alerts.panel_alert.hold_s = 0
    engine = _engine(pool, _store(alerts), _wire(_Contacts(), system_alert=True))

    now = asyncio.get_running_loop().time()
    for step in range(6, 0, -1):
        engine._alarm_edges.append(now - step * 2.5)

    await engine.sweep()

    detail = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'panel_alert'")
    assert detail is not None
    assert "pulsing slowly" in detail, detail
    assert "yearly service reminder" in detail, detail
    assert "{" not in detail and "}" not in detail, detail


async def test_a_second_device_going_quiet_is_news_of_its_own(pool, sent):
    """The gap that hid the X-408 going off the network on 2026-09-16.

    The device alert is one row covering every device, and its subject is a
    list. It opened about the Meter at 06:13. At 06:48 the module that reads
    the panel dropped off the network, which changed the sentence from one name
    to two, but the alert was already open: the insert conflicted, nothing was
    written, and nobody was told. The dashboard showed it, because the
    dashboard reads the devices directly, so the one part of this system whose
    job is to speak up was the part that stayed quiet.
    """
    await _a_person(pool)
    store = _store()
    store.mqtt = MqttSettings(
        host="broker",
        enabled=True,
        health=[
            HealthSource(name="Inputs", topic="pit/in/tick", expect_s=60),
            HealthSource(name="Meter", topic="pit/meter/tick", expect_s=60),
        ],
    )
    engine = _engine(pool, store, _Contacts())

    await _device(pool, "health0", True)
    await _device(pool, "health1", False)
    await engine.sweep()

    first = await pool.fetchval("SELECT detail FROM alert WHERE rule = 'device_offline'")
    assert "Meter" in first and "Inputs" not in first, first
    assert len(sent) == 1, sent

    # Now the other one goes too, while the alert is still open.
    await _device(pool, "health0", False)
    await engine.sweep()

    rows = await pool.fetch("SELECT detail FROM alert WHERE rule = 'device_offline'")
    assert len(rows) == 1, "still one alert, not two"
    assert "Inputs" in rows[0]["detail"] and "Meter" in rows[0]["detail"], rows[0]["detail"]
    assert len(sent) == 2, "the second device going quiet was never announced"


async def test_an_alert_that_is_merely_still_true_says_nothing_further(pool, sent):
    """The other half, and the reason this is opt in per rule. Almost every
    detail here moves on every sweep: the time is in most of them, the amps in
    others, how many hours it has been in the rest. A rule that re-announced
    itself whenever its wording changed is one nobody could leave switched
    on."""
    await _a_person(pool)
    contacts = _wire(_Contacts(), high_water=True)
    engine = _engine(pool, _store(), contacts)

    await engine.sweep()
    await engine.sweep()
    await engine.sweep()

    assert len(sent) == 1, sent
    assert await pool.fetchval("SELECT count(*) FROM alert WHERE rule = 'high_water'") == 1
