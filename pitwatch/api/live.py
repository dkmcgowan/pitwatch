"""The JSON the front end reads, and the device test buttons.

Kept small on purpose. This is not a public API and there is no versioning
promise; it is the shape the pages in this repository happen to want.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from pitwatch import auth
from pitwatch.api import forms
from pitwatch.ingest import shelly as shelly_ingest
from pitwatch.ingest.sink import LiveState
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/state", include_in_schema=False)
async def state(request: Request) -> JSONResponse:
    """Everything the dashboard needs in one request.

    One endpoint rather than several because the pieces have to agree with each
    other. A page that fetched currents and float states separately could show
    a pump running against a pit that had already emptied, and someone would
    reasonably believe it.
    """
    store: SettingsStore = request.app.state.settings
    live: LiveState = request.app.state.live
    pool = request.app.state.pool

    shelly = store.shelly
    pumps = store.pumps

    devices = {
        row["device"]: {
            "online": row["online"],
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            "last_error": row["last_error"],
        }
        for row in await pool.fetch("SELECT * FROM device_status")
    }

    def pump_state(number: int) -> dict:
        channel = shelly.pump1_channel if number == 1 else shelly.pump2_channel
        sample = live.samples.get(channel)
        settings = pumps.for_pump(number)
        current = sample.current if sample else None
        return {
            "name": settings.name,
            "channel": channel,
            "current": current,
            "voltage": sample.voltage if sample else None,
            "act_power": sample.act_power if sample else None,
            "pf": sample.pf if sample else None,
            "reading_at": sample.ts.isoformat() if sample else None,
            "running": current is not None and current >= settings.running_amps,
            "nameplate_amps": settings.nameplate_amps,
        }

    return JSONResponse(
        {
            "site": store.site.model_dump(mode="json"),
            "pumps": {"1": pump_state(1), "2": pump_state(2)},
            "devices": devices,
            "updated_at": live.updated_at.isoformat() if live.updated_at else None,
        }
    )


@router.post("/test/shelly", include_in_schema=False)
async def test_shelly(request: Request) -> JSONResponse:
    """Connect to a Shelly and report what it says.

    Takes the address from the form rather than from the saved settings, so the
    button works before anything has been saved, which is the moment it is
    most useful. It is behind a sign in once an account exists, because
    otherwise it is a way to make the server open connections to arbitrary
    hosts on the local network.
    """
    pool = request.app.state.pool
    if await auth.any_user_exists(pool):
        auth.require_user(request)

    form = await request.form()
    try:
        settings = forms.shelly_from(form)
    except (ValueError, ValidationError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    if not settings.host:
        return JSONResponse({"ok": False, "error": "Enter an address first"}, status_code=400)

    try:
        return JSONResponse(await shelly_ingest.probe(settings))
    except shelly_ingest.ShellyAuthError as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=200)
    except (shelly_ingest.ShellyError, OSError, TimeoutError) as error:
        return JSONResponse(
            {"ok": False, "error": f"Could not reach {settings.host}: {error}"}, status_code=200
        )


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
