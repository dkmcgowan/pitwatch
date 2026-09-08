"""The part that wakes people up.

Every test here is about restraint rather than detection. Noticing a wet float
is the easy half; the half that decides whether this thing is worth running is
whether it can notice one wet float and send one message about it, and then say
nothing for the six hours it stays wet.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pitwatch.domain.engine import AlertEngine
from pitwatch.schemas import (
    AlertsSettings,
    ClampSource,
    ContactInput,
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

    monkeypatch.setattr("pitwatch.domain.engine.email_sender.send", fake_email)
    monkeypatch.setattr("pitwatch.domain.engine.sms_sender.send", fake_sms)
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

    monkeypatch.setattr("pitwatch.domain.engine.email_sender.send", explode)
    await _a_person(pool)
    contacts = _wire(_Contacts(), high_water=True)

    await _engine(pool, _store(), contacts).sweep()

    assert await pool.fetchval("SELECT count(*) FROM alert") == 1
    row = await pool.fetchrow("SELECT status, error FROM notification")
    assert row["status"] == "failed"
    assert "connection refused" in row["error"]


# -- the rules that need a clamp ---------------------------------------------


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
