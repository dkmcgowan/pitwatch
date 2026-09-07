"""Turning a published body into a number or a set of contact states.

This is the layer that lets PitWatch stop knowing what hardware it is talking
to. Everything above it deals in "channel 3 closed" and "pump 1 is drawing 15.4
amps"; everything below it is a topic and some JSON somebody else chose the
shape of.

**Profiles rather than templates.** The obvious design is a path expression per
topic, the way Home Assistant does it, and it is also the part of Home Assistant
people complain about most: a template language is a second thing to get wrong,
in a text box, with no way to test it except by waiting for a message. So the
common shapes are named instead, and there is one generic profile with a dotted
path for everything else. A dropdown that covers the case beats a language that
covers every case and is empty by default.

Four profiles, and the first two are the two devices this was built against:

``shelly_em1``
    A Shelly Gen2 or Gen3 energy meter channel. ``{"id":0,"current":16.714,...}``,
    which is what arrives on ``<prefix>/status/em1:0`` and inside the ``result``
    of an ``EM1.GetStatus`` reply.
``x408_inputs``
    A ControlByWeb X-408 publishing all eight inputs in one body. Forgiving
    about how the keys are spelled, because the body is typed into the device by
    hand.
``number``
    Any JSON, with a dotted path to the value. ``power.total`` reaches into
    nested objects, and a bare path reads a top level key. For anything that is
    already just a number, leave the path empty.
``on_off``
    A single contact as its own topic, for hardware that publishes one input per
    topic rather than all of them together. Understands the spellings people
    actually use.

A profile returns None rather than raising when a body simply does not carry
what was asked for. A meter that publishes a voltage-only frame is not an error,
it is a frame with no current in it, and treating that as a failure would fill
the log with the device working correctly.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# The eight inputs a panel module brings out. Same limit as the settings model:
# this is a duplex pump panel, not a PLC.
INPUT_COUNT = 8

PROFILES: tuple[tuple[str, str], ...] = (
    ("shelly_em1", "Shelly EM channel"),
    ("number", "A number, at a path"),
    ("x408_inputs", "X-408, all inputs in one body"),
    ("on_off", "One contact, on or off"),
)

CLAMP_PROFILES = {"shelly_em1", "number"}
CONTACT_PROFILES = {"x408_inputs", "on_off"}


class PayloadError(Exception):
    """A body that cannot be read at all, as opposed to one with nothing in it."""


def _body(payload: str) -> object:
    try:
        return json.loads(payload)
    except (ValueError, TypeError) as error:
        raise PayloadError(f"Not JSON: {payload[:80]!r}") from error


def _at_path(body: object, path: str) -> object:
    """Walk a dotted path, or return the body itself when the path is empty.

    A missing key is None rather than an error. A path that points somewhere
    that does not exist in this particular frame is the normal case for a
    device that publishes several frame shapes on one topic.
    """
    found = body
    for step in (part for part in path.split(".") if part):
        if isinstance(found, dict):
            found = found.get(step)
        elif isinstance(found, list) and step.lstrip("-").isdigit():
            index = int(step)
            found = found[index] if -len(found) <= index < len(found) else None
        else:
            return None
        if found is None:
            return None
    return found


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# What people write for a contact that is closed, and for one that is not.
_TRUE = {"1", "true", "on", "yes", "closed", "high", "active"}
_FALSE = {"0", "false", "off", "no", "open", "low", "inactive"}


def state_from(value: object) -> bool | None:
    """One contact's state, from whatever the device felt like publishing."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    return None


# -- clamps ------------------------------------------------------------------


def clamp_value(profile: str, payload: str, path: str = "") -> float | None:
    """One current reading, or None when this frame does not carry one."""
    if profile == "shelly_em1":
        return _shelly_em1(payload)
    if profile == "number":
        return _as_number(_at_path(_body(payload), path))
    raise PayloadError(f"{profile!r} does not read a clamp")


def _shelly_em1(payload: str) -> float | None:
    """A Shelly EM channel's current.

    Two shapes arrive on the same wire and both are handled here rather than
    being two profiles, because they are the same device saying the same thing.
    A status frame is the component's own object; an RPC reply wraps it in
    ``result``, and a notification wraps it under ``params`` keyed by component.

    Current rather than power on purpose. This meter's voltage reference is its
    own supply rather than the phase the clamps are around, so real power here
    is not a measurement of the motor. A CT measures the conductor directly and
    does not care, which is why current is the only figure this reads.
    """
    body = _body(payload)
    if not isinstance(body, dict):
        return None

    # An RPC reply: {"id":1,"result":{"id":0,"current":...}}
    inner = body.get("result")
    if isinstance(inner, dict) and "current" in inner:
        return _as_number(inner.get("current"))

    # A NotifyStatus frame: {"params":{"em1:0":{"current":...}}}
    params = body.get("params")
    if isinstance(params, dict):
        for key, value in params.items():
            if key.startswith("em1:") and isinstance(value, dict) and "current" in value:
                return _as_number(value.get("current"))

    # A plain status frame, which is what <prefix>/status/em1:0 carries.
    if "current" in body:
        return _as_number(body.get("current"))
    return None


# -- contacts ----------------------------------------------------------------


def contact_states(profile: str, payload: str, path: str = "") -> dict[int, bool]:
    """Every contact state this frame carries, keyed by input number.

    A dict rather than one value even for the single contact profiles, so that
    everything above this reads the same shape whether the device publishes all
    eight together or one per topic. The caller supplies the channel number for
    a single contact topic, so this returns it under 1 and the caller renumbers.
    """
    if profile == "x408_inputs":
        return _x408(payload)
    if profile == "on_off":
        state = state_from(_at_path(_body(payload), path) if path else _raw(payload))
        return {} if state is None else {1: state}
    raise PayloadError(f"{profile!r} does not read contacts")


def _raw(payload: str) -> object:
    """A body that may be JSON or may be the bare word ``on``.

    A device publishing one contact per topic usually publishes a word rather
    than an object, and requiring JSON of it would be requiring it to be a
    different device.
    """
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


# A key that is plainly an input: a bare number, or one of the shapes somebody
# writes when naming eight of them. The X-408's own token for an input is
# ${digitalInput1}, so that spelling is the likely one.
_INPUT_KEY = re.compile(r"^(?:digital[\s_-]*input|input|di|in|channel|ch)?[\s_-]*(\d+)$")
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def _x408(payload: str) -> dict[int, bool]:
    """All eight inputs out of one published body.

    Forgiving about shape, because the body is typed into the device by hand and
    there are several reasonable ways to write it. What is not forgiven is a key
    nobody can map to an input, which is dropped rather than guessed at.
    """
    body = _body(payload)
    if not isinstance(body, dict):
        raise PayloadError(f"Expected an object, got {type(body).__name__}")

    states: dict[int, bool] = {}
    for key, value in _channels_in(body).items():
        state = state_from(value)
        if state is not None:
            states[key] = state
    if not states:
        raise PayloadError(f"No input states in {payload[:80]!r}")
    return states


def _channels_in(body: dict) -> dict[int, object]:
    """Which entries in a body are inputs, and which input each one is.

    Two passes, and the second is the interesting one. The device offers tokens
    for things that are not inputs and some of them end in a digit:
    ``${register1}``, ``${relay1}``. Reading digits out of every key would file
    a register under input 1 and there would be nothing to notice it by. So a
    body with keys that plainly name inputs is read strictly, and the loose
    reading is kept only for a body where nothing named an input at all, which
    is what somebody who labeled the keys after the floats has written.
    """
    strict: dict[int, object] = {}
    for key, value in body.items():
        match = _INPUT_KEY.match(str(key).strip().lower())
        if match:
            number = int(match.group(1))
            if 1 <= number <= INPUT_COUNT:
                strict[number] = value
    if strict:
        return strict

    loose: dict[int, object] = {}
    for key, value in body.items():
        found = _TRAILING_NUMBER.search(str(key))
        if found:
            number = int(found.group(1))
            if 1 <= number <= INPUT_COUNT:
                loose[number] = value
    return loose


__all__ = [
    "CLAMP_PROFILES",
    "CONTACT_PROFILES",
    "INPUT_COUNT",
    "PROFILES",
    "PayloadError",
    "clamp_value",
    "contact_states",
    "state_from",
]
