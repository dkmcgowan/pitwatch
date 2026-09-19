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
    """The window rolls forward on every poll, so a moment that carried an
    observation last time arrives next time as a prediction and nothing else.
    It matters more now that the frequent poll carries no prediction at all.
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
    """Forty minutes of silence and the card should not print a level as if it
    were current. Nothing is a better answer than stale, and the window is wide
    enough for NOAA being its normal eleven minutes behind, not for a gauge
    that has stopped."""
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


async def test_high_and_low_water_per_day_for_the_chat(pool, store):
    """Cut on the site's own midnight, the same as the calls and the rain, so a
    day of tide and a day of pumping describe the same hours."""
    from pitwatch import chat

    await store.put(SiteSettings(timezone="America/New_York", latitude=40.74, longitude=-74.01))
    await store.put(TideSettings(enabled=True, station="8518750", station_name="The Battery, NY"))

    now = datetime.now(UTC)
    rows = []
    for step in range(1, 200):
        when = now - timedelta(minutes=6 * step)
        rows.append(tides.Reading(ts=when, observed=3.0 + 2.8 * math.cos(step / 20), predicted=3.0))
    await _readings(pool, rows)

    sent = await chat.tide(pool, store, chat.WINDOW, "America/New_York")

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
    from pitwatch import chat

    await store.put(SiteSettings(timezone="America/New_York"))
    await store.put(TideSettings())

    assert await chat.tide(pool, store, chat.WINDOW, "America/New_York") is None


def test_the_poll_is_often_enough_that_a_reading_still_counts_as_current():
    """The bug this encodes: three numbers that each looked reasonable alone
    and did not fit together.

    NOAA publishes the gauge every six minutes and is some minutes behind real
    time when it does, so a reading is already old when it is fetched. It then
    ages until the next fetch replaces it. If that total can pass the window
    the dashboard calls current, the level on the card goes blank for part of
    every cycle, which is what it did on a fifteen minute poll."""
    # Measured against the Battery over three cycles: eleven minutes, plus a
    # few for the day NOAA is slower than it was that morning.
    behind_when_fetched = timedelta(minutes=14)
    oldest = timedelta(seconds=tides.EVERY_S) + behind_when_fetched

    assert oldest < tide_domain.NOW_WITHIN, (
        f"the newest reading reaches {oldest} before it is replaced, "
        f"past the {tide_domain.NOW_WITHIN} the dashboard will print"
    )


def test_the_frequent_poll_asks_only_for_the_half_that_goes_stale():
    """The prediction is harmonic and costs four days of six minute rows. The
    observation is the only half with a reason to be asked for often, and its
    window is hours rather than days."""
    now = datetime(2026, 9, 13, 21, 45, tzinfo=UTC)

    begin, end = tides._recent_window(now)

    assert end == "20260913 21:45", "to the minute, not to the day"
    assert begin == "20260913 15:45"
    assert tides.PREDICTION_EVERY_S > tides.EVERY_S, "the expensive half is asked for less"


async def test_a_flat_crest_is_one_high_tide_and_not_two(pool):
    """Slack water is flat, so a crest arrives as two samples at the same
    height. Counting both made the next turn and the one after it the same
    tide, six minutes apart."""
    now = datetime.now(UTC)
    # Rise, sit at the top for two samples, then fall.
    levels = [3.0, 3.6, 4.2, 4.8, 5.1, 5.1, 4.8, 4.2, 3.6, 3.0, 2.4, 2.0, 2.4, 3.0]
    rows = [
        tides.Reading(ts=now + timedelta(minutes=6 * step), observed=None, predicted=level)
        for step, level in enumerate(levels, start=1)
    ]
    await _readings(pool, rows)

    tide = await tide_domain.read(pool, timedelta(hours=1), timedelta(hours=4))

    assert tide is not None
    assert tide.next_turn is not None and tide.next_turn.high is True
    assert tide.next_turn.level == pytest.approx(5.1, abs=0.01)
    # The one after it is the low that follows, not the same crest again.
    assert tide.following is not None
    assert tide.following.high is False, "the crest was counted twice"
    assert tide.following.level == pytest.approx(2.0, abs=0.01)


async def test_the_surge_is_measured_at_the_moment_the_reading_was_taken(pool):
    """Against the prediction for now instead, a reading that is minutes old on
    a moving tide invents a surge out of nothing but its own age. Near mid tide
    the water runs a foot an hour, and the card starts calling three tenths of
    a foot a surge worth printing."""
    now = datetime.now(UTC)
    rows = []
    # A tide running hard, a foot an hour, with the water doing exactly what
    # was predicted. Observations stop twelve minutes ago, which is ordinary:
    # NOAA is always somewhat behind.
    for step in range(-20, 40):
        when = now + timedelta(minutes=6 * step)
        level = 4.0 + 0.1 * step
        rows.append(tides.Reading(ts=when, observed=level if step <= -2 else None, predicted=level))
    await _readings(pool, rows)

    tide = await tide_domain.read(pool, timedelta(hours=3), timedelta(hours=4))

    assert tide is not None
    assert tide.now == pytest.approx(3.8, abs=0.01), "the last reading, twelve minutes old"
    assert tide.surge == pytest.approx(0.0, abs=0.01), "the water did what was predicted"
