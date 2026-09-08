"""What leaves the building, and what it is worth once it gets there.

The numbers are built here and read by a model somewhere else, so the tests
that matter are about the shape of the page it is handed: that the rain is on
it, that the days line up, and that nothing on it points at the building.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pitwatch import summary
from pitwatch.schemas import (
    ClampSource,
    MqttSettings,
    SiteSettings,
    WeatherSettings,
)

# Two decimal places, about a kilometer, which is what the settings page keeps.
BROOKLYN = {"latitude": 40.68, "longitude": -73.99}


def _app(pool, store) -> SimpleNamespace:
    """What `facts` actually reaches for, and nothing else."""
    return SimpleNamespace(state=SimpleNamespace(pool=pool, settings=store, history=None))


async def _site(store, **overrides):
    await store.put(SiteSettings(timezone="America/New_York", **BROOKLYN, **overrides))


async def _rain(pool, when: datetime, mm: float) -> None:
    await pool.execute(
        """
        INSERT INTO weather_hour (ts, precipitation, fetched_at)
        VALUES ($1, $2, now())
        ON CONFLICT (ts) DO UPDATE SET precipitation = excluded.precipitation
        """,
        when,
        mm,
    )


async def test_the_rain_goes_with_the_numbers(pool, store):
    """The one thing in here from outside the building. Forty calls in a dry
    week and forty in two inches of rain are two different findings, and
    without this column the model cannot tell them apart."""
    await _site(store)
    await store.put(WeatherSettings(enabled=True, units="in"))
    yesterday = datetime.now(UTC) - timedelta(hours=20)
    await _rain(pool, yesterday.replace(minute=0, second=0, microsecond=0), 12.7)

    rain = await summary.rainfall(pool, store, summary.WINDOW, "America/New_York")

    assert rain is not None
    assert rain["units"] == "in"
    # Half an inch, converted at the edge the way every page does it.
    assert sum(day["rain"] for day in rain["days"]) == 0.5
    # Said out loud, because a model handed a rainfall column will otherwise
    # write about it as though somebody had read a gauge.
    assert "not a rain gauge" in rain["source"]


async def test_no_coordinates_is_not_a_dry_week(pool, store):
    """Nothing to ask about reads as nothing to say. Sending a column of zeroes
    would have the model explaining a drought that never happened."""
    await store.put(SiteSettings(timezone="America/New_York"))
    await store.put(WeatherSettings(enabled=True))

    assert await summary.rainfall(pool, store, summary.WINDOW, "America/New_York") is None


async def test_rain_turned_off_sends_none_of_it(pool, store):
    await _site(store)
    await store.put(WeatherSettings(enabled=False))
    await _rain(pool, datetime.now(UTC) - timedelta(hours=3), 5.0)

    assert await summary.rainfall(pool, store, summary.WINDOW, "America/New_York") is None


async def test_a_wet_day_and_a_busy_day_are_the_same_day(pool, store):
    """The whole point of sending rain, and it only works if the days agree.

    Cut on UTC midnight, a run at nine in the evening in New York lands on
    tomorrow and the rain that caused it lands on today, and the model is
    handed two columns that cannot be read across. Both are cut on the site's
    own midnight instead."""
    await _site(store)
    await store.put(WeatherSettings(enabled=True, units="mm"))
    await store.put(
        MqttSettings(
            enabled=True,
            host="broker",
            clamps=[ClampSource(pump=1, topic="meter/em1:0", path="current")],
        )
    )

    # Nine in the evening in New York, which is the small hours of the next day
    # in UTC. Yesterday, so it is inside the window whatever hour it is now.
    evening = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    await _rain(pool, evening, 8.0)
    await pool.execute(
        """
        INSERT INTO pump_run (pump, started_at, ended_at, duration_s, role, started_by)
        VALUES (1, $1::timestamptz, $1::timestamptz + interval '12 seconds', 12, 'lead', 'contact')
        """,
        evening,
    )

    numbers = await summary.facts(_app(pool, store))

    rain_days = {day["day"] for day in numbers["rain"]["days"] if day["rain"]}
    run_days = {day["day"] for pump in numbers["pumps"] for day in pump["days"] if day.get("runs")}
    assert rain_days and rain_days == run_days


async def test_the_page_the_model_gets_names_no_place(pool, store):
    """The address is the one thing the owner of this pit has been clear about,
    and the rain is the part most likely to carry it: it is asked for by
    coordinate. It leaves as a daily total with no coordinate on it."""
    await _site(store, name="14 Example Street", address="14 Example Street, Brooklyn NY")
    await store.put(WeatherSettings(enabled=True))
    await _rain(pool, datetime.now(UTC) - timedelta(hours=5), 3.0)

    numbers = await summary.facts(_app(pool, store))
    body = summary.messages(store.summary, numbers)[1]["content"]

    for leaked in ("Example Street", "Brooklyn", "40.68", "-73.99", "latitude"):
        assert leaked not in body, leaked
