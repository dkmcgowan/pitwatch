"""Turning a published body into a number, or into on and off.

Two functions, because a pump panel asks two kinds of question of a topic and
there is no third. What a pump is drawing is a number. What a contact is doing
is on or off. Everything above this deals in those; everything below is a topic
and some JSON somebody else chose the shape of.

**There were profiles here and they are gone.** First a `shelly_em1` one, which
was never a shape: it tried three paths in turn because one meter publishes the
same reading in three envelopes, and three paths is three settings. Then three
shape named ones, which lasted until the obvious question got asked. If a clamp
is always a number and a contact is always on or off, a dropdown asking which
has exactly one right answer per kind, and a setting with one right answer is a
setting that should not exist.

What survives is the path: dots step into nested objects, so `result.current`
reaches into an RPC reply and `params.em1:0.current` into a notification. An
empty path takes the body itself, which is what a module publishing `1` or `on`
sends.

Nothing here is named after a device, and there is a test that keeps it that
way. A shape belongs to whoever publishes it.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


class PayloadError(Exception):
    """A body that cannot be read at all, as opposed to one with nothing in it."""


def _body(payload: str) -> object:
    """The body as JSON, or as itself.

    A device publishing one contact per topic usually publishes a word rather
    than an object, and requiring JSON of it would be requiring it to be a
    different device.
    """
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


def _at_path(body: object, path: str) -> object:
    """Walk a dotted path, or return the body itself when the path is empty.

    A missing key is None rather than an error. A path that points somewhere
    this particular frame does not have is the normal case for a device that
    publishes several shapes on one topic.
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


def value(payload: str, path: str = "") -> float | None:
    """One number, or None when this frame does not carry one.

    None rather than an exception. A meter that sends a voltage only frame has
    not failed, it has sent a frame with no current in it, and raising there
    would fill the log with the hardware behaving.
    """
    found = _at_path(_body(payload), path)
    if isinstance(found, bool):
        # bool is an int in Python, and a contact that closed is not one amp.
        return None
    if isinstance(found, int | float):
        return float(found)
    if isinstance(found, str):
        try:
            return float(found.strip())
        except ValueError:
            return None
    return None


# What people write for a contact that is closed, and for one that is not.
_TRUE = {"1", "true", "on", "yes", "closed", "high", "active"}
_FALSE = {"0", "false", "off", "no", "open", "low", "inactive"}


def state(payload: str, path: str = "") -> bool | None:
    """One contact, or None when this frame does not say."""
    return state_from(_at_path(_body(payload), path))


def state_from(found: object) -> bool | None:
    """A contact's state, from whatever the device felt like publishing."""
    if isinstance(found, bool):
        return found
    if isinstance(found, int | float):
        return bool(found)
    if isinstance(found, str):
        word = found.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    return None


__all__ = ["PayloadError", "state", "state_from", "value"]
