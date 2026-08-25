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


def test_pump_two_is_always_the_other_clamp():
    """There is no way to configure both pumps onto one clamp.

    An earlier version stored both channel numbers and validated that they
    differed, which is a check that can be failed. Deriving the second one
    removes the state rather than guarding it.
    """
    assert ShellySettings(pump1_channel=0).pump2_channel == 1
    assert ShellySettings(pump1_channel=1).pump2_channel == 0


def test_a_stored_pump2_channel_from_an_older_version_is_ignored():
    settings = ShellySettings.model_validate(
        {"host": "10.0.0.9", "pump1_channel": 1, "pump2_channel": 1}
    )

    assert settings.pump2_channel == 0
