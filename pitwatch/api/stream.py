"""The live feed the dashboard reads.

A websocket rather than the page polling, for the same reason ingest is a
websocket: a pump that starts should appear on the screen when it starts, not
up to a refresh interval later. The payload is the same shape /api/state
returns, so the page has one renderer and the first paint and every update go
through it.

Updates are sent on a short timer rather than on every reading. The Shelly
pushes about twice a second across the two clamps, and a browser does not need
to be told about a current that moved by a hundredth of an amp. What it does
need is to never be more than a moment behind, which is what the interval buys,
and to be told immediately when something changes state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pitwatch.api.live import build_state

log = logging.getLogger(__name__)

router = APIRouter()

# How often a connected dashboard is refreshed. Fast enough that a pump
# starting looks immediate, slow enough that a tablet left on all day is not
# doing anything.
INTERVAL_S = 1.0


@router.websocket("/ws/state")
async def stream_state(websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    log.debug("Dashboard connected")

    try:
        while True:
            payload = await build_state(app)
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
