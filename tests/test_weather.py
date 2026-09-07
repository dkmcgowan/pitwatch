"""Rain over the pit.

The parsing and the arithmetic, with no network. The one call that does reach
Open-Meteo is the poller's, and what it does with the answer is what is worth
testing: the shape of the response, the upsert, and the two questions the
dashboard asks of what was stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from pitwatch.domain import weather as reading
from pitwatch.ingest import weather as source

# -- the response ------------------------------------------------------------


def _body(times, rain=None, chance=None, temperature=None, codes=None):
    hourly = {"time": times}
    if rain is not None:
        hourly["precipitation"] = rain
    if chance is not None:
        hourly["precipitation_probability"] = chance
    if temperature is not None:
        hourly["temperature_2m"] = temperature
    if codes is not None:
        hourly["weather_code"] = codes
    return {"hourly": hourly}


def test_the_hourly_columns_are_pulled_apart_into_rows():
    """Open-Meteo answers in columns: one array of times and one per variable,
    lined up by position. Everything downstream wants rows."""
    hours = source._hours(
        _body(
            ["2026-09-07T00:00", "2026-09-07T01:00"],
            rain=[0.0, 1.4],
            chance=[0, 60],
            temperature=[18.2, 17.9],
            codes=[0, 61],
        )
    )

    assert [hour.precipitation for hour in hours] == [0.0, 1.4]
    assert [hour.probability for hour in hours] == [0, 60]
    assert [hour.code for hour in hours] == [0, 61]


def test_the_times_come_back_stamped_utc():
    """They are asked for in UTC and arrive without an offset on them. Reading
    them as naive would put every row in whatever zone the server happens to be
    in, which is the bug that only shows up on a machine that is not UTC."""
    hours = source._hours(_body(["2026-09-07T00:00"], rain=[0.0]))

    assert hours[0].ts == datetime(2026, 9, 7, 0, 0, tzinfo=UTC)


def test_a_column_that_did_not_come_back_is_not_an_error():
    """A variable the model had nothing for is missing rather than short, but
    this does not rely on that: an hour with no reading is None, and None is a
    thing the pages already draw."""
    hours = source._hours(_body(["2026-09-07T00:00"], rain=[0.6]))

    assert hours[0].precipitation == pytest.approx(0.6)
    assert hours[0].probability is None
    assert hours[0].temperature is None


def test_an_answer_with_no_series_in_it_says_so():
    with pytest.raises(source.WeatherError, match="hourly series"):
        source._hours({"error": True, "reason": "No data"})


def test_a_refusal_is_raised_with_open_meteos_own_words():
    """It answers a bad request with a reason, and the reason is usually the
    whole diagnosis."""
    message = source._refusal(400, '{"error": true, "reason": "Latitude must be in range"}')

    assert "Latitude must be in range" in message


def test_a_refusal_that_is_not_json_still_says_something():
    message = source._refusal(502, "<html>Bad Gateway</html>")

    assert "502" in message


# -- the coordinates ---------------------------------------------------------


# The coordinates throughout this file are the Empire State Building, on
# purpose. A worked example in a public repository should be somewhere anybody
# can point at rather than somewhere somebody lives.


def test_coordinates_are_rounded_before_they_go_anywhere():
    """Two places is about a kilometer, and the finest rainfall grid in use is
    three. So the rounding costs nothing that could be measured, and it stops
    the request pointing at a building."""
    assert source.rounded(40.748442) == 40.75
    assert source.rounded(-73.985659) == -73.99


class _Answer:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = "{}"

    def json(self):
        return self._payload


class _Client:
    """A stand in for the HTTP client that remembers what it was asked."""

    asked: ClassVar[dict] = {}
    answer: ClassVar[object] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def __init__(self, *args, **kwargs):
        pass

    async def get(self, url, params=None, headers=None):
        _Client.asked = {"url": url, "params": params, "headers": headers}
        return _Client.answer


@pytest.fixture
def called(monkeypatch):
    _Client.asked = {}
    monkeypatch.setattr(source.httpx2, "AsyncClient", _Client)
    return _Client


async def test_the_geocoder_is_told_who_is_calling(called):
    """Nominatim asks that anything calling it identify itself. A button on a
    settings page pressed a handful of times is squarely inside its policy; an
    anonymous one is not."""
    _Client.answer = _Answer([{"lat": "40.7484421", "lon": "-73.9856589", "display_name": "here"}])

    place = await source.geocode("350 5th Ave, New York")

    assert "PitWatch" in called.asked["headers"]["User-Agent"]
    # And what comes back is already rounded, so the rounded pair is what gets
    # stored and what the settings page shows.
    assert (place.latitude, place.longitude) == (40.75, -73.99)
    assert place.label == "here"


async def test_an_address_that_matches_nothing_says_what_to_do(called):
    _Client.answer = _Answer([])

    with pytest.raises(source.WeatherError, match="latitude and longitude"):
        await source.geocode("nowhere at all")


async def test_the_forecast_asks_in_utc_and_in_millimeters(called):
    """One storage unit and one conversion at the edge. A column whose meaning
    depends on a setting somebody can change later is a column that goes wrong
    the day they change it."""
    _Client.answer = _Answer(_body(["2026-09-07T00:00"], rain=[0.0]))

    await source.fetch(40.748442, -73.985659)

    params = called.asked["params"]
    assert params["timezone"] == "UTC"
    assert params["precipitation_unit"] == "mm"
    # Rounded here too, not only when they are saved. The stored pair is
    # already rounded; this is the belt to that pair of braces.
    assert (params["latitude"], params["longitude"]) == (40.75, -73.99)


# -- what got stored ---------------------------------------------------------


async def test_an_hour_is_rewritten_rather_than_written_twice(pool):
    """An hour arrives several times over its life: as a forecast three days
    out, then as a firmer one, then as the model's reconstruction of what
    actually fell. The last word wins."""
    when = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    await source.store(
        pool, [source.Hour(ts=when, precipitation=0.2, probability=40, temperature=18.0, code=61)]
    )
    await source.store(
        pool, [source.Hour(ts=when, precipitation=3.1, probability=100, temperature=17.0, code=63)]
    )

    rows = await pool.fetch("SELECT ts, precipitation FROM weather_hour")

    assert len(rows) == 1, "one hour, one row"
    assert rows[0]["precipitation"] == pytest.approx(3.1)


async def test_rain_from_before_any_window_is_dropped(pool):
    """Rain older than the longest window the history page offers is rain
    nothing can draw."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    old = now - timedelta(days=200)

    await source.store(
        pool,
        [source.Hour(ts=old, precipitation=9.9, probability=None, temperature=None, code=None)],
    )

    assert await pool.fetchval("SELECT count(*) FROM weather_hour") == 0


async def _hours(pool, entries):
    """entries is (hours from now, mm, code)."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    await source.store(
        pool,
        [
            source.Hour(
                ts=now + timedelta(hours=offset),
                precipitation=mm,
                probability=100 if mm else 0,
                temperature=12.0,
                code=code,
            )
            for offset, mm, code in entries
        ],
    )
    return now


async def test_what_fell_and_what_is_coming_are_counted_apart(pool):
    """The two numbers on the card are a measurement and a promise, and adding
    them together would be neither."""
    await _hours(pool, [(-3, 2.0, 61), (-1, 1.0, 61), (0, 0.0, 0), (2, 4.0, 63), (5, 1.0, 63)])

    rain = await reading.read(pool, timedelta(hours=24), timedelta(hours=24))

    assert rain.last_24h == pytest.approx(3.0)
    assert rain.next_24h == pytest.approx(5.0)


async def test_the_hour_in_progress_decides_whether_it_is_raining(pool):
    await _hours(pool, [(0, 1.6, 63)])

    rain = await reading.read(pool, timedelta(hours=24), timedelta(hours=24))

    assert rain.now is True
    assert rain.doing == "rain"


async def test_a_trace_of_a_millimeter_is_not_rain(pool):
    """Models produce long tails of hundredths that are not weather. A card
    that says "raining" because of two hundredths is a card nobody trusts
    twice."""
    await _hours(pool, [(0, 0.02, 61)])

    rain = await reading.read(pool, timedelta(hours=24), timedelta(hours=24))

    assert rain.now is False


async def test_snow_is_marked_as_not_being_in_the_pit_yet(pool):
    """It reaches the pit when it melts, which can be days. Saying "raining" on
    a night it snowed would be the one wrong word on the card."""
    await _hours(pool, [(0, 2.0, 73)])

    rain = await reading.read(pool, timedelta(hours=24), timedelta(hours=24))

    assert rain.frozen is True
    assert rain.doing == "snow"


async def test_nothing_stored_is_a_different_answer_from_no_rain(pool):
    """One means the pit is dry and the other means nobody has looked, and the
    card draws them differently."""
    assert await reading.read(pool, timedelta(hours=24), timedelta(hours=24)) is None


async def test_the_history_series_leaves_the_forecast_out(pool):
    """A forecast drawn on a page of facts is a promise that looks like a
    measurement, and once they are both bars there is no telling them apart."""
    await _hours(pool, [(-5, 3.0, 61), (5, 9.0, 63)])

    series = await reading.rain_series(pool, timedelta(days=1), timedelta(days=1), "UTC")

    assert sum(rain for _, rain in series) == pytest.approx(3.0)


# -- the reading ------------------------------------------------------------


def test_millimeters_are_turned_into_whatever_the_site_reads():
    assert reading.as_read(25.4, "in") == pytest.approx(1.0)
    assert reading.as_read(25.4, "mm") == pytest.approx(25.4)
    assert reading.as_read(None, "in") is None


def test_a_rainfall_figure_carries_its_unit():
    assert reading.spoken(25.4, "in") == '1.00"'
    assert reading.spoken(25.4, "mm") == "25.4 mm"
    assert reading.spoken(None, "in") == "--"
