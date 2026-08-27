"""What each alert is for, in the words the settings page shows.

Kept apart from the settings model on purpose. The model holds what somebody
chose; this holds what the choice means, which is prose, and prose in a
validator is prose nobody finds when they want to change it.

**Placeholders.** Each rule lists what it can fill in. They are written in
braces in the message and replaced when it is sent. Anything unrecognized is
left alone rather than raising, because a typo in a message should produce a
slightly odd alert rather than no alert at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Threshold:
    """A number a rule needs, and how to ask for it."""

    field: str
    label: str
    hint: str = ""
    step: str = "1"
    minimum: str = "0"


@dataclass(frozen=True, slots=True)
class Spec:
    """One rule, as the page describes it."""

    key: str
    title: str
    what: str
    # What it reads. Said out loud because half of these cannot fire until the
    # I/O module is wired, and a rule that silently never runs looks exactly
    # like a rule that never found anything.
    needs: str
    placeholders: tuple[str, ...] = ()
    thresholds: tuple[Threshold, ...] = ()
    clears: str = ""


CLAMPS = "the clamps"
CONTACTS = "the panel contacts"
BOTH = "the clamps and the panel contacts"

COMMON = ("{site}", "{time}")

SPECS: tuple[Spec, ...] = (
    Spec(
        key="high_water",
        title="High water",
        what=(
            "The float above the lag float is wet, which means the pit is "
            "filling faster than one pump empties it. The controller answers "
            "by calling both pumps, so the message says whether they are "
            "running: both pumps on and the water still rising is a different "
            "night from both pumps on and coping."
        ),
        needs=CONTACTS,
        placeholders=(*COMMON, "{pumps_state}"),
        clears="the float drops",
    ),
    Spec(
        key="panel_alert",
        title="Panel alert, unexplained",
        what=(
            "The controller's own alarm, which carries no detail: a power "
            "failure, an open panel door and half a dozen faults all raise the "
            "same contact. This waits briefly to see whether something that "
            "does carry detail explains it, and stays quiet if one does, so "
            "one event does not arrive as two alerts."
        ),
        needs=CONTACTS,
        placeholders=COMMON,
        thresholds=(
            Threshold(
                "hold_s",
                "Wait for a better explanation (s)",
                "Long enough to cover the poll and the debounce, not long "
                "enough to matter. The controller raises everything at once.",
            ),
        ),
        clears="the alarm goes out",
    ),
    Spec(
        key="overload",
        title="Overload tripped",
        what=(
            "A motor overload has cut a pump, and it will not run again until "
            "somebody does something about it. The default message assumes the "
            "overload is set to hand reset, which is what the red button is "
            "for; change the wording if yours is on auto."
        ),
        needs=CONTACTS,
        placeholders=(*COMMON, "{pump}", "{overload}", "{amps}", "{duration}"),
        clears="it is reset",
    ),
    Spec(
        key="contactor_no_current",
        title="Switched on, drawing nothing",
        what=(
            "The run contact is closed and the clamp reads nothing, so the "
            "controller believes a pump is running and no current is flowing. "
            "The motor is not turning: seized, a lost phase, a blown fuse, a "
            "broken coupling. Neither sensor can see this alone, which is the "
            "whole argument for having both."
        ),
        needs=BOTH,
        placeholders=(*COMMON, "{pump}"),
        clears="current appears or the contact opens",
    ),
    Spec(
        key="over_current",
        title="Drawing too much",
        what=(
            "A motor pulling more than it should, for long enough that this is "
            "not the starting surge. Set the threshold against the typical "
            "load on the dashboard rather than against the plate: what matters "
            "is what this pump normally draws."
        ),
        needs=CLAMPS,
        placeholders=(*COMMON, "{pump}", "{amps}", "{threshold}"),
        thresholds=(
            Threshold("pump1_amps", "Pump 1 over (A)", step="0.1"),
            Threshold("pump2_amps", "Pump 2 over (A)", step="0.1"),
            Threshold(
                "readings",
                "for this many readings",
                "Readings, not seconds: the meter reports on its own schedule. "
                "Two is about half a minute, and it is also what ignores the "
                "starting surge.",
                minimum="1",
            ),
        ),
        clears="it drops back",
    ),
    Spec(
        key="run_too_long",
        title="Ran too long",
        what=(
            "A stuck float, or pumping against a blockage. Off until there is "
            "a number behind it: run length cannot be measured from the "
            "clamps, which report the start and the end of a run and nothing "
            "in between."
        ),
        needs=CONTACTS,
        placeholders=(*COMMON, "{pump}", "{duration}"),
        thresholds=(Threshold("longer_than_ms", "Longer than (ms)", step="500", minimum="1000"),),
        clears="the pump stops",
    ),
    Spec(
        key="short_cycling",
        title="Short cycling",
        what=(
            "A check valve that has stopped sealing lets the discharge run "
            "back into the pit and calls the pump straight out again. Counted "
            "by how soon rather than how often, because a pit taking roof "
            "water cycles all through a storm and that is the equipment "
            "working."
        ),
        needs=CLAMPS,
        placeholders=(*COMMON, "{gap}", "{times}"),
        thresholds=(
            Threshold("restart_within_ms", "Restarts within (ms)", step="100", minimum="100"),
            Threshold("times_in_a_row", "this many times running", minimum="2"),
        ),
        clears="the gaps lengthen",
    ),
    Spec(
        key="nothing_has_run",
        title="Nothing has run",
        what=(
            "Silence, which is either a genuinely dry spell or a stuck float, "
            "a clamp that has fallen off, or a dead panel. Set it longer than "
            "the longest gap you see on a dry week."
        ),
        needs=CLAMPS,
        placeholders=(*COMMON, "{quiet}"),
        thresholds=(Threshold("quiet_minutes", "Quiet for (minutes)", minimum="5"),),
        clears="a pump runs",
    ),
    Spec(
        key="load_drift",
        title="Drawing more than it used to",
        what=(
            "The steady draw climbing week over week, which is the reason "
            "typical load exists. A pump gaining an amp a month is a pump on "
            "its way to a problem, and nothing is ever wrong on the day. Needs "
            "a month of history before it can say anything."
        ),
        needs=CLAMPS,
        placeholders=(*COMMON, "{pump}", "{amps}", "{was}"),
        thresholds=(Threshold("climb_amps", "Climbed by (A)", step="0.1"),),
        clears="it settles back",
    ),
    Spec(
        key="device_offline",
        title="A device stopped answering",
        what=(
            "The Shelly or the I/O module has gone quiet, so PitWatch is not "
            "watching the pumps and cannot tell you if something happens. "
            "Administrators only by default: it is worth waking somebody who "
            "can do something about it and is noise to everybody else."
        ),
        needs="PitWatch itself",
        placeholders=(*COMMON, "{device}"),
        clears="it answers again",
    ),
    Spec(
        key="float_activity",
        title="Float activity",
        what=(
            "Every float, every time it goes wet. Off by default and worth "
            "turning on for an afternoon while checking the wiring, not for a "
            "week: on a working pit this fires several times an hour."
        ),
        needs=CONTACTS,
        placeholders=(*COMMON, "{float}"),
    ),
    Spec(
        key="pump_running",
        title="A pump started",
        what=(
            "Every run, as it happens. Off by default for the same reason as "
            "the floats, and useful for the same reason: a day of these on a "
            "pump you are suspicious of tells you more than a week of "
            "wondering."
        ),
        needs=CONTACTS,
        placeholders=(*COMMON, "{pump}"),
    ),
)

BY_KEY: dict[str, Spec] = {spec.key: spec for spec in SPECS}


def fill(message: str, values: dict[str, object]) -> str:
    """Put the readings into a message, leaving anything unknown alone.

    Deliberately not str.format, which raises on a placeholder nobody defined
    and would turn a typo in a message into an alert that never arrives. A
    slightly odd alert beats a silent one every time.
    """
    filled = message
    for name, value in values.items():
        filled = filled.replace("{" + name + "}", str(value))
    return filled
