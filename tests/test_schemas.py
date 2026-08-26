"""Settings validation.

The channel map is the part worth testing hardest. It is the one piece of
configuration where a mistake is silent: a duplicated signal or a missing
channel does not raise anything at runtime, it just means an alarm that never
fires.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pitwatch.schemas import (
    SETTING_MODELS,
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


def test_a_partial_setting_falls_back_rather_than_being_guessed_at():
    """Nothing carries settings forward while the schema is still moving.

    A stored value that names only pump 1 no longer has pump 2 inferred for it.
    It fails the check that the two differ and the store falls back to defaults,
    which is the wipe and set up again path, and is deliberate: a half applied
    compatibility shim turns "your settings are gone" into "your settings are
    subtly wrong", and only one of those is obvious.
    """
    with pytest.raises(ValidationError, match="cannot read the same clamp"):
        ShellySettings.model_validate({"host": "10.0.0.9", "pump1_channel": 1})


def test_pump_settings_are_looked_up_by_number():
    pumps = PumpsSettings(pump1=PumpSettings(name="North"), pump2=PumpSettings(name="South"))

    assert pumps.by_number[1].name == "North"
    assert pumps.by_number[2].name == "South"


def test_the_old_channel_field_name_is_no_longer_accepted():
    """normally_closed became invert, with no alias kept.

    Pydantic ignores unknown keys, so an old value reads as False rather than
    raising. That is only acceptable while every schema change is followed by a
    wipe; when the shape settles this needs either an alias or a real migration,
    because silently un-inverting an alarm is the worst failure in this
    application.
    """
    channel = ChannelMap.model_validate(
        {"channel": 7, "signal": "pump1_overload", "normally_closed": True}
    )

    assert channel.invert is False


def test_every_settings_model_survives_a_save_and_a_load():
    """What the store does to every model, without needing a store.

    Settings are written as JSON and read back through the same model, so
    anything whose own defaults do not survive that trip is broken for every
    install that has not set it. A validator that rejects the model's own
    default values is the specific way this goes wrong, and it did: adding a
    check that the two clamps differ, while both defaulted to the same number,
    would have made ShellySettings unloadable.

    This runs without a database, which is where the equivalent failure was
    caught late twice.
    """
    for model in SETTING_MODELS:
        fresh = model()
        # json.dumps and back, because that is literally the round trip the
        # setting table performs, and it catches types that only look fine.
        dumped = json.loads(json.dumps(fresh.model_dump(mode="json")))

        assert model.model_validate(dumped) == fresh, f"{model.__name__} does not round trip"


def test_every_settings_model_loads_from_nothing():
    """An unset key reads as an empty object, which every model must accept.

    This is the state of every setting on a fresh install, so a model that
    cannot be built from `{}` is one the wizard can never render.
    """
    for model in SETTING_MODELS:
        assert model.model_validate({}) == model()
