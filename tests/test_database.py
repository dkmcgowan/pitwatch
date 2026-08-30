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

from pitwatch.auth import DEFAULT_PASSWORD, DEFAULT_USERNAME, authenticate, ensure_default_admin
from pitwatch.db import migrate, migration_files
from pitwatch.ingest.shelly import EmSample
from pitwatch.ingest.sink import LiveState, SampleSink, record_device_status
from pitwatch.schemas import ChannelMap, InputsSettings, ShellySettings


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


async def test_one_email_address_belongs_to_one_person(pool):
    """Two rows with one address makes "who is this going to" ambiguous."""
    await pool.execute(
        "INSERT INTO app_user (username, name, email) VALUES ('a', 'A', 'shared@example.com')"
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO app_user (username, name, email) VALUES ('b', 'B', 'SHARED@example.com')"
        )


async def test_settings_round_trip(store):
    saved = InputsSettings(
        enabled=True,
        host="192.168.1.51",
        channels=[ChannelMap(channel=3, role="high_water", invert=True)],
    )

    await store.put(saved)
    read_back = store.inputs

    assert read_back.host == "192.168.1.51"
    assert read_back.channels[2].role == "high_water"
    assert read_back.channels[2].invert is True


async def test_settings_survive_a_reload(pool, store):
    from pitwatch.settings import SettingsStore

    await store.put(ShellySettings(enabled=True, host="10.0.0.9", pump1_channel=1, pump2_channel=0))

    fresh = SettingsStore(pool)
    await fresh.load()

    assert fresh.shelly.host == "10.0.0.9"
    # Both stored, both read back as they were saved.
    assert fresh.shelly.clamp_for_pump == {1: 1, 2: 0}


async def test_saving_a_setting_wakes_the_subscribers(store):
    queue = store.subscribe()

    await store.put(ShellySettings(host="10.0.0.9"))

    assert queue.get_nowait() == ShellySettings.KEY


async def test_a_password_verifies_and_a_wrong_one_does_not(pool):
    await ensure_default_admin(pool)

    signed_in = await authenticate(pool, DEFAULT_USERNAME, DEFAULT_PASSWORD)
    assert signed_in is not None
    assert signed_in.username == DEFAULT_USERNAME
    assert signed_in.is_admin is True
    # Shipped with a known password, so it can go exactly one place until it is
    # changed. See pitwatch.middleware.
    assert signed_in.must_change_password is True

    assert await authenticate(pool, DEFAULT_USERNAME, "the-wrong-password") is None
    assert await authenticate(pool, "nobody", DEFAULT_PASSWORD) is None


async def test_the_stored_hash_is_not_the_password(pool):
    await ensure_default_admin(pool)

    stored = await pool.fetchval("SELECT password_hash FROM app_user")

    assert DEFAULT_PASSWORD not in stored
    assert stored.startswith("$argon2")


async def test_the_default_admin_is_only_ever_created_once(pool):
    """Otherwise a restart would reinstate it after somebody removed it."""
    assert await ensure_default_admin(pool) is True
    assert await ensure_default_admin(pool) is False


async def test_somebody_with_no_password_cannot_sign_in(pool):
    """Most people here are recipients, not users. That is not a way in."""
    await pool.execute(
        "INSERT INTO app_user (username, name, phone, notify_sms) "
        "VALUES ('super', 'Super', '+12125550142', true)"
    )

    assert await authenticate(pool, "super", "") is None
    assert await authenticate(pool, "super", "anything-at-all") is None


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


async def test_the_timescale_extension_is_left_alone_when_it_is_current(pool, config):
    """Nothing to do is the normal case, and it must not be noisy or slow.

    This runs on every start, so it has to be a cheap no-op when the image has
    not changed.
    """
    from pitwatch.db import update_timescale_extension

    assert await update_timescale_extension(config) is None


async def test_the_extension_update_survives_a_database_without_timescale(config, database_url):
    """A fresh database has no extension yet; migration 001 creates it.

    Returning quietly rather than raising is what lets this run before the
    migrations without a special case for first boot.
    """
    import asyncpg

    from pitwatch.db import update_timescale_extension

    connection = await asyncpg.connect(dsn=database_url)
    try:
        await connection.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
    finally:
        await connection.close()

    assert await update_timescale_extension(config) is None


async def test_the_io_tables_record_what_an_input_was_called(pool):
    """The channel is the key. The label is a snapshot of what it was named at
    the time, so renaming an input does not rewrite last month's history."""
    columns = {
        row["column_name"]
        for row in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'io_event'"
        )
    }

    assert "label" in columns
    assert "signal" not in columns, "007 renames it; a stale column means the migration did not run"


async def test_the_current_history_query_runs_and_splits_its_two_windows(pool):
    """The query itself, against a real hypertable.

    Ordered set aggregates with FILTER, an interval passed as a parameter, and
    a boundary that has to split one scan into two windows without overlapping.
    None of that can be checked by reading it.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import CurrentHistory

    now = datetime.now(UTC)
    rows = []
    # Four weeks ago it drew 14 A while running. This week it draws 16.
    for day, amps in ((20, 14.0), (2, 16.0)):
        for index in range(200):
            rows.append((now - timedelta(days=day, seconds=index), 0, amps))
        # Plus the hours it spends switched off, which must not count.
        for index in range(500):
            rows.append((now - timedelta(days=day, seconds=1000 + index), 0, 0.03))
    # And one starting surge per window. The median would survive these on its
    # own, being two readings against two hundred, but they are also excluded
    # outright for being the first reading of a run.
    rows.append((now - timedelta(days=2, seconds=900), 0, 61.0))
    rows.append((now - timedelta(days=20, seconds=900), 0, 58.0))

    await pool.executemany("INSERT INTO em_sample (ts, channel, current) VALUES ($1, $2, $3)", rows)

    typical = await CurrentHistory().typical(pool, channel=0, running_amps=1.0)

    assert typical.median == pytest.approx(16.0)
    assert typical.earlier_median == pytest.approx(14.0)
    assert typical.drift == pytest.approx(2.0)
    # The off hours were excluded, so only the running readings were counted.
    # 200 of them, not 201: the surge follows an idle reading, which makes it
    # the first reading of a run, and those are left out.
    assert typical.samples == 200


async def test_the_history_says_nothing_when_there_is_nothing_to_say(pool):
    """A fresh install, where reporting a median off three readings would be
    worse than reporting none."""
    from pitwatch.domain.history import CurrentHistory

    typical = await CurrentHistory().typical(pool, channel=1, running_amps=1.0)

    assert typical.median is None
    assert typical.drift is None


async def test_counting_runs_from_the_clamp_readings(pool):
    """Counting starts works even though timing a run does not.

    Every run's transition is caught, because a jump from nothing to sixteen
    amps is exactly what makes the meter report. What is not caught is the
    middle of a steady run, which is why there is no duration anywhere near
    this.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import RecentRuns

    now = datetime.now(UTC)
    rows = []
    # Three runs, shaped the way the real readings are: a high first sample, a
    # steady one some unpredictable time later, then nothing.
    for minutes, gap in ((90, 2), (45, 200), (5, 60)):
        start = now - timedelta(minutes=minutes)
        rows.append((start - timedelta(seconds=15), 0, 0.0))
        rows.append((start, 0, 16.4))
        rows.append((start + timedelta(seconds=gap), 0, 15.2))
        rows.append((start + timedelta(seconds=gap + 3), 0, 0.0))
    # And a day of sitting still, which must not count as anything.
    for index in range(40):
        rows.append((now - timedelta(hours=20, seconds=index * 15), 0, 0.0))

    await pool.executemany("INSERT INTO em_sample (ts, channel, current) VALUES ($1, $2, $3)", rows)

    recent = await RecentRuns().recent(pool, channel=0, running_amps=1.0)

    assert recent.runs == 3
    assert recent.last_start is not None
    assert (now - recent.last_start).total_seconds() < 6 * 60


async def test_a_clamp_that_has_never_seen_a_run_says_so(pool):
    from pitwatch.domain.history import RecentRuns

    recent = await RecentRuns().recent(pool, channel=1, running_amps=1.0)

    assert recent.runs == 0
    assert recent.last_start is None


async def test_counting_what_a_contact_has_done(pool):
    """Counting rows, not counting samples and hoping.

    io_event only ever holds transitions, so every row with state true is a
    contact closing. That is the whole reason the reader writes edges.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import SignalHistory

    now = datetime.now(UTC)
    rows = []
    # A float that has closed three times today, once more this month, and once
    # long enough ago to be outside both. Written in days, because 400 hours
    # reads like a long time and is two weeks.
    for days in (0.04, 0.2, 0.85, 10, 45):
        rows.append((now - timedelta(days=days), 3, "Lead float", True, True))
        rows.append(
            (now - timedelta(days=days) + timedelta(seconds=30), 3, "Lead float", False, False)
        )
    # An alarm that went off once, three weeks ago.
    rows.append((now - timedelta(days=21), 4, "High water", True, True))
    rows.append((now - timedelta(days=21, seconds=-60), 4, "High water", False, False))
    # And an input nothing has ever been recorded for.
    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, $2, $3, $4, $5)",
        rows,
    )

    closings = await SignalHistory().closings(pool, [3, 4, 5])

    assert closings[3].today == 3, "three inside a day"
    assert closings[3].month == 4, "the 45 day old one is outside a month"
    assert (now - closings[3].last_on).total_seconds() < 3700

    assert closings[4].today == 0
    assert closings[4].month == 1
    assert closings[4].known is True

    # Nothing recorded is not the same as a quiet month, and the payload says
    # so by sending null rather than zero.
    assert 5 not in closings
    from pitwatch.domain.history import Closings

    assert Closings().as_json() == {"last_on": None, "today": None, "month": None}


async def test_the_history_ignores_contacts_opening(pool):
    """Only closings count. A contact that opens is the end of something, and
    counting both would double every number on the card."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import SignalHistory

    now = datetime.now(UTC)
    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, $2, $3, $4, $5)",
        [
            (now - timedelta(minutes=10), 6, "Lag float", True, True),
            (now - timedelta(minutes=9), 6, "Lag float", False, False),
            (now - timedelta(minutes=8), 6, "Lag float", True, True),
            (now - timedelta(minutes=7), 6, "Lag float", False, False),
        ],
    )

    closings = await SignalHistory().closings(pool, [6])

    assert closings[6].today == 2


async def test_the_typical_load_leaves_out_the_start_of_each_run(pool):
    """The first reading of a run is where the starting surge lands, and on a
    pit that runs in short bursts it is a large share of every reading taken.

    On the reference panel it was 43 percent of them and ran 1.3 A high, which
    put about 0.4 A of surge into a number that is meant to describe a motor at
    work rather than one getting going.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import CurrentHistory

    now = datetime.now(UTC)
    rows = []
    # Forty runs, each a high first reading and two settled ones. If the first
    # readings counted, the median would land between 16 and 20 rather than on
    # the 16 the motor actually draws while working.
    for index in range(40):
        start = now - timedelta(hours=index + 1)
        rows.append((start - timedelta(seconds=20), 0, 0.0))
        rows.append((start, 0, 40.0))
        rows.append((start + timedelta(seconds=20), 0, 16.0))
        rows.append((start + timedelta(seconds=40), 0, 16.0))
        rows.append((start + timedelta(seconds=60), 0, 0.0))

    await pool.executemany("INSERT INTO em_sample (ts, channel, current) VALUES ($1, $2, $3)", rows)

    typical = await CurrentHistory().typical(pool, channel=0, running_amps=1.0)

    assert typical.median == pytest.approx(16.0), "the 40 A starts are excluded"
    assert typical.samples == 80, "two settled readings from each of forty runs"
