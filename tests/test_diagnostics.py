"""What has arrived, per source, beside the setting that was meant to produce it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pitwatch.domain import diagnostics
from pitwatch.schemas import (
    ClampSource,
    ContactInput,
    HealthSource,
    MqttSettings,
    PumpsSettings,
    SiteSettings,
)

SITE = SiteSettings(name="A pit", timezone="America/New_York")


def _mqtt(**overrides) -> MqttSettings:
    fields = {
        "enabled": True,
        "host": "broker",
        "inputs": [
            ContactInput(channel=1, role="lead_float", topic="pit/in/1"),
            ContactInput(channel=7, role="pump1_fault", topic="pit/in/7", invert=True),
        ],
        "clamps": [ClampSource(pump=1, topic="meter/em1:0", path="current")],
        "health": [HealthSource(name="Meter", topic="meter/tick", expect_s=60)],
    }
    fields.update(overrides)
    return MqttSettings(**fields)


async def test_a_source_with_a_topic_and_no_traffic_is_the_whole_point(pool):
    """The question this page exists for. The dashboard says "never" and "0
    this month" for an input nothing has ever arrived on, because to somebody
    asking whether the pit is alright that means nothing has happened. Somebody
    who has just wired the panel needs the other answer."""
    report = await diagnostics.read(pool, _mqtt(), PumpsSettings(), SITE)

    heard = {row.name: row.heard for row in report.inputs}
    assert heard == {"Input 1": False, "Input 7": False}
    assert report.silent == 4, "two inputs, a clamp and a health check, all quiet"
    assert any("Nothing has arrived on" in line for line in report.watchouts)


async def test_an_input_that_has_spoken_says_when_and_how_often(pool):
    """And on the site's clock, not the server's."""
    await pool.execute(
        """
        INSERT INTO io_state (channel, label, state, raw, changed_at, updated_at)
        VALUES (1, 'lead_float', true, true, $1, $1)
        """,
        datetime(2026, 9, 8, 14, 47, tzinfo=UTC),
    )
    await pool.execute(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES (now(), 1, 'lead_float', true, true)"
    )

    report = await diagnostics.read(pool, _mqtt(), PumpsSettings(), SITE)
    row = next(row for row in report.inputs if row.name == "Input 1")

    assert row.heard is True
    assert "closed" in row.detail
    assert "1 change" in row.detail
    assert row.last == "8 Sep 10:47 AM", row.last


async def test_a_clamp_reading_nothing_but_zero_is_called_out(pool):
    """Which is what a CT that is not around a wire looks like, and is
    otherwise indistinguishable from a pump that has not run."""
    now = datetime.now(UTC)
    await pool.executemany(
        "INSERT INTO em_sample (ts, channel, current) VALUES ($1, 0, 0)",
        [(now - timedelta(seconds=i),) for i in range(5)],
    )

    report = await diagnostics.read(pool, _mqtt(), PumpsSettings(), SITE)
    clamp = report.clamps[0]

    assert clamp.heard is True
    assert "5 readings" in clamp.detail
    assert "unfitted clamp" in clamp.note
    assert any("unfitted clamp" in line for line in report.watchouts)


async def test_a_health_check_carries_the_reason_it_is_unhappy(pool):
    """The sentence somebody needs is the one the reader already wrote: how
    long the silence was, against the interval it was held to."""
    await pool.execute(
        """
        UPDATE device_status
        SET online = false, last_seen = now(),
            last_error = 'Nothing heard for 150 s, expected every 60 s'
        WHERE device = 'health0'
        """
    )

    report = await diagnostics.read(pool, _mqtt(), PumpsSettings(), SITE)
    check = report.health[0]

    assert check.name == "Meter"
    assert check.carries == "Expected every 60 s"
    assert "Nothing heard for 150 s" in check.detail
    assert "sends on a schedule" in check.note, "and only in the list at the top"


async def test_nothing_to_say_when_the_broker_is_switched_off(pool):
    report = await diagnostics.read(pool, _mqtt(enabled=False), PumpsSettings(), SITE)

    assert report.rows == []
    assert report.watchouts == ["MQTT is switched off, so nothing is being listened for."]


async def test_all_quiet_says_so_rather_than_saying_nothing(pool):
    """An empty list of watchouts reads as a page that failed to load."""
    now = datetime.now(UTC)
    await pool.execute(
        "INSERT INTO io_state (channel, label, state, raw, changed_at, updated_at)"
        " VALUES (1, 'lead_float', false, false, $1, $1)",
        now,
    )
    await pool.execute(
        "INSERT INTO io_state (channel, label, state, raw, changed_at, updated_at)"
        " VALUES (7, 'pump1_fault', false, true, $1, $1)",
        now,
    )
    await pool.execute("INSERT INTO em_sample (ts, channel, current) VALUES (now(), 0, 15.4)")
    await pool.execute(
        "UPDATE device_status SET online = true, last_seen = now() WHERE device = 'health0'"
    )

    report = await diagnostics.read(pool, _mqtt(), PumpsSettings(), SITE)

    assert report.silent == 0
    assert report.watchouts == ["Every configured source has been heard from in the last day."]
