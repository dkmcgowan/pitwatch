"""What the stored water table is asked for.

Two questions, and the card between them asks nothing else:

1. **Where is the water table, and when was that true?** Not "now": the series
   arrives about a month late, so the honest answer names its own date. A card
   that printed it without one would be claiming something it cannot know.
2. **Is that unusual?** The only question worth asking of a number like this.
   A water table at minus a third of a foot means nothing alone; against the
   same fortnight last year, and against the range this well has ever shown, it
   means quite a lot.

Feet against the well's own datum, stored exactly as the USGS publishes them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

log = logging.getLogger(__name__)

# How far either side of the same day last year to average for the comparison.
#
# A fortnight, because a water table wanders day to day on rain and tide and a
# single day a year ago is a coin flip. Two weeks either side smooths that
# without reaching into a different season.
SEASON_WINDOW = timedelta(days=14)


@dataclass(frozen=True)
class Groundwater:
    """The card's answer, in feet against the well's datum."""

    # The newest reading, and the day it belongs to. The date is not decoration:
    # this series runs weeks behind and the card has to say so.
    level: float | None
    at: datetime | None
    # The same fortnight a year earlier, and the difference. This is the whole
    # point of the series.
    a_year_ago: float | None
    # How the record has ever read, so a reader can place the current number.
    lowest: float | None
    highest: float | None
    readings: int
    # Day and level, oldest first, for the strip on the card.
    days: list[tuple[datetime, float]]

    @property
    def change(self) -> float | None:
        """Higher or lower than the same fortnight last year, in feet."""
        if self.level is None or self.a_year_ago is None:
            return None
        return round(self.level - self.a_year_ago, 2)

    def as_json(self) -> dict:
        return {
            "level": self.level,
            "at": self.at.isoformat() if self.at else None,
            "a_year_ago": self.a_year_ago,
            "change": self.change,
            "lowest": self.lowest,
            "highest": self.highest,
            "readings": self.readings,
            "days": [[when.isoformat(), level] for when, level in self.days],
        }


LATEST = """
SELECT ts, level FROM groundwater_reading ORDER BY ts DESC LIMIT 1
"""

RANGE = """
SELECT min(level) AS lowest, max(level) AS highest, count(*) AS readings
FROM groundwater_reading
"""

# The same fortnight a year before whatever the newest reading is, rather than
# a year before today. Comparing August against a September that has not been
# published yet would read a seasonal decline as a change in the ground.
A_YEAR_AGO = """
SELECT avg(level) AS level
FROM groundwater_reading
WHERE ts BETWEEN ($1::timestamptz - interval '1 year' - $2::interval)
              AND ($1::timestamptz - interval '1 year' + $2::interval)
"""

STRIP = """
SELECT ts, level FROM groundwater_reading
WHERE ts > now() - $1::interval ORDER BY ts
"""


async def read(pool: asyncpg.Pool, back: timedelta) -> Groundwater | None:
    """The card's slice of the well.

    None when nothing is stored at all, which is a different answer from a dry
    water table and has to be drawn differently.
    """
    try:
        latest = await pool.fetchrow(LATEST)
        if latest is None:
            return None
        span = await pool.fetchrow(RANGE)
        year = await pool.fetchval(A_YEAR_AGO, latest["ts"], SEASON_WINDOW)
        days = await pool.fetch(STRIP, back)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the groundwater: %s", error)
        return None

    return Groundwater(
        level=round(float(latest["level"]), 2),
        at=latest["ts"],
        a_year_ago=round(float(year), 2) if year is not None else None,
        lowest=round(float(span["lowest"]), 2) if span and span["lowest"] is not None else None,
        highest=round(float(span["highest"]), 2) if span and span["highest"] is not None else None,
        readings=int(span["readings"]) if span else 0,
        days=[(row["ts"], round(float(row["level"]), 2)) for row in days],
    )


def stale_days(water: Groundwater | None, now: datetime | None = None) -> int | None:
    """How far behind the newest reading is, in whole days.

    Exists so the card and the summary agree about what "late" means rather
    than each working it out. Around thirty is normal for this series and is
    not a fault.
    """
    if water is None or water.at is None:
        return None
    return max(0, ((now or datetime.now(UTC)) - water.at).days)


__all__ = ["SEASON_WINDOW", "Groundwater", "read", "stale_days"]
