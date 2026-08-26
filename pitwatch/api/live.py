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
from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import DASHBOARD_ROLES, DashboardSettings, WaveshareSettings
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# What the pill reads when an input is on and when it is off.
#
# Deliberately not "wet" and "dry". An input is whatever somebody wired to it,
# and this no longer has any idea which of them is a float, which is an alarm
# contact, or which way round means trouble. Saying ON and Off is the honest
# version of that, and it does not editorialize about a signal it cannot
# interpret.
ON_WORD = "ON"
OFF_WORD = "Off"


def lead_and_lag(dashboard: DashboardSettings, live_io: LiveIo) -> tuple[str, str]:
    """The two words on the panel display, in pump order.

    The controller alternates, so whichever pump went last is the one that sits
    out next: if pump 1 ran last then pump 2 is lead. That also reads correctly
    while pump 1 is still running, because what the word answers is which pump
    goes next, and the answer during pump 1's run is pump 2.

    An overload outranks it. A pump that has tripped is not lead or lag, it is
    FAIL, and the other one is lead whether or not it was next in the rotation,
    because it is the only one left. Both tripped is the display in the
    photograph nobody wants to be looking at.
    """
    faulted = (
        live_io.state_of(dashboard.pump1_fault) if dashboard.pump1_fault else None,
        live_io.state_of(dashboard.pump2_fault) if dashboard.pump2_fault else None,
    )
    if faulted[0] and faulted[1]:
        return ("FAIL", "FAIL")
    if faulted[0]:
        return ("FAIL", "LEAD")
    if faulted[1]:
        return ("LEAD", "FAIL")

    last_1 = live_io.came_on_at(dashboard.pump1_run)
    last_2 = live_io.came_on_at(dashboard.pump2_run)
    if last_1 is None and last_2 is None:
        # Nothing has run since this was wired up. Which pump is lead is the
        # controller's business and it does not tell us, so this waits rather
        # than picking one and being wrong half the time.
        return ("--", "--")
    if last_2 is None or (last_1 is not None and last_1 > last_2):
        return ("LAG", "LEAD")
    return ("LEAD", "LAG")


def panel_state(
    dashboard: DashboardSettings, waveshare: WaveshareSettings, live_io: LiveIo
) -> dict:
    """The lamps and the display, laid out the way the panel door is."""
    lamps = {}
    for role, title in DASHBOARD_ROLES:
        channel = getattr(dashboard, role)
        lamps[role] = {
            "title": title,
            "channel": channel,
            # What the input is called, so the dashboard shows the panel's own
            # word for it rather than ours when somebody has typed one.
            "label": waveshare.label_for(channel) if channel else None,
            # None covers both nothing assigned and nothing read yet. Neither
            # is off, and a lamp that reads off when it means unknown is the
            # lamp that gets believed.
            "state": live_io.state_of(channel) if channel else None,
        }

    first, second = lead_and_lag(dashboard, live_io)
    lamps["display"] = {"1": first, "2": second}
    return lamps


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

    clamp = shelly.clamp_for_pump
    pump_settings = pumps.by_number

    def pump_state(number: int) -> dict:
        channel = clamp[number]
        sample = live.samples.get(channel)
        settings = pump_settings[number]
        current = sample.current if sample else None
        drawing = current is not None and current >= settings.running_amps
        # Amps only. The device reports voltage, power and power factor as
        # well, and they are still recorded, but they are not reported here
        # because in this installation they are not measurements of the motor:
        # the meter's voltage reference is its own supply rather than the phase
        # the clamps are on. Current is unaffected, since a CT measures the
        # conductor directly. If the reference is ever moved onto a measured
        # phase, this is the place to start showing them again.
        return {
            "name": settings.name,
            "channel": channel,
            "current": current,
            "reading_at": sample.ts.isoformat() if sample else None,
            # Only the clamp now. The panel's run contact used to be reported
            # beside this, and the disagreement between them was the
            # interesting part: a closed contactor drawing nothing is a motor
            # that is not turning. Saying which input carries that contact
            # needs a way to point at one, and inputs are labels now, so there
            # is nothing here to point with. See NOTES.md.
            "drawing_current": drawing,
            "running": drawing,
            "nameplate_amps": settings.nameplate_amps,
        }

    waveshare = store.waveshare
    assigned = {channel for channel in store.dashboard.assignments.values() if channel}

    def input_state(mapped) -> dict:
        changed = live_io.changed_at(mapped.channel)
        return {
            "channel": mapped.channel,
            "label": mapped.label,
            # None means nothing has read it yet, which is not the same as off.
            "state": live_io.state_of(mapped.channel),
            "on_word": ON_WORD,
            "off_word": OFF_WORD,
            "changed_at": changed.isoformat() if changed else None,
        }

    return {
        "site": store.site.model_dump(mode="json"),
        "pumps": {"1": pump_state(1), "2": pump_state(2)},
        "panel": panel_state(store.dashboard, waveshare, live_io),
        # Named inputs that no lamp is showing. Nothing should go missing just
        # because the dashboard has no place built for it.
        "inputs": [
            input_state(mapped)
            for mapped in waveshare.used_channels
            if mapped.channel not in assigned
        ],
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
    form = await request.form()
    try:
        settings = forms.waveshare_from(form)
    except (ValueError, ValidationError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    if not settings.host:
        return JSONResponse({"ok": False, "error": "Enter an address first"}, status_code=400)

    result = await waveshare_ingest.probe(settings)
    return JSONResponse(result)


@router.post("/test/email", include_in_schema=False)
async def test_email(request: Request, user: auth.SignedIn) -> JSONResponse:
    """Send one real message to one address, using the form as it stands.

    Using the unsaved form matters: the point of a test button is to find out
    whether what you have just typed works, before committing it. Always behind
    a sign in, because this one makes the server send mail on request.
    """
    store: SettingsStore = request.app.state.settings
    form = await request.form()

    try:
        settings = forms.smtp_from(form, store.smtp)
    except (ValueError, ValidationError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    to = forms.text(form, "test_to")
    if not to:
        return JSONResponse(
            {"ok": False, "error": "Put an address in the box first"}, status_code=400
        )

    site = store.site
    where = site.pumps_at
    try:
        await email_sender.send(
            settings,
            to,
            f"PitWatch test{f' from {site.where}' if site.where else ''}",
            "This is a test from PitWatch.\n\n"
            f"If you are reading it, alerts for {where} will reach this address.\n\n"
            "Nothing is wrong with the pumps. Nobody needs to do anything.\n",
        )
    except email_sender.EmailError as error:
        return JSONResponse({"ok": False, "error": str(error)})
    return JSONResponse({"ok": True, "detail": f"Sent to {to}. Check the inbox, and spam."})


@router.post("/test/sms", include_in_schema=False)
async def test_sms(request: Request, user: auth.SignedIn) -> JSONResponse:
    """Send one real text to one number, using the form as it stands."""
    store: SettingsStore = request.app.state.settings
    form = await request.form()

    try:
        settings = forms.sms_from(form, store.sms)
    except (ValueError, ValidationError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    to = forms.text(form, "test_to")
    if not to:
        return JSONResponse(
            {"ok": False, "error": "Put a phone number in the box first"}, status_code=400
        )

    site = store.site
    try:
        # Kept short on purpose. A text is billed per segment and truncated
        # without warning, and this one only has to prove delivery.
        await sms_sender.send(
            settings,
            store.smtp,
            to,
            f"PitWatch test{f' from {site.where}' if site.where else ''}. "
            "Alerts will reach this number.",
        )
    except sms_sender.SmsError as error:
        return JSONResponse({"ok": False, "error": str(error)})
    return JSONResponse(
        {"ok": True, "detail": f"Sent to {sms_sender.normalize(to)}. It can take a moment."}
    )


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
