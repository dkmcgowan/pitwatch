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

**Only readings above the running threshold count.** Averaging in the hours a
pump spends switched off would produce a number near zero that moves when the
weather does, which is a rain gauge rather than a health check.
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
SELECT
    percentile_cont(0.5) WITHIN GROUP (ORDER BY current)
        FILTER (WHERE ts > now() - $3::interval)   AS recent_median,
    count(*) FILTER (WHERE ts > now() - $3::interval) AS recent_samples,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY current)
        FILTER (WHERE ts <= now() - $3::interval)  AS earlier_median,
    count(*) FILTER (WHERE ts <= now() - $3::interval) AS earlier_samples
FROM em_sample
WHERE channel = $1
  AND current >= $2
  AND ts > now() - $4::interval
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

# Shorter than the medians, because "last run" is a clock somebody is reading
# rather than a trend. Still not per frame: this is a query, and the panel lamp
# already says whether a pump is running right now.
REFRESH_RUNS = timedelta(seconds=60)

RUNS_QUERY = """
WITH readings AS (
    SELECT ts, current, lag(current) OVER (ORDER BY ts) AS previous
    FROM em_sample
    WHERE channel = $1 AND ts > now() - $3::interval
)
SELECT
    count(*)  FILTER (WHERE started) AS runs,
    max(ts)   FILTER (WHERE started) AS last_start
FROM (
    SELECT ts, current >= $2 AND (previous IS NULL OR previous < $2) AS started
    FROM readings
) edges
"""


@dataclass(frozen=True, slots=True)
class Recent:
    """How many times a pump has started lately, and when it last did."""

    runs: int = 0
    last_start: datetime | None = None

    def as_json(self) -> dict:
        return {
            "runs": self.runs,
            "last_start": self.last_start.isoformat() if self.last_start else None,
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
            row = await pool.fetchrow(RUNS_QUERY, channel, running_amps, RUN_WINDOW)
        except (asyncpg.PostgresError, OSError) as error:
            log.warning("Could not count recent runs for clamp %d: %s", channel, error)
            return self._cache[key][1]

        recent = Recent(runs=int(row["runs"] or 0), last_start=row["last_start"])
        self._cache[key] = (now, recent)
        return recent
