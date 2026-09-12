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
from pitwatch.ingest.readings import EmSample
from pitwatch.ingest.sink import LiveState, SampleSink, record_device_status
from pitwatch.schemas import ContactInput, MqttSettings


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


async def test_there_is_one_place_readings_are_read_from(pool):
    """The minute and hour rollups are gone, and nothing should bring them back
    without a reader.

    They existed so a chart covering a year would not read a year of one second
    rows, and nothing ever asked them for anything: the history page buckets
    em_sample directly and the longest window it offers is 30 days. A tier that
    is refreshed on a schedule, retained on a policy and queried by nobody is
    two more places for the same number to be wrong in.

    What replaced them is keeping the raw rows longer. Half a day of a pit that
    runs for twelve seconds at a time is four thousand rows, and the
    compression policy takes an order of magnitude off anything over a week
    old, so 400 days of samples answers the year over year question the hourly
    rollup was for.
    """
    views = {
        row["view_name"]
        for row in await pool.fetch(
            "SELECT view_name FROM timescaledb_information.continuous_aggregates"
        )
    }
    assert views == set()

    retention = await pool.fetchval(
        """
        SELECT config ->> 'drop_after' FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_retention' AND hypertable_name = 'em_sample'
        """
    )
    assert retention == "400 days"


async def test_a_reading_is_a_time_a_channel_and_an_amp(pool):
    """Five columns went with the rollups: voltage, real and apparent power,
    power factor and frequency, which were one meter's status frame and were
    NULL on every row ever written. A clamp source reads one number at one
    path, and the number a pump monitor wants is the current."""
    columns = {
        row["column_name"]
        for row in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'em_sample'"
        )
    }

    assert columns == {"ts", "channel", "current"}


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
    saved = MqttSettings(
        enabled=True,
        host="192.168.1.51",
        inputs=[ContactInput(channel=3, role="high_water", topic="pit/in/3", invert=True)],
    )

    await store.put(saved)
    read_back = store.mqtt

    assert read_back.host == "192.168.1.51"
    assert read_back.channels[2].role == "high_water"
    assert read_back.channels[2].invert is True


async def test_settings_survive_a_reload(pool, store):
    from pitwatch.schemas import ClampSource
    from pitwatch.settings import SettingsStore

    await store.put(
        MqttSettings(
            enabled=True,
            host="10.0.0.9",
            clamps=[
                ClampSource(pump=1, topic="a"),
                ClampSource(pump=2, topic="b"),
            ],
        )
    )

    fresh = SettingsStore(pool)
    await fresh.load()

    assert fresh.mqtt.host == "10.0.0.9"
    assert fresh.mqtt.clamp_for_pump == {1: 0, 2: 1}


async def test_saving_a_setting_wakes_the_subscribers(store):
    queue = store.subscribe()

    await store.put(MqttSettings(host="10.0.0.9"))

    assert queue.get_nowait() == MqttSettings.KEY


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
            EmSample(now, 0, 7.2),
            EmSample(now, 1, 0.02),
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
    await record_device_status(pool, "clamp1", True, None)
    seen = await pool.fetchval("SELECT last_seen FROM device_status WHERE device = 'clamp1'")
    assert seen is not None

    await record_device_status(pool, "clamp1", False, "connection refused")
    row = await pool.fetchrow("SELECT * FROM device_status WHERE device = 'clamp1'")

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
    #
    # Packed into the last two minutes rather than the last two hours, because
    # the count under test is the count since midnight and these have to be on
    # the same side of it as now(). Spread over hours it failed for the first
    # ninety minutes of every UTC day, which is a test that reports the hour it
    # ran at rather than whether the counting works.
    for seconds, gap in ((90, 2), (45, 20), (5, 1)):
        start = now - timedelta(seconds=seconds)
        rows.append((start - timedelta(seconds=15), 0, 0.0))
        rows.append((start, 0, 16.4))
        rows.append((start + timedelta(seconds=gap), 0, 15.2))
        rows.append((start + timedelta(seconds=gap + 1), 0, 0.0))
    # And a long spell of sitting still before them, which must not count as
    # anything.
    for index in range(40):
        rows.append((now - timedelta(minutes=30, seconds=index * 15), 0, 0.0))

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
    # Placed against local midnight rather than as "so many hours ago", because
    # that is what today means and a test written in hours would pass or fail
    # depending on what time it ran.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = []
    for at in (
        midnight + timedelta(seconds=1),
        midnight + (now - midnight) / 2,
        now - timedelta(seconds=1),
        # Just before midnight: inside the last twenty four hours, and not
        # today. This is the one that matters. It used to count as today, which
        # put a float count on the dashboard that could not be read against the
        # run counts beside it.
        midnight - timedelta(seconds=1),
        # And one outside the month entirely.
        now - timedelta(days=45),
    ):
        rows.append((at, 3, "Lead float", True, True))
        rows.append((at + timedelta(seconds=30), 3, "Lead float", False, False))
    # An alarm that went off once, three weeks ago.
    rows.append((now - timedelta(days=21), 4, "High water", True, True))
    rows.append((now - timedelta(days=21, seconds=-60), 4, "High water", False, False))
    # And an input nothing has ever been recorded for.
    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, $2, $3, $4, $5)",
        rows,
    )

    closings = await SignalHistory().closings(pool, [3, 4, 5], "UTC")

    assert closings[3].today == 3, "today is since midnight, not the last 24 hours"
    assert closings[3].month == 4, "the 45 day old one is outside a month"
    assert (now - closings[3].last_on).total_seconds() < 3700

    assert closings[4].today == 0
    assert closings[4].month == 1
    assert closings[4].known is True

    # Nothing recorded is not the same as a quiet month, and the payload says
    # so by sending null rather than zero.
    assert 5 not in closings
    from pitwatch.domain.history import Closings

    assert Closings().as_json() == {
        "last_on": None,
        "last_held_s": None,
        "daily_average": None,
        "today": None,
        "month": None,
    }


async def test_a_pulsing_alarm_is_counted_as_one_alarm(pool):
    """Sixty one alarms in a month, when five happened.

    The panel's alarm output pulses rather than holding it. On 2026-09-12 a
    real overload trip put a one hertz square wave on that contact and kept it
    up for the fifty two seconds nobody attended to it: 106 transitions, 53
    closings, one alarm. Counted an edge at a time that is the single number on
    the page which is deliberately counted by the month, because alarms are
    supposed to be rare, inflated twenty five fold by one event.

    Every edge is still stored. This is only about what gets called an event.
    """
    from pitwatch.domain.history import SignalHistory

    now = datetime.now(UTC)
    rows = []

    # A short, ordinary alarm first, so it is not the one reported last.
    first = now - timedelta(minutes=20)
    rows.append((first, 4, "System alert", True, False))
    rows.append((first + timedelta(seconds=8), 4, "System alert", False, True))

    # Then fifty two seconds of pulsing, half a second each way, as measured.
    started = now - timedelta(minutes=10)
    at = started
    for _ in range(53):
        rows.append((at, 4, "System alert", True, False))
        rows.append((at + timedelta(seconds=0.5), 4, "System alert", False, True))
        at += timedelta(seconds=1)
    ended = at - timedelta(seconds=0.5)

    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, $2, $3, $4, $5)",
        rows,
    )

    closings = await SignalHistory().closings(pool, [4], "UTC", 2.0)

    assert closings[4].month == 2, "two alarms, not fifty four"
    assert closings[4].today == 2

    # The pulsing one is reported as one alarm that lasted the whole time,
    # timed from the first rise to the last fall rather than half a flash.
    assert abs((closings[4].last_on - started).total_seconds()) < 1
    assert closings[4].last_held_s == pytest.approx((ended - started).total_seconds(), abs=1)

    # A gap shorter than the pulse puts every flash back, which is the proof
    # that the collapsing is the setting's doing rather than an accident of
    # the query.
    every = await SignalHistory().closings(pool, [4], "UTC", 0.1)
    assert every[4].month == 54
    assert every[4].last_held_s == pytest.approx(0.5, abs=0.1)

    # And a gap does not reach across two alarms that really were separate.
    # These are eighteen minutes apart and stay two however wide it is set.
    apart = await SignalHistory().closings(pool, [4], "UTC", 120.0)
    assert apart[4].month == 2


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


async def test_runs_today_is_counted_from_local_midnight(pool):
    """Today, not the last twenty four hours.

    A run at ten last night and a run just after midnight are one run today
    and one yesterday, and a rolling day calls them both today until ten
    tonight. That is a defensible window and it is not the one the word
    promises: somebody reading "2 today" over breakfast is being told
    something they will reasonably believe.

    The day is the site's, not the server's. This asserts on a timezone the
    machine running the tests is very unlikely to be in, so a query that quietly
    used the server's clock would count both runs.
    """
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from pitwatch.domain.history import RecentRuns

    where_the_pit_is = "Pacific/Kiritimati"
    midnight = datetime.now(ZoneInfo(where_the_pit_is)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    def run(at):
        """A rise off nothing and back to it, which is what a start looks like
        from the meter's side."""
        return [
            (at - timedelta(minutes=1), 0, 0.0),
            (at, 0, 15.0),
            (at + timedelta(minutes=1), 0, 0.0),
        ]

    rows = run(midnight - timedelta(hours=2)) + run(midnight + timedelta(minutes=1))
    await pool.executemany(
        "INSERT INTO em_sample (ts, channel, current) VALUES ($1, $2, $3)",
        [(ts.astimezone(UTC), channel, amps) for ts, channel, amps in rows],
    )

    recent = await RecentRuns().recent(pool, channel=0, running_amps=1.0, timezone=where_the_pit_is)

    assert recent.runs == 1, "the one two hours before midnight was yesterday"
    assert recent.last_start is not None
    # Three days of history before an average is worth printing, and this is
    # two hours of it.
    assert recent.daily_average is None


# -- the history page's numbers ----------------------------------------------
#
# All of them read the panel's own record of what ran, rather than looking for
# the current rising off nothing. The meter reports when something changes, so
# two runs close together arrive from it as one and a four second run can
# arrive as nothing; the contacts have neither problem.


async def _a_call(pool, ago, pump=1, seconds=12.0, both=False, high=False, steady=None):
    """One call for water with one run in it, placed in the past.

    A call marked as taking both pumps gets a second run against the same
    cycle, which is the shape that makes the count of runs sit above the count
    of calls on the page.
    """
    from datetime import timedelta

    ran = timedelta(seconds=seconds)
    cycle = await pool.fetchval(
        """
        INSERT INTO pump_cycle (started_at, ended_at, first_pump, both_ran, high_water)
        VALUES (now() - $1::interval, now() - $1::interval + $2::interval, $3, $4, $5)
        RETURNING id
        """,
        ago,
        ran,
        pump,
        both,
        high,
    )
    await pool.execute(
        """
        INSERT INTO pump_run (cycle_id, pump, started_at, ended_at, duration_s,
                              steady_current, role, started_by, ended_by)
        VALUES ($1, $2, now() - $3::interval, now() - $3::interval + $4::interval, $5,
                $6, 'lead', 'contact', 'contact')
        """,
        cycle,
        pump,
        ago,
        ran,
        seconds,
        steady,
    )
    if both:
        await pool.execute(
            """
            INSERT INTO pump_run (cycle_id, pump, started_at, ended_at, duration_s,
                                  role, started_by, ended_by)
            VALUES ($1, $2, now() - $3::interval, now() - $3::interval + $4::interval, $5,
                    'lag', 'contact', 'contact')
            """,
            cycle,
            3 - pump,
            ago,
            ran,
            seconds,
        )


async def test_calls_are_counted_from_the_cycles_and_not_from_the_amps(pool):
    """One call is one filling of the pit however many pumps answered it, which
    is why this is not a count of runs."""
    from datetime import timedelta

    from pitwatch.domain import series

    await _a_call(pool, timedelta(hours=2))
    await _a_call(pool, timedelta(hours=4), pump=2, both=True)
    await _a_call(pool, timedelta(hours=6), high=True)

    counted = await series.calls_series(pool, series.WINDOWS["today"], "UTC")

    assert sum(calls for _, calls, _, _ in counted) == 3
    assert sum(both for _, _, both, _ in counted) == 1
    assert sum(high for _, _, _, high in counted) == 1


async def test_the_spacing_needs_the_call_before_the_window(pool):
    """The first call inside the window has nothing before it to be measured
    from unless one is fetched from outside it, and a made up gap on the first
    dot is the one somebody would read as a storm."""
    from datetime import timedelta

    from pitwatch.domain import series

    # One outside the window, then two inside it: ninety minutes after that
    # one, and an hour after that.
    await _a_call(pool, timedelta(hours=25))
    await _a_call(pool, timedelta(hours=23, minutes=30))
    await _a_call(pool, timedelta(hours=22, minutes=30))

    gaps = await series.call_gaps(pool, series.WINDOWS["today"])

    assert len(gaps) == 2, "both of the ones inside the window have a spacing"
    assert [round(gap) for _, gap, _, _ in gaps] == [5400, 3600], gaps


async def test_a_run_carries_what_the_clamp_saw_and_what_it_did_not(pool):
    from datetime import timedelta

    from pitwatch.domain import series

    await _a_call(pool, timedelta(hours=1), steady=15.4)
    await _a_call(pool, timedelta(hours=2), pump=2)

    runs = await series.runs_series(pool, series.WINDOWS["today"])

    assert [run.pump for run in runs] == [2, 1], "oldest first"
    assert runs[1].steady_current == pytest.approx(15.4)
    # Pump 2 has no clamp fitted on the reference pit, and None is the honest
    # answer for what it drew.
    assert runs[0].steady_current is None


async def test_the_daily_pattern_is_counted_in_the_buildings_own_time(pool):
    """Whether the pit runs at four in the morning is a question about four in
    the morning where the pit is."""
    from datetime import timedelta

    from pitwatch.domain import series

    await _a_call(pool, timedelta(hours=3))

    here = await series.hour_profile(pool, series.WINDOWS["today"], "UTC")
    there = await series.hour_profile(pool, series.WINDOWS["today"], "Australia/Sydney")

    assert sum(here.values()) == 1
    assert sum(there.values()) == 1
    assert list(here) != list(there), "the same call falls in a different hour"


async def test_the_daily_pattern_counts_calls_rather_than_runs(pool):
    """The chart asks when the water comes in. A call answered by both pumps is
    one filling of the pit and belongs in its hour once, and which pump answered
    is not part of the question: the panel alternates, so a split by pump is
    half and half in every hour."""
    from datetime import timedelta

    from pitwatch.domain import series

    await _a_call(pool, timedelta(hours=3), both=True)

    profile = await series.hour_profile(pool, series.WINDOWS["today"], "UTC")

    assert sum(profile.values()) == 1, "two runs, one filling of the pit"


async def test_runs_sit_above_calls_by_the_calls_that_took_both_pumps(pool):
    """The two counts side by side on the page are not meant to match, and the
    difference between them is not slop. One call answered by both pumps is one
    call and two runs."""
    from datetime import timedelta

    from pitwatch.domain import series

    await _a_call(pool, timedelta(hours=1))
    await _a_call(pool, timedelta(hours=2))
    await _a_call(pool, timedelta(hours=3), both=True)

    calls = await series.calls_series(pool, series.WINDOWS["today"], "UTC")
    runs = await series.runs_series(pool, series.WINDOWS["today"])

    counted = sum(count for _, count, _, _ in calls)
    both = sum(mark for _, _, mark, _ in calls)
    assert (counted, both, len(runs)) == (3, 1, 4)
    assert len(runs) == counted + both


async def test_a_clamp_that_has_never_read_current_is_known_to_be_unfitted(pool):
    """The reference pit has one CT and two pumps, so pump 2's channel reads a
    perfectly convincing zero on every run. Drawing that as a flat healthy line
    would be a measurement of nothing."""
    from pitwatch.domain import series

    await pool.execute(
        "INSERT INTO em_sample (ts, channel, current) VALUES (now(), 0, 15.4), (now(), 1, 0.0)"
    )

    assert await series.clamp_fitted(pool, 0, 1.0) is True
    assert await series.clamp_fitted(pool, 1, 1.0) is False


async def test_a_contact_closed_before_the_window_still_counts(pool):
    """Read by the weekly summary, which says how many times each contact
    closed. A float that closed an hour before the window opened and is still
    closed has no event inside it, and reading only the events would take it as
    having been open the whole time, which is the opposite of what happened."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain import series

    now = datetime.now(UTC)
    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, $2, $3, $4, $4)",
        [
            # Closed well before the week being counted, and never reopened.
            (now - timedelta(days=2), 3, "High water", True),
            # And a second input that went and came back inside the window.
            (now - timedelta(hours=6), 4, "Lead float", True),
            (now - timedelta(hours=5, minutes=58), 4, "Lead float", False),
        ],
    )

    spans = await series.contact_spans(pool, [3, 4], series.WINDOWS["today"])

    assert len(spans[3]) == 1
    opened, shut = spans[3][0]
    # Clipped to the window at the near end and running to now at the far one,
    # because it is closed as this is being read.
    assert (now - opened) < timedelta(hours=25)
    assert (now - shut) < timedelta(minutes=1)

    assert len(spans[4]) == 1
    opened, shut = spans[4][0]
    assert (shut - opened) == pytest.approx(timedelta(minutes=2), abs=timedelta(seconds=5))


async def test_a_summary_keeps_the_numbers_it_was_given(pool):
    """A summary read a month later is an opinion unless what it was looking at
    is beside it."""
    import json

    row = await pool.fetchrow(
        """
        INSERT INTO summary (window_key, model, body, facts, written_by)
        VALUES ('7d', 'gpt-4o-mini', 'Both pumps look normal.', $1::jsonb, 'david')
        RETURNING id, created_at, facts
        """,
        json.dumps({"pumps": [{"pump": 1, "runs_this_week": 12}]}),
    )

    assert row["created_at"] is not None
    assert json.loads(row["facts"])["pumps"][0]["runs_this_week"] == 12


# -- runs recorded from the panel's own contacts -----------------------------
#
# The layer that replaced inferring runs from current. A contact says a pump
# started at the moment it started, so these are tallies and measurements
# rather than floors and estimates, and that only holds if the recorder gets
# the edges right.


def _store(pump1_run=1, pump2_run=2, high_water=3):
    """Just enough settings for the recorder: which input carries what.

    Which clamp belongs to which pump is no longer a choice. It was one only
    to keep stored readings under the numbers a meter gave them, and those
    were wiped.
    """
    from types import SimpleNamespace

    from pitwatch.schemas import ClampSource, ContactInput, MqttSettings

    return SimpleNamespace(
        mqtt=MqttSettings(
            inputs=[
                ContactInput(channel=pump1_run, role="pump1_run", topic=f"pit/in/{pump1_run}"),
                ContactInput(channel=pump2_run, role="pump2_run", topic=f"pit/in/{pump2_run}"),
                ContactInput(channel=high_water, role="high_water", topic=f"pit/in/{high_water}"),
            ],
            clamps=[
                ClampSource(pump=1, topic="meter/1"),
                ClampSource(pump=2, topic="meter/2"),
            ],
        ),
    )


def _edge(channel, state, at):
    from pitwatch.ingest.contacts import IoEvent

    return IoEvent(ts=at, channel=channel, label=f"DI{channel}", state=state, raw=state)


async def test_a_run_is_the_contact_closing_and_opening(pool):
    """Start to stop, measured. Not inferred from the load rising off nothing,
    which could only ever give a floor."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, False, began + timedelta(seconds=41))])

    row = await pool.fetchrow("SELECT * FROM pump_run WHERE pump = 1")
    assert row["duration_s"] == pytest.approx(41.0)
    assert row["started_by"] == "contact" and row["ended_by"] == "contact"
    assert row["role"] == "lead", "the first pump on a call is the lead one"

    cycle = await pool.fetchrow("SELECT * FROM pump_cycle WHERE id = $1", row["cycle_id"])
    assert cycle["ended_at"] is not None, "the cycle closes when its last run does"
    assert cycle["both_ran"] is False


async def test_both_pumps_on_one_call_is_one_cycle(pool):
    """The question that matters on a duplex panel. Two pumps running together
    is the pit winning, and it is only answerable if they share a cycle."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    await recorder.record([_edge(1, True, began)])
    # The pit is still filling, so the controller calls the lag pump too.
    await recorder.record([_edge(2, True, began + timedelta(seconds=10))])
    await recorder.record([_edge(1, False, began + timedelta(seconds=60))])
    await recorder.record([_edge(2, False, began + timedelta(seconds=70))])

    cycles = await pool.fetch("SELECT * FROM pump_cycle")
    assert len(cycles) == 1, "one call for water, not two"
    assert cycles[0]["both_ran"] is True
    assert cycles[0]["first_pump"] == 1
    # Open until the second of them stopped, not the first.
    assert cycles[0]["ended_at"] == began + timedelta(seconds=70)

    roles = dict(await pool.fetch("SELECT pump, role FROM pump_run"))
    assert roles == {1: "lead", 2: "lag"}


async def test_the_clamp_describes_the_run_without_deciding_it(pool):
    """Amps are recorded against the run and no longer say whether it
    happened. The inrush is kept in peak, because a starting current climbing
    month over month is a motor with a problem, and left out of the average and
    the median, which are about the pump rather than about the surge."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    await pool.executemany(
        "INSERT INTO em_sample (ts, channel, current) VALUES ($1, $2, $3)",
        [
            (began, 0, 48.0),
            (began + timedelta(seconds=1), 0, 44.0),
            (began + timedelta(seconds=5), 0, 16.0),
            (began + timedelta(seconds=10), 0, 16.0),
            (began + timedelta(seconds=15), 0, 16.0),
        ],
    )

    recorder = RunRecorder(pool, _store())
    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, False, began + timedelta(seconds=20))])

    row = await pool.fetchrow("SELECT * FROM pump_run WHERE pump = 1")
    assert row["peak_current"] == pytest.approx(48.0), "the surge is kept"
    assert row["steady_current"] == pytest.approx(16.0), "and left out of the median"
    assert row["avg_current"] == pytest.approx(23.0), "the second reading is still settling"
    assert row["samples"] == 5


async def test_a_run_short_enough_to_give_one_reading_still_gets_it(pool):
    """The real meter reports on change rather than on a schedule, so a four
    second run on this pit yields one or two readings.

    Excluding the inrush by time threw all of them away and every run came back
    with a null average and a null median. Dropping the first reading instead
    is the same intent in the units the meter actually delivers, and a run that
    produced only one reading keeps it: one running reading beats nothing, and
    a reading that arrived seconds after the start is not the surge.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    await pool.execute(
        "INSERT INTO em_sample (ts, channel, current) VALUES ($1, 0, 15.5)",
        began + timedelta(seconds=2),
    )

    recorder = RunRecorder(pool, _store())
    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, False, began + timedelta(seconds=4))])

    row = await pool.fetchrow("SELECT * FROM pump_run WHERE pump = 1")
    assert row["samples"] == 1
    assert row["steady_current"] == pytest.approx(15.5)
    assert row["avg_current"] == pytest.approx(15.5)


async def test_a_run_with_no_reading_at_all_is_still_a_run(pool):
    """A pump with no clamp fitted, which is where this installation is while
    the second CT is on order. The contacts alone say it ran and for how long,
    and those are the two facts a duration is made of."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())
    await recorder.record([_edge(2, True, began)])
    await recorder.record([_edge(2, False, began + timedelta(seconds=6))])

    row = await pool.fetchrow("SELECT * FROM pump_run WHERE pump = 2")
    assert row["duration_s"] == pytest.approx(6.0)
    assert row["peak_current"] is None and row["samples"] == 0


async def test_a_blip_too_short_to_be_a_pump_is_not_recorded_as_one(pool):
    """A contactor takes ten to thirty milliseconds just to pull in, and a run
    on this pit is twelve seconds. There is nothing real in between, so a
    closure of a few milliseconds is noise that got past the debounce rather
    than a very short run."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, False, began + timedelta(milliseconds=9))])

    assert await pool.fetchval("SELECT count(*) FROM pump_run") == 0
    # And the call it invented goes with it. A filling of the pit that no pump
    # answered was never a filling of the pit.
    assert await pool.fetchval("SELECT count(*) FROM pump_cycle") == 0


async def test_the_phantom_that_actually_happened_stops_being_a_both_pumps_call(pool):
    """The one this exists for, replayed from the real panel.

    On 2026-09-06 pump 2 ran a normal twelve second call. Nineteen milliseconds
    before it finished, pump 1's contact closed and opened again inside nine
    milliseconds, with the clamp reading nothing at all. That set `both_ran` on
    the cycle, which is an alert condition: it is on the dashboard, it is a
    figure on the history page, and it sends messages.

    `both_ran` is set the instant a second pump joins an open cycle, which is
    before anything can know how long that pump will stay. So it cannot be
    prevented on the way in; it has to be put back on the way out.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    # The real call.
    await recorder.record([_edge(2, True, began)])
    # The blip, near the end of it and inside it.
    await recorder.record([_edge(1, True, began + timedelta(seconds=12.13))])
    await recorder.record([_edge(1, False, began + timedelta(seconds=12.139))])
    # And the real call finishing.
    await recorder.record([_edge(2, False, began + timedelta(seconds=12.15))])

    runs = await pool.fetch("SELECT pump, duration_s FROM pump_run ORDER BY pump")
    assert [run["pump"] for run in runs] == [2], "only the pump that really ran"
    assert runs[0]["duration_s"] == pytest.approx(12.15, abs=0.01)

    cycle = await pool.fetchrow("SELECT * FROM pump_cycle")
    assert cycle["both_ran"] is False, "nobody gets woken up for this"
    assert cycle["ended_at"] is not None, "and the call still closes"


async def test_a_real_call_that_took_both_pumps_still_says_so(pool):
    """The floor has to leave the true case alone. A lag pump that genuinely
    joined the lead one is the thing this panel exists to report."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(2, True, began + timedelta(seconds=8))])
    await recorder.record([_edge(1, False, began + timedelta(seconds=30))])
    await recorder.record([_edge(2, False, began + timedelta(seconds=44))])

    cycle = await pool.fetchrow("SELECT * FROM pump_cycle")
    assert cycle["both_ran"] is True
    assert await pool.fetchval("SELECT count(*) FROM pump_run") == 2


async def test_a_blip_leaves_the_call_it_interrupted_alone(pool):
    """Discarding the blip must not take the real run's cycle with it, and must
    not leave that cycle open either."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(2, True, began + timedelta(seconds=3))])
    await recorder.record([_edge(2, False, began + timedelta(seconds=3.05))])
    await recorder.record([_edge(1, False, began + timedelta(seconds=20))])

    runs = await pool.fetch("SELECT pump, duration_s FROM pump_run")
    assert [run["pump"] for run in runs] == [1]
    assert runs[0]["duration_s"] == pytest.approx(20.0, abs=0.01)

    cycle = await pool.fetchrow("SELECT * FROM pump_cycle")
    assert cycle["both_ran"] is False
    assert cycle["ended_at"] is not None


async def test_what_the_panel_said_survives_the_run_being_discarded(pool):
    """The floor is a rule about the derived layer, not about the record. The
    raw edges keep their real timestamps in io_event, so the blip is still
    there to be found by anybody asking what the panel actually said."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    recorder = RunRecorder(pool, _store())

    # The reader writes io_event; the recorder is handed the same edges.
    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, $2, $3, $4, $4)",
        [
            (began, 1, "Pump 1 running", True),
            (began + timedelta(milliseconds=9), 1, "Pump 1 running", False),
        ],
    )
    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, False, began + timedelta(milliseconds=9))])

    assert await pool.fetchval("SELECT count(*) FROM pump_run") == 0
    assert await pool.fetchval("SELECT count(*) FROM io_event WHERE channel = 1") == 2


async def test_a_stop_with_no_start_is_not_a_run(pool):
    """The usual cause is a restart across a run: the contact was already
    closed when this came up, so the opening edge belonged to the process
    before. Inventing a run with no beginning would put a wrong duration on
    every page that reads it."""
    from datetime import UTC, datetime

    from pitwatch.domain.runs import RunRecorder

    await RunRecorder(pool, _store()).record([_edge(1, False, datetime.now(UTC))])

    assert await pool.fetchval("SELECT count(*) FROM pump_run") == 0
    assert await pool.fetchval("SELECT count(*) FROM pump_cycle") == 0


async def test_a_second_start_closes_the_run_left_open(pool):
    """A missed stop edge would otherwise leave a run open forever, and the
    unique index would refuse the new one. A pump that is running now matters
    more than tidying up one that is not, so the orphan is closed and said
    so."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=10)
    recorder = RunRecorder(pool, _store())

    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, True, began + timedelta(minutes=5))])

    rows = await pool.fetch("SELECT ended_by, ended_at FROM pump_run ORDER BY started_at")
    assert len(rows) == 2
    assert rows[0]["ended_by"] == "timeout" and rows[0]["ended_at"] is not None
    assert rows[1]["ended_at"] is None, "the run that is actually happening stays open"


async def test_a_cycle_remembers_the_pit_came_up_high(pool):
    """Read off the float's own history rather than from whatever it says once
    the pumps have finished, because by then it has usually dropped again,
    which is the system working."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.runs import RunRecorder

    began = datetime.now(UTC) - timedelta(minutes=5)
    await pool.execute(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, 3, 'High water', true, true)",
        began + timedelta(seconds=5),
    )

    recorder = RunRecorder(pool, _store())
    await recorder.record([_edge(1, True, began)])
    await recorder.record([_edge(1, False, began + timedelta(seconds=90))])

    assert await pool.fetchval("SELECT high_water FROM pump_cycle") is True


# -- what a contact has been doing -------------------------------------------


async def test_a_contact_that_never_closed_still_has_a_row(pool):
    """A high water float that stayed dry all month should read never and
    none, not n/a.

    It read n/a, because the query was driven off the closings and "has never
    closed" and "we have no data" arrived looking identical. `known` exists to
    tell those apart and could not, since a contact with no closings produced
    no row at all.
    """
    from pitwatch.domain.history import SignalHistory

    await pool.execute(
        """
        INSERT INTO io_state (channel, label, state, raw, changed_at, updated_at)
        VALUES (4, 'High water', false, false, now(), now())
        """
    )

    closings = await SignalHistory().closings(pool, [4])

    assert 4 in closings, "an input being read has a row even with nothing to show"
    assert closings[4].known is True
    assert closings[4].today == 0 and closings[4].month == 0
    assert closings[4].last_on is None
    assert closings[4].last_held_s is None
    assert closings[4].as_json()["today"] == 0, "zero, not null: this is a real count"


async def test_a_closing_carries_how_long_it_was_held(pool):
    """A float wet for sixteen seconds and one wet for six minutes are the same
    row without it, and they are not the same news."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import SignalHistory

    began = datetime.now(UTC) - timedelta(minutes=10)
    await pool.execute(
        "INSERT INTO io_state (channel, label, state, raw, changed_at, updated_at)"
        " VALUES (5, 'Lead float', false, false, now(), now())"
    )
    await pool.executemany(
        "INSERT INTO io_event (ts, channel, label, state, raw) VALUES ($1, 5, 'Lead float', $2, $2)",
        [
            (began, True),
            (began + timedelta(seconds=90), False),
            # The most recent one is the one reported.
            (began + timedelta(minutes=5), True),
            (began + timedelta(minutes=5, seconds=16), False),
        ],
    )

    closings = await SignalHistory().closings(pool, [5])

    assert closings[5].today == 2
    assert closings[5].last_held_s == pytest.approx(16.0), "the latest, not the longest"
    assert closings[5].last_on == began + timedelta(minutes=5)


async def test_a_contact_still_held_has_no_duration_yet(pool):
    """Null rather than a number counted up to now. A float that is wet right
    now has not finished being wet."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.domain.history import SignalHistory

    began = datetime.now(UTC) - timedelta(minutes=2)
    await pool.execute(
        "INSERT INTO io_state (channel, label, state, raw, changed_at, updated_at)"
        " VALUES (5, 'Lead float', true, true, now(), now())"
    )
    await pool.execute(
        "INSERT INTO io_event (ts, channel, label, state, raw)"
        " VALUES ($1, 5, 'Lead float', true, true)",
        began,
    )

    closings = await SignalHistory().closings(pool, [5])

    assert closings[5].last_on == began
    assert closings[5].last_held_s is None
