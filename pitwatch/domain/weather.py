"""What the stored rain is asked for.

Three questions, and the pages between them ask nothing else:

1. **Is it raining now, and how much has fallen?** The dashboard's question.
   Somebody looking at a busy pit wants the last day of rain beside it.
2. **How much is coming?** Also the dashboard's. Two inches forecast overnight
   is a reason to look at the panel before going to bed.
3. **How much fell on each of the days the pit was busy?** The history page's,
   and the whole reason any of this exists.

Everything is stored and read in millimeters and Celsius. The conversion to
whatever the site chose to read happens once, at the edge, in `as_read`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from pitwatch.ingest.weather import MM_PER_INCH

log = logging.getLogger(__name__)

# WMO weather codes, grouped rather than enumerated. The full table separates
# "slight" from "moderate" drizzle and this page has no use for the difference:
# what matters is whether water is arriving and roughly how fast.
#
# Only the wet ones are named. A code that is not here is not weather this pit
# cares about, and printing "mainly clear" on a pump monitor is noise.
WET = {
    51: "drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow",
    80: "showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorms",
    96: "thunderstorms",
    99: "thunderstorms",
}

# Snow does not reach the pit until it melts, which can be days. Saying "0.4 in
# of rain" on a night it snowed would be wrong in the way that matters here.
FROZEN = {71, 73, 75, 77, 85, 86, 56, 57, 66, 67}

# Below this an hour is dry. Models produce long tails of hundredths of a
# millimeter that are not rain, and a dashboard that says "raining" because of
# 0.02 mm is a dashboard nobody trusts twice.
WET_ENOUGH_MM = 0.1


def as_read(mm: float | None, units: str) -> float | None:
    """Millimeters into whatever the site chose to read."""
    if mm is None:
        return None
    return round(mm, 1) if units == "mm" else round(mm / MM_PER_INCH, 2)


def spoken(mm: float | None, units: str) -> str:
    """A rainfall figure with its unit on it, for a page to print."""
    value = as_read(mm, units)
    if value is None:
        return "--"
    return f"{value:.1f} mm" if units == "mm" else f'{value:.2f}"'


def described(code: int | None) -> str:
    """What the sky is doing, in two words, or nothing when it is not raining."""
    return WET.get(code or -1, "")


@dataclass(frozen=True)
class Rain:
    """The dashboard's answer, in millimeters."""

    # Whether anything is falling in the hour now in progress.
    now: bool
    # What the sky is doing, when it is doing something.
    doing: str
    # Whether what is falling is frozen and therefore not in the pit yet.
    frozen: bool
    last_24h: float | None
    next_24h: float | None
    # The highest chance of rain in any of the next twenty four hours, which is
    # the honest way to say "is rain coming" without adding up probabilities
    # that do not add.
    chance: int | None
    # When the forecast was last refreshed. A forecast that stopped updating
    # eight hours ago is one to distrust, and the page can only say so if it
    # knows.
    fetched_at: datetime | None
    # Hour by hour either side of now, for the strip on the card. Oldest first.
    hours: list[tuple[datetime, float | None, bool]]

    def as_json(self, units: str) -> dict:
        return {
            "now": self.now,
            "doing": self.doing,
            "frozen": self.frozen,
            "units": units,
            "last_24h": as_read(self.last_24h, units),
            "next_24h": as_read(self.next_24h, units),
            "chance": self.chance,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "hours": [
                [when.isoformat(), as_read(mm, units), ahead] for when, mm, ahead in self.hours
            ],
        }


NOW = """
SELECT ts, precipitation, probability, temperature, code, fetched_at
FROM weather_hour
WHERE ts >= date_trunc('hour', now()) - $1::interval
  AND ts <  date_trunc('hour', now()) + $2::interval
ORDER BY ts
"""


async def read(pool: asyncpg.Pool, back: timedelta, ahead: timedelta) -> Rain | None:
    """The dashboard's slice of the stored hours.

    None when there is nothing stored at all, which is a different answer from
    "no rain" and has to be drawn differently: one means the pit is dry and the
    other means nobody has looked.
    """
    try:
        rows = await pool.fetch(NOW, back, ahead)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the weather: %s", error)
        return None
    if not rows:
        return None

    this_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    fell = 0.0
    coming = 0.0
    chance: int | None = None
    now_wet = False
    doing = ""
    frozen = False
    hours: list[tuple[datetime, float | None, bool]] = []
    fetched: datetime | None = None

    for row in rows:
        when = row["ts"]
        mm = row["precipitation"]
        forward = when >= this_hour
        hours.append((when, mm, forward))
        if fetched is None or (row["fetched_at"] and row["fetched_at"] > fetched):
            fetched = row["fetched_at"]

        if forward:
            coming += mm or 0.0
            probability = row["probability"]
            if probability is not None and (chance is None or probability > chance):
                chance = int(probability)
        else:
            fell += mm or 0.0

        if when == this_hour:
            now_wet = (mm or 0.0) >= WET_ENOUGH_MM
            doing = described(row["code"])
            frozen = row["code"] in FROZEN

    return Rain(
        now=now_wet,
        doing=doing,
        frozen=frozen,
        last_24h=round(fell, 2),
        next_24h=round(coming, 2),
        chance=chance,
        fetched_at=fetched,
        hours=hours,
    )


# Rain per bucket over a window, cut on local midnight the same way the calls
# are, so a bar of rain and a bar of calls describe the same day.
#
# Only the past. A forecast drawn on a history page is a promise on a page of
# facts, and the two would be indistinguishable once they were both bars.
SERIES = """
SELECT time_bucket($2::interval, ts, timezone => $3::text) AS bucket,
       sum(precipitation)                                  AS rain
FROM weather_hour
WHERE ts > now() - $1::interval AND ts <= now()
GROUP BY 1
ORDER BY 1
"""


async def rain_series(
    pool: asyncpg.Pool, span: timedelta, bucket: timedelta, zone: str
) -> list[tuple[datetime, float]]:
    """Rain per bucket, in millimeters, for the history page."""
    try:
        rows = await pool.fetch(SERIES, span, bucket, zone)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the rain history: %s", error)
        return []
    return [(row["bucket"], float(row["rain"] or 0.0)) for row in rows]


__all__ = [
    "Rain",
    "as_read",
    "described",
    "rain_series",
    "read",
    "spoken",
]
