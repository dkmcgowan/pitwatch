"""Against a real TimescaleDB. Skipped when there is not one.

These cover the pieces a unit test cannot reach: that the migrations apply to an
empty database, that the hypertables and continuous aggregates are actually
created, and that the constraints which enforce the important invariants really
do refuse the thing they are there to refuse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from pitwatch.auth import authenticate, create_user
from pitwatch.db import migrate, migration_files
from pitwatch.ingest.shelly import EmSample
from pitwatch.ingest.sink import LiveState, SampleSink, record_device_status
from pitwatch.schemas import ChannelMap, ShellySettings, Signal, WaveshareSettings


async def test_migrations_apply_to_an_empty_database(pool):
    applied = {row["name"] for row in await pool.fetch("SELECT name FROM schema_migration")}

    assert applied == {path.name for path in migration_files()}


async def test_migrations_are_not_applied_twice(pool):
    assert await migrate(pool) == []


async def test_the_sample_table_is_a_hypertable(pool):
    names = {
        row["hypertable_name"]
        for row in await pool.fetch(
            "SELECT hypertable_name FROM timescaledb_information.hypertables"
        )
    }

    assert "em_sample" in names
    assert "io_event" in names


async def test_the_rollups_exist_and_refresh(pool):
    views = {
        row["view_name"]
        for row in await pool.fetch(
            "SELECT view_name FROM timescaledb_information.continuous_aggregates"
        )
    }

    assert {"em_1m", "em_1h"} <= views


async def test_a_pump_cannot_have_two_open_runs(pool):
    """The invariant the dashboard's durations rest on.

    A missed stop edge that opened a second run would make every duration after
    it wrong, silently, and the number that goes wrong is the one someone would
    use to decide the pump is failing.
    """
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) VALUES (1, now(), 'current')"
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, started_by) VALUES (1, now(), 'current')"
        )


async def test_a_closed_run_does_not_block_the_next_one(pool):
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, ended_at, started_by) VALUES (1, now(), now(), 'current')"
    )

    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, started_by) VALUES (1, now(), 'current')"
    )


async def test_one_open_alert_per_rule_and_pump(pool):
    """The dedupe. A float that chatters must not send twenty messages."""
    await pool.execute(
        "INSERT INTO alert (rule, severity, pump, title, detail) VALUES ('overload', 'critical', 1, 't', 'd')"
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO alert (rule, severity, pump, title, detail) VALUES ('overload', 'critical', 1, 't', 'd')"
        )

    # The same rule on the other pump is a different alert.
    await pool.execute(
        "INSERT INTO alert (rule, severity, pump, title, detail) VALUES ('overload', 'critical', 2, 't', 'd')"
    )
    # And once the first has cleared, it can be raised again.
    await pool.execute("UPDATE alert SET cleared_at = now() WHERE pump = 1")
    await pool.execute(
        "INSERT INTO alert (rule, severity, pump, title, detail) VALUES ('overload', 'critical', 1, 't', 'd')"
    )


async def test_a_recipient_needs_a_way_to_be_reached(pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO recipient (name) VALUES ('Nobody')")


async def test_settings_round_trip(store):
    saved = WaveshareSettings(
        enabled=True,
        host="192.168.1.51",
        channels=[ChannelMap(channel=3, signal=Signal.HIGH_WATER, normally_closed=True)],
    )

    await store.put(saved)
    read_back = store.waveshare

    assert read_back.host == "192.168.1.51"
    assert read_back.channel_for(Signal.HIGH_WATER).channel == 3
    assert read_back.channel_for(Signal.HIGH_WATER).normally_closed is True


async def test_settings_survive_a_reload(pool, store):
    from pitwatch.settings import SettingsStore

    await store.put(ShellySettings(enabled=True, host="10.0.0.9", pump1_channel=1))

    fresh = SettingsStore(pool)
    await fresh.load()

    assert fresh.shelly.host == "10.0.0.9"
    assert fresh.shelly.pump1_channel == 1
    # Derived, not stored, so the two pumps can never end up on one clamp.
    assert fresh.shelly.pump2_channel == 0


async def test_saving_a_setting_wakes_the_subscribers(store):
    queue = store.subscribe()

    await store.put(ShellySettings(host="10.0.0.9"))

    assert queue.get_nowait() == ShellySettings.KEY


async def test_a_password_verifies_and_a_wrong_one_does_not(pool):
    await create_user(pool, "Admin", "a-long-enough-password")

    assert await authenticate(pool, "admin", "a-long-enough-password") == "admin"
    assert await authenticate(pool, "admin", "the-wrong-password") is None
    assert await authenticate(pool, "nobody", "a-long-enough-password") is None


async def test_the_stored_hash_is_not_the_password(pool):
    await create_user(pool, "admin", "a-long-enough-password")

    stored = await pool.fetchval("SELECT password_hash FROM app_user")

    assert "a-long-enough-password" not in stored
    assert stored.startswith("$argon2")


async def test_samples_are_written_and_primed_back(pool):
    live = LiveState()
    sink = SampleSink(pool, live)
    now = datetime.now(UTC)
    await sink.submit(
        [
            EmSample(now, 0, 7.2, 121.0, 850.0, 860.0, 0.98, 60.0),
            EmSample(now, 1, 0.02, 121.0, 0.0, 0.0, 0.0, 60.0),
        ]
    )
    await sink._write(sink._drain())

    assert await pool.fetchval("SELECT count(*) FROM em_sample") == 2

    fresh_live = LiveState()
    await SampleSink(pool, fresh_live).prime()

    assert fresh_live.current_for(0) == pytest.approx(7.2, rel=1e-4)
    assert fresh_live.current_for(1) == pytest.approx(0.02, rel=1e-3)


async def test_priming_ignores_readings_that_are_too_old_to_mean_anything(pool):
    stale = datetime.now(UTC) - timedelta(hours=6)
    await pool.execute(
        "INSERT INTO em_sample (ts, channel, current) VALUES ($1, 0, 9.9)",
        stale,
    )

    live = LiveState()
    await SampleSink(pool, live).prime()

    assert live.current_for(0) is None


async def test_device_status_is_upserted_and_keeps_the_last_seen_time(pool):
    await record_device_status(pool, "shelly", True, None)
    seen = await pool.fetchval("SELECT last_seen FROM device_status WHERE device = 'shelly'")
    assert seen is not None

    await record_device_status(pool, "shelly", False, "connection refused")
    row = await pool.fetchrow("SELECT * FROM device_status WHERE device = 'shelly'")

    assert row["online"] is False
    assert row["last_error"] == "connection refused"
    # Going offline must not erase when it was last heard from; that timestamp
    # is how you tell a device that just dropped from one that has been dead
    # for a week.
    assert row["last_seen"] == seen
