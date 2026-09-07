"""The numbers behind the history page.

**The panel says what happened, and the clamp says what it cost.** Everything
here counts runs and calls out of `pump_run` and `pump_cycle`, which are
written from the panel's own run contacts at the moment the contact moves. It
used to count them out of the meter's readings, by looking for the current
rising off nothing, and that was a floor rather than a tally: the Shelly
reports when something changes rather than on a clock, so two runs close
together arrive looking like one and a short run can arrive as nothing at all.
The contacts have neither problem.

That change is what this page is now shaped around, because it makes different
questions answerable. The old page drew a line of amps over time, which for
this pit is a flat zero with a spike every half hour, and a chart of eight
contact rows, most of them empty. Neither answers the question somebody opens
this page with, which is some version of "is it working harder than it was".

The four things worth reading, in the order they matter:

1. **How often the pit calls for water.** The nearest thing to a measurement of
   what is coming in, and the number that moves when it rains.
2. **How long a run lasts.** Twelve seconds, every time, on this pit. A run
   that starts taking twenty is a pump moving less water per second.
3. **What it draws while it runs.** Steady current per run, which is the motor
   itself. Only for a pump whose clamp has ever seen current: a channel with no
   CT on it reads a perfectly convincing zero, and a chart of zeros is a lie
   that looks like a measurement.
4. **What time of day it calls.** Says whether the water is the building's or
   the ground's. On the reference pit it is four an hour at noon and one an
   hour at four in the morning, which is people.

Three things that were here and are not any more. A scatter of the spacing
between calls, which said nothing the count of calls per day does not say
louder, and read as a cloud of dots. A timeline of every contact, which on the
default week is a hairline per twelve second run and is answered better by the
dashboard and the alert history; `contact_spans` stays, because the weekly
summary counts closings out of it. And peak current, which on this pump is the
starting surge every single time: it is a fact about induction motors rather
than a fact about this one, and drawn beside the steady current it was the
larger number and therefore the one the eye read. The spacing survives as a
single median in the figures, which is the part of it worth reading.

Windows are 24 hours, 7 days and 30 days. Raw readings are kept ninety days, so
every window is inside what is there.
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
    # How wide a bar is on the counting charts. Runs and calls are individual
    # events drawn one dot each, so they have no bucket of their own.
    count_bucket: timedelta


WINDOWS: dict[str, Window] = {
    "24h": Window(
        key="24h", title="24 hours", span=timedelta(hours=24), count_bucket=timedelta(hours=1)
    ),
    "7d": Window(key="7d", title="7 days", span=timedelta(days=7), count_bucket=timedelta(days=1)),
    "30d": Window(
        key="30d", title="30 days", span=timedelta(days=30), count_bucket=timedelta(days=1)
    ),
}

DEFAULT_WINDOW = "7d"

# How far back to look past the window's edge for the call before the first
# one. Without it the first call in the window has no previous to be measured
# from and every chart opens with a gap that is not a gap.
LEAD_IN = timedelta(days=1)

# Buckets are cut on local midnight rather than on UTC midnight. A bar labelled
# Tuesday that holds eight o'clock Monday evening through eight o'clock Tuesday
# evening is a bar that answers a different question than the one its label
# asks.
CALLS = """
SELECT time_bucket($2::interval, started_at, timezone => $3::text) AS bucket,
       count(*)                                 AS calls,
       count(*) FILTER (WHERE both_ran)         AS both_ran,
       count(*) FILTER (WHERE high_water)       AS high_water
FROM pump_cycle
WHERE started_at > now() - $1::interval
GROUP BY 1
ORDER BY 1
"""

# One row per call, with the time since the call before it. The lead in is
# fetched and then dropped: it is here to give the first row inside the window
# something to subtract from.
GAPS = """
SELECT started_at,
       extract(epoch FROM started_at - lag(started_at) OVER (ORDER BY started_at)) AS gap_s,
       both_ran,
       high_water
FROM pump_cycle
WHERE started_at > now() - $1::interval - $2::interval
ORDER BY started_at
"""

# One row per run. Ordered oldest first, the way every chart on this page reads.
#
# Peak current is written by the recorder and is not read here. On this motor
# it is the starting surge on every run without exception, so a chart of it is
# a chart of a constant with noise on it.
RUNS = """
SELECT r.started_at,
       r.pump,
       r.duration_s,
       r.steady_current,
       r.role,
       r.ended_at IS NULL      AS running,
       coalesce(c.both_ran, false)   AS both_ran,
       coalesce(c.high_water, false) AS high_water
FROM pump_run r
LEFT JOIN pump_cycle c ON c.id = r.cycle_id
WHERE r.started_at > now() - $1::interval
ORDER BY r.started_at
"""

# Calls by hour of the local day, which is a different question from calls over
# time: it is asked of the whole window at once and answers what the routine is
# rather than what happened.
#
# Calls rather than runs, so this chart and the one above it count the same
# thing. Which pump answered is not part of the question either: the panel
# alternates, so splitting the bars by pump drew two colors that always came
# out half and half and said nothing about the water.
HOURS = """
SELECT extract(hour FROM started_at AT TIME ZONE $2::text)::int AS hour,
       count(*) AS calls
FROM pump_cycle
WHERE started_at > now() - $1::interval
GROUP BY 1
ORDER BY 1
"""

# Whether this clamp has ever, in all of the readings kept, seen current. A
# channel with no CT fitted reads zero all day and is indistinguishable from a
# motor that never turns, except by this.
CLAMP_FITTED = "SELECT EXISTS (SELECT 1 FROM em_sample WHERE channel = $1 AND current >= $2)"

# Every change inside the window, and the state going into it. The second one
# matters: a float that closed an hour before the window opened and is still
# closed has no event inside it, and reading only the events would take it as
# having been open the whole time.
#
# The history page drew a timeline off these and does not any more. The weekly
# summary still counts them, which is where a contact closing belongs: how many
# times the high float was reached this week is a sentence, and on the default
# seven day window it was a hairline nobody could see.
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


def median(values: list[float]) -> float | None:
    """The middle value, or None when there is nothing to take a middle of.

    A median rather than a mean everywhere on this page. One twenty minute run
    after somebody held the panel switch down would drag a mean for the week,
    and the number is being read as "what a run looks like here".
    """
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float(ordered[middle - 1] + ordered[middle]) / 2


async def _fetch(pool: asyncpg.Pool, what: str, query: str, *args) -> list:
    """Every read on this page, wrapped the same way.

    A history page that fails to draw is an annoyance. A history page that
    stops the application is a monitor that is not watching the pump, so a
    query that will not run costs its own chart and nothing else.
    """
    try:
        return await pool.fetch(query, *args)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read %s: %s", what, error)
        return []


async def calls_series(
    pool: asyncpg.Pool, window: Window, zone: str
) -> list[tuple[datetime, int, int, int]]:
    """Calls for water per bucket: how many, how many took both pumps, and how
    many reached the high float."""
    rows = await _fetch(pool, "the call history", CALLS, window.span, window.count_bucket, zone)
    return [
        (row["bucket"], int(row["calls"]), int(row["both_ran"]), int(row["high_water"]))
        for row in rows
    ]


async def call_gaps(pool: asyncpg.Pool, window: Window) -> list[tuple[datetime, float, bool, bool]]:
    """Every call in the window with the minutes since the one before it.

    The first call of all has nothing before it and is left out rather than
    given a made up gap.
    """
    rows = await _fetch(pool, "the call spacing", GAPS, window.span, LEAD_IN)
    start = datetime.now(UTC) - window.span
    return [
        (row["started_at"], float(row["gap_s"]), row["both_ran"], row["high_water"])
        for row in rows
        if row["gap_s"] is not None and row["started_at"] >= start
    ]


@dataclass(frozen=True)
class Run:
    """One run of one pump, the way the panel recorded it."""

    started_at: datetime
    pump: int
    duration_s: float | None
    steady_current: float | None
    role: str
    running: bool
    both_ran: bool
    high_water: bool


async def runs_series(pool: asyncpg.Pool, window: Window) -> list[Run]:
    rows = await _fetch(pool, "the run history", RUNS, window.span)
    return [
        Run(
            started_at=row["started_at"],
            pump=int(row["pump"]),
            duration_s=None if row["duration_s"] is None else float(row["duration_s"]),
            steady_current=(
                None if row["steady_current"] is None else float(row["steady_current"])
            ),
            role=row["role"],
            running=row["running"],
            both_ran=row["both_ran"],
            high_water=row["high_water"],
        )
        for row in rows
    ]


async def hour_profile(pool: asyncpg.Pool, window: Window, zone: str) -> dict[int, int]:
    """Calls by hour of the local day, as {hour: calls}."""
    rows = await _fetch(pool, "the daily pattern", HOURS, window.span, zone)
    return {int(row["hour"]): int(row["calls"]) for row in rows}


async def clamp_fitted(pool: asyncpg.Pool, channel: int, running_amps: float) -> bool:
    """Whether this channel has ever read above the running threshold."""
    try:
        return bool(await pool.fetchval(CLAMP_FITTED, channel, running_amps))
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not check clamp %d: %s", channel, error)
        return False


async def contact_spans(
    pool: asyncpg.Pool, channels: list[int], window: Window
) -> dict[int, list[tuple[datetime, datetime]]]:
    """When each contact was closed, as spans clipped to the window.

    Read by the weekly summary, which counts them and says when the last one
    was. A span still open at the end runs to now, which is the honest answer:
    the float is wet as this is being read.
    """
    if not channels:
        return {}

    now = datetime.now(UTC)
    start = now - window.span
    before = await _fetch(pool, "the contact history", CONTACT_BEFORE, channels, window.span)
    events = await _fetch(pool, "the contact history", CONTACT_EVENTS, channels, window.span)

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
