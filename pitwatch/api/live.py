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
from pitwatch.ingest import waveshare as waveshare_ingest
from pitwatch.ingest.sink import LiveIo, LiveState
from pitwatch.schemas import SIGNAL_LABELS, Signal
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


async def build_state(app) -> dict:
    """Everything the dashboard needs, in one snapshot.

    One payload rather than several because the pieces have to agree with each
    other. A page that fetched currents and float states separately could show
    a pump running against a pit that had already emptied, and someone would
    reasonably believe it.

    Shared by the GET endpoint below and by the websocket in api/stream.py, so
    the first paint and every update are built the same way.
    """
    store: SettingsStore = app.state.settings
    live: LiveState = app.state.live
    live_io: LiveIo = app.state.live_io
    pool = app.state.pool

    shelly = store.shelly
    pumps = store.pumps

    # "Not set up" and "should be talking and is not" are different answers and
    # the dashboard has to be able to tell them apart. Running with only the
    # clamps connected is a normal way to start, and it should not paint a red
    # fault on the page for a device nobody has configured yet.
    configured = {
        "shelly": bool(shelly.enabled and shelly.host),
        "waveshare": bool(store.waveshare.enabled and store.waveshare.host),
    }
    devices = {
        row["device"]: {
            "configured": configured.get(row["device"], False),
            "online": row["online"],
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            "last_error": row["last_error"] if configured.get(row["device"]) else None,
        }
        for row in await pool.fetch("SELECT * FROM device_status")
    }

    run_signal = {1: Signal.PUMP1_RUN, 2: Signal.PUMP2_RUN}
    overload_signal = {1: Signal.PUMP1_OVERLOAD, 2: Signal.PUMP2_OVERLOAD}
    clamp = shelly.clamp_for_pump
    pump_settings = pumps.by_number

    def pump_state(number: int) -> dict:
        channel = clamp[number]
        sample = live.samples.get(channel)
        settings = pump_settings[number]
        current = sample.current if sample else None
        drawing = current is not None and current >= settings.running_amps
        contact = live_io.state_of(run_signal[number])
        return {
            "name": settings.name,
            "channel": channel,
            "current": current,
            "voltage": sample.voltage if sample else None,
            "act_power": sample.act_power if sample else None,
            "pf": sample.pf if sample else None,
            "reading_at": sample.ts.isoformat() if sample else None,
            # Two independent answers to the same question, reported separately
            # rather than merged. When they disagree, that disagreement is the
            # interesting thing: a closed contactor drawing nothing is a motor
            # that is not turning.
            "drawing_current": drawing,
            "run_contact": contact,
            "running": drawing or bool(contact),
            "overload_tripped": live_io.state_of(overload_signal[number]),
            "nameplate_amps": settings.nameplate_amps,
        }

    def signal_state(signal: Signal) -> dict:
        changed = live_io.changed_at(signal)
        return {
            "label": SIGNAL_LABELS[signal],
            # None means nothing is wired to it, which is not the same as off.
            "state": live_io.state_of(signal),
            "changed_at": changed.isoformat() if changed else None,
        }

    return {
        "site": store.site.model_dump(mode="json"),
        "pumps": {"1": pump_state(1), "2": pump_state(2)},
        "floats": {
            signal.value: signal_state(signal)
            for signal in (
                Signal.LEAD_FLOAT,
                Signal.LAG_FLOAT,
                Signal.HIGH_WATER,
                Signal.PANEL_ALARM,
            )
        },
        "devices": devices,
        "updated_at": live.updated_at.isoformat() if live.updated_at else None,
    }


@router.get("/state", include_in_schema=False)
async def state(request: Request) -> JSONResponse:
    return JSONResponse(await build_state(request.app))


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


@router.post("/test/waveshare", include_in_schema=False)
async def test_waveshare(request: Request) -> JSONResponse:
    """Read the eight inputs once and report them, raw and interpreted.

    The setup page calls this on a short timer while the channel map is open,
    so someone at the panel can lift a float by hand and watch a row change.
    That is by far the fastest way to get the mapping right, and reading the
    wire labels is how it ends up wrong.
    """
    pool = request.app.state.pool
    if await auth.any_user_exists(pool):
        auth.require_user(request)

    form = await request.form()
    try:
        settings = forms.waveshare_from(form)
    except (ValueError, ValidationError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    if not settings.host:
        return JSONResponse({"ok": False, "error": "Enter an address first"}, status_code=400)

    result = await waveshare_ingest.probe(settings)
    for channel in result.get("channels", []):
        channel["label"] = SIGNAL_LABELS[Signal(channel["signal"])]
    return JSONResponse(result)


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
