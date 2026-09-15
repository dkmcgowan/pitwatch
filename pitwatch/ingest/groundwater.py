"""The water table under the pit.

The tide reader next door is a proxy for this one. Near tidal water the tide
moves the water table, which is why it correlates at all, but what comes
through a failed seal is the groundwater itself, and the tide is only one of
the things that lifts it. Rain lifts it, the season lifts it, and so does
whatever is being done to the ground next door.

**USGS, and no key.** The same door as NOAA and Open-Meteo: an application name
as a courtesy and nothing else. About six hundred monitoring wells across the
five boroughs, published through the National Water Information System, with
records going back two decades on some of them.

**It is slow and it is late, and that is the point.** Daily values, published
around a month behind. Useless for a lamp. Exactly right for the question the
tide could not answer, which is not what the water is doing this minute but
whether this year is unusual: the well two hundred meters from the reference
installation read a quarter of a foot higher in August 2026 than in August
2025, measured by somebody else on equipment nobody here can misconfigure.

Nothing downstream pretends the newest row is current. It carries its date and
the card prints it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx2

from pitwatch.schemas import GroundwaterSettings

log = logging.getLogger(__name__)

DAILY_URL = "https://waterservices.usgs.gov/nwis/dv/"
SITE_URL = "https://waterservices.usgs.gov/nwis/site/"

TIMEOUT_S = 30

# The well search gets its own, longer. Asking for the daily values of one
# named well is a small question and USGS answers it in a second; asking which
# wells exist inside a bounding box makes it search, and it routinely takes
# half a minute. Timing that out turns a slow answer into "no wells near here",
# which is the wrong thing to tell somebody who has one across the street.
SEARCH_TIMEOUT_S = 90

# Water level above a vertical datum, in feet. 62611 is NAVD88 and 62610 is the
# older NGVD29; wells carry one or the other and a few carry both, so both are
# asked for and whichever answers is used.
#
# Not 72019, which is depth below the land surface and therefore upside down:
# it grows as the water table falls. Storing a number whose sign is backwards
# from every other series here is how somebody later reads a drought as a
# flood.
PARAMETERS = "62611,62610"

# How far back to ask on every poll.
#
# Wide enough that a well which has been quiet for weeks fills in the moment it
# catches up, and wide enough to survive this having been switched off over a
# holiday. The rows are one a day, so asking for two months is a few kilobytes.
BACK = timedelta(days=60)

# How often to ask. The series moves once a day and arrives a month late, so
# this could be daily and is hourly only because an hour is a small number to
# reason about and the request is tiny.
EVERY_S = 3600

# Older than anything worth drawing. Two decades is what the wells hold, and a
# water table is the one series here where the old readings are the valuable
# ones: this year means nothing except against other years.
KEEP = timedelta(days=365 * 25)


class GroundwaterError(Exception):
    """Something the person configuring it can act on."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One day's water level, in feet against the well's own datum."""

    ts: datetime
    level: float
    site_no: str


@dataclass(frozen=True, slots=True)
class Well:
    """A monitoring well, as the settings page offers it."""

    site_no: str
    name: str
    latitude: float
    longitude: float
    miles: float


def _rows(body: str) -> list[list[str]]:
    """The data lines out of an RDB table.

    USGS answers in its own tab separated format: comment lines behind a hash,
    then a header, then a line of column widths that is not data and has caught
    every parser written against this in a hurry.
    """
    out: list[list[str]] = []
    header_seen = False
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if not header_seen:
            header_seen = True
            out.append(parts)
            continue
        if parts and parts[0].endswith("s") and parts[0][:-1].isdigit():
            continue  # the widths line
        out.append(parts)
    return out


async def _ask(client, url: str, params: dict) -> str:
    try:
        response = await client.get(url, params=params)
    except httpx2.HTTPError as error:
        raise GroundwaterError(f"Could not reach the USGS: {error}") from error
    if response.status_code == 404:
        # What the USGS answers when a query matches no sites at all, which is
        # a real answer rather than a failure: an empty series.
        return ""
    if response.status_code >= 400:
        raise GroundwaterError(f"The USGS refused it with HTTP {response.status_code}")
    return response.text


async def fetch(site_no: str, now: datetime | None = None) -> list[Reading]:
    """Daily water levels for one well, oldest first."""
    if not site_no:
        raise GroundwaterError("No well chosen")
    start = ((now or datetime.now(UTC)) - BACK).strftime("%Y-%m-%d")

    async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
        body = await _ask(
            client,
            DAILY_URL,
            {
                "format": "rdb",
                "sites": site_no,
                "startDT": start,
                "parameterCd": PARAMETERS,
            },
        )

    rows = _rows(body)
    if len(rows) < 2:
        return []
    header = rows[0]
    try:
        when_at = header.index("datetime")
    except ValueError as error:
        raise GroundwaterError("The USGS answered without a datetime column") from error

    # The value column's name carries the series and parameter, so it cannot be
    # named in advance. It is the first column after datetime that is not a
    # qualifier, and qualifier columns are the ones ending in _cd.
    value_at = next(
        (index for index in range(when_at + 1, len(header)) if not header[index].endswith("_cd")),
        None,
    )
    if value_at is None:
        return []

    readings: list[Reading] = []
    for parts in rows[1:]:
        if len(parts) <= max(when_at, value_at):
            continue
        try:
            when = datetime.strptime(parts[when_at], "%Y-%m-%d").replace(tzinfo=UTC)
            level = float(parts[value_at])
        except ValueError:
            continue  # a day the well was not read, which is ordinary
        readings.append(Reading(ts=when, level=level, site_no=site_no))
    readings.sort(key=lambda row: row.ts)
    return readings


def _miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle, the same as the tide reader's."""
    from math import asin, cos, radians, sin, sqrt

    radius = 3958.7613
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * radius * asin(sqrt(a))


async def nearest(latitude: float, longitude: float) -> Well:
    """The closest monitoring well that has published anything lately.

    Searched in a box rather than by county, because a county line is not a
    hydrological boundary and the nearest well to a building on the edge of one
    is often on the other side of it. A fifth of a degree is roughly fourteen
    miles, which is already further than a water table has any business being
    compared across, and asking for more makes the search slow enough to time
    out.
    """
    span = 0.2
    box = (
        f"{longitude - span:.4f},{latitude - span:.4f},{longitude + span:.4f},{latitude + span:.4f}"
    )
    async with httpx2.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
        body = await _ask(
            client,
            SITE_URL,
            {
                "format": "rdb",
                "bBox": box,
                "siteType": "GW",
                "siteStatus": "all",
                "hasDataTypeCd": "dv",
            },
        )

    rows = _rows(body)
    if len(rows) < 2:
        raise GroundwaterError("The USGS listed no monitoring wells near here")
    header = rows[0]
    try:
        at = {
            name: header.index(name)
            for name in ("site_no", "station_nm", "dec_lat_va", "dec_long_va")
        }
    except ValueError as error:
        raise GroundwaterError(
            "The USGS answered the well list in a shape we cannot read"
        ) from error

    found: Well | None = None
    for parts in rows[1:]:
        if len(parts) <= max(at.values()):
            continue
        try:
            lat = float(parts[at["dec_lat_va"]])
            lon = float(parts[at["dec_long_va"]])
        except ValueError:
            continue
        miles = _miles(latitude, longitude, lat, lon)
        if found is None or miles < found.miles:
            found = Well(
                site_no=parts[at["site_no"]].strip(),
                name=parts[at["station_nm"]].strip(),
                latitude=lat,
                longitude=lon,
                miles=miles,
            )
    if found is None or not found.site_no:
        raise GroundwaterError("The USGS listed no monitoring wells near here")
    return found


UPSERT = """
INSERT INTO groundwater_reading (ts, level, site_no, fetched_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (ts) DO UPDATE SET
    level = excluded.level,
    site_no = excluded.site_no,
    fetched_at = now()
"""


async def store(pool: asyncpg.Pool, readings: list[Reading]) -> int:
    if not readings:
        return 0
    await pool.executemany(UPSERT, [(row.ts, row.level, row.site_no) for row in readings])
    await pool.execute("DELETE FROM groundwater_reading WHERE ts < now() - $1::interval", KEEP)
    return len(readings)


class GroundwaterReader:
    """Asks the USGS for the water table under the pit, on a timer.

    The same shape as the tide and weather pollers: it runs until its stop
    event is set, it reports whether it is reaching anything, and a bad
    afternoon at the USGS costs a cycle rather than the task.
    """

    def __init__(self, settings: GroundwaterSettings, pool: asyncpg.Pool, on_status=None) -> None:
        self._settings = settings
        self._pool = pool
        self._on_status = on_status

    async def run(self, stop: asyncio.Event) -> None:
        settings = self._settings
        if not settings.enabled:
            log.info("Groundwater is off")
            await self._report(False, "Turned off")
            return
        if not settings.site_no:
            log.info("Groundwater has nowhere to look: no well chosen")
            await self._report(False, "No well chosen")
            return

        log.info("Groundwater from well %s", settings.site_name or settings.site_no)
        while not stop.is_set():
            try:
                written = await store(self._pool, await fetch(settings.site_no))
                log.debug("Wrote %d groundwater reading(s)", written)
                await self._report(True, None)
            except GroundwaterError as error:
                log.warning("Could not read the groundwater: %s", error)
                await self._report(False, str(error))
            except (asyncpg.PostgresError, OSError) as error:
                log.warning("Could not store the groundwater: %s", error)
                await self._report(False, str(error))

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=EVERY_S)

    async def _report(self, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(online, error)


__all__ = [
    "BACK",
    "EVERY_S",
    "GroundwaterError",
    "GroundwaterReader",
    "Reading",
    "Well",
    "fetch",
    "nearest",
    "store",
]
