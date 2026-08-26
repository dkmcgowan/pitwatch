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

from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


class ChannelMap(BaseModel):
    """One Waveshare digital input.

    The input number is the identity. Everything else about a channel is
    description: what it is called is a label somebody types, and a blank label
    means nothing is wired there, so it is not watched and not shown.

    There is no separate list of signals to pick from. There was, and it bought
    nothing: a name that has to be defined somewhere before it can be used, a
    key underneath the name so a rename was not a delete, a rule against two
    inputs claiming the same name, and a refusal to remove a name still in use.
    All of that existed to keep a second identity in step with the one the panel
    already gives you, which is the terminal the wire lands on.

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
    label: str = Field(default="", max_length=60)
    invert: bool = False

    @field_validator("label")
    @classmethod
    def tidy(cls, value: str) -> str:
        return value.strip()

    @property
    def used(self) -> bool:
        """Whether anything is wired here, which is whether it has a name."""
        return bool(self.label)


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

    # How long a reading has to hold before it counts as a change. One number
    # for all eight inputs, not one each: contacts bounce, floats bounce
    # longest because a float bobs, and without this one call for water writes
    # a dozen events and the run detector sees a dozen starts.
    #
    # This also covers an AC control circuit, where a bidirectional optocoupler
    # drops out at every zero crossing, 120 times a second on 60 Hz, so a poll
    # can land in a gap and read a live signal as off. Anything longer than a
    # couple of poll intervals discards that, because the next poll disagrees
    # and the candidate change is abandoned. Hence a default that is not zero.
    #
    # The cost is that every transition is reported this much later than it
    # happened, which against a run of a few seconds is invisible: the start
    # and the end move together, so the duration is unchanged.
    debounce_ms: int = Field(default=500, ge=0, le=30_000)

    channels: list[ChannelMap] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_in_every_channel(self) -> WaveshareSettings:
        """Always eight, in order, whatever was saved.

        The module has eight inputs whether or not anything is wired to them,
        and a settings page that showed only the configured ones would have no
        row to configure the next one in.
        """
        by_channel = {channel.channel: channel for channel in self.channels}
        self.channels = [
            by_channel.get(number, ChannelMap(channel=number)) for number in range(1, 9)
        ]
        return self

    @property
    def used_channels(self) -> list[ChannelMap]:
        """The inputs something is wired to, in input order."""
        return [channel for channel in self.channels if channel.used]

    def label_for(self, channel: int) -> str:
        """What to call an input, falling back to its terminal marking.

        Never empty. A reading from an input whose name has since been cleared
        still has to be describable, and "DI4" is what is printed on the module.
        """
        for mapped in self.channels:
            if mapped.channel == channel and mapped.used:
                return mapped.label
        return f"DI{channel}"


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


class DashboardSettings(BaseModel):
    """Which input drives which lamp on the dashboard.

    The settings above describe the wiring: input 3 is called High water. This
    describes the display: the High water lamp is input 3. They are separate
    because they are separate questions, and because a panel can bring out
    things the dashboard has no lamp for and a lamp can go unassigned.

    Every role is optional. Unassigned means that lamp reads "not set" rather
    than reading off, which is the same distinction the inputs themselves make:
    not knowing is not the same as knowing it is fine.

    Two roles may share an input on purpose. A simple panel really can bring
    out one contact that is both its high water float and its alarm, and
    refusing that would be this application arguing with the panel.
    """

    KEY: ClassVar[str] = "dashboard"

    # The red lamp on the panel door, and the one somebody wrote a plumber's
    # number under.
    system_alert: int | None = Field(default=None, ge=1, le=8)
    high_water: int | None = Field(default=None, ge=1, le=8)

    # The two the panel does not show at all. Worth having, because they are
    # what the pumps are answering.
    lead_float: int | None = Field(default=None, ge=1, le=8)
    lag_float: int | None = Field(default=None, ge=1, le=8)

    # The two lit selector switches along the bottom.
    pump1_run: int | None = Field(default=None, ge=1, le=8)
    pump2_run: int | None = Field(default=None, ge=1, le=8)

    # Overload relays. These are what turn a pump's word on the display from
    # LEAD or LAG into FAIL.
    pump1_fault: int | None = Field(default=None, ge=1, le=8)
    pump2_fault: int | None = Field(default=None, ge=1, le=8)

    @property
    def assignments(self) -> dict[str, int | None]:
        return {role: getattr(self, role) for role, _ in DASHBOARD_ROLES}


# Role to what the dashboard calls it, in the order the settings page asks.
# Grouped the way the panel is: alarms, then floats, then pumps.
DASHBOARD_ROLES: tuple[tuple[str, str], ...] = (
    ("system_alert", "System alert"),
    ("high_water", "High water"),
    ("lead_float", "Lead float"),
    ("lag_float", "Lag float"),
    ("pump1_run", "Pump 1 running"),
    ("pump2_run", "Pump 2 running"),
    ("pump1_fault", "Pump 1 overload"),
    ("pump2_fault", "Pump 2 overload"),
)


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
    #
    # Short on purpose. This is an ejector pit: the thing on the other end of a
    # delay is water coming up through a floor, and half a minute of politeness
    # is not worth it.
    notify_delay_s: int = Field(default=5, ge=0, le=3600)
    # Do not send the same alert again within this window.
    notify_cooldown_s: int = Field(default=900, ge=0, le=86_400)


SETTING_MODELS: tuple[type[BaseModel], ...] = (
    SiteSettings,
    ShellySettings,
    WaveshareSettings,
    PumpsSettings,
    DashboardSettings,
    SmtpSettings,
    SmsSettings,
)
