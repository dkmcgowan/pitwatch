"""The JSON the front end reads, and the device test buttons.

Kept small on purpose. This is not a public API and there is no versioning
promise; it is the shape the pages in this repository happen to want.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from pitwatch import auth, domain
from pitwatch.api import forms
from pitwatch.domain.history import (
    Closings,
    CurrentHistory,
    Recent,
    RecentRuns,
    SignalHistory,
    Typical,
)
from pitwatch.ingest import inputs as inputs_ingest
from pitwatch.ingest import shelly as shelly_ingest
from pitwatch.ingest.sink import LiveIo, LiveState
from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import DASHBOARD_ROLES, InputsSettings
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# What the pill reads when an input is on and when it is off.
#
def lead_and_lag(inputs: InputsSettings, live_io: LiveIo) -> tuple[str, str]:
    """The two words in the middle of the panel, in pump order.

    Written to match the controller on the wall, because the point of this
    display is that somebody who has stood in front of that panel already knows
    how to read it.

    **A running pump is LEAD.** It holds the word for as long as its contact is
    closed, and the rotation flips when it stops, not when it starts. So pump 1
    reads LEAD all the way through its run and becomes LAG the moment it drops
    out, at which point pump 2 is lead and is the one that answers the next
    call.

    **Both running is ON and ON.** That is the high water case: the pit has
    come up past the lag float and the controller has called both. Neither is
    leading anything at that point, they are both just running, and calling one
    of them lead would be describing a rotation that has been overtaken by
    events.

    **An overload outranks all of it.** A tripped pump reads FAIL and the other
    is lead, whether or not the rotation said so, because it is the only one
    left. Both tripped is the display in the photograph that nobody wants to be
    looking at.
    """
    fault_1 = inputs.channel_for("pump1_fault")
    fault_2 = inputs.channel_for("pump2_fault")
    run_1 = inputs.channel_for("pump1_run")
    run_2 = inputs.channel_for("pump2_run")

    faulted_1 = live_io.state_of(fault_1) if fault_1 else None
    faulted_2 = live_io.state_of(fault_2) if fault_2 else None
    if faulted_1 and faulted_2:
        return ("FAIL", "FAIL")

    running_1 = live_io.state_of(run_1) if run_1 else None
    running_2 = live_io.state_of(run_2) if run_2 else None

    if faulted_1:
        return ("FAIL", "LEAD")
    if faulted_2:
        return ("LEAD", "FAIL")

    if running_1 and running_2:
        return ("ON", "ON")
    if running_1:
        return ("LEAD", "LAG")
    if running_2:
        return ("LAG", "LEAD")

    # Neither is running, so the rotation decides, and the pump that went last
    # is the one sitting out. came_on_at is when a contact was last seen to
    # close, which outlives the run itself; changed_at would only say when it
    # dropped out and would be the same answer for a pump that has not run in a
    # month as for one that stopped a second ago.
    last_1 = live_io.came_on_at(run_1)
    last_2 = live_io.came_on_at(run_2)
    if last_1 is None and last_2 is None:
        # Nothing has run since this was wired up. Which pump is lead is the
        # controller's business and it does not tell us, so this waits rather
        # than picking one and being wrong half the time.
        return ("--", "--")
    if last_2 is None or (last_1 is not None and last_1 > last_2):
        return ("LAG", "LEAD")
    return ("LEAD", "LAG")


def panel_state(
    inputs: InputsSettings,
    live_io: LiveIo,
    closings: dict[int, Closings] | None = None,
) -> dict:
    """The lamps and the display, laid out the way the panel door is."""
    closings = closings or {}
    lamps = {}
    for role, title in DASHBOARD_ROLES:
        channel = inputs.channel_for(role)
        history = closings.get(channel) if channel else None
        lamps[role] = {
            "title": title,
            "channel": channel,
            # Which terminal this came off, for anybody standing at the panel.
            "label": inputs.label_for(channel) if channel else None,
            # None covers both nothing assigned and nothing read yet. Neither
            # is off, and a lamp that reads off when it means unknown is the
            # lamp that gets believed.
            "state": live_io.state_of(channel) if channel else None,
            # When this contact last closed and how often it has lately, which
            # is the part a lamp cannot say: a lamp that is off now looks the
            # same whether it went twenty times today or never.
            "history": (history or Closings()).as_json(),
        }

    first, second = lead_and_lag(inputs, live_io)
    lamps["display"] = {"1": first, "2": second}
    return lamps


def _with_live_rise(recent: Recent, rose_at) -> dict:
    """The run counts from the database, with the clock from memory."""
    payload = recent.as_json()
    if rose_at is not None and (recent.last_start is None or rose_at > recent.last_start):
        payload["last_start"] = rose_at.isoformat()
        # The count is left alone. It is a minute stale at worst, and guessing
        # at it here would be right until two runs landed inside one cache
        # window and then quietly wrong.
    return payload


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
        "inputs": bool(store.inputs.enabled and store.inputs.host),
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

    # What each pump has been drawing when it runs, which is the number that
    # says something on a pit that is dry most of the time. Cached and slow
    # moving; see pitwatch.domain.history.
    history: CurrentHistory | None = getattr(app.state, "history", None)
    counter: RecentRuns | None = getattr(app.state, "recent_runs", None)
    typical: dict[int, Typical] = {}
    recent: dict[int, Recent] = {}
    for number in pump_settings:
        typical[number] = (
            Typical()
            if history is None
            else await history.typical(pool, clamp[number], domain.RUNNING_AMPS)
        )
        # The site's own timezone, because today is a word about where the pit
        # is and not about where the server is.
        recent[number] = (
            Recent()
            if counter is None
            else await counter.recent(pool, clamp[number], domain.RUNNING_AMPS, store.site.timezone)
        )

    def pump_state(number: int) -> dict:
        channel = clamp[number]
        sample = live.samples.get(channel)
        settings = pump_settings[number]
        current = sample.current if sample else None
        drawing = current is not None and current >= domain.RUNNING_AMPS
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
            # Still only the clamp. The panel's run contact used to be
            # reported beside this, and the disagreement between them is the
            # interesting part: a closed contactor drawing nothing is a motor
            # that is not turning. That needed a way to point at the input
            # carrying the run contact, which did not exist while inputs were
            # free text. It does now, as inputs.channel_for("pump1_run"), so
            # this is buildable whenever the detector wants it. See NOTES.md.
            "drawing_current": drawing,
            "running": drawing,
            "typical": typical[number].as_json(),
            # The query behind this is cached for a minute, which is right for
            # a count and wrong for a clock. The live state knows exactly when
            # the current last rose, so it wins whenever it is newer.
            "recent": _with_live_rise(recent[number], live.rose_at(channel)),
        }

    inputs = store.inputs
    # The inputs a lamp is drawn from, which is simply the ones that have been
    # told what they carry. It used to be the set named on a second page, and
    # the two could disagree.
    assigned = {mapped.channel for mapped in inputs.used_channels}

    signals: SignalHistory | None = getattr(app.state, "signal_history", None)
    closings = await signals.closings(pool, sorted(assigned)) if signals else {}

    return {
        "site": store.site.model_dump(mode="json"),
        "pumps": {"1": pump_state(1), "2": pump_state(2)},
        "panel": panel_state(inputs, live_io, closings),
        # No list of inputs carrying nothing. The panel brings out eight
        # contacts and the module has eight inputs, so every one of them is a
        # lamp or a run signal and the list was always empty. An input with no
        # meaning assigned is still read, debounced and recorded; it simply has
        # nowhere on a dashboard to be shown, which is what taking its meaning
        # away asked for.
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
        return JSONResponse({"ok": False, "error": "Enter a broker address first"}, status_code=400)

    try:
        return JSONResponse(await shelly_ingest.probe(settings))
    except shelly_ingest.ShellyAuthError as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=200)
    except (shelly_ingest.ShellyError, OSError, TimeoutError) as error:
        return JSONResponse(
            {"ok": False, "error": f"Could not reach {settings.host}: {error}"}, status_code=200
        )


@router.post("/test/inputs", include_in_schema=False)
async def test_inputs(request: Request) -> JSONResponse:
    """Listen for one published body and report it, raw and interpreted.

    The setup page calls this while the channel map is open, so someone at the
    panel can lift a float by hand and watch a row change. That is by far the
    fastest way to get the mapping right, and reading the wire labels is how it
    ends up wrong.

    It waits rather than asks, because there is nothing to ask: the module
    publishes on change, so if nothing moves there is nothing to hear.
    """
    form = await request.form()
    store: SettingsStore = request.app.state.settings
    try:
        settings = forms.inputs_from(form, store.inputs)
    except (ValueError, ValidationError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

    if not settings.host:
        return JSONResponse({"ok": False, "error": "Enter a broker address first"}, status_code=400)

    result = await inputs_ingest.probe(settings)
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
        reply = await email_sender.send(
            settings,
            to,
            f"PitWatch test{f' from {site.where}' if site.where else ''}",
            "This is a test from PitWatch.\n\n"
            f"If you are reading it, alerts for {where} will reach this address.\n\n"
            "Nothing is wrong with the pumps. Nobody needs to do anything.\n",
        )
    except email_sender.EmailError as error:
        return JSONResponse({"ok": False, "error": str(error)})
    # The server's reply, verbatim. On SES it carries the message id, which is
    # the only thing that makes a message the server accepted and never
    # delivered searchable afterwards.
    return JSONResponse(
        {
            "ok": True,
            "detail": f"Accepted for {to}. The server said: {reply}",
            "note": (
                "Accepted is not delivered. If it does not arrive, that id is "
                "what to search for at the other end."
            ),
        }
    )


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
