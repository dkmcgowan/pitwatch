"""One building's rows are not another's.

Written before the queries were scoped, on purpose. Row level isolation fails
silently: a query that forgets its site returns every building's data, and it
passes every test written against a single site. There is one site in
production, so nothing here would ever fail by accident during normal work, and
that is exactly why it has to be deliberate.

The rule these tests enforce is simple. Put identical-looking data in two
buildings, read as one of them, and see only one building's rows back. Every
reader that grows a new query should get a line here.

**This is not a substitute for row level security.** Scoping by hand relies on
every future query remembering, and these tests only cover the readers somebody
thought to list. Before a second site holds real data, the database should
refuse rather than the developer remembering. See the note in migration 029.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def _two_sites(pool) -> tuple[int, int]:
    """Two buildings, the second added beside the one the migration made."""
    first = await pool.fetchval("SELECT min(id) FROM site")
    second = await pool.fetchval(
        "INSERT INTO site (name) VALUES ('The other building') RETURNING id"
    )
    return first, second


async def test_the_migration_leaves_exactly_one_site(pool):
    """A fresh install is one building, and everything in it belongs to that
    one. Anything that arrives later has to say which."""
    rows = await pool.fetch("SELECT id, name FROM site")

    assert len(rows) == 1
    assert rows[0]["name"], "a site with no name is a site nobody can pick"


async def test_a_reading_cannot_be_written_without_a_building(pool):
    """`NOT NULL` with no default, which is the point.

    A default would let a future insert quietly land in whichever building the
    default named, and that row would then appear on somebody else's
    dashboard. Better it fails the first time somebody forgets, which is what
    this asserts.
    """
    # Deliberately no site_id. If a later mechanical pass ever "fixes" this
    # insert, the test fails rather than quietly passing, which is how it was
    # caught the first time.
    with pytest.raises(Exception, match="site_id"):
        await pool.execute(
            "INSERT INTO pump_run (pump, started_at, started_by) VALUES (1, now(), 'contact')"
        )


async def test_two_buildings_keep_their_own_contacts_runs_and_alerts(pool):
    """The core of it. Identical rows in two sites, and a read scoped to one
    returns one."""
    first, second = await _two_sites(pool)
    now = datetime.now(UTC)

    for site in (first, second):
        await pool.execute(
            "INSERT INTO io_event (site_id, ts, channel, label, state, raw)"
            " VALUES ($1, $2, 3, 'System alert', true, false)",
            site,
            now,
        )
        await pool.execute(
            "INSERT INTO pump_run (site_id, pump, started_at, ended_at, duration_s, started_by)"
            " VALUES ($1, 1, $2, $3, 12.0, 'contact')",
            site,
            now,
            now + timedelta(seconds=12),
        )
        await pool.execute(
            "INSERT INTO alert (site_id, rule, severity, title, detail)"
            " VALUES ($1, 'high_water', 'critical', 'High water', 'Wet')",
            site,
        )
        await pool.execute(
            "INSERT INTO em_sample (site_id, ts, channel, current) VALUES ($1, $2, 0, 15.1)",
            site,
            now,
        )

    for table, when in (
        ("io_event", "ts"),
        ("pump_run", "started_at"),
        ("alert", "raised_at"),
        ("em_sample", "ts"),
    ):
        both = await pool.fetchval(f"SELECT count(*) FROM {table}")
        mine = await pool.fetchval(
            f"SELECT count(*) FROM {table} WHERE site_id = $1",
            first,
        )
        assert both == 2, f"{table}: the fixture did not write both"
        assert mine == 1, f"{table}: scoping by site returned {mine} rows, not one"
        assert when  # the ordering column exists, which the readers rely on


async def test_the_same_channel_number_means_a_different_wire_in_each_building(pool):
    """`io_state` was one row per channel. Channel 3 is the alarm here and
    could be anything next door, so the key had to grow."""
    first, second = await _two_sites(pool)

    for site in (first, second):
        await pool.execute(
            "INSERT INTO io_state (site_id, channel, label, state, raw, changed_at, updated_at)"
            " VALUES ($1, 3, 'System alert', false, false, now(), now())",
            site,
        )

    assert await pool.fetchval("SELECT count(*) FROM io_state") == 2


async def test_two_buildings_can_both_call_a_device_inputs(pool):
    """`device_status` was one row per name, and every installation calls its
    module the same thing.

    The first building's rows are the ones the schema seeds, carried across by
    the migration, so this is the real case rather than a contrived one: a new
    building adding a device that already exists next door.
    """
    first, second = await _two_sites(pool)

    seeded = await pool.fetchval(
        "SELECT count(*) FROM device_status WHERE site_id = $1 AND device = 'health0'", first
    )
    assert seeded == 1, "the migration should have carried the seeded devices across"

    await pool.execute(
        "INSERT INTO device_status (site_id, device, online) VALUES ($1, 'health0', true)",
        second,
    )

    assert await pool.fetchval("SELECT count(*) FROM device_status WHERE device = 'health0'") == 2
    # And a building still cannot have two rows for one device.
    with pytest.raises(Exception, match="device_status_pkey"):
        await pool.execute(
            "INSERT INTO device_status (site_id, device, online) VALUES ($1, 'health0', true)",
            second,
        )


async def test_an_open_alert_in_one_building_does_not_silence_another(pool):
    """The most dangerous index in the schema.

    One open alert per rule is what stops the same alarm being raised twice.
    Left unscoped it would stop one building's overload being raised at all,
    because another building already had one open, and nobody would be told
    their pump was out.
    """
    first, second = await _two_sites(pool)

    for site in (first, second):
        await pool.execute(
            "INSERT INTO alert (site_id, rule, severity, pump, title, detail)"
            " VALUES ($1, 'overload', 'critical', 1, 'Overload', 'Pump 1 tripped')",
            site,
        )

    assert await pool.fetchval("SELECT count(*) FROM alert WHERE cleared_at IS NULL") == 2

    # And it still refuses a second open one within the same building.
    with pytest.raises(Exception, match="alert_one_open_per_rule"):
        await pool.execute(
            "INSERT INTO alert (site_id, rule, severity, pump, title, detail)"
            " VALUES ($1, 'overload', 'critical', 1, 'Overload', 'Again')",
            first,
        )


async def test_settings_are_per_building_and_the_account_is_not(pool):
    """Two tables rather than one nullable column. A building's panel is its
    own; the Twilio account is PitWatch's."""
    first, second = await _two_sites(pool)

    for site, name in ((first, "822 Greenwich St"), (second, "Somewhere else")):
        await pool.execute(
            "INSERT INTO site_setting (site_id, key, value) VALUES ($1, 'site', $2::jsonb)",
            site,
            f'{{"name": "{name}"}}',
        )
    await pool.execute(
        "INSERT INTO setting (key, value) VALUES ('sms', '{\"account_sid\": \"one\"}'::jsonb)"
    )

    here = await pool.fetchval(
        "SELECT value #>> '{name}' FROM site_setting WHERE site_id = $1 AND key = 'site'", first
    )
    assert here == "822 Greenwich St"

    # And one account, not one per building.
    assert await pool.fetchval("SELECT count(*) FROM setting WHERE key = 'sms'") == 1
    assert await pool.fetchval("SELECT count(*) FROM site_setting WHERE key = 'site'") == 2


async def test_a_person_holds_a_role_in_a_building_rather_than_everywhere(pool):
    """A membership, because the shape that matters later is somebody owning
    their own building and reading the one next door."""
    first, second = await _two_sites(pool)
    who = await pool.fetchval(
        "INSERT INTO app_user (username, name, email, role, enabled, min_severity)"
        " VALUES ('someone', 'Someone', 's@example.com', 'viewer', true, 'info') RETURNING id"
    )

    await pool.execute(
        "INSERT INTO site_member (site_id, user_id, role) VALUES ($1, $2, 'owner')", first, who
    )
    await pool.execute(
        "INSERT INTO site_member (site_id, user_id, role) VALUES ($1, $2, 'viewer')", second, who
    )

    roles = {
        row["site_id"]: row["role"]
        for row in await pool.fetch("SELECT site_id, role FROM site_member WHERE user_id = $1", who)
    }
    assert roles == {first: "owner", second: "viewer"}
