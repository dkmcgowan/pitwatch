"""What a pump has been drawing lately, and whether that is changing.

The live reading answers "is it running right now", which on an ejector pit is
no most of the time. This answers the more useful question: when it does run,
what does it draw, and is that number moving.

**Why the median and not an average with the peaks trimmed.** Both are trying
to describe the steady part of a run without the starting surge dragging it up.
Trimming needs a rule about what counts as a peak, and that rule is a guess
about a motor nobody here has measured. The median needs no rule: the surge is
a couple of readings out of every run, so it sits in the tail by construction
and the middle value is the steady draw whether the surge is two times the
running current or eight.

**Why two windows.** One number is not information. Sixteen amps is fine or
alarming depending entirely on what it was last month, and an impeller packing
up or a bearing going dry shows as a number that climbs over weeks, not as a
number that is wrong on any given day. So this reports the last week against
the four weeks before it, and the difference is the part worth reading.

**Only readings above the running threshold count**, and not the first one of
each run. Averaging in the hours a pump spends switched off would produce a
number near zero that moves when the weather does, which is a rain gauge rather
than a health check. And the first reading of a run is where the starting surge
lands: on the reference panel it ran 1.3 A above every later reading, and it is
43 percent of all the readings taken while running, because a short run
produces two of them and one is the first. Leaving it in put about 0.4 A of
starting surge into a number that is supposed to describe a motor at work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

log = logging.getLogger(__name__)

# The window that counts as "lately", and the one before it to compare against.
RECENT = timedelta(days=7)
EARLIER = timedelta(days=35)

# Below this many readings the median is describing two runs and a coincidence.
# A pump running four seconds twenty times a day gives several hundred a week,
# so this only excludes an install that genuinely has no history yet.
MIN_SAMPLES = 30

# These move over weeks. Recomputing per websocket frame would run a scan a
# second to watch a number change monthly.
REFRESH = timedelta(minutes=5)

QUERY = """
WITH readings AS (
    -- Every reading in the window, running or not. The threshold cannot be
    -- applied here: lag() has to see the reading before this one as it
    -- actually was, or the reading before every start would be the end of the
    -- previous run and no start would ever be recognized.
    SELECT ts, current, lag(current) OVER (ORDER BY ts) AS previous
    FROM em_sample
    WHERE channel = $1 AND ts > now() - $4::interval
), running AS (
    SELECT ts, current
    FROM readings
    WHERE current >= $2
      -- Not the first reading of a run, which is where the surge lands, and
      -- not the first reading in the window either, because there is no way to
      -- tell whether that one started a run or sat in the middle of one.
      AND previous IS NOT NULL
      AND previous >= $2
)
SELECT
    percentile_cont(0.5) WITHIN GROUP (ORDER BY current)
        FILTER (WHERE ts > now() - $3::interval)      AS recent_median,
    count(*) FILTER (WHERE ts > now() - $3::interval) AS recent_samples,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY current)
        FILTER (WHERE ts <= now() - $3::interval)     AS earlier_median,
    count(*) FILTER (WHERE ts <= now() - $3::interval) AS earlier_samples
FROM running
"""


@dataclass(frozen=True, slots=True)
class Typical:
    """What one clamp has seen while its pump was actually running."""

    median: float | None = None
    earlier_median: float | None = None
    samples: int = 0
    earlier_samples: int = 0

    @property
    def drift(self) -> float | None:
        """How much the steady draw has moved, or None if there is no baseline.

        Positive is a pump working harder than it was. That is the number worth
        a phone call, and it is invisible in any single reading.
        """
        if self.median is None or self.earlier_median is None:
            return None
        return self.median - self.earlier_median

    def as_json(self) -> dict:
        return {
            "median": round(self.median, 2) if self.median is not None else None,
            "earlier_median": (
                round(self.earlier_median, 2) if self.earlier_median is not None else None
            ),
            "drift": round(self.drift, 2) if self.drift is not None else None,
            "samples": self.samples,
        }


class CurrentHistory:
    """The medians, recomputed occasionally rather than on demand.

    Keyed by clamp and threshold together, so moving the running threshold on
    the settings page recomputes rather than reporting a number that was
    measured against the old one.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, float], tuple[datetime, Typical]] = {}

    async def typical(self, pool: asyncpg.Pool, channel: int, running_amps: float) -> Typical:
        key = (channel, running_amps)
        now = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < REFRESH:
            return cached[1]

        # Stamped before the query, not after. Two dashboards refreshing at the
        # same moment would otherwise both find the entry stale and both run the
        # scan, which is the one way this becomes expensive.
        self._cache[key] = (now, cached[1] if cached else Typical())

        try:
            row = await pool.fetchrow(QUERY, channel, running_amps, RECENT, EARLIER)
        except (asyncpg.PostgresError, OSError) as error:
            # A dashboard that loses this still shows live amps and every lamp.
            # Failing the whole payload over a history number would take the
            # working part of the page down with it.
            log.warning("Could not read the current history for clamp %d: %s", channel, error)
            return self._cache[key][1]

        recent = int(row["recent_samples"] or 0)
        earlier = int(row["earlier_samples"] or 0)
        typical = Typical(
            median=float(row["recent_median"]) if recent >= MIN_SAMPLES else None,
            earlier_median=float(row["earlier_median"]) if earlier >= MIN_SAMPLES else None,
            samples=recent,
            earlier_samples=earlier,
        )
        self._cache[key] = (now, typical)
        return typical


# -- how often, and how recently -------------------------------------------
#
# **Why there is no run duration here.** A night of real readings from the
# clamps settled it: the meter reports about every fifteen seconds while
# nothing is changing, and pushes immediately when something does. So the start
# and the end of a run are both caught, and the middle is not. Of 73 runs, 48
# produced exactly two readings, and the time between them ranged from one
# second to nearly four minutes for runs that drew the same steady current.
#
# That is enough to count runs and to say when the last one was, because both
# only need the transition. It is nowhere near enough to time one. Duration has
# to come from the panel's run contact, which is polled five times a second.
# Showing a duration derived from these readings would be inventing a number.

RUN_WINDOW = timedelta(hours=24)

# How far back the average of a normal day is worked out over. Long enough that
# one storm does not become the baseline, short enough to still be this season.
RUN_BASELINE = timedelta(days=30)

# And how much of that has to have actually been recorded before an average is
# worth printing. Two days of history divided into a month is not an average,
# it is a small number with a decimal point.
BASELINE_MIN_DAYS = 3

# Shorter than the medians, because "last run" is a clock somebody is reading
# rather than a trend. Still not per frame: this is a query, and the panel lamp
# already says whether a pump is running right now.
REFRESH_RUNS = timedelta(seconds=60)

RUNS_QUERY = """
WITH readings AS (
    SELECT ts, current, lag(current) OVER (ORDER BY ts) AS previous
    FROM em_sample
    WHERE channel = $1 AND ts > now() - $4::interval
), edges AS (
    SELECT ts, current >= $2 AND (previous IS NULL OR previous < $2) AS started
    FROM readings
)
SELECT
    count(*) FILTER (WHERE started AND ts > now() - $3::interval) AS runs,
    -- The last run whenever it was, not the last one today. A pump that has
    -- not gone since Tuesday should say Tuesday, not say nothing.
    max(ts)  FILTER (WHERE started)                              AS last_start,
    count(*) FILTER (WHERE started)                              AS baseline_runs,
    min(ts)                                                      AS first_seen
FROM edges
"""


@dataclass(frozen=True, slots=True)
class Recent:
    """How many times a pump has started lately, and when it last did."""

    runs: int = 0
    last_start: datetime | None = None
    # Runs on an ordinary day, so today's count has something to be read
    # against. Eighty-nine is a lot or a Tuesday depending on what the month
    # looks like, and only one of those is worth getting out of bed for.
    daily_average: float | None = None

    def as_json(self) -> dict:
        return {
            "runs": self.runs,
            "last_start": self.last_start.isoformat() if self.last_start else None,
            "daily_average": (
                round(self.daily_average) if self.daily_average is not None else None
            ),
        }


class RecentRuns:
    """Run counts and last start, cached for a minute."""

    def __init__(self) -> None:
        self._cache: dict[tuple[int, float], tuple[datetime, Recent]] = {}

    async def recent(self, pool: asyncpg.Pool, channel: int, running_amps: float) -> Recent:
        key = (channel, running_amps)
        now = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < REFRESH_RUNS:
            return cached[1]
        self._cache[key] = (now, cached[1] if cached else Recent())

        try:
            row = await pool.fetchrow(RUNS_QUERY, channel, running_amps, RUN_WINDOW, RUN_BASELINE)
        except (asyncpg.PostgresError, OSError) as error:
            log.warning("Could not count recent runs for clamp %d: %s", channel, error)
            return self._cache[key][1]

        recent = Recent(
            runs=int(row["runs"] or 0),
            last_start=row["last_start"],
            daily_average=_daily_average(row["baseline_runs"], row["first_seen"], now),
        )
        self._cache[key] = (now, recent)
        return recent


def _daily_average(runs: int | None, first_seen: datetime | None, now: datetime) -> float | None:
    """Runs a day, over as much of the baseline as has actually been recorded.

    Dividing by thirty regardless would make every new install look quiet and
    then slowly look busier as the denominator became real, which is a graph of
    the install rather than of the pit.
    """
    if not runs or first_seen is None:
        return None
    days = (now - first_seen).total_seconds() / 86_400
    if days < BASELINE_MIN_DAYS:
        return None
    return runs / min(days, RUN_BASELINE.total_seconds() / 86_400)


# -- how often a contact has closed, and when it last did -------------------
#
# Cheap, because io_event only ever holds transitions. Every row with state
# true is a contact closing, so counting them is counting events rather than
# counting samples and hoping. That is the whole reason the reader writes edges
# instead of polls.
#
# Two windows on one scan. A float closes every time the pit fills, so a day is
# the useful number for one; an alarm that went off twice in a month is the
# whole story for the other. Both are cheap enough that the display picks which
# to show rather than the query being asked twice.

SIGNAL_TODAY = timedelta(hours=24)
SIGNAL_MONTH = timedelta(days=30)

SIGNAL_QUERY = """
SELECT
    channel,
    max(ts)                                          AS last_on,
    count(*) FILTER (WHERE ts > now() - $2::interval) AS today,
    count(*) FILTER (WHERE ts > now() - $3::interval) AS month
FROM io_event
WHERE state AND channel = ANY($1::smallint[])
GROUP BY channel
"""


@dataclass(frozen=True, slots=True)
class Closings:
    """What one contact has been doing."""

    last_on: datetime | None = None
    today: int = 0
    month: int = 0
    # False when nothing has ever been recorded for this input, which is not
    # the same as a contact that has sat open. One means nobody has wired it or
    # the module has never been reachable; the other is a quiet week.
    known: bool = False

    def as_json(self) -> dict:
        return {
            "last_on": self.last_on.isoformat() if self.last_on else None,
            "today": self.today if self.known else None,
            "month": self.month if self.known else None,
        }


class SignalHistory:
    """Closings per input, for every input a lamp is pointed at."""

    def __init__(self) -> None:
        self._at: datetime | None = None
        self._by_channel: dict[int, Closings] = {}

    async def closings(self, pool: asyncpg.Pool, channels: list[int]) -> dict[int, Closings]:
        now = datetime.now(UTC)
        if self._at is not None and now - self._at < REFRESH_RUNS:
            return self._by_channel
        if not channels:
            self._at, self._by_channel = now, {}
            return self._by_channel

        self._at = now
        try:
            rows = await pool.fetch(SIGNAL_QUERY, channels, SIGNAL_TODAY, SIGNAL_MONTH)
        except (asyncpg.PostgresError, OSError) as error:
            log.warning("Could not read the contact history: %s", error)
            return self._by_channel

        self._by_channel = {
            row["channel"]: Closings(
                last_on=row["last_on"],
                today=int(row["today"] or 0),
                month=int(row["month"] or 0),
                known=True,
            )
            for row in rows
        }
        return self._by_channel
