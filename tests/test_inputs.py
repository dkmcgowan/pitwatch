"""The panel inputs: reading a published body, debouncing it, inverting it.

None of this has run against a real X-408 yet. What it can be held to is the
part that does not depend on the device: that a body typed in by hand several
reasonable ways still reads, that a bounce does not become a run, and that a
fail safe contact is recorded as the signal rather than as the bit.
"""

from __future__ import annotations

import asyncio

import pytest

from pitwatch.ingest.inputs import Debouncer, InputsError, InputsReader, parse
from pitwatch.schemas import ChannelMap, InputsSettings


def _settings(**overrides) -> InputsSettings:
    base = {
        "enabled": True,
        "host": "127.0.0.1",
        "debounce_ms": 0,
        "channels": [ChannelMap(channel=number) for number in range(1, 9)],
    }
    base.update(overrides)
    return InputsSettings(**base)


# -- reading a body ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        '{"1": 1, "2": 0}',
        '{"input1": true, "input2": false}',
        '{"digitalInput1": "on", "digitalInput2": "off"}',
        '{"di1": "1", "di2": "0"}',
        '{"1": "closed", "2": "open"}',
    ],
)
def test_a_body_reads_however_it_was_typed(payload):
    """The body is typed into the device by hand, and there are several
    reasonable ways to write it. Refusing all but one of them would mean a
    working panel and an empty dashboard, with nothing on either end saying
    which spelling was wanted."""
    assert parse(payload) == {1: True, 2: False}


def test_keys_named_after_the_floats_still_read():
    """The keys are typed in by hand, and somebody naming them after what is
    wired to each input rather than after the input is being reasonable. With
    nothing in the body that looks like an input, the digits are all there is."""
    assert parse('{"High Water Float 3": 1, "Lead Float 1": 0}') == {3: True, 1: False}


def test_a_key_that_is_not_an_input_is_dropped_rather_than_guessed_at():
    """The device offers the uptime and the MAC address in the same body, and
    somebody will leave one in. Neither is an input, and neither is a reason to
    throw away the seven states next to it."""
    assert parse('{"mac": "00:0C:C8:01:02:03", "uptime": 4210, "4": 1}') == {4: True}


def test_a_register_is_not_input_one():
    """This is the one that would have been silent. The device's tokens for
    things that are not inputs include ${register1} and ${relay1}, and reading
    the digits out of every key would file a register under input 1 with
    nothing anywhere to say so. A body that names its inputs is read strictly,
    and the loose reading is kept for a body where nothing does."""
    body = '{"digitalInput1": 1, "register1": 42, "relay1": 1, "vin": 24.1}'
    assert parse(body) == {1: True}


def test_an_input_number_off_the_end_is_not_an_input():
    with pytest.raises(InputsError):
        parse('{"9": 1}')


def test_a_body_that_is_not_json_says_so():
    with pytest.raises(InputsError):
        parse("DI1=1")


def test_a_body_that_is_not_an_object_says_so():
    with pytest.raises(InputsError):
        parse("[1, 0, 1]")


# -- debounce ---------------------------------------------------------------


def test_a_bounce_is_not_a_change():
    """A float bobbing sends a burst. Held long enough, only the settled state
    counts, and the burst leaves nothing behind."""
    debouncer = Debouncer(hold_ms=500)
    debouncer.prime(1, False)

    assert debouncer.feed(1, True, now=0.0) is None
    assert debouncer.feed(1, False, now=0.1) is None
    assert debouncer.feed(1, True, now=0.2) is None
    assert debouncer.stable_state(1) is False


def test_a_change_that_lasts_is_a_change():
    debouncer = Debouncer(hold_ms=500)
    debouncer.prime(1, False)

    assert debouncer.feed(1, True, now=0.0) is None
    assert debouncer.feed(1, True, now=0.6) is True
    assert debouncer.stable_state(1) is True


def test_the_hold_restarts_when_the_candidate_flips():
    """Half a second of one state, not half a second of noise."""
    debouncer = Debouncer(hold_ms=500)
    debouncer.prime(1, False)

    debouncer.feed(1, True, now=0.0)
    debouncer.feed(1, False, now=0.4)
    assert debouncer.feed(1, True, now=0.45) is None
    assert debouncer.feed(1, True, now=0.8) is None
    assert debouncer.feed(1, True, now=1.0) is True


def test_one_channel_bouncing_does_not_hold_up_another():
    debouncer = Debouncer(hold_ms=500)
    debouncer.prime(1, False)
    debouncer.prime(2, False)

    debouncer.feed(1, True, now=0.0)
    debouncer.feed(1, False, now=0.1)
    debouncer.feed(2, True, now=0.0)
    assert debouncer.feed(2, True, now=0.6) is True
    assert debouncer.stable_state(1) is False


# -- what the reader records ------------------------------------------------


def test_the_first_body_is_the_truth_not_a_transition():
    """There is nothing to debounce the first body against, and a module that
    reconnects mid run would otherwise sit blind for the whole hold."""

    async def run():
        reader = InputsReader(_settings(debounce_ms=5000), _nothing)
        return reader._apply(parse('{"1": 1, "2": 0}'))

    events = asyncio.run(run())
    assert [(event.channel, event.state) for event in events] == [(1, True), (2, False)]


def test_a_first_boot_records_every_input_including_the_quiet_ones():
    """The off states matter as much as the on ones here, and only here.

    The module speaks only when something changes, so an input that is resting
    and stays resting would never be recorded at all, and the dashboard would
    have nothing to draw its lamp from until the first time it moved. The first
    body is the one chance to write down where everything started."""

    async def run():
        reader = InputsReader(_settings(), _nothing)
        return reader._apply(parse('{"1": 0, "2": 0, "3": 0}'))

    events = asyncio.run(run())
    assert [(event.channel, event.state) for event in events] == [
        (1, False),
        (2, False),
        (3, False),
    ]


def test_the_first_body_agreeing_with_the_database_writes_nothing():
    """A restart is not eight contacts changing. Only a contact that moved
    while this was down is worth an event."""

    async def run():
        reader = InputsReader(_settings(), _nothing, initial_state={1: True})
        return reader._apply(parse('{"1": 1}'))

    assert asyncio.run(run()) == []


def test_a_contact_that_moved_while_this_was_down_is_still_noticed():
    async def run():
        reader = InputsReader(_settings(), _nothing, initial_state={1: False})
        return reader._apply(parse('{"1": 1}'))

    events = asyncio.run(run())
    assert [(event.channel, event.state) for event in events] == [(1, True)]


def test_a_fail_safe_contact_is_recorded_as_the_signal_not_as_the_bit():
    """The panel alarm contact is live when nothing is wrong and drops on the
    fault. Recording the bit would leave the alarm permanently on, and silent
    at the moment it fires."""
    channels = [ChannelMap(channel=number) for number in range(1, 9)]
    channels[3] = ChannelMap(channel=4, role="system_alert", invert=True)

    async def run():
        reader = InputsReader(_settings(channels=channels), _nothing)
        healthy = reader._apply(parse('{"4": 1}'))
        faulted = reader._apply(parse('{"4": 0}'))
        return healthy, faulted

    healthy, faulted = asyncio.run(run())
    # Live is the resting state, so the signal is off while the bit is on.
    assert [(event.state, event.raw) for event in healthy] == [(False, True)]
    assert [(event.state, event.raw) for event in faulted] == [(True, False)]


def test_the_label_travels_with_the_event():
    """The channel is the identity; what it was called is recorded alongside so
    old history still reads after somebody moves a role to another input."""
    channels = [ChannelMap(channel=number) for number in range(1, 9)]
    channels[5] = ChannelMap(channel=6, role="pump2_run")

    async def run():
        reader = InputsReader(_settings(channels=channels), _nothing)
        return reader._apply(parse('{"6": 1, "7": 1}'))

    events = asyncio.run(run())
    by_channel = {event.channel: event.label for event in events}
    assert by_channel[6] == "Pump 2 running"
    # An input carrying nothing is still read and still recorded. It just has
    # no name but the one printed on the module.
    assert by_channel[7] == "DI7"


def test_a_body_missing_an_input_says_nothing_about_it():
    """Every message is meant to carry all eight. One that does not is not a
    reason to invent a state for the ones it left out."""

    async def run():
        reader = InputsReader(_settings(), _nothing)
        reader._apply(parse('{"1": 1, "2": 1}'))
        return reader._apply(parse('{"1": 0}'))

    events = asyncio.run(run())
    assert [(event.channel, event.state) for event in events] == [(1, False)]


async def _nothing(events) -> None:
    return None
