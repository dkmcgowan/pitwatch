"""The water table the pit sits in.

The feature exists because of one afternoon on the reference installation: the
pit's call rate went up sevenfold at 19:18 and nothing inside the building
explained it, and high water at the Battery that evening was the highest in nine
days at 19:43. These tests are about the parts of that which can be got wrong
quietly, which is the arithmetic and the storage rather than the fetching.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from pitwatch.domain import tides as tide_domain
from pitwatch.ingest import tides
from pitwatch.schemas import SiteSettings, TideSettings


def test_feet_are_what_is_stored_and_not_always_what_is_shown():
    assert tides.as_read(5.80, "ft") == 5.8
    assert tides.as_read(5.80, "m") == 1.77
    assert tides.as_read(None, "ft") is None
    assert tides.spoken(5.80, "ft") == "5.8 ft"
    assert tides.spoken(5.80, "m") == "1.77 m"
    assert tides.spoken(None, "ft") == "--"


def test_a_station_needs_choosing_before_anything_is_asked():
    """Off by default, because most pits are nowhere near tidal water and a
    station somebody has not chosen is a wrong answer rather than a missing
    one."""
    assert not TideSettings().ready
    assert not TideSettings(enabled=True).ready, "enabled with no station is not ready"
    assert not TideSettings(station="8518750").ready, "a station with the switch off is not"
    assert TideSettings(enabled=True, station="8518750").ready


def test_the_nearest_gauge_is_the_nearest_one():
    """The distance is great circle, which is far finer than picking between
    gauges tens of miles apart."""
    battery = (40.7006, -74.0142)
    # 822 Greenwich St, near enough. The Battery is a couple of miles south.
    miles = tides._miles(40.7390, -74.0090, *battery)
    assert 2 < miles < 4, miles
    # And it is symmetric, which a sloppy formula is not.
    assert math.isclose(miles, tides._miles(*battery, 40.7390, -74.0090))


async def _readings(pool, rows):
    await tides.store(pool, rows)


async def test_an_observation_is_never_overwritten_by_a_prediction(pool):
    """The window rolls forward every fifteen minutes, so a moment that carried
    an observation last time arrives next time as a prediction and nothing else.
    Writing that null over the reading would quietly erase the record of what
    the water actually did, which is the half nobody can recompute."""
    when = datetime(2026, 9, 9, 23, 42, tzinfo=UTC)

    await _readings(pool, [tides.Reading(ts=when, observed=5.79, predicted=5.80)])
    await _readings(pool, [tides.Reading(ts=when, observed=None, predicted=5.80)])

    row = await pool.fetchrow("SELECT observed, predicted FROM tide_reading WHERE ts = $1", when)
    assert float(row["observed"]) == pytest.approx(5.79)
    assert float(row["predicted"]) == pytest.approx(5.80)


async def test_the_turning_points_come_out_of_the_stored_curve(pool):
    """Rather than a second call to NOAA. The high on the card and the curve
    drawn under it then cannot disagree, which they would if one were fetched
    and the other derived."""
    now = datetime.now(UTC)
    rows = []
    # A clean half cycle: rising to a peak an hour out, then falling.
    for step in range(-30, 90):
        when = now + timedelta(minutes=6 * step)
        level = 3.0 + 2.8 * math.cos(math.pi * (step - 10) / 60)
        rows.append(tides.Reading(ts=when, observed=level if step <= 0 else None, predicted=level))
    await _readings(pool, rows)

    tide = await tide_domain.read(pool, timedelta(hours=4), timedelta(hours=10))

    assert tide is not None
    assert tide.rising is True
    assert tide.next_turn is not None and tide.next_turn.high is True
    # The peak of that cosine is an hour out, give or take one six minute step.
    assert abs((tide.next_turn.ts - now).total_seconds() - 3600) < 400
    assert tide.next_turn.level == pytest.approx(5.8, abs=0.05)


async def test_the_surge_is_the_water_the_moon_did_not_put_there(pool):
    """A foot above prediction is wind pushing water inshore, and it is the
    number that floods a cellar."""
    now = datetime.now(UTC)
    rows = []
    for step in range(-20, 40):
        when = now + timedelta(minutes=6 * step)
        rows.append(
            tides.Reading(
                ts=when,
                observed=4.0 + 1.1 if step <= 0 else None,
                predicted=4.0,
            )
        )
    await _readings(pool, rows)

    tide = await tide_domain.read(pool, timedelta(hours=3), timedelta(hours=4))

    assert tide is not None
    assert tide.now == pytest.approx(5.1, abs=0.01)
    assert tide.surge == pytest.approx(1.1, abs=0.01)


async def test_a_gauge_that_stopped_reporting_says_nothing_rather_than_something_old(pool):
    """Twenty minutes of silence and the card should not print a level as if it
    were current. Nothing is a better answer than stale."""
    now = datetime.now(UTC)
    rows = [
        tides.Reading(ts=now - timedelta(minutes=40), observed=4.2, predicted=4.2),
        tides.Reading(ts=now - timedelta(minutes=34), observed=None, predicted=4.3),
        tides.Reading(ts=now + timedelta(minutes=6), observed=None, predicted=4.5),
        tides.Reading(ts=now + timedelta(minutes=12), observed=None, predicted=4.6),
    ]
    await _readings(pool, rows)

    tide = await tide_domain.read(pool, timedelta(hours=2), timedelta(hours=2))

    assert tide is not None
    assert tide.now is None
    assert tide.surge is None


async def test_high_and_low_water_per_day_for_the_summary(pool, store):
    """Cut on the site's own midnight, the same as the calls and the rain, so a
    day of tide and a day of pumping describe the same hours."""
    from pitwatch import summary

    await store.put(SiteSettings(timezone="America/New_York", latitude=40.74, longitude=-74.01))
    await store.put(TideSettings(enabled=True, station="8518750", station_name="The Battery, NY"))

    now = datetime.now(UTC)
    rows = []
    for step in range(1, 200):
        when = now - timedelta(minutes=6 * step)
        rows.append(tides.Reading(ts=when, observed=3.0 + 2.8 * math.cos(step / 20), predicted=3.0))
    await _readings(pool, rows)

    sent = await summary.tide(pool, store, summary.WINDOW, "America/New_York")

    assert sent is not None
    assert sent["units"] == "ft"
    assert sent["station"] == "The Battery, NY"
    assert "not a level in the pit" in sent["source"]
    assert sent["days"], "at least the day the readings are on"
    day = sent["days"][-1]
    assert day["high_water"] > day["low_water"]


async def test_a_pit_with_no_station_is_sent_no_tide_column(pool, store):
    """Most installations. A pit in the middle of a county has no tide and
    should not be handed a column of nulls to explain."""
    from pitwatch import summary

    await store.put(SiteSettings(timezone="America/New_York"))
    await store.put(TideSettings())

    assert await summary.tide(pool, store, summary.WINDOW, "America/New_York") is None
