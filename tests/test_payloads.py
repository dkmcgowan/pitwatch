"""Reading a published body without knowing what published it.

Every Shelly payload here was captured off the real broker on 2026-09-07, from
the meter on the real pit, rather than written from the documentation. The
documentation says what a device should publish; these are what one did.
"""

from __future__ import annotations

import pytest

from pitwatch.ingest import payloads

# -- the three shapes a Shelly EM channel arrives in -------------------------
#
# Same device, same reading, three envelopes. They are one profile rather than
# three because they are one device saying one thing.

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

X408_BODY = '{"1":"1","2":"0","3":"1","4":"0","5":"0","6":"0","7":"0","8":"0"}'


def test_a_status_frame_gives_its_current():
    assert payloads.clamp_value("shelly_em1", STATUS_FRAME) == pytest.approx(16.714)


def test_an_rpc_reply_gives_its_current():
    """The reply to a forced reading wraps the same object in `result`. It is
    the same measurement and it goes to the same place."""
    assert payloads.clamp_value("shelly_em1", RPC_REPLY) == pytest.approx(15.487)


def test_a_notification_gives_its_current():
    """NotifyStatus keys the component under `params`, which is a third
    envelope around the identical object."""
    assert payloads.clamp_value("shelly_em1", NOTIFY_FRAME) == pytest.approx(16.714)


def test_a_frame_with_no_current_in_it_is_not_an_error():
    """The meter publishes voltage-only and system frames on the same wire. A
    frame that does not carry a reading is the device working, and treating it
    as a failure would fill the log with correct behavior."""
    assert payloads.clamp_value("shelly_em1", '{"id":0,"voltage":118.1}') is None
    assert payloads.clamp_value("shelly_em1", '{"mac":"DCB4D9C564F4"}') is None


def test_current_is_read_and_power_is_not():
    """This meter's voltage reference is its own supply rather than the phase
    the clamps are around, so real power here is not a measurement of the
    motor. The captured frame proves the point: 16.7 A of current against
    negative real power, because the CT is clamped on backwards. A CT measures
    the conductor directly and does not care, which is why current is the only
    figure read."""
    assert payloads.clamp_value("shelly_em1", STATUS_FRAME) > 0

    # And nothing anywhere reaches for the signed figures.
    from pathlib import Path

    source = Path("pitwatch/ingest/payloads.py").read_text(encoding="utf-8")
    body = source.split("def _shelly_em1", 1)[1].split("def contact_states", 1)[0]
    assert "act_power" not in body.replace("real power here", "")


def test_rubbish_that_is_not_json_says_so():
    with pytest.raises(payloads.PayloadError, match="Not JSON"):
        payloads.clamp_value("shelly_em1", "<html>no</html>")


# -- the generic number profile ----------------------------------------------


def test_a_number_at_a_dotted_path():
    """For hardware nobody has written a profile for. A path rather than a
    template language: a dropdown that covers the case beats a language that
    covers every case and is empty by default."""
    assert payloads.clamp_value("number", '{"power":{"l1":{"amps":9.5}}}', "power.l1.amps") == 9.5


def test_a_path_that_is_not_there_is_nothing_rather_than_a_crash():
    assert payloads.clamp_value("number", '{"power":{}}', "power.l1.amps") is None


def test_a_bare_number_needs_no_path():
    assert payloads.clamp_value("number", "15.4") == pytest.approx(15.4)


def test_a_number_written_as_a_string_still_reads():
    """Plenty of devices publish "15.4" rather than 15.4."""
    assert payloads.clamp_value("number", '{"a":"15.4"}', "a") == pytest.approx(15.4)


def test_true_is_not_a_number():
    """bool is an int in Python, and a contact that closed is not one amp."""
    assert payloads.clamp_value("number", '{"a":true}', "a") is None


def test_a_path_can_index_a_list():
    assert payloads.clamp_value("number", '{"ch":[{"a":1.5},{"a":2.5}]}', "ch.1.a") == 2.5


# -- contacts ----------------------------------------------------------------


def test_the_real_x408_body():
    """Captured off the broker: pump 1 running, system alert healthy."""
    states = payloads.contact_states("x408_inputs", X408_BODY)

    assert states == {1: True, 2: False, 3: True, 4: False, 5: False, 6: False, 7: False, 8: False}


def test_the_x408_body_however_somebody_spelled_the_keys():
    """The body is typed into the device by hand and there are several
    reasonable ways to write it."""
    for body in (
        '{"digitalInput1": 1, "digitalInput2": 0}',
        '{"di1": true, "di2": false}',
        '{"input 1": "on", "input 2": "off"}',
        '{"ch1": "closed", "ch2": "open"}',
    ):
        assert payloads.contact_states("x408_inputs", body) == {1: True, 2: False}, body


def test_a_key_that_is_not_an_input_is_dropped_rather_than_guessed_at():
    """The device offers tokens for things that are not inputs and some of them
    end in a digit. Reading digits out of every key would file a register under
    input 1 with nothing to notice it by."""
    states = payloads.contact_states("x408_inputs", '{"digitalInput1":1,"register1":99,"relay1":1}')

    assert states == {1: True}


def test_a_body_that_named_nothing_an_input_is_read_loosely():
    """Somebody who labeled the keys after the floats rather than the inputs.
    Kept only for a body where the strict pass found nothing at all."""
    states = payloads.contact_states("x408_inputs", '{"leadFloat1":1,"lagFloat2":0}')

    assert states == {1: True, 2: False}


def test_a_body_with_no_inputs_at_all_says_so():
    with pytest.raises(payloads.PayloadError, match="No input states"):
        payloads.contact_states("x408_inputs", '{"uptime": 178845}')


def test_one_contact_on_its_own_topic():
    """For hardware that publishes one input per topic rather than all of them
    together, which is the other common arrangement."""
    for body in ("1", "true", "ON", '"closed"'):
        assert payloads.contact_states("on_off", body) == {1: True}, body
    for body in ("0", "false", "off", '"open"'):
        assert payloads.contact_states("on_off", body) == {1: False}, body


def test_one_contact_that_is_not_json_at_all():
    """A device publishing one contact per topic usually publishes a word
    rather than an object, and requiring JSON of it would be requiring it to be
    a different device."""
    assert payloads.contact_states("on_off", "on") == {1: True}
    assert payloads.contact_states("on_off", "off") == {1: False}


def test_one_contact_at_a_path():
    assert payloads.contact_states("on_off", '{"state":{"on":true}}', "state.on") == {1: True}


# -- the profiles are kept apart ---------------------------------------------


def test_a_clamp_profile_cannot_be_asked_for_contacts():
    """The settings page offers the right list for each kind, and the parser
    refuses the wrong one rather than returning something plausible."""
    with pytest.raises(payloads.PayloadError, match="does not read contacts"):
        payloads.contact_states("shelly_em1", STATUS_FRAME)

    with pytest.raises(payloads.PayloadError, match="does not read a clamp"):
        payloads.clamp_value("x408_inputs", X408_BODY)


def test_every_profile_offered_is_one_of_the_two_kinds():
    """A profile in the dropdown that no reader knows how to use is a setting
    somebody can save and then wonder why nothing arrives."""
    offered = {name for name, _ in payloads.PROFILES}

    assert offered == payloads.CLAMP_PROFILES | payloads.CONTACT_PROFILES
    assert not payloads.CLAMP_PROFILES & payloads.CONTACT_PROFILES
