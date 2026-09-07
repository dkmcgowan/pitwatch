"""Turning a published body into a number or a set of contact states.

This is the layer that lets PitWatch stop knowing what hardware it is talking
to. Everything above it deals in "channel 3 closed" and "pump 1 is drawing 15.4
amps"; everything below it is a topic and some JSON somebody else chose the
shape of.

**Nothing here is named after a device.** There was a ``shelly_em1`` profile in
the first draft of this file and it was a mistake: it named where a shape came
from rather than what the shape is, which is the same error as having a section
of settings called "the Shelly". It also turned out not to be a shape at all.
All it did was try three paths in turn, because a Shelly EM channel publishes
the same reading in three envelopes:

===========================  ==============================
``status/em1:0`` frame       ``current``
an RPC reply                 ``result.current``
an ``events/rpc`` notify     ``params.em1:0.current``
===========================  ==============================

Those are three settings, not three code paths. Once a source can name a reply
topic separately from the topic it subscribes to, a dotted path reaches all of
them and there is no vendor left in the parser.

So there are three profiles, and each is a statement about shape:

``number``
    A number, at a dotted path. ``power.l1.amps`` reaches into nested objects
    and an empty path takes the body itself, for a device that publishes a bare
    number.
``contact_map``
    Several contacts in one body, keyed by input number. Forgiving about how
    the keys are spelled, because a body like this is usually typed into a
    device by hand and there are several reasonable ways to write it.
``contact``
    One contact, on or off, as its own topic. Understands the spellings people
    actually use, and does not insist on JSON: a device publishing one input
    per topic usually publishes a word.

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
    ("number", "A number, at a path"),
    ("contact_map", "Several contacts in one body"),
    ("contact", "One contact, on or off"),
)

VALUE_PROFILES = {"number"}
CONTACT_PROFILES = {"contact_map", "contact"}


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


def value(profile: str, payload: str, path: str = "") -> float | None:
    """One number, or None when this frame does not carry one.

    None rather than an exception for a missing path. A device that publishes
    several frame shapes on one topic is a device working normally, and a meter
    that sends a voltage-only frame has not failed, it has sent a frame with no
    current in it. Raising there would fill the log with the hardware behaving.
    """
    if profile not in VALUE_PROFILES:
        raise PayloadError(f"{profile!r} does not read a number")
    return _as_number(_at_path(_body(payload), path))


# -- contacts ----------------------------------------------------------------


def contact_states(profile: str, payload: str, path: str = "") -> dict[int, bool]:
    """Every contact state this frame carries, keyed by input number.

    A dict rather than one value even for the single contact profiles, so that
    everything above this reads the same shape whether the device publishes all
    eight together or one per topic. The caller supplies the channel number for
    a single contact topic, so this returns it under 1 and the caller renumbers.
    """
    if profile == "contact_map":
        return _contact_map(payload)
    if profile == "contact":
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
# writes when naming eight of them. One module in use spells its own token
# ${digitalInput1}, which is why that shape is here; it is not the only one.
_INPUT_KEY = re.compile(r"^(?:digital[\s_-]*input|input|di|in|channel|ch)?[\s_-]*(\d+)$")
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def _contact_map(payload: str) -> dict[int, bool]:
    """Every contact in one published body, keyed by input number.

    Forgiving about shape, because a body like this is usually typed into a
    device by hand and there are several reasonable ways to write it. What is
    not forgiven is a key nobody can map to an input, which is dropped rather
    than guessed at.
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

    Two passes, and the second is the interesting one. A module offers tokens
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
    "CONTACT_PROFILES",
    "INPUT_COUNT",
    "PROFILES",
    "VALUE_PROFILES",
    "PayloadError",
    "contact_states",
    "state_from",
    "value",
]
