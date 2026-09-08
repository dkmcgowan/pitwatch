"""Reading a published body: a number, or on and off.

Every meter payload here was captured off the real broker on 2026-09-07, from
the meter on the real pit, rather than written from the documentation. The
documentation says what a device should publish; these are what one did.

Nothing in this file names a vendor except to say which capture a body came
from. A shape belongs to whoever publishes it.
"""

from __future__ import annotations

import pytest

from pitwatch.ingest import payloads

# The three envelopes one meter publishes the same reading in, on three topics.
STATUS_FRAME = (
    '{"id":0,"voltage":117.6,"current":16.714,"act_power":-1899.8,'
    '"aprt_power":1974.5,"pf":0.82,"freq":60.0,"calibration":"factory"}'
)

RPC_REPLY = (
    '{"id":1,"src":"shellyemg3-dcb4d9c564f4","dst":"pitwatch-c1",'
    '"result":{"id":0,"voltage":121.0,"current":15.487,"act_power":-1760.2}}'
)

NOTIFY_FRAME = (
    '{"src":"shellyemg3-dcb4d9c564f4","dst":"shellyemg3/events",'
    '"method":"NotifyStatus","params":{"ts":1788820998.37,'
    '"em1:0":{"id":0,"current":16.714,"act_power":-1899.8,"voltage":117.6}}}'
)


# -- a number ----------------------------------------------------------------


def test_all_three_envelopes_are_reachable_by_path_alone():
    """The argument that deleted the vendor profile, and then the profile
    concept entirely. One meter publishes the same reading three ways. Those
    are three settings, not three code paths."""
    assert payloads.value(STATUS_FRAME, "current") == pytest.approx(16.714)
    assert payloads.value(RPC_REPLY, "result.current") == pytest.approx(15.487)
    assert payloads.value(NOTIFY_FRAME, "params.em1:0.current") == pytest.approx(16.714)


def test_a_path_segment_may_contain_a_colon():
    """`em1:0` is one key, not two. Only the dot separates."""
    assert payloads.value('{"a:0":{"b":7}}', "a:0.b") == 7


def test_a_frame_with_nothing_at_the_path_is_not_an_error():
    """A meter publishes voltage-only and system frames on the same wire. A
    frame with no reading in it is the device working, and treating it as a
    failure would fill the log with correct behavior."""
    assert payloads.value('{"id":0,"voltage":118.1}', "current") is None
    assert payloads.value('{"mac":"DCB4D9C564F4"}', "current") is None
    assert payloads.value('{"power":{}}', "power.l1.amps") is None


def test_a_bare_number_needs_no_path():
    """For a device that publishes the value and nothing else."""
    assert payloads.value("15.4") == pytest.approx(15.4)


def test_a_number_written_as_a_string_still_reads():
    """Plenty of devices publish "15.4" rather than 15.4."""
    assert payloads.value('{"a":"15.4"}', "a") == pytest.approx(15.4)


def test_true_is_not_a_number():
    """bool is an int in Python, and a contact that closed is not one amp."""
    assert payloads.value('{"a":true}', "a") is None


def test_a_path_can_index_a_list():
    assert payloads.value('{"ch":[{"a":1.5},{"a":2.5}]}', "ch.1.a") == 2.5


def test_a_body_that_is_not_json_reads_as_nothing_rather_than_raising():
    """A number is being asked for. Rubbish arriving on a clamp topic is a
    misconfigured device, and the next message may be fine."""
    assert payloads.value("<html>no</html>", "current") is None


def test_which_figure_to_read_is_the_callers_choice():
    """It used to be hardcoded to current, on the grounds that this meter's
    voltage reference is its own supply rather than the phase the clamps are
    around, so its real power is not a measurement of the motor. The captured
    frame shows it: 16.7 A drawn against negative real power, because the CT is
    clamped on backwards.

    That reasoning is about one installation's wiring, so it belongs in the
    setting rather than in the parser.
    """
    assert payloads.value(STATUS_FRAME, "current") == pytest.approx(16.714)
    assert payloads.value(STATUS_FRAME, "act_power") == pytest.approx(-1899.8)


# -- on and off --------------------------------------------------------------


def test_a_contact_on_its_own_topic():
    """One topic per contact, which is what a contact is. Eight states in one
    body was what one module happened to publish, and reading it cost a parser
    that had to guess how somebody had spelled eight keys."""
    for body in ("1", "true", "ON", '"closed"', "yes", "high"):
        assert payloads.state(body) is True, body
    for body in ("0", "false", "off", '"open"', "no", "low"):
        assert payloads.state(body) is False, body


def test_a_contact_that_is_not_json_at_all():
    """A device publishing one contact per topic usually publishes a word
    rather than an object, and requiring JSON of it would be requiring it to be
    a different device."""
    assert payloads.state("on") is True
    assert payloads.state("off") is False


def test_a_contact_wrapped_in_an_object():
    assert payloads.state('{"state":{"on":true}}', "state.on") is True
    assert payloads.state('{"value":0}', "value") is False


def test_a_body_that_says_nothing_useful_is_neither():
    """None is not False. A lamp reading off when nothing has read it is an
    alarm that will never fire and will look like it is working."""
    assert payloads.state("maybe") is None
    assert payloads.state('{"other":1}', "state") is None


# -- what is left -------------------------------------------------------------


def test_nothing_here_is_named_after_a_device():
    """There were profiles named shelly_em1 and x408_inputs, then three named
    after shapes, and now none at all: if a clamp is always a number and a
    contact is always on or off, a dropdown asking which has exactly one right
    answer per kind."""
    import ast
    from pathlib import Path

    source = Path("pitwatch/ingest/payloads.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Docstrings explain where a shape came from, which is allowed to name it.
    # Code is not.
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value.value = ""
    code = ast.unparse(tree)
    for vendor in ("shelly", "x408", "controlbyweb", "tasmota", "em1"):
        assert vendor not in code.lower(), vendor
    assert '"current"' not in code, "reading a vendor's key name is a vendor profile"
    assert not hasattr(payloads, "PROFILES"), "the concept went with them"
