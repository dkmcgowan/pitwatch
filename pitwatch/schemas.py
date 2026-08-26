"""The shape of everything that gets configured.

Each model here is stored as one JSON row in the setting table, under the key in
its ``KEY`` attribute. Validation lives in the model, so a value that reaches
the database has already been checked.

**Nothing here carries settings forward across a rename or a change of shape.**
While the schema is still moving, the way to take a change is to wipe and set up
again, which is cheap and honest. Compatibility shims for a schema that is still
being argued about cost more than they save, and a half applied one is worse
than none: it turns "your settings are gone" into "your settings are subtly
wrong". Add them when the shape settles, not before.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


class Signal(StrEnum):
    """The signals PitWatch itself understands.

    These are the names the rest of the application speaks. A rule asks for
    HIGH_WATER, not for channel 3, so rewiring the panel is a settings change
    and not a code change.

    They are not the whole list, and they are not a limit. What a panel
    actually carries is a setting, in ``WaveshareSettings.signals``, and can be
    renamed, added to, or cut down. The eight below are special only in that
    code elsewhere refers to them by name: the dashboard puts the run and
    overload contacts on the pump tiles and draws the pit from the floats. A
    signal somebody adds is recorded, shown and alerted on like any other. It
    simply has no built in meaning for anything to attach to.
    """

    UNUSED = "unused"
    LEAD_FLOAT = "lead_float"
    LAG_FLOAT = "lag_float"
    HIGH_WATER = "high_water"
    PANEL_ALARM = "panel_alarm"
    PUMP1_RUN = "pump1_run"
    PUMP2_RUN = "pump2_run"
    PUMP1_OVERLOAD = "pump1_overload"
    PUMP2_OVERLOAD = "pump2_overload"


# What an input is set to when nothing is wired to it. Deliberately not in the
# catalog below: it is the absence of a signal rather than one of them, and any
# number of inputs can be in that state at once.
UNUSED: str = Signal.UNUSED.value

# Keys are ASCII and lower case because they are what the database rows and the
# alert rules hold. Labels are what people read and can say anything.
SIGNAL_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"

# What a new install starts with. Every one of these can be renamed or removed
# afterwards, and others added, because panels differ: yours may bring out a
# seal failure, a phase monitor, a hand-off-auto position, or nothing but the
# floats.
BUILTIN_SIGNALS: tuple[tuple[str, str], ...] = (
    (Signal.LEAD_FLOAT.value, "Lead float"),
    (Signal.LAG_FLOAT.value, "Lag float"),
    (Signal.HIGH_WATER.value, "High water alarm float"),
    (Signal.PANEL_ALARM.value, "Panel alarm contact"),
    (Signal.PUMP1_RUN.value, "Pump 1 running"),
    (Signal.PUMP2_RUN.value, "Pump 2 running"),
    (Signal.PUMP1_OVERLOAD.value, "Pump 1 overload tripped"),
    (Signal.PUMP2_OVERLOAD.value, "Pump 2 overload tripped"),
)

BUILTIN_SIGNAL_LABELS: dict[str, str] = dict(BUILTIN_SIGNALS)


def signal_key(label: str) -> str:
    """A storage key from a label somebody typed.

    Only ever used when a signal is first added. After that the key is fixed
    and the label is free to change, which is what keeps a rename from
    orphaning the history recorded under the old name.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    if cleaned and not cleaned[0].isalpha():
        cleaned = f"s_{cleaned}"
    return cleaned[:40].rstrip("_") or "signal"


class SignalDef(BaseModel):
    """One name an input can be given, as it appears in the picker."""

    key: str = Field(pattern=SIGNAL_KEY_PATTERN)
    label: str = Field(min_length=1, max_length=60)

    @property
    def builtin(self) -> bool:
        """Whether code elsewhere knows this one by name.

        Only used to warn before removing one. Removing it is allowed; the
        parts that look for it simply find nothing wired, which is a state they
        already have to handle.
        """
        return self.key in BUILTIN_SIGNAL_LABELS


def default_signals() -> list[SignalDef]:
    return [SignalDef(key=key, label=label) for key, label in BUILTIN_SIGNALS]


class ChannelMap(BaseModel):
    """One Waveshare digital input.

    ``invert`` is the setting that is easiest to get wrong and worst to get
    wrong, and it covers two arrangements that look different on the wire and
    mean the same thing here:

    * A dry contact wired normally closed, which sits closed while nothing is
      happening and opens on the event.
    * A live signal wired fail safe, which holds voltage while all is well and
      drops it on the event. Panel alarm and motor overload contacts are often
      built this way on purpose, so that a cut wire reads the same as a fault.

    Either way the raw reading means the opposite of the signal. Without this
    flag such an alarm reads as on permanently and goes quiet at exactly the
    moment it fires. Panels are not consistent, so it is per channel rather
    than global, and it is worth checking each one against the panel rather
    than assuming.
    """

    channel: int = Field(ge=1, le=8)
    # A key from the signal catalog on WaveshareSettings, or "unused". A plain
    # string rather than the enum, because the catalog is editable and a signal
    # somebody added at three in the morning is as real as one shipped here.
    signal: str = Field(default=UNUSED, pattern=SIGNAL_KEY_PATTERN)
    invert: bool = False
    # Contacts bounce, and float switches bounce for longer than most because a
    # float bobs. A state has to hold for this long before it counts.
    #
    # This also covers an AC signal. A bidirectional optocoupler fed from an AC
    # source drops out briefly at every zero crossing, 120 times a second on 60
    # Hz, so a poll can land in a gap and read a live signal as off. Any
    # debounce longer than a couple of poll intervals discards that, because the
    # next poll disagrees and the candidate change is abandoned. Hence a default
    # that is not zero.
    debounce_ms: int = Field(default=500, ge=0, le=30_000)


class WaveshareSettings(BaseModel):
    KEY: ClassVar[str] = "waveshare"

    enabled: bool = False
    host: str = ""
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=0, le=247)
    # Modbus is request and response, so the inputs have to be asked for. Eight
    # bits at five times a second is nothing on the wire, and it is the
    # difference between catching a two second lag float call and missing it.
    poll_ms: int = Field(default=200, ge=50, le=10_000)
    timeout_s: float = Field(default=3.0, gt=0, le=60)

    # What this panel brings out, which is a property of the panel and not of
    # this application. Starts as the eight in BUILTIN_SIGNALS and is edited
    # from the settings page.
    signals: list[SignalDef] = Field(default_factory=default_signals)
    channels: list[ChannelMap] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_and_check_channels(self) -> WaveshareSettings:
        keys = [signal.key for signal in self.signals]
        if UNUSED in keys:
            raise ValueError(
                'Nothing can be named "unused". That name is how an input with '
                "nothing wired to it is recorded."
            )
        repeated = {key for key in keys if keys.count(key) > 1}
        if repeated:
            raise ValueError("Two signals cannot share a name: " + ", ".join(sorted(repeated)))

        by_channel = {channel.channel: channel for channel in self.channels}
        self.channels = [
            by_channel.get(number, ChannelMap(channel=number)) for number in range(1, 9)
        ]

        # A channel pointing at a signal that is no longer on the list. The
        # usual way to get here is removing a signal without first freeing the
        # input that uses it, and saying which input is the whole of the fix.
        known = set(keys)
        for channel in self.channels:
            if channel.signal != UNUSED and channel.signal not in known:
                raise ValueError(
                    f"DI{channel.channel} is wired to a signal that is not on the "
                    "list any more. Set that input to Not connected first, then "
                    "remove the signal."
                )

        assigned = [c.signal for c in self.channels if c.signal != UNUSED]
        duplicates = {s for s in assigned if assigned.count(s) > 1}
        if duplicates:
            names = ", ".join(sorted(self.label_for(s) for s in duplicates))
            raise ValueError(f"Each signal can only be on one channel. Repeated: {names}")
        return self

    def label_for(self, key: str) -> str:
        """What to call a signal on screen.

        Falls back to the key itself for a signal that has since been removed,
        so old history stays readable rather than blank.
        """
        if key == UNUSED:
            return "Not connected"
        for signal in self.signals:
            if signal.key == key:
                return signal.label
        return BUILTIN_SIGNAL_LABELS.get(key, key)

    @property
    def options(self) -> list[tuple[str, str]]:
        """What each input's dropdown offers, in order."""
        return [(UNUSED, "Not connected")] + [(s.key, s.label) for s in self.signals]

    @property
    def channel_of(self) -> dict[str, int]:
        """Signal key to the input carrying it, for everything assigned."""
        return {c.signal: c.channel for c in self.channels if c.signal != UNUSED}

    def channel_for(self, signal: str) -> ChannelMap | None:
        return next((c for c in self.channels if c.signal == signal), None)


class ShellySettings(BaseModel):
    KEY: ClassVar[str] = "shelly"

    enabled: bool = False
    host: str = ""
    # 'client' means PitWatch dials the device and holds the socket open, which
    # works on a flat network and needs nothing configured on the Shelly.
    # 'outbound' means the device dials PitWatch, which is what you want when
    # the Shelly cannot be reached from here but can reach out.
    mode: str = Field(default="client", pattern="^(client|outbound)$")
    # Only set when the device has a password on its local web interface.
    password: str | None = None
    # Which em1 instance is on each pump. Both are stored and both are read
    # back as they were saved. An earlier version stored only pump 1 and
    # returned `1 - pump1_channel` for pump 2, which meant a setting nobody had
    # written and a rule that had to be remembered everywhere it was read. The
    # browser keeps the two in step; that is a convenience, not the source of
    # truth.
    pump1_channel: int = Field(default=0, ge=0, le=1)
    pump2_channel: int = Field(default=1, ge=0, le=1)
    # The device pushes on change. This poll exists only to notice that it has
    # stopped pushing, which a silent socket does not tell us.
    heartbeat_s: int = Field(default=30, ge=5, le=600)

    @model_validator(mode="after")
    def clamps_differ(self) -> ShellySettings:
        if self.pump1_channel == self.pump2_channel:
            raise ValueError(
                "The two pumps cannot read the same clamp. There are two clamps "
                "and two pumps, so one goes to each."
            )
        return self

    @property
    def clamp_for_pump(self) -> dict[int, int]:
        """Pump number to clamp, read straight from what was saved."""
        return {1: self.pump1_channel, 2: self.pump2_channel}


class PumpSettings(BaseModel):
    """What one motor is expected to draw.

    Everything here is in amps except the hold, which is in milliseconds,
    because an ejector pump runs for a few seconds at a time and seconds are too
    coarse a unit to describe anything that happens inside one.
    """

    name: str = "Pump"

    # The line between off and running. Not zero: a clamp on a live conductor
    # reads a little noise, and a control transformer sharing the conductor
    # reads more than a little.
    running_amps: float = Field(default=1.0, ge=0)

    # The full load amps printed on the motor's nameplate, which is what it
    # draws doing the work it was built for. Nothing is computed from it. It is
    # here so that the numbers below can be judged against something real when
    # somebody is deciding what to put in them, and so a pump drawing more than
    # its own rating is visible for what it is.
    nameplate_amps: float | None = Field(default=None, gt=0)

    # Above this, for longer than overcurrent_hold_ms, is an overload the panel
    # has not tripped on yet. The starting surge is excluded before this is
    # applied; see PumpsSettings.inrush_ignore_ms.
    overcurrent_amps: float | None = Field(default=None, gt=0)

    # How long the current has to stay above that before it counts.
    #
    # Milliseconds, and short. These pumps run for three or four seconds. A hold
    # measured in seconds, as this was, is longer than the entire run: the
    # condition could never last long enough to fire and the alert would simply
    # never happen, which is the worst way for a check to be wrong.
    overcurrent_hold_ms: int = Field(default=1500, ge=100, le=600_000)


class PumpsSettings(BaseModel):
    KEY: ClassVar[str] = "pumps"

    pump1: PumpSettings = Field(default_factory=lambda: PumpSettings(name="Pump 1"))
    pump2: PumpSettings = Field(default_factory=lambda: PumpSettings(name="Pump 2"))

    # How much of the start to throw away.
    #
    # A motor pulls six to eight times its running current for a fraction of a
    # second as it comes up to speed. Sixty amps settling to sixteen is a
    # healthy pump, not an overloaded one. Every reading in this window is left
    # out of the run's averages and out of the overcurrent check, so a threshold
    # can be set just above the running current without the start tripping it.
    #
    # The peak is still recorded, on its own, because a starting surge climbing
    # month over month is a bearing on its way out.
    inrush_ignore_ms: int = Field(default=800, ge=0, le=30_000)

    # A run longer than this is a stuck float or a blockage. Normal here is a
    # few seconds, so ten is already well outside it. Empty turns the check off.
    #
    # Deciding a run has ended is not a setting. It used to be, and it was a
    # knob nobody could set without knowing how the detector works. See
    # pitwatch.domain.RUN_STOP_HOLD_MS.
    max_runtime_ms: int | None = Field(default=10_000, ge=1_000, le=86_400_000)

    # Short cycling, detected by the gap between runs rather than by counting
    # starts in an hour.
    #
    # Counting starts does not work at a site that takes roof water. During a
    # storm the pit refills as fast as it empties and the pumps cycle
    # continuously for hours, which is the equipment doing its job; a rate based
    # rule fires on every rainstorm and is then ignored, which is worse than not
    # having it.
    #
    # A failed check valve looks different. When the pump stops, the column of
    # water standing in the discharge pipe runs back down into the pit, refills
    # it, and calls the pump straight back out. The distinguishing mark is not
    # how often that happens but how soon: the same column takes about the same
    # short time to fall back every time, so the gaps are both very short and
    # very alike. Inflow, even heavy inflow, has to fill the volume between the
    # off level and the lead float, which takes longer and varies.
    #
    # Off by default. What counts as suspiciously soon depends on the pit, and a
    # threshold guessed at before there is any run history to look at is a
    # threshold that mostly produces false alarms.
    restart_gap_ms: int | None = Field(default=None, ge=100, le=600_000)
    # How many restarts that soon, one after another, before it means something.
    restart_streak: int = Field(default=4, ge=2, le=50)

    # Nothing running at all for this long is either a very dry spell or a
    # sensor that has quietly died, and the second is worth knowing about.
    # Empty turns the check off.
    quiet_minutes_before_flag: int | None = Field(default=240, ge=5, le=525_600)

    @property
    def by_number(self) -> dict[int, PumpSettings]:
        """Pump number to its settings. A lookup, not a branch."""
        return {1: self.pump1, 2: self.pump2}


class SmtpSettings(BaseModel):
    KEY: ClassVar[str] = "smtp"

    enabled: bool = False
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    password: str = ""
    # 'starttls' upgrades a plain connection, 'tls' opens an encrypted one,
    # 'none' is for a relay on the same machine that does not want either.
    security: str = Field(default="starttls", pattern="^(starttls|tls|none)$")
    from_address: str = ""
    from_name: str = "PitWatch"

    @field_validator("host", "from_address")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class SmsSettings(BaseModel):
    KEY: ClassVar[str] = "sms"

    enabled: bool = False
    # 'sns' publishes to Amazon SNS. 'email_gateway' sends a short email to a
    # carrier address such as 5551234567@vtext.com, which costs nothing, needs
    # no registration, and is delivered at the carrier's convenience. Which is
    # right depends on whether a delayed flood alarm is acceptable.
    provider: str = Field(default="sns", pattern="^(sns|email_gateway)$")

    # An IAM access key with permission to call sns:Publish, and nothing else.
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # The registered 10DLC or toll-free number messages are sent from. Required
    # in practice for US destinations; AWS refuses without one and says so
    # obscurely.
    origination_number: str = ""
    # An alphabetic sender name. Works in much of the world and is ignored in
    # the US and Canada, where an origination number is required instead.
    sender_id: str = ""

    # Only used by email_gateway, for example "vtext.com".
    gateway_domain: str = ""


class SiteSettings(BaseModel):
    KEY: ClassVar[str] = "site"

    # The building. An address or a name, whichever somebody woken at two in the
    # morning would recognize: "822 Greenwich St".
    #
    # This is not the name of the application, which is always PitWatch. It is
    # which pumps, and it goes in the subject line of every alert. It has no
    # default, because a placeholder here would end up printed on the public
    # policy pages, and "Ejector pit uses PitWatch to monitor" reads exactly as
    # badly as it sounds.
    name: str = ""
    timezone: str = "America/New_York"
    # The address this is reachable at from outside, used to build invitation
    # links. Behind a reverse proxy the application cannot work this out for
    # itself: it sees the proxy's idea of the request, not the name somebody
    # typed. Leave it empty and links are built from the incoming request,
    # which is right often enough but not always.
    base_url: str = ""
    # Shown on the public messaging policy, because a carrier reviewing a
    # toll-free registration wants a way to contact somebody, and so does a
    # recipient who wants to be taken off the list.
    contact_email: str = ""
    contact_phone: str = ""

    @property
    def where(self) -> str:
        """The building, or empty if nobody has said yet.

        Empty rather than a placeholder on purpose. Every caller has to decide
        what to say when it is not set, which is the only way the policy pages
        avoid announcing a default nobody chose.
        """
        return self.name.strip()

    @property
    def pumps_at(self) -> str:
        """A phrase for prose: "the pumps at 822 Greenwich St"."""
        return f"the pumps at {self.where}" if self.where else "the pumps in this building"

    # An alert has to stay open this long before anyone is told, which stops a
    # float that bobs once from waking the building. Critical alerts ignore it.
    notify_delay_s: int = Field(default=30, ge=0, le=3600)
    # Do not send the same alert again within this window.
    notify_cooldown_s: int = Field(default=900, ge=0, le=86_400)


SETTING_MODELS: tuple[type[BaseModel], ...] = (
    SiteSettings,
    ShellySettings,
    WaveshareSettings,
    PumpsSettings,
    SmtpSettings,
    SmsSettings,
)
