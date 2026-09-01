"""The numbers behind the history page.

Three questions, one time window, and every answer shaped the same way: a list
of buckets from oldest to newest. What draws them is a few hundred lines of
SVG in the browser rather than a charting library, so this is where the
thinking lives and the drawing is only drawing.

Everything here reads raw samples, at every window. The hourly rollup would be
a cheaper scan for a month, and it cannot answer either of the two questions
this page is actually asked. A start is the current rising off nothing, and an
hourly average cannot tell one long run from four short ones. The same goes
for leaving out the starting surge: that means dropping the first reading of
each run, and a rollup has already averaged it in.

The scan is affordable because the application already does it. Typical load
on the dashboard reads five weeks of raw readings per pump every five minutes,
and raw samples are kept for ninety days, so a month is inside what is there.
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
    ),
}

DEFAULT_WINDOW = "7d"

# One bucket of lead in, so that the first reading inside the window has
# something before it to be compared against. Without it the earliest reading
# is treated as a rising edge and every chart opens with a start that did not
# happen.
LEAD_IN = timedelta(hours=1)


# Two numbers per bucket: the highest reading in it, and the highest reading in
# it that was not the first of a run.
#
# The second is the same exclusion typical load makes, for the same reason. A
# motor draws several times its running current for the moment it starts, and a
# chart of peaks is a chart of those moments: forty amps every time, telling
# you nothing about the pump. Leaving them out shows what it settles at.
#
# The lead in is what makes the first bucket in the window honest. Without a
# reading before it, the earliest reading has no previous to be compared
# against and cannot be recognized as a surge.
LOAD = """
WITH readings AS (
    SELECT ts, current, lag(current) OVER (ORDER BY ts) AS previous
    FROM em_sample
    WHERE channel = $1 AND ts > now() - $2::interval - $5::interval
)
SELECT time_bucket($3::interval, ts) AS bucket,
       max(current)                   AS peak,
       max(current) FILTER (
           WHERE NOT (current >= $4 AND (previous IS NULL OR previous < $4))
       )                              AS settled
FROM readings
WHERE ts > now() - $2::interval
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
    pool: asyncpg.Pool, channel: int, window: Window, running_amps: float
) -> list[tuple[datetime, float, float | None]]:
    """Peak load per bucket, and peak with the starting surge left out.

    The second is None for a bucket whose every reading was the first of a run,
    which is a real answer: there is nothing in that bucket but starting.
    """
    try:
        rows = await pool.fetch(
            LOAD, channel, window.span, window.load_bucket, running_amps, LEAD_IN
        )
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the load history: %s", error)
        return []
    return [
        (
            row["bucket"],
            float(row["peak"] or 0.0),
            None if row["settled"] is None else float(row["settled"]),
        )
        for row in rows
    ]


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
