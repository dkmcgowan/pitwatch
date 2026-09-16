"""What the history page reads.

One request per view rather than one per chart. Everything on the page shares a
time window and is read together, and four round trips to draw one screen is
four chances for them to disagree about what "now" means.

Cached for a minute per window, because a row of buttons that switches window
is a row of buttons somebody will press four times in a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from pitwatch import auth, clock, domain
from pitwatch.domain import export as export_domain
from pitwatch.domain import series
from pitwatch.domain import weather as weather_domain
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

CACHE_FOR = timedelta(seconds=60)


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
    units = store.weather.units
    now = datetime.now(UTC)

    calls = await series.calls_series(pool, window, zone)
    # Rain on the same buckets and the same local midnights as the calls, so a
    # bar of rain and a bar of calls describe the same day. This is the whole
    # reason the weather is collected: "sixty calls on Tuesday" and "it rained
    # on Tuesday" are one fact, and until they were on one chart nobody could
    # see it.
    rain = await weather_domain.rain_series(pool, window.span, window.count_bucket, zone)
    gaps = await series.call_gaps(pool, window)
    runs = await series.runs_series(pool, window)
    hours = await series.hour_profile(pool, window, zone)

    clamp = store.mqtt.clamp_for_pump
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
        "heading": window.heading,
        "over": window.over,
        "within": window.within,
        "from": (now - window.span).isoformat(),
        "to": now.isoformat(),
        "count_bucket": _seconds(window.count_bucket),
        "pumps": pumps,
        "figures": _figures(calls, gaps, runs),
        "calls": [[bucket.isoformat(), count, both, high] for bucket, count, both, high in calls],
        "rain": [
            [bucket.isoformat(), weather_domain.as_read(millimeters, units)]
            for bucket, millimeters in rain
        ],
        "rain_units": units,
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
    }


@router.get("/history", include_in_schema=False)
async def history(request: Request, user: auth.SignedIn, window: str | None = None) -> JSONResponse:
    app = request.app
    chosen = series.window_for(window, app.state.settings.site.timezone)

    cache: dict[str, tuple[datetime, dict]] = getattr(app.state, "history_cache", None) or {}
    app.state.history_cache = cache
    cached = cache.get(chosen.key)
    now = datetime.now(UTC)
    if cached and now - cached[0] < CACHE_FOR:
        return JSONResponse(cached[1])

    payload = await build_history(app, chosen)
    cache[chosen.key] = (now, payload)
    return JSONResponse(payload)


@router.get("/history/export.xlsx", include_in_schema=False)
async def history_export(request: Request, user: auth.SignedIn, window: str | None = None):
    """The window the page is drawing, as a spreadsheet.

    Signed in and nothing more, which is the same as the page it comes from: a
    chart of the last week and a table of the same week are the same
    disclosure, and making the readable version of it harder to get than the
    drawn one would be a rule about file formats pretending to be a rule about
    access.

    Written to a temporary file rather than assembled in memory. The amps sheet
    is every meter reading in the window, twelve thousand a day on this
    installation, and a monitor that pauses to build tens of megabytes of
    spreadsheet is a monitor that is not watching the pit while it does it.
    """
    app = request.app
    store: SettingsStore = app.state.settings
    zone = store.site.timezone
    chosen = series.window_for(window, zone)

    until = datetime.now(UTC)
    # The same span the page draws. `window_for` has already turned "today"
    # into a span measured from the building's own midnight, so subtracting is
    # correct for every window rather than only the rolling ones.
    since = until - chosen.span

    names = {number: pump.name for number, pump in store.pumps.by_number.items()}
    sheets = await export_domain.gather(app.state.pool, since, until, zone, names)

    about = [
        ("PitWatch export", ""),
        ("Window", chosen.heading),
        ("From", clock.on_at(since, zone)),
        ("To", clock.on_at(until, zone)),
        ("Times are in", zone or "UTC"),
        ("Generated", clock.on_at(until, zone)),
    ]
    about += [(one.title, f"{len(one.rows):,} rows") for one in sheets]

    # mkstemp rather than NamedTemporaryFile, because the file has to outlive
    # this function: the response streams it after we return and the background
    # task removes it afterwards.
    fileno, temp = tempfile.mkstemp(suffix=".xlsx", prefix="pitwatch-export-")
    os.close(fileno)
    try:
        await asyncio.to_thread(export_domain.write, temp, sheets, about)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise

    def tidy_up() -> None:
        # Once the response has gone out, whether or not it arrived. Without
        # this every download leaves a spreadsheet in the container's temp
        # directory until somebody restarts it.
        with contextlib.suppress(OSError):
            os.unlink(temp)

    return FileResponse(
        temp,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=export_domain.filename(chosen.key, until),
        background=BackgroundTask(tidy_up),
    )


def register(app) -> None:
    app.include_router(router)


__all__ = ["build_history", "register", "router"]
