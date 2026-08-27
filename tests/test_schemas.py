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


def test_the_overcurrent_check_is_counted_in_readings_not_milliseconds():
    """It was milliseconds, and it could never have worked.

    The meter reports on its own schedule, roughly every fifteen seconds while
    a current is steady. "Held for 1500 ms" asked for two readings 1.5 s apart,
    which essentially never exist, so the alert could not fire and a check that
    silently never fires is worse than no check: it looks like coverage.

    A count of readings degrades properly instead. It means about half a minute
    at the meter's own cadence and would mean two seconds if anything ever
    sampled faster.
    """
    pumps = PumpsSettings()

    assert not hasattr(pumps.pump1, "overcurrent_hold_ms")
    assert pumps.pump1.overcurrent_readings >= 2, "one reading is the starting surge"


def test_there_is_no_inrush_window():
    """Removed rather than retuned.

    It assumed readings arrive fast enough that a fraction of a second at the
    start of a run contains several of them. A real panel produces two readings
    for a whole run. The surge lands in the first reading and nowhere else, so
    the detector drops that reading instead of a span of time.
    """
    assert not hasattr(PumpsSettings(), "inrush_ignore_ms")
    assert domain.DISCARD_FIRST_READING is True


def test_run_length_is_off_until_something_can_measure_it():
    """The clamps report the start and the end of a run and nothing in between,
    so two readings four minutes apart and two four seconds apart look the
    same. A default here would fire on a sampling gap."""
    assert PumpsSettings().max_runtime_ms is None


def test_short_cycling_is_on_because_there_is_finally_a_measurement():
    """The shortest rest between runs across a night on the reference panel was
    82 seconds. The default has to sit below that with room, because the end of
    a run is only known to within a reading."""
    pumps = PumpsSettings()

    assert pumps.restart_gap_ms is not None
    assert pumps.restart_gap_ms <= 60_000
    assert pumps.restart_streak >= 3, "one mismeasured gap must not raise an alert"


def test_silence_is_noticed_within_a_working_day():
    """A pit that has not run in hours is either dry or blind, and four hours
    was most of a day before anybody heard the clamp had fallen off."""
    assert PumpsSettings().quiet_minutes_before_flag == 120


def test_a_run_can_end_before_the_next_one_starts():
    """These pumps alternate, seconds apart. If it took longer to notice a stop
    than the gap between runs, two runs would be recorded as one.

    Not a setting any more, which is the point of checking it here. It is a
    fact about how often the meter reports, so it lives in the detector and the
    only thing left to get wrong is this number.

    It also has to be shorter than the shortest rest between runs, or two calls
    for water are recorded as one. That rest was 82 seconds on the reference
    panel, so a hold measured in a second or two has room to spare.
    """
    assert domain.RUN_STOP_HOLD_MS <= 2000
    assert PumpsSettings().restart_gap_ms > domain.RUN_STOP_HOLD_MS


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


def test_short_cycling_is_on_now_that_it_is_not_a_guess():
    """It used to be off, and that was right at the time: a threshold guessed
    at before there is any run history mostly produces false alarms, and an
    alert that cries wolf gets ignored along with the ones that matter.

    There is run history now. A night on the reference panel put the shortest
    rest between runs at 82 seconds, so the default sits below that with room
    for the fact that the end of a run is only known to within a reading.
    """
    assert PumpsSettings().restart_gap_ms == 45_000


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
