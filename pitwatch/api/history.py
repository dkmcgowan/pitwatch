"""What the history page reads.

One request per view rather than one per chart. The three charts share a time
window and are read together, and three round trips to draw one screen is
three chances for them to disagree about what "now" means.

Cached for a minute per window. The heaviest query here counts rising edges
across raw readings, and a row of buttons that switches window is a row of
buttons somebody will press four times in a second.
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


def _seconds(span: timedelta) -> int:
    return int(span.total_seconds())


async def build_history(app, window: series.Window) -> dict:
    """Load, starts and contacts over one window, in one shape."""
    store: SettingsStore = app.state.settings
    pool = app.state.pool

    clamp = store.shelly.clamp_for_pump
    inputs = store.inputs
    now = datetime.now(UTC)

    load: dict[str, list] = {}
    starts: dict[str, list] = {}
    names: dict[str, str] = {}
    for number, pump in store.pumps.by_number.items():
        channel = clamp[number]
        names[str(number)] = pump.name or f"Pump {number}"
        load[str(number)] = [
            [at.isoformat(), round(peak, 2), None if settled is None else round(settled, 2)]
            for at, peak, settled in await series.load_series(
                pool, channel, window, domain.RUNNING_AMPS
            )
        ]
        starts[str(number)] = [
            [at.isoformat(), count]
            for at, count in await series.starts_series(pool, channel, window, domain.RUNNING_AMPS)
        ]

    # Only the inputs somebody has said carry something. An input with no
    # meaning assigned is still recorded, and a row on a chart labelled DI6
    # with nothing to say about it is a row nobody can read.
    assigned = list(inputs.used_channels)
    spans = await series.contact_spans(pool, [mapped.channel for mapped in assigned], window)
    contacts = []
    for mapped in assigned:
        closed_spans = spans.get(mapped.channel, [])
        contacts.append(
            {
                "role": mapped.role,
                "title": mapped.title,
                "channel": mapped.channel,
                "spans": [[opened.isoformat(), shut.isoformat()] for opened, shut in closed_spans],
                "closings": len(closed_spans),
            }
        )

    return {
        "window": window.key,
        "title": window.title,
        "from": (now - window.span).isoformat(),
        "to": now.isoformat(),
        "load_bucket": _seconds(window.load_bucket),
        "count_bucket": _seconds(window.count_bucket),
        "running_amps": domain.RUNNING_AMPS,
        "pumps": names,
        "load": load,
        "starts": starts,
        "contacts": contacts,
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
