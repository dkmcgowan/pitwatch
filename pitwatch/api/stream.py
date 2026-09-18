"""The live feed the dashboard reads.

A websocket rather than the page polling, for the same reason ingest is a
websocket: a pump that starts should appear on the screen when it starts, not
up to a refresh interval later. The payload is the same shape /api/state
returns, so the page has one renderer and the first paint and every update go
through it.

Updates are sent on a short timer rather than on every reading. The meter on
this pit pushes about twice a second across the two clamps, and a browser does
not need
to be told about a current that moved by a hundredth of an amp. What it does
need is to never be more than a moment behind, which is what the interval buys,
and to be told immediately when something changes state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pitwatch import auth
from pitwatch.api.live import build_state
from pitwatch.domain import sites

log = logging.getLogger(__name__)

router = APIRouter()

# How often a connected dashboard is refreshed. Fast enough that a pump
# starting looks immediate, slow enough that a tablet left on all day is not
# doing anything.
INTERVAL_S = 1.0


async def _store_for(websocket: WebSocket):
    """Which building this socket is watching.

    Worked out here rather than inherited, because the sign in middleware is an
    HTTP middleware and does not run for a websocket. The session cookie does
    reach us, so the same two questions get asked again: who is this, and which
    of their buildings did they pick. A socket that cannot answer watches the
    application's own site, which on every single site installation is the only
    one there is.
    """
    app = websocket.app
    store = app.state.settings
    pool = getattr(app.state, "pool", None)
    user_id = websocket.session.get(auth.SESSION_USER_KEY)
    if pool is None or not isinstance(user_id, int):
        return store
    user = await auth.get_user(pool, user_id)
    if user is None or not user.enabled:
        return store
    chosen = websocket.session.get(sites.SESSION_SITE_KEY)
    site_id = await sites.resolve(pool, user, chosen if isinstance(chosen, int) else None)
    return store if site_id is None else store.for_site(site_id)


@router.websocket("/ws/state")
async def stream_state(websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    log.debug("Dashboard connected")

    # Settled once, at connect. A person who switches buildings gets a new page
    # and therefore a new socket, so re-asking every second would be a query
    # per second per open tab to learn something that cannot have changed.
    store = await _store_for(websocket)

    try:
        while True:
            payload = await build_state(app, store)
            await websocket.send_json(payload)
            await asyncio.sleep(INTERVAL_S)
    except WebSocketDisconnect:
        log.debug("Dashboard disconnected")
    except (asyncio.CancelledError, RuntimeError):
        # RuntimeError is what Starlette raises when the socket is already
        # closing underneath us, which happens on shutdown and on a browser
        # that navigated away mid send. Neither is worth a traceback.
        raise
    finally:
        with contextlib.suppress(RuntimeError):
            await websocket.close()


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
