"""Rain over the pit.

An ejector pit answers to one thing above all others, and it is not a schedule.
It is water arriving. Groundwater rises after rain and keeps rising for a day
or two after it stops; a blocked area drain or a downspout that has come apart
puts a roof's worth into the pit inside an hour. The history page can already
say the pit called for water sixty times on Tuesday. Until now nothing on the
page could say whether it rained on Tuesday, which is the first thing anybody
asks and the difference between "something is wrong" and "it rained".

**Open-Meteo, and no key.** For the United States it serves NOAA's own HRRR and
GFS output, so this is NOAA data through a door that does not need an account.
That matters more here than it looks: every other outside service in this
application costs a password field, a rule about never returning it to the
browser, a way to clear it, and tests that all three hold. This costs a
latitude and a longitude.

**What it is not.** These are model numbers, not a rain gauge on the roof. Past
hours are the model's best reconstruction of what fell, which for a
thunderstorm cell that sat over one block can be wrong by a lot. It is right
often enough to answer "was it raining", and that is the question being asked
of it. Nothing here should ever be worded as a measurement.

**The address goes out once.** Geocoding is the only call that sees it, it
happens when somebody presses the button on the settings page, and what is kept
afterwards is a rounded pair of coordinates. The quarter hourly weather calls
send those. See ``SiteSettings.latitude``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx2

from pitwatch.schemas import SiteSettings, WeatherSettings

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"

TIMEOUT_S = 20

# Nominatim asks that anything calling it identify itself and stay under a call
# a second. A button on a settings page pressed a handful of times is squarely
# inside that; the header is how it stays polite rather than anonymous.
USER_AGENT = "PitWatch pump monitor (https://github.com/dkmcgowan/pitwatch)"

# How much either side of now to keep asking for. Two days back covers the
# "why was last night so busy" question without refetching a month every
# quarter hour, and the far past is already stored. Three days forward is as
# far as an hourly precipitation forecast is worth printing.
PAST_DAYS = 2
FORECAST_DAYS = 3

# How often to ask. The model behind it updates hourly at best, so this is
# already more often than the answer changes; it is this rather than hourly so
# that a container started five minutes ago has something to draw.
EVERY_S = 900

# How far the coordinates are rounded before they are sent. Two places is about
# a kilometer, and the finest grid in play is HRRR at three, so this throws
# away nothing that could be measured.
PLACES = 2

MM_PER_INCH = 25.4


class WeatherError(Exception):
    """Something the person configuring it can act on."""


@dataclass(frozen=True)
class Place:
    """What the geocoder made of an address."""

    latitude: float
    longitude: float
    # The geocoder's own words for what it matched, printed back on the
    # settings page. A lookup that found the right street in the wrong state is
    # the failure that matters and the only way to catch it is to show it.
    label: str


@dataclass(frozen=True)
class Hour:
    """One hour of rain over the pit."""

    ts: datetime
    precipitation: float | None
    probability: int | None
    temperature: float | None
    code: int | None


def rounded(value: float) -> float:
    return round(value, PLACES)


def as_inches(mm: float) -> float:
    return mm / MM_PER_INCH


async def geocode(address: str) -> Place:
    """Turn a street address into a coordinate pair.

    OpenStreetMap's Nominatim, which needs no key and handles a street address
    rather than only a place name. It is asked once, by hand, from the settings
    page.
    """
    wanted = address.strip()
    if not wanted:
        raise WeatherError("Type an address to look up")

    params = {"q": wanted, "format": "jsonv2", "limit": 1, "addressdetails": 0}
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.get(
                GEOCODE_URL, params=params, headers={"User-Agent": USER_AGENT}
            )
    except httpx2.HTTPError as error:
        raise WeatherError(f"Could not reach the geocoder: {error}") from error

    if response.status_code >= 400:
        raise WeatherError(f"The geocoder refused it with HTTP {response.status_code}")

    try:
        found = response.json()
    except ValueError as error:
        raise WeatherError("The geocoder answered something that was not JSON") from error

    if not isinstance(found, list) or not found:
        raise WeatherError(
            f"Nothing found for {wanted!r}. Try a fuller address, or put the "
            f"latitude and longitude in directly."
        )

    first = found[0]
    try:
        latitude = float(first["lat"])
        longitude = float(first["lon"])
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherError("The geocoder answered without coordinates") from error

    return Place(
        latitude=rounded(latitude),
        longitude=rounded(longitude),
        label=str(first.get("display_name") or wanted),
    )


async def fetch(latitude: float, longitude: float) -> list[Hour]:
    """Hourly rain either side of now, oldest first.

    Asked for in UTC rather than in the site's zone. The rows mean the same
    thing afterwards whoever reads them, nothing here has to reason about a
    clock going back an hour, and the conversion to local time belongs on the
    page that draws a local axis.
    """
    params = {
        "latitude": rounded(latitude),
        "longitude": rounded(longitude),
        "hourly": "precipitation,precipitation_probability,temperature_2m,weather_code",
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
        "timezone": "UTC",
        "precipitation_unit": "mm",
    }
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.get(FORECAST_URL, params=params)
    except httpx2.HTTPError as error:
        raise WeatherError(f"Could not reach Open-Meteo: {error}") from error

    if response.status_code >= 400:
        raise WeatherError(_refusal(response.status_code, response.text))

    try:
        body = response.json()
    except ValueError as error:
        raise WeatherError("Open-Meteo answered something that was not JSON") from error

    return _hours(body)


def _refusal(status: int, body: str) -> str:
    """Open-Meteo's own words where it gives them.

    It answers a bad request with {"error": true, "reason": "..."} and the
    reason is usually the whole diagnosis.
    """
    try:
        problem = json.loads(body)
    except ValueError:
        problem = None
    if isinstance(problem, dict) and problem.get("reason"):
        return f"Open-Meteo refused it: {problem['reason']}"
    return f"Open-Meteo refused it with HTTP {status}: {body[:200]}"


def _hours(body: object) -> list[Hour]:
    """Pull the hourly arrays apart into rows.

    Open-Meteo answers in columns: one array of times and one array per
    variable, lined up by position. Anything missing comes back as null rather
    than as a shorter array, but this does not rely on that.
    """
    if not isinstance(body, dict):
        raise WeatherError("Open-Meteo answered something that was not an object")
    hourly = body.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise WeatherError("Open-Meteo answered without an hourly series")

    times = hourly["time"]

    def column(name: str) -> list:
        values = hourly.get(name)
        return values if isinstance(values, list) else []

    rain = column("precipitation")
    chance = column("precipitation_probability")
    temperature = column("temperature_2m")
    codes = column("weather_code")

    def at(values: list, index: int):
        return values[index] if index < len(values) else None

    hours: list[Hour] = []
    for index, stamp in enumerate(times):
        try:
            when = datetime.fromisoformat(str(stamp)).replace(tzinfo=UTC)
        except ValueError:
            continue
        hours.append(
            Hour(
                ts=when,
                precipitation=_number(at(rain, index)),
                probability=_whole(at(chance, index)),
                temperature=_number(at(temperature, index)),
                code=_whole(at(codes, index)),
            )
        )
    return hours


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _whole(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


UPSERT = """
INSERT INTO weather_hour (ts, precipitation, probability, temperature, code, fetched_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (ts) DO UPDATE SET
    precipitation = excluded.precipitation,
    probability   = excluded.probability,
    temperature   = excluded.temperature,
    code          = excluded.code,
    fetched_at    = now()
"""

# Rain from before the oldest window the history page offers is rain nothing
# can draw. Kept a little past thirty days so the edge of that window is not
# the edge of the data.
KEEP = timedelta(days=40)


async def store(pool: asyncpg.Pool, hours: list[Hour]) -> int:
    """Write the hours, overwriting whatever was there for the same hour.

    An hour arrives several times over its life: first as a forecast three days
    out, then as a firmer one, then as the model's reconstruction of what
    actually fell. The last word wins, which is why this is an upsert rather
    than an append and why nothing stamps a row as a forecast.
    """
    if not hours:
        return 0
    await pool.executemany(
        UPSERT,
        [
            (hour.ts, hour.precipitation, hour.probability, hour.temperature, hour.code)
            for hour in hours
        ],
    )
    await pool.execute("DELETE FROM weather_hour WHERE ts < now() - $1::interval", KEEP)
    return len(hours)


class WeatherReader:
    """Asks Open-Meteo for the rain over the pit, on a timer.

    Shaped like the other readers: it runs until its stop event is set, it
    reports whether it is talking to anything, and a failure costs a cycle
    rather than the task. A monitor that stops watching the pump because a
    weather API had a bad afternoon is a worse monitor than one that shows
    yesterday's rain.
    """

    def __init__(
        self,
        site: SiteSettings,
        settings: WeatherSettings,
        pool: asyncpg.Pool,
        on_status=None,
    ) -> None:
        self._site = site
        self._settings = settings
        self._pool = pool
        self._on_status = on_status

    async def run(self, stop: asyncio.Event) -> None:
        if not self._settings.enabled:
            log.info("Weather is off")
            await self._report(False, "Turned off")
            return
        if not self._site.has_coordinates:
            log.info("Weather has nowhere to look: no coordinates for the site")
            await self._report(False, "No location set")
            return

        latitude = self._site.latitude
        longitude = self._site.longitude
        log.info("Weather reading for %.2f, %.2f", latitude, longitude)

        while not stop.is_set():
            try:
                hours = await fetch(latitude, longitude)
                written = await store(self._pool, hours)
                log.debug("Wrote %d hour(s) of weather", written)
                await self._report(True, None)
            except WeatherError as error:
                log.warning("Could not read the weather: %s", error)
                await self._report(False, str(error))
            except (asyncpg.PostgresError, OSError) as error:
                log.warning("Could not store the weather: %s", error)
                await self._report(False, str(error))

            # Woken by the stop event rather than sleeping through it, so a
            # settings change applies now instead of up to a quarter of an hour
            # from now.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=EVERY_S)

    async def _report(self, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(online, error)


__all__ = [
    "Hour",
    "Place",
    "WeatherError",
    "WeatherReader",
    "as_inches",
    "fetch",
    "geocode",
    "rounded",
    "store",
]
