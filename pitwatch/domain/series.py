"""The numbers behind the history page.

Three questions, one time window, and every answer shaped the same way: a list
of buckets from oldest to newest. What draws them is a few hundred lines of
SVG in the browser rather than a charting library, so this is where the
thinking lives and the drawing is only drawing.

The window decides where the readings come from. A day of raw samples is a
scan of one chunk; a month of them is thirty, so a month reads the hourly
rollup instead. That rollup exists for exactly this and is why the retention
policy keeps hours forever and raw samples for ninety days.

Starts are counted from raw readings at every window. A start is the current
rising off nothing, and an hourly average cannot tell one long run from four
short ones, which on a pit that runs for seconds at a time is the whole
question.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Window:
    """One choice of how far back to look, and how finely."""

    key: str
    title: str
    span: timedelta
    # How wide a bucket is on the load chart, and on the count chart. They are
    # not the same: a count of starts is only worth reading by the hour or by
    # the day, while the load line wants as many points as will fit.
    load_bucket: timedelta
    count_bucket: timedelta
    # Raw samples, or the hourly rollup. See the module docstring.
    hourly: bool = False


WINDOWS: dict[str, Window] = {
    "24h": Window(
        key="24h",
        title="24 hours",
        span=timedelta(hours=24),
        load_bucket=timedelta(minutes=5),
        count_bucket=timedelta(hours=1),
    ),
    "7d": Window(
        key="7d",
        title="7 days",
        span=timedelta(days=7),
        load_bucket=timedelta(hours=1),
        count_bucket=timedelta(days=1),
    ),
    "30d": Window(
        key="30d",
        title="30 days",
        span=timedelta(days=30),
        load_bucket=timedelta(hours=6),
        count_bucket=timedelta(days=1),
        hourly=True,
    ),
}

DEFAULT_WINDOW = "7d"

# One bucket of lead in, so that the first reading inside the window has
# something before it to be compared against. Without it the earliest reading
# is treated as a rising edge and every chart opens with a start that did not
# happen.
LEAD_IN = timedelta(hours=1)


LOAD_RAW = """
SELECT time_bucket($3::interval, ts) AS bucket,
       max(current)                  AS peak,
       avg(current)                  AS mean
FROM em_sample
WHERE channel = $1 AND ts > now() - $2::interval
GROUP BY 1
ORDER BY 1
"""

LOAD_HOURLY = """
SELECT time_bucket($3::interval, bucket) AS bucket,
       max(max_current)                  AS peak,
       avg(avg_current)                  AS mean
FROM em_1h
WHERE channel = $1 AND bucket > now() - $2::interval
GROUP BY 1
ORDER BY 1
"""

STARTS = """
WITH readings AS (
    SELECT ts, current, lag(current) OVER (ORDER BY ts) AS previous
    FROM em_sample
    WHERE channel = $1 AND ts > now() - $2::interval - $5::interval
)
SELECT time_bucket($4::interval, ts) AS bucket, count(*) AS starts
FROM readings
WHERE ts > now() - $2::interval
  AND current >= $3
  AND (previous IS NULL OR previous < $3)
GROUP BY 1
ORDER BY 1
"""

# Every change inside the window, and the state going into it. The second one
# matters: a float that closed an hour before the window opened and is still
# closed has no event inside it, and a chart that reads only the events would
# draw it as having been open the whole time.
CONTACT_EVENTS = """
SELECT channel, ts, state
FROM io_event
WHERE channel = ANY($1::smallint[]) AND ts > now() - $2::interval
ORDER BY channel, ts
"""

CONTACT_BEFORE = """
SELECT DISTINCT ON (channel) channel, state
FROM io_event
WHERE channel = ANY($1::smallint[]) AND ts <= now() - $2::interval
ORDER BY channel, ts DESC
"""


def window_for(key: str | None) -> Window:
    """The window somebody asked for, or the default if it is not one of ours."""
    return WINDOWS.get(key or "", WINDOWS[DEFAULT_WINDOW])


async def load_series(
    pool: asyncpg.Pool, channel: int, window: Window
) -> list[tuple[datetime, float, float]]:
    """Peak and mean load per bucket, oldest first."""
    query = LOAD_HOURLY if window.hourly else LOAD_RAW
    try:
        rows = await pool.fetch(query, channel, window.span, window.load_bucket)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the load history: %s", error)
        return []
    return [(row["bucket"], float(row["peak"] or 0.0), float(row["mean"] or 0.0)) for row in rows]


async def starts_series(
    pool: asyncpg.Pool, channel: int, window: Window, running_amps: float
) -> list[tuple[datetime, int]]:
    """How many times the load rose off nothing, per bucket, oldest first."""
    try:
        rows = await pool.fetch(
            STARTS, channel, window.span, running_amps, window.count_bucket, LEAD_IN
        )
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the run history: %s", error)
        return []
    return [(row["bucket"], int(row["starts"])) for row in rows]


async def contact_spans(
    pool: asyncpg.Pool, channels: list[int], window: Window
) -> dict[int, list[tuple[datetime, datetime]]]:
    """When each contact was closed, as spans clipped to the window.

    A span still open at the end runs to now, which is the honest drawing: the
    float is wet as this is being read.
    """
    if not channels:
        return {}

    now = datetime.now(UTC)
    start = now - window.span
    try:
        before = await pool.fetch(CONTACT_BEFORE, channels, window.span)
        events = await pool.fetch(CONTACT_EVENTS, channels, window.span)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the contact history: %s", error)
        return {}

    spans: dict[int, list[tuple[datetime, datetime]]] = {channel: [] for channel in channels}
    opened: dict[int, datetime | None] = {
        row["channel"]: (start if row["state"] else None) for row in before
    }

    for row in events:
        channel, at, state = row["channel"], row["ts"], row["state"]
        if state and opened.get(channel) is None:
            opened[channel] = at
        elif not state and opened.get(channel) is not None:
            spans[channel].append((opened[channel], at))
            opened[channel] = None

    for channel, since in opened.items():
        if since is not None and channel in spans:
            spans[channel].append((since, now))
    return spans
