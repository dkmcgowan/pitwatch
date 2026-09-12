"""The tide over the pit.

The rain says what fell on the building. This says what the water table under
it is doing, which on a pit near tidal water turns out to matter more.

**How that was learned.** On the reference installation the pit's call rate went
up sevenfold at 19:18 one Wednesday evening and stayed up. Nothing inside the
building explained it: the pumps were healthy to a tenth of an amp, the panel
was new, the boiler room was dry, and the water meter accounted for a quarter of
what the pumps were moving. The answer was that high water at the Battery that
evening was 5.80 ft, the highest in nine days, at 19:43. Twenty five minutes
after the pit changed. Spring tides had lifted the water table past the level of
some defect, and it has tracked high and low water since.

That is a free number, published by NOAA, and until now nothing put it beside
the pump record.

**NOAA CO-OPS, and no key.** Same door as Open-Meteo: an application name as a
courtesy and nothing else. Three hundred stations, six minute resolution,
harmonic predictions on both sides of now and observed water level behind it.

**Both halves are kept.** The prediction is what the moon says; the observation
is what the water did. The difference between them is the storm surge, and a
foot of water the moon did not put there is the thing that floods a cellar.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx2

from pitwatch.schemas import TideSettings

log = logging.getLogger(__name__)

DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"

TIMEOUT_S = 25

# NOAA asks callers to identify themselves rather than arrive anonymously. It is
# not a key and there is nothing to register; it is the same courtesy the
# geocoder is shown.
APPLICATION = "PitWatch"

# The datum every tide table in the country is published against.
DATUM = "MLLW"

# How much either side of now to ask for. Two days back answers "what was the
# water doing when the pit got busy last night", two days forward is as far as a
# prediction is worth drawing on a card.
PAST_DAYS = 2
AHEAD_DAYS = 2

# How often to ask. The observed series updates every six minutes and the
# prediction never changes, so this is already more often than the answer moves.
# It matches the weather poller so the two stay legible together.
EVERY_S = 900

# Older than the longest window anything draws, plus a margin so the edge of the
# window is not the edge of the data.
KEEP = timedelta(days=40)

METERS_PER_FOOT = 0.3048


class TideError(Exception):
    """Something the person configuring it can act on."""


@dataclass(frozen=True)
class Station:
    """A NOAA gauge, and how far it is from the pit."""

    id: str
    name: str
    state: str
    latitude: float
    longitude: float
    miles: float

    @property
    def where(self) -> str:
        return f"{self.name}, {self.state}" if self.state else self.name


@dataclass(frozen=True)
class Reading:
    """The water at one moment: what was predicted, and what happened."""

    ts: datetime
    observed: float | None
    predicted: float | None


def as_read(feet: float | None, units: str) -> float | None:
    """Feet into whatever the site chose to read."""
    if feet is None:
        return None
    return round(feet, 2) if units == "ft" else round(feet * METERS_PER_FOOT, 2)


def spoken(feet: float | None, units: str) -> str:
    """A water level with its unit on it, for a page to print."""
    value = as_read(feet, units)
    if value is None:
        return "--"
    return f"{value:.1f} ft" if units == "ft" else f"{value:.2f} m"


def _miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle distance, which is plenty for picking the nearest gauge."""
    radius = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


async def nearest(latitude: float, longitude: float) -> Station:
    """The closest water level gauge to the pit.

    Pressed by hand from the settings page, the same as the geocoder. Three
    hundred stations arrive in one call and the nearest is arithmetic; making
    somebody find a station id on a government website is the kind of setup step
    that stops a feature being used.
    """
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.get(
                STATIONS_URL, params={"type": "waterlevels", "units": "english"}
            )
    except httpx2.HTTPError as error:
        raise TideError(f"Could not reach NOAA: {error}") from error

    if response.status_code >= 400:
        raise TideError(f"NOAA refused the station list with HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as error:
        raise TideError(
            "NOAA answered the station list with something that was not JSON"
        ) from error

    found: Station | None = None
    for row in body.get("stations", []) if isinstance(body, dict) else []:
        try:
            miles = _miles(latitude, longitude, float(row["lat"]), float(row["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        if found is None or miles < found.miles:
            found = Station(
                id=str(row.get("id", "")),
                name=str(row.get("name", "")),
                state=str(row.get("state", "")),
                latitude=float(row["lat"]),
                longitude=float(row["lng"]),
                miles=miles,
            )

    if found is None or not found.id:
        raise TideError("NOAA listed no water level stations")
    return found


def _window(now: datetime) -> tuple[str, str]:
    """The range to ask for, in the compact form NOAA wants."""
    start = (now - timedelta(days=PAST_DAYS)).strftime("%Y%m%d")
    end = (now + timedelta(days=AHEAD_DAYS)).strftime("%Y%m%d")
    return start, end


async def _ask(client, station: str, product: str, begin: str, end: str) -> list[dict]:
    params = {
        "product": product,
        "application": APPLICATION,
        "station": station,
        "begin_date": begin,
        "end_date": end,
        "datum": DATUM,
        "time_zone": "gmt",
        "units": "english",
        "format": "json",
    }
    try:
        response = await client.get(DATA_URL, params=params)
    except httpx2.HTTPError as error:
        raise TideError(f"Could not reach NOAA: {error}") from error

    if response.status_code >= 400:
        raise TideError(f"NOAA refused {product} with HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as error:
        raise TideError(f"NOAA answered {product} with something that was not JSON") from error

    if not isinstance(body, dict):
        raise TideError(f"NOAA answered {product} with something that was not an object")
    # Its own words where it gives them: a bad station id comes back as
    # {"error": {"message": "..."}} with HTTP 200, which is worth reading out
    # rather than reporting as an empty series.
    problem = body.get("error")
    if isinstance(problem, dict) and problem.get("message"):
        raise TideError(f"NOAA refused it: {str(problem['message']).strip()}")

    rows = body.get("predictions") if product == "predictions" else body.get("data")
    return rows if isinstance(rows, list) else []


def _when(stamp: object) -> datetime | None:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def _level(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


async def fetch(station: str, now: datetime | None = None) -> list[Reading]:
    """The water either side of now, oldest first.

    Two calls, merged on the timestamp: the harmonic prediction, which exists on
    both sides of now, and the observed level, which only exists behind it.
    """
    if not station:
        raise TideError("No station chosen")
    begin, end = _window(now or datetime.now(UTC))

    async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
        predicted = await _ask(client, station, "predictions", begin, end)
        observed = await _ask(client, station, "water_level", begin, end)

    merged: dict[datetime, list[float | None]] = {}
    for row in predicted:
        when = _when(row.get("t"))
        if when is not None:
            merged.setdefault(when, [None, None])[1] = _level(row.get("v"))
    for row in observed:
        when = _when(row.get("t"))
        if when is not None:
            merged.setdefault(when, [None, None])[0] = _level(row.get("v"))

    return [
        Reading(ts=when, observed=pair[0], predicted=pair[1])
        for when, pair in sorted(merged.items())
    ]


UPSERT = """
INSERT INTO tide_reading (ts, observed, predicted, fetched_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (ts) DO UPDATE SET
    -- An observation never becomes unknown again. A later fetch whose window
    -- has rolled past this moment carries a prediction and no observation, and
    -- overwriting a real reading with a null would quietly erase the record of
    -- what the water actually did.
    observed   = coalesce(excluded.observed, tide_reading.observed),
    predicted  = coalesce(excluded.predicted, tide_reading.predicted),
    fetched_at = now()
"""


async def store(pool: asyncpg.Pool, readings: list[Reading]) -> int:
    if not readings:
        return 0
    await pool.executemany(UPSERT, [(row.ts, row.observed, row.predicted) for row in readings])
    await pool.execute("DELETE FROM tide_reading WHERE ts < now() - $1::interval", KEEP)
    return len(readings)


class TideReader:
    """Asks NOAA for the water over the pit, on a timer.

    The same shape as the weather poller: it runs until its stop event is set,
    it reports whether it is reaching anything, and a bad afternoon at NOAA
    costs a cycle rather than the task.
    """

    def __init__(self, settings: TideSettings, pool: asyncpg.Pool, on_status=None) -> None:
        self._settings = settings
        self._pool = pool
        self._on_status = on_status

    async def run(self, stop: asyncio.Event) -> None:
        settings = self._settings
        if not settings.enabled:
            log.info("Tides are off")
            await self._report(False, "Turned off")
            return
        if not settings.station:
            log.info("Tides have nowhere to look: no station chosen")
            await self._report(False, "No station chosen")
            return

        log.info("Tide reading from station %s", settings.station_name or settings.station)
        while not stop.is_set():
            try:
                written = await store(self._pool, await fetch(settings.station))
                log.debug("Wrote %d tide reading(s)", written)
                await self._report(True, None)
            except TideError as error:
                log.warning("Could not read the tide: %s", error)
                await self._report(False, str(error))
            except (asyncpg.PostgresError, OSError) as error:
                log.warning("Could not store the tide: %s", error)
                await self._report(False, str(error))

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=EVERY_S)

    async def _report(self, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(online, error)


__all__ = [
    "EVERY_S",
    "Reading",
    "Station",
    "TideError",
    "TideReader",
    "as_read",
    "fetch",
    "nearest",
    "spoken",
    "store",
]
