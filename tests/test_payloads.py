"""Reading a published body without knowing what published it.

Every meter payload here was captured off the real broker on 2026-09-07, from
the meter on the real pit, rather than written from the documentation. The
documentation says what a device should publish; these are what one did.

Nothing in this file names a vendor except to say which capture a body came
from. That is the point of the layer: a profile is a statement about shape, and
a shape belongs to whoever publishes it.
"""

from __future__ import annotations

import pytest

from pitwatch.ingest import payloads

# -- the three envelopes one reading arrives in ------------------------------
#
# Same meter, same measurement, three shapes, on three topics. There was a
# profile that tried all three paths in turn; there is not any more, because
# three paths is three settings rather than three code paths.

STATUS_FRAME = (
    '{"id":0,"voltage":117.6,"current":16.714,"act_power":-1899.8,'
    '"aprt_power":1974.5,"pf":0.82,"freq":60.0,"calibration":"factory"}'
)

RPC_REPLY = (
    '{"id":1,"src":"shellyemg3-dcb4d9c564f4","dst":"pitwatch-test",'
    '"result":{"id":0,"voltage":121.0,"current":15.487,"act_power":-1760.2,'
    '"aprt_power":1827.1,"pf":0.96,"freq":60.0}}'
)

NOTIFY_FRAME = (
    '{"src":"shellyemg3-dcb4d9c564f4","dst":"shellyemg3/events",'
    '"method":"NotifyStatus","params":{"ts":1788820998.37,'
    '"em1:0":{"id":0,"current":16.714,"act_power":-1899.8,"voltage":117.6}}}'
)

CONTACT_BODY = '{"1":"1","2":"0","3":"1","4":"0","5":"0","6":"0","7":"0","8":"0"}'


def test_all_three_envelopes_are_reachable_by_path_alone():
    """The whole argument for deleting the vendor profile.

    One meter publishes the same reading three ways on three topics. Those are
    three settings, not three code paths, and once a source can name a reply
    topic separately from the one it subscribes to, a dotted path reaches all
    of them with no vendor left in the parser.
    """
    assert payloads.value("number", STATUS_FRAME, "current") == pytest.approx(16.714)
    assert payloads.value("number", RPC_REPLY, "result.current") == pytest.approx(15.487)
    assert payloads.value("number", NOTIFY_FRAME, "params.em1:0.current") == pytest.approx(16.714)


def test_a_path_segment_may_contain_a_colon():
    """`em1:0` is one key, not two. Only the dot separates."""
    assert payloads.value("number", '{"a:0":{"b":7}}', "a:0.b") == 7


def test_a_frame_with_nothing_at_the_path_is_not_an_error():
    """The meter publishes voltage-only and system frames on the same wire. A
    frame that does not carry a reading is the device working, and treating it
    as a failure would fill the log with correct behavior."""
    assert payloads.value("number", '{"id":0,"voltage":118.1}', "current") is None
    assert payloads.value("number", '{"mac":"DCB4D9C564F4"}', "current") is None


def test_reading_current_rather_than_power_is_the_caller_s_choice_now():
    """It used to be hardcoded, on the grounds that this meter's voltage
    reference is its own supply rather than the phase the clamps are around, so
    its real power is not a measurement of the motor. The captured frame shows
    it: 16.7 A drawn against negative real power, because the CT is clamped on
    backwards.

    That reasoning is about one installation's wiring, so it belongs in the
    setting rather than in the parser. The parser reads the path it is given.
    """
    assert payloads.value("number", STATUS_FRAME, "current") == pytest.approx(16.714)
    assert payloads.value("number", STATUS_FRAME, "act_power") == pytest.approx(-1899.8)


def test_rubbish_that_is_not_json_says_so():
    with pytest.raises(payloads.PayloadError, match="Not JSON"):
        payloads.value("number", "<html>no</html>", "current")


# -- the generic number profile ----------------------------------------------


def test_a_number_at_a_dotted_path():
    """For hardware nobody has written a profile for. A path rather than a
    template language: a dropdown that covers the case beats a language that
    covers every case and is empty by default."""
    assert payloads.value("number", '{"power":{"l1":{"amps":9.5}}}', "power.l1.amps") == 9.5


def test_a_path_that_is_not_there_is_nothing_rather_than_a_crash():
    assert payloads.value("number", '{"power":{}}', "power.l1.amps") is None


def test_a_bare_number_needs_no_path():
    assert payloads.value("number", "15.4") == pytest.approx(15.4)


def test_a_number_written_as_a_string_still_reads():
    """Plenty of devices publish "15.4" rather than 15.4."""
    assert payloads.value("number", '{"a":"15.4"}', "a") == pytest.approx(15.4)


def test_true_is_not_a_number():
    """bool is an int in Python, and a contact that closed is not one amp."""
    assert payloads.value("number", '{"a":true}', "a") is None


def test_a_path_can_index_a_list():
    assert payloads.value("number", '{"ch":[{"a":1.5},{"a":2.5}]}', "ch.1.a") == 2.5


# -- contacts ----------------------------------------------------------------


def test_the_real_captured_contact_body():
    """Captured off the broker: pump 1 running, system alert healthy."""
    states = payloads.contact_states("contact_map", CONTACT_BODY)

    assert states == {1: True, 2: False, 3: True, 4: False, 5: False, 6: False, 7: False, 8: False}


def test_a_contact_body_however_somebody_spelled_the_keys():
    """The body is typed into the device by hand and there are several
    reasonable ways to write it."""
    for body in (
        '{"digitalInput1": 1, "digitalInput2": 0}',
        '{"di1": true, "di2": false}',
        '{"input 1": "on", "input 2": "off"}',
        '{"ch1": "closed", "ch2": "open"}',
    ):
        assert payloads.contact_states("contact_map", body) == {1: True, 2: False}, body


def test_a_key_that_is_not_an_input_is_dropped_rather_than_guessed_at():
    """The device offers tokens for things that are not inputs and some of them
    end in a digit. Reading digits out of every key would file a register under
    input 1 with nothing to notice it by."""
    states = payloads.contact_states("contact_map", '{"digitalInput1":1,"register1":99,"relay1":1}')

    assert states == {1: True}


def test_a_body_that_named_nothing_an_input_is_read_loosely():
    """Somebody who labeled the keys after the floats rather than the inputs.
    Kept only for a body where the strict pass found nothing at all."""
    states = payloads.contact_states("contact_map", '{"leadFloat1":1,"lagFloat2":0}')

    assert states == {1: True, 2: False}


def test_a_body_with_no_inputs_at_all_says_so():
    with pytest.raises(payloads.PayloadError, match="No input states"):
        payloads.contact_states("contact_map", '{"uptime": 178845}')


def test_one_contact_on_its_own_topic():
    """For hardware that publishes one input per topic rather than all of them
    together, which is the other common arrangement."""
    for body in ("1", "true", "ON", '"closed"'):
        assert payloads.contact_states("contact", body) == {1: True}, body
    for body in ("0", "false", "off", '"open"'):
        assert payloads.contact_states("contact", body) == {1: False}, body


def test_one_contact_that_is_not_json_at_all():
    """A device publishing one contact per topic usually publishes a word
    rather than an object, and requiring JSON of it would be requiring it to be
    a different device."""
    assert payloads.contact_states("contact", "on") == {1: True}
    assert payloads.contact_states("contact", "off") == {1: False}


def test_one_contact_at_a_path():
    assert payloads.contact_states("contact", '{"state":{"on":true}}', "state.on") == {1: True}


# -- the profiles are kept apart ---------------------------------------------


def test_a_value_profile_cannot_be_asked_for_contacts():
    """The settings page offers the right list for each kind, and the parser
    refuses the wrong one rather than returning something plausible."""
    with pytest.raises(payloads.PayloadError, match="does not read contacts"):
        payloads.contact_states("number", STATUS_FRAME)

    with pytest.raises(payloads.PayloadError, match="does not read a number"):
        payloads.value("contact_map", CONTACT_BODY)


def test_every_profile_offered_is_one_of_the_two_kinds():
    """A profile in the dropdown that no reader knows how to use is a setting
    somebody can save and then wonder why nothing arrives."""
    offered = {name for name, _ in payloads.PROFILES}

    assert offered == payloads.VALUE_PROFILES | payloads.CONTACT_PROFILES
    assert not payloads.VALUE_PROFILES & payloads.CONTACT_PROFILES


def test_no_profile_is_named_after_a_device():
    """A profile names a shape. Naming where a shape came from is the same
    mistake as a section of settings called "the Shelly", and it is the mistake
    this layer exists to undo."""
    from pathlib import Path

    names = " ".join(name for name, _ in payloads.PROFILES)
    labels = " ".join(label for _, label in payloads.PROFILES).lower()
    for vendor in ("shelly", "x408", "controlbyweb", "tasmota", "em1"):
        assert vendor not in names, vendor
        assert vendor not in labels, vendor

    # And nothing in the module reaches for a vendor's key names either.
    source = Path("pitwatch/ingest/payloads.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"""', "``"))
    )
    assert '"current"' not in code, "reading a vendor's key name is a vendor profile"
