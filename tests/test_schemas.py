"""Settings validation.

The channel map is the part worth testing hardest. It is the one piece of
configuration where a mistake is silent: a duplicated signal or a missing
channel does not raise anything at runtime, it just means an alarm that never
fires.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pitwatch.schemas import (
    ChannelMap,
    PumpSettings,
    PumpsSettings,
    ShellySettings,
    Signal,
    WaveshareSettings,
)


def test_channels_are_filled_in_when_missing():
    settings = WaveshareSettings(channels=[ChannelMap(channel=3, signal=Signal.HIGH_WATER)])

    assert [channel.channel for channel in settings.channels] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert settings.channels[2].signal is Signal.HIGH_WATER
    assert settings.channels[0].signal is Signal.UNUSED


def test_a_signal_cannot_be_on_two_channels():
    with pytest.raises(ValidationError, match="only be on one channel"):
        WaveshareSettings(
            channels=[
                ChannelMap(channel=1, signal=Signal.HIGH_WATER),
                ChannelMap(channel=2, signal=Signal.HIGH_WATER),
            ]
        )


def test_unused_channels_may_repeat():
    settings = WaveshareSettings(channels=[])

    assert all(channel.signal is Signal.UNUSED for channel in settings.channels)


def test_channel_lookup_by_signal():
    settings = WaveshareSettings(channels=[ChannelMap(channel=7, signal=Signal.PUMP1_OVERLOAD)])

    found = settings.channel_for(Signal.PUMP1_OVERLOAD)
    assert found is not None
    assert found.channel == 7
    assert settings.channel_for(Signal.LEAD_FLOAT) is None


def test_channel_numbers_outside_the_module_are_rejected():
    with pytest.raises(ValidationError):
        ChannelMap(channel=9)


def test_shelly_defaults_put_one_pump_on_each_clamp():
    settings = ShellySettings()

    assert {settings.pump1_channel, settings.pump2_channel} == {0, 1}


def test_both_clamp_assignments_are_stored_and_read_back_as_saved():
    """No derivation. What was saved is what comes back.

    An earlier version stored only pump 1 and returned `1 - pump1_channel` for
    pump 2, which meant reading a setting nobody had written.
    """
    settings = ShellySettings(pump1_channel=1, pump2_channel=0)

    assert settings.pump1_channel == 1
    assert settings.pump2_channel == 0
    assert settings.clamp_for_pump == {1: 1, 2: 0}

    swapped = ShellySettings(pump1_channel=0, pump2_channel=1)
    assert swapped.clamp_for_pump == {1: 0, 2: 1}


def test_both_pumps_cannot_read_the_same_clamp():
    with pytest.raises(ValidationError, match="cannot read the same clamp"):
        ShellySettings(pump1_channel=1, pump2_channel=1)


def test_settings_saved_before_pump2_was_stored_keep_their_mapping():
    """The compatibility shim, and the reason it exists.

    Without it a stored {"pump1_channel": 1} takes the default of 1 for pump 2,
    fails the check that they differ, and falls back to defaults, quietly moving
    pump 1 back to clamp 0. Both pumps would then read the wrong motor and the
    page would look entirely reasonable.
    """
    settings = ShellySettings.model_validate({"host": "10.0.0.9", "pump1_channel": 1})

    assert settings.pump1_channel == 1
    assert settings.pump2_channel == 0


def test_the_shim_does_not_touch_settings_that_name_both_clamps():
    settings = ShellySettings.model_validate({"pump1_channel": 0, "pump2_channel": 1})

    assert settings.clamp_for_pump == {1: 0, 2: 1}


def test_pump_settings_are_looked_up_by_number():
    pumps = PumpsSettings(pump1=PumpSettings(name="North"), pump2=PumpSettings(name="South"))

    assert pumps.by_number[1].name == "North"
    assert pumps.by_number[2].name == "South"


def test_a_setting_saved_under_the_old_name_still_inverts():
    """The field was renamed from normally_closed to invert.

    Dropping the old name would read as False on the next load, which silently
    un-inverts an alarm: it would then read as permanently on and go quiet at
    the moment it fires. Exactly the failure the flag exists to prevent.
    """
    channel = ChannelMap.model_validate(
        {"channel": 7, "signal": "pump1_overload", "normally_closed": True}
    )

    assert channel.invert is True
