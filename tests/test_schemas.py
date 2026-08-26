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

from pitwatch import domain
from pitwatch.schemas import (
    SETTING_MODELS,
    ChannelMap,
    PumpSettings,
    PumpsSettings,
    ShellySettings,
    WaveshareSettings,
)


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
        {"channel": 7, "label": "Pump 1 overload", "normally_closed": True}
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


def test_every_property_on_every_settings_model_actually_works():
    """A property that raises does not look like a bug. It looks like a choice.

    `SiteSettings.where` kept referring to a field that had been deleted. In
    Python that is an AttributeError; in a Jinja template it is an empty string,
    so the building name simply stopped appearing in the footer and the policy
    pages quietly said "this building" instead. Nothing failed, nothing logged,
    and it read as something somebody had decided.

    So: touch every property on every settings model, with defaults and with
    values, and let an exception be an exception.
    """
    for model in SETTING_MODELS:
        for instance in (model(), model.model_validate(_filled(model))):
            for name in dir(type(instance)):
                if name.startswith("_") or name in model.model_fields:
                    continue
                attribute = getattr(type(instance), name, None)
                if isinstance(attribute, property):
                    getattr(instance, name)


def _filled(model) -> dict:
    """Plausible values for a model's own fields, so properties see real data."""
    values: dict = {}
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if annotation is str or annotation == (str | None):
            values[name] = "822 Greenwich St" if name == "name" else "something"
        elif annotation is bool:
            values[name] = True
    # Anything that will not take a made up string keeps its default.
    for name in list(values):
        try:
            model.model_validate({name: values[name]})
        except ValueError:
            del values[name]
    return values


def test_an_overcurrent_decision_fits_inside_a_real_run():
    """The defaults have to be able to fire on the pump they are for.

    An ejector pump runs for three or four seconds. The check cannot start until
    the starting surge has been discarded, and then has to hold. If those two
    added up to longer than a run, the alert could never happen at all, and a
    check that silently never fires is worse than no check: it looks like
    coverage.

    This was real. The hold defaulted to fifteen seconds against runs of four.
    """
    pumps = PumpsSettings()

    decided_at = pumps.inrush_ignore_ms + pumps.pump1.overcurrent_hold_ms

    assert decided_at <= 3000, f"needs {decided_at} ms of run to say anything"


def test_the_inrush_window_is_long_enough_to_cover_a_start():
    """Six to eight times running current, for a fraction of a second.

    Too short and the surge lands in the average and in the overcurrent check,
    which is what forces a threshold to be set uselessly high.
    """
    assert PumpsSettings().inrush_ignore_ms >= 500


def test_a_run_can_end_before_the_next_one_starts():
    """These pumps alternate, seconds apart. If it took longer to notice a stop
    than the gap between runs, two runs would be recorded as one.

    Not a setting any more, which is the point of checking it here. It is a
    fact about how often the meter reports, so it lives in the detector and the
    only thing left to get wrong is this number.
    """
    assert domain.RUN_STOP_HOLD_MS <= 2000
    assert PumpsSettings().max_runtime_ms > domain.RUN_STOP_HOLD_MS


def test_every_alert_threshold_can_be_turned_off():
    """Empty means do not run that check, everywhere, with no exceptions.

    The two that used to be compulsory were compulsory by accident rather than
    by argument, and a threshold that cannot be turned off is one that gets set
    to an absurd value instead, which reads as configured and behaves as off.
    """
    off = PumpsSettings(
        max_runtime_ms=None,
        restart_gap_ms=None,
        quiet_minutes_before_flag=None,
        pump1=PumpSettings(overcurrent_amps=None),
    )

    assert off.max_runtime_ms is None
    assert off.restart_gap_ms is None
    assert off.quiet_minutes_before_flag is None
    assert off.pump1.overcurrent_amps is None


def test_there_is_no_undercurrent_alert():
    """Removed. Deciding a pump is drawing too little needs a model of what it
    should be drawing, which varies with head and with what is in the pit, and
    a threshold guessed at would mostly produce false alarms."""
    assert "undercurrent" not in str(PumpSettings.model_fields)


def test_short_cycling_is_off_until_somebody_sets_a_gap():
    """A threshold guessed at before there is run history to look at is a
    threshold that mostly produces false alarms, and an alert that cries wolf
    gets ignored along with the ones that matter."""
    assert PumpsSettings().restart_gap_ms is None


def test_short_cycling_is_measured_by_the_gap_not_by_a_rate():
    """A pit that takes roof water cycles continuously through a storm, which is
    the equipment doing its job. A rule counting starts per hour fires on every
    rainstorm. The gap between runs is what separates a failed check valve, where
    the same column of water falls back in about the same short time each time,
    from inflow, which has to refill a real volume and varies with the weather.
    """
    fields = PumpsSettings.model_fields

    assert "restart_gap_ms" in fields
    assert "max_starts" not in fields
    assert "starts_window_min" not in fields


# -- what each input is called ----------------------------------------------
#
# The input number is the identity and the name is description. There is no
# list of allowed names, no key under the name, and no rule about two inputs
# sharing one, because none of that is needed once the terminal the wire lands
# on is what everything is keyed by.


def test_every_input_is_present_whether_or_not_it_is_wired():
    """The module has eight inputs regardless, and the settings page needs a row
    to configure the next one in."""
    settings = WaveshareSettings(channels=[ChannelMap(channel=3, label="High water")])

    assert [channel.channel for channel in settings.channels] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert settings.channels[2].label == "High water"
    assert settings.channels[0].label == ""


def test_an_input_with_no_name_is_not_wired():
    settings = WaveshareSettings(channels=[ChannelMap(channel=3, label="High water")])

    assert settings.channels[2].used is True
    assert settings.channels[0].used is False
    assert [channel.channel for channel in settings.used_channels] == [3]


def test_a_name_that_is_only_spaces_is_no_name_at_all():
    """Otherwise an input somebody cleared by holding the space bar stays
    watched, shows a blank row on the dashboard, and looks like a bug."""
    assert ChannelMap(channel=1, label="   ").used is False
    assert ChannelMap(channel=1, label="  Lead float  ").label == "Lead float"


def test_two_inputs_may_share_a_name():
    """Nothing keys on the name, so there is nothing to collide. Two contacts
    both marked "High water" in a panel is a real thing, and refusing it would
    be this application arguing with the label maker."""
    settings = WaveshareSettings(
        channels=[
            ChannelMap(channel=1, label="High water"),
            ChannelMap(channel=2, label="High water"),
        ]
    )

    assert len(settings.used_channels) == 2


def test_an_input_always_has_something_to_call_it():
    """Including one nobody has named, because a reading from it still has to be
    describable. DI4 is what is printed on the module."""
    settings = WaveshareSettings(channels=[ChannelMap(channel=3, label="High water")])

    assert settings.label_for(3) == "High water"
    assert settings.label_for(4) == "DI4"
