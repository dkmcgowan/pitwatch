"""The water table under the pit.

The tide was a proxy for this and a confounded one: on every night measured so
far, high water sat near midnight and low near dawn, so "the tide is falling"
and "the building has gone to sleep" were the same curve and nothing could tell
them apart. A well measures the thing itself.

These tests are about the parts that can be got wrong quietly, which is the
parsing and the arithmetic rather than the fetching. USGS answers in its own
tab separated format with a line of column widths that is not data, and the
value column is named after the series so it cannot be looked up by name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pitwatch.domain import groundwater as gw_domain
from pitwatch.ingest import groundwater as gw
from pitwatch.schemas import GroundwaterSettings

# One real answer from the well two hundred meters from the reference
# installation, trimmed. The widths line and the qualifier column are the two
# things a parser written in a hurry gets wrong.
ANSWER = "\n".join(
    [
        "# a comment USGS puts at the top",
        "#",
        "agency_cd\tsite_no\tdatetime\t355012_62611_00003\t355012_62611_00003_cd",
        "5s\t15s\t20d\t14n\t10s",
        "USGS\t404424074002301\t2026-08-17\t-0.35\tP",
        "USGS\t404424074002301\t2026-08-18\t-0.37\tP",
        "USGS\t404424074002301\t2026-08-19\t-0.38\tP",
    ]
)


def test_a_well_needs_choosing_before_anything_is_asked():
    """Off by default. Most pits have nobody measuring the ground under them,
    and a well somebody has not chosen is a wrong answer rather than a missing
    one."""
    assert not GroundwaterSettings().ready
    assert not GroundwaterSettings(enabled=True).ready, "enabled with no well is not ready"
    assert not GroundwaterSettings(site_no="404424074002301").ready, "a well with the switch off"
    assert GroundwaterSettings(enabled=True, site_no="404424074002301").ready


def test_the_widths_line_is_not_a_reading():
    """The line under the header holds column widths, not data. It looks like a
    row, it parses like a row, and taking it as one puts a reading of "20d" in
    the record."""
    rows = gw._rows(ANSWER)

    assert rows[0][0] == "agency_cd", "the header survives"
    assert len(rows) == 4, rows
    assert all(row[0] == "USGS" for row in rows[1:])


def test_the_value_column_is_found_rather_than_named():
    """Its name carries the series id and the parameter, so it cannot be known
    in advance. It is the first column after datetime that is not a qualifier,
    and qualifiers are the ones ending in _cd. Taking the column after datetime
    blindly works here and breaks on a well that reports two parameters."""
    rows = gw._rows(ANSWER)
    header = rows[0]
    after = header.index("datetime") + 1

    assert header[after].endswith("_00003"), "the value column, named for its series"
    assert header[after + 1].endswith("_cd"), "and its qualifier, which is not a reading"


async def test_a_day_the_well_was_not_read_is_skipped_rather_than_stored_as_zero(pool):
    """A gap in a water table record is a gap. Storing it as zero would read as
    the water table arriving exactly at the datum, which on this well is about
    a third of a foot above where it actually sits."""
    body = ANSWER.replace("-0.37", "")
    rows = gw._rows(body)
    header = rows[0]
    kept = []
    for parts in rows[1:]:
        try:
            kept.append(float(parts[header.index("datetime") + 1]))
        except ValueError:
            continue

    assert kept == [-0.35, -0.38], kept


async def test_a_reading_keeps_the_well_it_came_from(pool):
    """Per row rather than only in settings, so changing the well later does
    not silently restate every old reading as having come from the new one."""
    await gw.store(
        pool,
        [
            gw.Reading(ts=datetime(2026, 8, 18, tzinfo=UTC), level=-0.37, site_no="one"),
            gw.Reading(ts=datetime(2026, 8, 19, tzinfo=UTC), level=-0.38, site_no="one"),
        ],
    )

    rows = await pool.fetch("SELECT site_no FROM groundwater_reading ORDER BY ts")
    assert [row["site_no"] for row in rows] == ["one", "one"]


async def test_the_comparison_is_against_the_same_fortnight_a_year_before(pool):
    """A year before the newest reading, not a year before today.

    The series runs about a month behind, so "a year ago today" would compare
    an August reading against a September that has not been published yet, and
    read an ordinary seasonal decline as a change in the ground.
    """
    newest = datetime(2026, 8, 19, tzinfo=UTC)
    rows = []
    # Last year's August, the fortnight the comparison should find.
    for day in range(-14, 15):
        rows.append(gw.Reading(ts=newest - timedelta(days=365 - day), level=-0.60, site_no="w"))
    # Last year's autumn, which it should not: lower, and a month later.
    for day in range(20, 40):
        rows.append(gw.Reading(ts=newest - timedelta(days=365 - day), level=-1.50, site_no="w"))
    rows.append(gw.Reading(ts=newest, level=-0.33, site_no="w"))
    await gw.store(pool, rows)

    water = await gw_domain.read(pool, timedelta(days=90))

    assert water is not None
    assert water.level == pytest.approx(-0.33)
    assert water.a_year_ago == pytest.approx(-0.60, abs=0.01), "the autumn rows leaked in"
    # The finding that made this worth building: a quarter foot higher than the
    # same fortnight last year.
    assert water.change == pytest.approx(0.27, abs=0.01)


async def test_the_card_is_told_how_old_its_reading_is(pool):
    """This series is weeks behind by nature, and a card that printed the
    number without its date would be claiming something it cannot know."""
    now = datetime.now(UTC)
    await gw.store(pool, [gw.Reading(ts=now - timedelta(days=27), level=-0.38, site_no="w")])

    water = await gw_domain.read(pool, timedelta(days=90))

    assert water is not None and water.at is not None
    assert gw_domain.stale_days(water, now) == 27
    assert water.as_json()["at"], "the date reaches the browser"


async def test_nothing_stored_is_a_different_answer_from_a_dry_season(pool):
    """None means nobody has looked, which the card draws by hiding itself. A
    number means the well was read. Confusing the two puts an empty card on the
    dashboard of every installation that has no well near it."""
    assert await gw_domain.read(pool, timedelta(days=90)) is None


def test_depth_below_surface_is_not_what_gets_stored():
    """72019 is depth to water, which grows as the water table falls. Storing a
    number whose sign runs backwards from every other series here is how
    somebody later reads a drought as a flood."""
    assert "72019" not in gw.PARAMETERS
    assert "62611" in gw.PARAMETERS, "NAVD88, which is a height"


def test_the_first_pass_asks_for_the_whole_record():
    """The comparison against other years is the only reason this series is
    worth having, and it cannot be made from two months of readings. So the
    reader's first request reaches back past the well's own beginning and every
    one after it takes the recent window."""
    assert gw.EVERYTHING < "1950", gw.EVERYTHING
    assert gw.BACK.days == 60, "the routine poll stays small"


async def test_the_year_on_year_comparison_needs_the_backfill(pool):
    """The failure this guards against is quiet: with only recent readings
    stored, the card renders, the level is right, and the one number that makes
    it meaningful is simply absent. Nothing errors."""
    newest = datetime(2026, 8, 19, tzinfo=UTC)
    await gw.store(pool, [gw.Reading(ts=newest, level=-0.33, site_no="w")])

    thin = await gw_domain.read(pool, timedelta(days=90))
    assert thin is not None
    assert thin.level == pytest.approx(-0.33)
    assert thin.a_year_ago is None, "nothing to compare against yet"
    assert thin.change is None

    await gw.store(
        pool,
        [
            gw.Reading(ts=newest - timedelta(days=365 + day), level=-0.60, site_no="w")
            for day in range(-7, 8)
        ],
    )

    filled = await gw_domain.read(pool, timedelta(days=90))
    assert filled is not None
    assert filled.change == pytest.approx(0.27, abs=0.01)
