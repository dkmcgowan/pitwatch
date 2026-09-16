"""Handing the window over as rows.

Every chart on the history page answers the question it was drawn for. The
person best placed to answer a question about this pit is often not sitting in
front of the application at all, so the same window has to be available as
something a plumber or a board can open.

These tests read the workbook back rather than trusting that writing it went
well. A spreadsheet that opens to the wrong hour, or silently loses the rows
past a limit, fails in a way nobody checks until they are arguing about which
Tuesday they are both looking at.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta

from pitwatch.domain import export


async def _some_history(pool):
    base = datetime(2026, 9, 14, 18, 30, tzinfo=UTC)
    await pool.execute(
        "INSERT INTO io_event (ts, channel, label, state, raw)"
        " VALUES ($1, 3, 'System alert', true, false)",
        base,
    )
    await pool.execute(
        "INSERT INTO pump_run (pump, started_at, ended_at, duration_s, peak_current,"
        " steady_current, started_by) VALUES (1, $1, $2, 12.4, 38.9, 15.1, 'contact')",
        base,
        base + timedelta(seconds=12),
    )
    await pool.execute("INSERT INTO em_sample (ts, channel, current) VALUES ($1, 0, 15.1)", base)
    await pool.execute(
        "INSERT INTO alert (rule, severity, pump, title, detail, raised_at)"
        " VALUES ('overload', 'critical', 1, 'Overload tripped', 'Pump 1 tripped', $1)",
        base,
    )
    return base


async def test_the_workbook_has_a_tab_for_each_kind_of_thing(pool, tmp_path):
    """Four sheets and an About, because a file of numbers with no window, no
    zone and no date on it is a file somebody misreads a month later."""
    await _some_history(pool)

    sheets = await export.gather(
        pool,
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 30, tzinfo=UTC),
        "America/New_York",
        {1: "Pump 1", 2: "Pump 2"},
    )
    path = tmp_path / "book.xlsx"
    export.write(str(path), sheets, [("PitWatch export", ""), ("Window", "The last 7 days")])

    assert [one.title for one in sheets] == ["Contacts", "Pump runs", "Amps", "Alerts"]
    assert all(len(one.rows) == 1 for one in sheets), [len(o.rows) for o in sheets]

    # A real xlsx is a zip, and every sheet has to be in it.
    with zipfile.ZipFile(path) as book:
        names = book.namelist()
        assert any(name.endswith("workbook.xml") for name in names)
        assert len([n for n in names if "worksheets/sheet" in n]) == 5, names
        inside = book.read("xl/workbook.xml").decode()
    for title in ("About", "Contacts", "Pump runs", "Amps", "Alerts"):
        assert title in inside, title


async def test_times_are_written_on_the_buildings_clock(pool):
    """Spreadsheets have no timezone type. A column of UTC against a building
    that runs on Eastern is a column somebody reads four hours wrong, and
    nothing in the file would ever tell them."""
    base = await _some_history(pool)

    sheets = await export.gather(
        pool,
        base - timedelta(days=1),
        base + timedelta(days=1),
        "America/New_York",
        {1: "Pump 1", 2: "Pump 2"},
    )
    contacts = next(one for one in sheets if one.title == "Contacts")
    when = contacts.rows[0][0]

    # 18:30 UTC is 14:30 in New York in September.
    assert when.hour == 14 and when.minute == 30, when
    assert when.tzinfo is None, "an offset here is either dropped or silently applied"


async def test_a_contact_says_closed_rather_than_true(pool):
    """The column is read by somebody holding a wiring diagram, and that is the
    word on it."""
    await _some_history(pool)

    sheets = await export.gather(
        pool,
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 30, tzinfo=UTC),
        "UTC",
        {1: "Pump 1", 2: "Pump 2"},
    )
    contacts = next(one for one in sheets if one.title == "Contacts")

    assert contacts.rows[0][3] == "Closed"


async def test_an_alert_still_open_is_carried_in_however_old_it_is(pool):
    """The one row somebody acts on. An alert raised a fortnight before the
    window and never cleared is the most important line in the file, and
    windowing it out the way the other sheets are windowed would be the single
    omission that matters."""
    old = datetime.now(UTC) - timedelta(days=40)
    await pool.execute(
        "INSERT INTO alert (rule, severity, title, detail, raised_at)"
        " VALUES ('high_water', 'critical', 'High water', 'Still wet', $1)",
        old,
    )

    sheets = await export.gather(
        pool,
        datetime.now(UTC) - timedelta(days=7),
        datetime.now(UTC),
        "UTC",
        {},
    )
    alerts = next(one for one in sheets if one.title == "Alerts")

    assert len(alerts.rows) == 1, "an open alert older than the window was dropped"
    assert alerts.rows[0][2] == "high_water"


async def test_a_cleared_alert_older_than_the_window_is_left_out(pool):
    """The other half of that rule. Everything that ever happened is not a
    window."""
    old = datetime.now(UTC) - timedelta(days=40)
    await pool.execute(
        "INSERT INTO alert (rule, severity, title, detail, raised_at, cleared_at)"
        " VALUES ('high_water', 'critical', 'High water', 'Was wet', $1, $2)",
        old,
        old + timedelta(hours=1),
    )

    sheets = await export.gather(
        pool,
        datetime.now(UTC) - timedelta(days=7),
        datetime.now(UTC),
        "UTC",
        {},
    )
    alerts = next(one for one in sheets if one.title == "Alerts")

    assert alerts.rows == []


def test_the_file_is_named_so_a_folder_of_them_sorts():
    """Somebody downloading one a month wants them in order without renaming
    anything."""
    when = datetime(2026, 9, 16, tzinfo=UTC)

    assert export.filename("30d", when) == "pitwatch-30d-2026-09-16.xlsx"
