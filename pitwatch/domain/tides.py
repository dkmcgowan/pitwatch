"""What the stored tide is asked for.

Three questions, and the pages between them ask nothing else:

1. **What is the water doing right now?** The dashboard's question. A pit near
   tidal water fills faster at high tide, so "3.9 ft and rising" is a forecast
   of the next hour's pumping.
2. **When is the next high, and how high?** Also the dashboard's. The turning
   points are found in the stored six minute series rather than fetched
   separately, so they agree with the curve drawn beside them.
3. **What was the water doing each day the pit was busy?** The history page's
   and the summary's, and the reason any of this is stored.

Everything is stored and read in feet above MLLW. The conversion to whatever
the site chose happens once, at the edge, in `ingest.tides.as_read`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from pitwatch.ingest.tides import as_read

log = logging.getLogger(__name__)

# How close to now a reading has to be to count as the level now. Two readings
# either side of six minutes; beyond that the gauge has stopped reporting and
# saying nothing is better than saying something twenty minutes old as if it
# were current.
NOW_WITHIN = timedelta(minutes=18)


@dataclass(frozen=True)
class Turn:
    """A high or a low, and when."""

    ts: datetime
    level: float
    high: bool


@dataclass(frozen=True)
class Tide:
    """The dashboard's answer, in feet above MLLW."""

    # What the gauge last said, and whether that is recent enough to print.
    now: float | None
    # Rising or falling, from the predicted curve either side of now, which is
    # smooth. Working it out from observations would have it flapping on chop.
    rising: bool | None
    # The next turning point, and the one after it.
    next_turn: Turn | None
    following: Turn | None
    # How far above or below the prediction the water is: the surge. A foot the
    # moon did not put there is the number worth seeing.
    surge: float | None
    fetched_at: datetime | None
    # Predicted, and observed where it exists, for the strip on the card.
    hours: list[tuple[datetime, float | None, float | None]]

    def as_json(self, units: str) -> dict:
        def turn(one: Turn | None) -> dict | None:
            if one is None:
                return None
            return {
                "at": one.ts.isoformat(),
                "level": as_read(one.level, units),
                "high": one.high,
            }

        return {
            "units": units,
            "now": as_read(self.now, units),
            "rising": self.rising,
            "next": turn(self.next_turn),
            "following": turn(self.following),
            "surge": as_read(self.surge, units),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "hours": [
                [when.isoformat(), as_read(observed, units), as_read(predicted, units)]
                for when, observed, predicted in self.hours
            ],
        }


WINDOW = """
SELECT ts, observed, predicted, fetched_at
FROM tide_reading
WHERE ts >= now() - $1::interval AND ts < now() + $2::interval
ORDER BY ts
"""


def _turns(rows: list, after: datetime) -> list[Turn]:
    """Every turning point in the predicted curve after a moment.

    Found by looking for where the curve stops going the way it was going. The
    six minute series makes that exact to within three minutes, which is finer
    than anybody reads a tide table.
    """
    points = [(row["ts"], row["predicted"]) for row in rows if row["predicted"] is not None]
    turns: list[Turn] = []
    for index in range(1, len(points) - 1):
        before, here, following = points[index - 1][1], points[index][1], points[index + 1][1]
        when = points[index][0]
        if when <= after:
            continue
        if here >= before and here >= following and not (here == before == following):
            turns.append(Turn(ts=when, level=here, high=True))
        elif here <= before and here <= following and not (here == before == following):
            turns.append(Turn(ts=when, level=here, high=False))
    return turns


async def read(pool: asyncpg.Pool, back: timedelta, ahead: timedelta) -> Tide | None:
    """The dashboard's slice of the stored water.

    None when there is nothing stored at all, which is a different answer from
    "the tide is out" and has to be drawn differently.
    """
    try:
        rows = await pool.fetch(WINDOW, back, ahead)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the tide: %s", error)
        return None
    if not rows:
        return None

    now = datetime.now(UTC)
    fetched = max((row["fetched_at"] for row in rows if row["fetched_at"]), default=None)

    # The most recent observation, if it is recent enough to call current.
    seen = [row for row in rows if row["observed"] is not None and row["ts"] <= now]
    latest = seen[-1] if seen else None
    level = latest["observed"] if latest and (now - latest["ts"]) <= NOW_WITHIN else None

    # The prediction at the same moment, for the surge and for the direction.
    predicted_now = None
    rising = None
    ordered = [(row["ts"], row["predicted"]) for row in rows if row["predicted"] is not None]
    for index, (when, value) in enumerate(ordered):
        if when > now:
            predicted_now = value
            if index:
                rising = value > ordered[index - 1][1]
            break

    turns = _turns(list(rows), now)
    return Tide(
        now=level,
        rising=rising,
        next_turn=turns[0] if turns else None,
        following=turns[1] if len(turns) > 1 else None,
        surge=(
            round(level - predicted_now, 2)
            if level is not None and predicted_now is not None
            else None
        ),
        fetched_at=fetched,
        hours=[(row["ts"], row["observed"], row["predicted"]) for row in rows],
    )


# Per day, cut on the site's own midnight the same way the calls and the rain
# are, so a day of tide and a day of pumping describe the same hours.
DAILY = """
SELECT time_bucket('1 day', ts, timezone => $2::text) AS day,
       max(coalesce(observed, predicted))             AS high_water,
       min(coalesce(observed, predicted))             AS low_water,
       max(observed - predicted)                      AS most_above_prediction
FROM tide_reading
WHERE ts > now() - $1::interval AND ts <= now()
GROUP BY 1
ORDER BY 1
"""


async def daily(pool: asyncpg.Pool, span: timedelta, zone: str) -> list[dict]:
    """High and low water per day, for the summary and the history page."""
    try:
        rows = await pool.fetch(DAILY, span, zone)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the daily tide: %s", error)
        return []
    return [
        {
            "day": row["day"].date().isoformat(),
            "high_water": round(float(row["high_water"]), 2)
            if row["high_water"] is not None
            else None,
            "low_water": round(float(row["low_water"]), 2)
            if row["low_water"] is not None
            else None,
            "most_above_prediction": (
                round(float(row["most_above_prediction"]), 2)
                if row["most_above_prediction"] is not None
                else None
            ),
        }
        for row in rows
    ]


__all__ = ["NOW_WITHIN", "Tide", "Turn", "daily", "read"]
