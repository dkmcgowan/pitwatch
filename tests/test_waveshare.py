"""Debounce and inversion, without a Modbus device.

These two are the whole reason the reader is more than a loop around a read
call, and both fail quietly when they are wrong: a missing debounce turns one
call for water into a dozen recorded starts, and a missing inversion leaves an
alarm on forever and silent when it fires. Neither shows up as an error.

The monotonic clock is passed into the debouncer rather than read inside it, so
these run instantly instead of sleeping through every hold time.
"""

from __future__ import annotations

from pitwatch.ingest.waveshare import Debouncer, WaveshareReader
from pitwatch.schemas import ChannelMap, WaveshareSettings


def settings_with(*channels: ChannelMap, debounce_ms: int = 0) -> WaveshareSettings:
    """A configured module. No debounce by default, so a frame is a transition
    and the reader tests do not have to move a clock to say anything."""
    return WaveshareSettings(
        enabled=True, host="192.0.2.10", debounce_ms=debounce_ms, channels=list(channels)
    )


def bits(**closed: bool) -> list[bool]:
    """Eight raw input bits, named by channel: bits(di3=True)."""
    return [closed.get(f"di{number}", False) for number in range(1, 9)]


def all_off() -> dict[int, bool]:
    """What io_state holds on any install that has already run once."""
    return dict.fromkeys(range(1, 9), False)


# -- debounce ----------------------------------------------------------------


def test_a_change_has_to_hold_before_it_counts():
    debouncer = Debouncer(500)
    debouncer.prime(1, False)

    # The reading that starts a change cannot also settle it, however long the
    # hold is. It takes a later reading that still agrees.
    assert debouncer.feed(1, True, now=100.0) is None
    assert debouncer.feed(1, True, now=100.3) is None
    assert debouncer.feed(1, True, now=100.6) is True


def test_a_bounce_that_falls_back_is_abandoned():
    """A float that bobs up and drops again is not a call for water."""
    debouncer = Debouncer(500)
    debouncer.prime(1, False)

    debouncer.feed(1, True, now=100.0)
    debouncer.feed(1, False, now=100.2)

    # The clock has passed the hold time, but the run of True never did.
    assert debouncer.feed(1, True, now=100.7) is None
    assert debouncer.feed(1, True, now=101.3) is True


def test_a_settled_state_is_only_reported_once():
    debouncer = Debouncer(100)
    debouncer.prime(1, False)

    assert debouncer.feed(1, True, now=100.0) is None
    assert debouncer.feed(1, True, now=100.2) is True
    assert debouncer.feed(1, True, now=100.4) is None
    assert debouncer.feed(1, True, now=200.0) is None


def test_zero_debounce_passes_a_change_straight_through():
    """What the tests below run with, so a frame is a transition."""
    debouncer = Debouncer(0)
    debouncer.prime(1, False)

    assert debouncer.feed(1, True, now=100.0) is True


def test_channels_settle_independently():
    """One hold for all eight, but each channel tracks its own candidate. A
    float bouncing on DI1 must not delay a contact settling on DI2."""
    debouncer = Debouncer(500)
    debouncer.prime(1, False)
    debouncer.prime(2, False)

    debouncer.feed(1, True, now=100.0)
    assert debouncer.feed(2, True, now=100.0) is None
    assert debouncer.feed(2, True, now=100.6) is True
    assert debouncer.feed(1, True, now=100.6) is True


def test_the_default_hold_is_long_enough_to_be_worth_having():
    """One setting for all eight inputs, so its default is what most installs
    will run on and nobody will think about it again.

    Long enough to outlast contact bounce and to bridge the zero crossing gaps
    of an AC control circuit, which needs more than a couple of poll intervals.
    Short enough to be invisible against a run of a few seconds.
    """
    settings = WaveshareSettings()

    assert 200 <= settings.debounce_ms <= 1000
    assert 2 * settings.poll_ms < settings.debounce_ms


def test_the_reader_uses_the_hold_from_the_settings():
    """Changing it on the page has to reach the thing that debounces, which is
    the one link in that chain nothing else would notice was broken."""
    reader = WaveshareReader(settings_with(debounce_ms=1234), on_events=None)

    assert reader._debouncer._hold_ms == 1234


# -- the first frame ---------------------------------------------------------


def test_the_first_frame_matching_what_was_stored_produces_no_events():
    """A restart is not eight state changes."""
    reader = WaveshareReader(
        settings_with(ChannelMap(channel=1, label="Lead float")),
        on_events=None,
        initial_state=all_off(),
    )

    assert reader._apply(bits(), first=True) == []


def test_a_fresh_install_records_all_eight_inputs_as_a_baseline():
    """Nothing is known yet, so the first reading starts the history.

    This happens once, ever. Without it io_state stays empty until something
    changes, and a dashboard opened on a quiet day would show every contact as
    unknown rather than as off.
    """
    reader = WaveshareReader(
        settings_with(ChannelMap(channel=1, label="Lead float")),
        on_events=None,
        initial_state={},
    )

    events = reader._apply(bits(), first=True)

    assert len(events) == 8
    assert all(event.state is False for event in events)


def test_the_first_frame_reports_what_changed_while_we_were_down():
    reader = WaveshareReader(
        settings_with(ChannelMap(channel=3, label="High water")),
        on_events=None,
        initial_state=all_off(),
    )

    events = reader._apply(bits(di3=True), first=True)

    assert len(events) == 1
    assert events[0].label == "High water"
    assert events[0].state is True


# -- inversion ---------------------------------------------------------------


def test_an_inverted_channel_reads_the_opposite_of_the_wire():
    """The setting that is worst to get wrong.

    A fail safe overload signal is present while the motor is fine and drops on
    the trip, whether that is a dry contact wired normally closed or a 24 V line
    that goes away. Recording the raw bit would leave the alarm on permanently
    and turn it off at the moment it trips.
    """
    reader = WaveshareReader(
        settings_with(
            ChannelMap(
                channel=7,
                label="Pump 1 overload",
                invert=True,
                debounce_ms=0,
            )
        ),
        on_events=None,
        initial_state=all_off(),
    )

    # Closed contact, nothing wrong. Inverted, that reads as off, which is what
    # was already recorded, so there is nothing to report.
    assert reader._apply(bits(di7=True), first=True) == []

    events = reader._apply(bits(di7=False), first=False)

    assert len(events) == 1
    assert events[0].state is True, "the signal going away means tripped"
    assert events[0].raw is False, "the raw bit is recorded as it was read"


def test_a_plain_channel_is_not_inverted():
    reader = WaveshareReader(
        settings_with(ChannelMap(channel=1, label="Lead float")),
        on_events=None,
        initial_state=all_off(),
    )
    reader._apply(bits(), first=True)

    events = reader._apply(bits(di1=True), first=False)

    assert events[0].state is True
    assert events[0].raw is True


# -- everything else ---------------------------------------------------------


def test_an_unmapped_channel_still_produces_an_event():
    """Recorded, not ignored.

    An input nobody has mapped yet is exactly the input somebody is about to
    map, and having its history already is what makes "which channel did that"
    answerable after the fact rather than only while watching.
    """
    reader = WaveshareReader(
        settings_with(ChannelMap(channel=5)),
        on_events=None,
        initial_state=all_off(),
    )
    reader._apply(bits(), first=True)

    events = reader._apply(bits(di5=True), first=False)

    assert len(events) == 1
    assert events[0].label == "", "an unnamed input is still read, just not named"


def test_a_channel_that_does_not_change_produces_nothing():
    reader = WaveshareReader(
        settings_with(ChannelMap(channel=1, label="Lead float")),
        on_events=None,
        initial_state=all_off(),
    )
    reader._apply(bits(di1=True), first=True)

    assert reader._apply(bits(di1=True), first=False) == []
    assert reader._apply(bits(di1=True), first=False) == []
