"""What the history page reads.

One request per view rather than one per chart. Everything on the page shares a
time window and is read together, and four round trips to draw one screen is
four chances for them to disagree about what "now" means.

Cached for a minute per window, because a row of buttons that switches window
is a row of buttons somebody will press four times in a second.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pitwatch import auth, domain
from pitwatch.domain import series
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

CACHE_FOR = timedelta(seconds=60)

# How many runs the table under the charts lists. The charts draw every run in
# the window; this is the part somebody reads line by line, and past twenty
# lines nobody does.
RECENT_RUNS = 20


def _seconds(span: timedelta) -> int:
    return int(span.total_seconds())


def _figures(calls: list, gaps: list, runs: list) -> dict:
    """The window in eight numbers.

    Every one of them is either counted or a median. Nothing here is a mean:
    one twenty minute run after somebody held the panel switch down would drag
    an average for the week, and these are read as "what it looks like here".

    Calls and runs are deliberately both here and are not the same number. One
    call answered by both pumps is one call and two runs, so runs sits above
    calls by exactly the number of calls that took both. A page showing one of
    them invites the reader to divide by twelve seconds and get the wrong
    answer for how much water moved.
    """
    finished = [run.duration_s for run in runs if run.duration_s is not None]
    return {
        "calls": sum(count for _, count, _, _ in calls),
        "runs": len(runs),
        "both_ran": sum(both for _, _, both, _ in calls),
        "high_water": sum(high for _, _, _, high in calls),
        "typical_gap_s": series.median([gap for _, gap, _, _ in gaps]),
        "typical_run_s": series.median(finished),
        "longest_run_s": max(finished, default=None),
        "running_s": round(sum(finished), 1) if finished else 0.0,
    }


async def build_history(app, window: series.Window) -> dict:
    """Everything the page draws, over one window, in one shape."""
    store: SettingsStore = app.state.settings
    pool = app.state.pool
    zone = store.site.timezone
    now = datetime.now(UTC)

    calls = await series.calls_series(pool, window, zone)
    gaps = await series.call_gaps(pool, window)
    runs = await series.runs_series(pool, window)
    hours = await series.hour_profile(pool, window, zone)

    clamp = store.shelly.clamp_for_pump
    pumps = {}
    for number, pump in store.pumps.by_number.items():
        channel = clamp[number]
        pumps[str(number)] = {
            "name": pump.name or f"Pump {number}",
            # A clamp that has never read current is far more likely to be a CT
            # nobody fitted than a pump that has never once worked, and drawing
            # its runs at zero amps would be a measurement of nothing presented
            # as a flat healthy line.
            "clamp": await series.clamp_fitted(pool, channel, domain.RUNNING_AMPS),
            "runs": sum(1 for run in runs if run.pump == number),
        }

    return {
        "window": window.key,
        "title": window.title,
        "from": (now - window.span).isoformat(),
        "to": now.isoformat(),
        "count_bucket": _seconds(window.count_bucket),
        "pumps": pumps,
        "figures": _figures(calls, gaps, runs),
        "calls": [[bucket.isoformat(), count, both, high] for bucket, count, both, high in calls],
        "gaps": [[at.isoformat(), round(gap, 1), both, high] for at, gap, both, high in gaps],
        "runs": [
            [
                run.started_at.isoformat(),
                run.pump,
                None if run.duration_s is None else round(run.duration_s, 1),
                None if run.steady_current is None else round(run.steady_current, 2),
                run.role,
                run.both_ran,
                run.high_water,
            ]
            for run in runs
        ],
        # Twenty four entries whether or not anything ran in any of them. An
        # hour with no water coming in is part of the shape rather than a hole.
        "hours": [[hour, hours.get(hour, 0)] for hour in range(24)],
        "recent": RECENT_RUNS,
    }


@router.get("/history", include_in_schema=False)
async def history(request: Request, user: auth.SignedIn, window: str | None = None) -> JSONResponse:
    chosen = series.window_for(window)
    app = request.app

    cache: dict[str, tuple[datetime, dict]] = getattr(app.state, "history_cache", None) or {}
    app.state.history_cache = cache
    cached = cache.get(chosen.key)
    now = datetime.now(UTC)
    if cached and now - cached[0] < CACHE_FOR:
        return JSONResponse(cached[1])

    payload = await build_history(app, chosen)
    cache[chosen.key] = (now, payload)
    return JSONResponse(payload)


def register(app) -> None:
    app.include_router(router)


__all__ = ["build_history", "register", "router"]
