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

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator

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


class ChannelMap(BaseModel):
    """One digital input on the panel module, and what the panel put on it.

    The input number is the identity. The other two fields are the only things
    worth saying about it, and they answer different questions:

    * ``role`` is what this input means, chosen from the eight the dashboard
      can draw. It replaced a name somebody typed, which was two things badly:
      a caption, and a way of saying an input was in use. It was neither. Every
      input is read and recorded whatever it is called, so a blank name never
      turned anything off, and the caption had to be matched by hand against a
      second list on another page to make a lamp light. Picking the meaning
      here does both jobs at once and there is no second list to disagree with.

    * ``invert`` is which way round the wire works, and it is the setting that
      is easiest to get wrong and worst to get wrong. It covers two
      arrangements that look different on the wire and mean the same thing
      here: a dry contact wired normally closed, and a live signal wired fail
      safe that holds voltage while all is well and drops it on the event.
      Panel alarm and motor overload contacts are often built the second way on
      purpose, so a cut wire reads the same as a fault. Either way the raw
      reading means the opposite of the signal, and without this such an alarm
      reads as on permanently and goes quiet at the moment it fires.
    """

    channel: int = Field(ge=1, le=8)
    role: str = Field(default="", max_length=40)
    invert: bool = False

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        value = value.strip()
        if value and value not in {role for role, _ in DASHBOARD_ROLES}:
            raise ValueError(f"{value} is not one of the roles the dashboard draws")
        return value

    @property
    def used(self) -> bool:
        """Whether this input has been told what it means.

        Not whether it is read. Every input is read and recorded either way;
        this only decides whether it lights a lamp.
        """
        return bool(self.role)

    @property
    def title(self) -> str:
        """What to call this input in prose. Never empty."""
        for role, label in DASHBOARD_ROLES:
            if role == self.role:
                return label
        return f"DI{self.channel}"


class InputsSettings(BaseModel):
    """The panel contacts, which arrive over MQTT.

    The device is a ControlByWeb X-408: eight optically isolated inputs, 4 to
    26 V DC, which suits a 24 V panel. It publishes when an input changes
    rather than answering when asked, so there is no polling loop here and no
    poll interval to tune. That is the whole reason for the change: Modbus is
    master and slave by design and a slave may never speak first, so watching
    it meant asking five times a second forever and still being up to a poll
    late.

    **Everything arrives on one topic.** The device is configured to publish
    all eight inputs together in one JSON body whenever any of them changes, so
    every message carries the complete state. A message that carried only the
    input that changed would leave this holding a picture assembled from
    fragments, and a fragment lost on a reconnect would leave that picture
    quietly wrong.

    **Online and offline come from the broker.** The device sets a birth
    message when it connects and a last will that the broker publishes for it
    when it stops, so a device that loses power says so through the broker
    rather than being noticed missing after a timeout.
    """

    KEY: ClassVar[str] = "inputs"

    enabled: bool = False

    # The broker. Bundled alongside this application by default, on the host's
    # own network, which is why the default is a loopback address: the device
    # dials in from the LAN and this reads from the same machine.
    host: str = "127.0.0.1"
    port: int = Field(default=1883, ge=1, le=65535)
    username: str = ""
    password: str = ""
    # Off by default because the bundled broker listens on loopback. Turn it on
    # for a broker somewhere else, and set the port to 8883.
    encrypted: bool = False

    # What the device publishes to, and what the broker publishes for it when
    # the device stops talking. Both are typed into the device as well; the
    # instructions in the README give the exact words.
    topic: str = Field(default="pitwatch/inputs", min_length=1, max_length=200)
    status_topic: str = Field(default="pitwatch/status", min_length=1, max_length=200)

    # How this client identifies itself to the broker. Two clients sharing an
    # id knock each other off, so it is worth being able to change.
    client_id: str = Field(default="pitwatch", min_length=1, max_length=64)

    # How long a state has to hold before it counts as a change.
    #
    # Still needed, and for the same reason as ever: contacts bounce, and
    # floats bounce longest because a float bobs on the water. What changed is
    # that a bounce now arrives as a burst of messages rather than as a run of
    # disagreeing polls, so this waits after a change to see whether it lasts.
    #
    # The cost is that every transition is reported this much later than it
    # happened, which against a run of a few seconds is invisible: the start
    # and the end move together, so the duration is unchanged.
    debounce_ms: int = Field(default=500, ge=0, le=30_000)

    channels: list[ChannelMap] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_in_every_channel(self) -> InputsSettings:
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

    @model_validator(mode="after")
    def one_input_per_role(self) -> InputsSettings:
        """A role belongs to one input.

        Two lamps drawn from one contact was allowed while the mapping ran the
        other way round, and a simple panel really can bring out one contact
        that is both its high water float and its alarm. It is not worth the
        confusion here: with the meaning chosen on the input, the same input
        claiming two meanings has nowhere to be written down, and silently
        keeping one of them would be worse than saying so.
        """
        seen: dict[str, int] = {}
        for mapped in self.channels:
            if not mapped.role:
                continue
            if mapped.role in seen:
                raise ValueError(
                    f"{mapped.title} is on DI{seen[mapped.role]} and DI{mapped.channel}. "
                    "Each one belongs to a single input."
                )
            seen[mapped.role] = mapped.channel
        return self

    @property
    def used_channels(self) -> list[ChannelMap]:
        """The inputs that have been told what they mean, in input order."""
        return [channel for channel in self.channels if channel.used]

    def channel_for(self, role: str) -> int | None:
        """Which input carries a role, or None if nothing has been given it."""
        for mapped in self.channels:
            if mapped.role == role:
                return mapped.channel
        return None

    def label_for(self, channel: int) -> str:
        """What to call an input, falling back to its terminal marking.

        Never empty. A reading from an input whose role has since been cleared
        still has to be describable, and DI4 is what is printed on the module.
        """
        for mapped in self.channels:
            if mapped.channel == channel:
                return mapped.title
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
    """What one motor is.

    Deliberately not what to complain about. Every threshold that raises an
    alert lives with its alert, on the alerts page, because a number on this
    page tells you nothing about what happens when it is crossed and a number
    beside its own message tells you everything.
    """

    name: str = "Pump"

    # The line between off and running.
    #
    # Not zero, but not for the reason this used to say. It claimed a clamp on
    # a live conductor reads noise; on a real one it reads 0.000 exactly, for
    # hours. This is margin against a control transformer sharing the
    # conductor, and nothing finer matters: on the readings from the reference
    # panel every threshold from 0.2 A to 10 A found the same 73 runs.

    # Full load amps off the motor's own plate. Nothing is computed from it. It
    # is here so the typical load on the dashboard can be judged against
    # something real, and so a pump drawing more than its own rating is visible
    # for what it is, which on the reference panel it turned out to be.


class PumpsSettings(BaseModel):
    KEY: ClassVar[str] = "pumps"

    pump1: PumpSettings = Field(default_factory=lambda: PumpSettings(name="Pump 1"))
    pump2: PumpSettings = Field(default_factory=lambda: PumpSettings(name="Pump 2"))

    @property
    def by_number(self) -> dict[int, PumpSettings]:
        """Pump number to its settings. A lookup, not a branch."""
        return {1: self.pump1, 2: self.pump2}


class SmtpSettings(BaseModel):
    KEY: ClassVar[str] = "smtp"

    enabled: bool = False
    host: str = ""
    port: int = Field(default=465, ge=1, le=65535)
    username: str = ""
    password: str = ""
    # 'starttls' upgrades a plain connection, 'tls' opens an encrypted one,
    # 'none' is for a relay on the same machine that does not want either.
    security: str = Field(default="tls", pattern="^(starttls|tls|none)$")
    from_address: str = ""
    from_name: str = "PitWatch"

    @field_validator("host", "from_address")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class SummarySettings(BaseModel):
    """What the summary page needs: a description of the system, and a key.

    The description is the half a model cannot work out from the numbers. Two
    pumps in a pit under a building on a corner in Manhattan, a check valve
    that was replaced in the spring, a superintendent who empties the pit by
    hand when it storms: none of that is in a current reading, and all of it
    changes what the readings mean.

    The key is somebody's OpenAI account and this is the only place it is
    stored. It is never rendered back to the browser, the same as every other
    secret here.
    """

    KEY: ClassVar[str] = "summary"

    description: str = Field(default="", max_length=4000)
    api_key: str = ""
    # Any model name the account can reach. A field rather than a list,
    # because the list changes faster than this application does and a
    # dropdown that has gone stale is a page that cannot be used at all.
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"

    @field_validator("api_key", "model", "base_url")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @property
    def ready(self) -> bool:
        """Enough to ask. The description is optional and the key is not."""
        return bool(self.api_key and self.model and self.base_url)


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


class Severity(StrEnum):
    """How loud an alert is, which decides who it reaches.

    Every account picks a floor: everything, warnings and worse, or critical
    only. These are the three steps on that dial and there are deliberately no
    more, because a scale nobody can hold in their head gets set to the middle
    and left there.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertRule(BaseModel):
    """One thing worth telling somebody about.

    The rules are a fixed list. A builder for arbitrary conditions is a query
    language nobody asked to learn, and the eight inputs a duplex panel brings
    out only support so many questions. What is configurable is whether a rule
    runs, how loudly, who hears it, and what it says.
    """

    enabled: bool = True

    severity: Severity = Severity.WARNING

    # Who hears it. A separate question from how urgent it is: "the Shelly
    # stopped answering" is worth waking an administrator for and is noise to
    # somebody whose only job is to know the basement is flooding. Severity
    # says how loud, this says who can do anything about it.
    admins_only: bool = False

    # The one line that goes out. A text message gets this and nothing else,
    # an email gets this and then the readings behind it, so it is written
    # once and never twice. Placeholders in braces are filled in; anything
    # unrecognized is left alone rather than raising.
    message: str = ""

    # Whether to say so when it stops. Being told the pit flooded and never
    # told it drained is its own kind of bad, and for the rules where the end
    # is the good news it matters more than the start.
    tell_when_it_clears: bool = True


class PanelAlertRule(AlertRule):
    """The controller's own alarm, which is a last resort by design.

    It carries no detail: a power failure, an open panel door and half a dozen
    faults all raise the same contact. So it waits, briefly, to see whether
    something that does carry detail explains it, and stays quiet if one does.
    Otherwise two alerts arrive for one event and the vaguer one is the one
    that gets read first.
    """

    # How long to wait for a better explanation. Short, because the controller
    # raises everything at once rather than in sequence; this only has to
    # cover the poll interval and the debounce, not a human's reaction.
    hold_s: int = Field(default=5, ge=0, le=300)


class OverCurrentRule(AlertRule):
    """A motor pulling more than it should, per motor.

    Per motor because the two can be different sizes, and because a threshold
    that is right for one and wrong for the other is worse than no threshold:
    it fires constantly on one pump and never on the other, and both get
    ignored together.
    """

    pump1_amps: float | None = Field(default=None, gt=0)
    pump2_amps: float | None = Field(default=None, gt=0)

    # Readings, not milliseconds. The meter reports on its own schedule, so a
    # hold measured in time asks for readings that may not exist. Two is also
    # what discards the starting surge, which only ever lands in the first
    # reading of a run.
    readings: int = Field(default=2, ge=1, le=100)


class RunTooLongRule(AlertRule):
    """A stuck float, or pumping against something.

    Needs the panel's run contact. Run length cannot be measured from the
    clamps: the meter reports the start and the end of a run and nothing in
    between.
    """

    longer_than_ms: int | None = Field(default=None, ge=1_000, le=86_400_000)


class ShortCyclingRule(AlertRule):
    """A check valve that has stopped sealing.

    The column of water in the discharge pipe runs back into the pit the
    moment a pump stops and calls it straight out again. Counted by how soon
    rather than how often, because a pit taking roof water cycles all through
    a storm and that is the equipment working.
    """

    restart_within_ms: int | None = Field(default=45_000, ge=100, le=600_000)
    times_in_a_row: int = Field(default=4, ge=2, le=50)


class NothingHasRunRule(AlertRule):
    """Silence, which is a dry spell or a blind monitor."""

    quiet_minutes: int | None = Field(default=120, ge=5, le=525_600)


class LoadDriftRule(AlertRule):
    """The steady draw climbing week over week.

    The reason typical load exists. A pump gaining an amp a month is a pump on
    its way to a problem, and it is invisible in any single reading: nothing is
    ever wrong on the day, only over the weeks.
    """

    climb_amps: float | None = Field(default=1.0, gt=0, le=100)


class AlertsSettings(BaseModel):
    KEY: ClassVar[str] = "alerts"

    high_water: AlertRule = Field(
        default_factory=lambda: AlertRule(
            severity=Severity.CRITICAL,
            message=("High water at {site}. The top float is wet, {pumps_state}. Time {time}."),
        )
    )
    panel_alert: PanelAlertRule = Field(
        default_factory=lambda: PanelAlertRule(
            severity=Severity.CRITICAL,
            message=(
                "Panel alert at {site} and nothing here explains it. Often a "
                "power failure or the panel door left open. Somebody has to "
                "look. Time {time}."
            ),
        )
    )
    overload: AlertRule = Field(
        default_factory=lambda: AlertRule(
            severity=Severity.CRITICAL,
            message=(
                "{pump} overload tripped at {site}. That pump is off and will "
                "not run until somebody opens the panel and presses the red "
                "button on {overload}. Time {time}."
            ),
        )
    )
    contactor_no_current: AlertRule = Field(
        default_factory=lambda: AlertRule(
            severity=Severity.CRITICAL,
            message=(
                "{pump} at {site} is switched on and drawing nothing. The "
                "contactor is closed and the motor is not turning. Time {time}."
            ),
        )
    )
    # The two rules that need a number typed in before they can do anything.
    #
    # Off, therefore, rather than on: a rule with no threshold behind it cannot
    # fire, so shipping it ticked meant a page that said it was raising an
    # alert while it sat there unable to. The ticked box was the wrong half to
    # believe. Set the threshold and tick it, in that order, and the box means
    # what it says from then on.
    over_current: OverCurrentRule = Field(
        default_factory=lambda: OverCurrentRule(
            enabled=False,
            message=("{pump} at {site} drew {amps} A, over its {threshold} A limit. Time {time}."),
        )
    )
    run_too_long: RunTooLongRule = Field(
        default_factory=lambda: RunTooLongRule(
            enabled=False,
            message="{pump} at {site} ran for {duration} without stopping. Time {time}.",
        )
    )
    short_cycling: ShortCyclingRule = Field(
        default_factory=lambda: ShortCyclingRule(
            message=(
                "The pumps at {site} are restarting within {gap} of stopping, "
                "{times} times running. Usually a check valve letting the "
                "discharge run back into the pit."
            ),
        )
    )
    nothing_has_run: NothingHasRunRule = Field(
        default_factory=lambda: NothingHasRunRule(
            message=(
                "No pump has run at {site} for {quiet}. Either a very dry spell "
                "or something has stopped watching the pit."
            ),
        )
    )
    load_drift: LoadDriftRule = Field(
        default_factory=lambda: LoadDriftRule(
            severity=Severity.INFO,
            message=(
                "{pump} at {site} is drawing {amps} A when it runs, up from {was} A a month ago."
            ),
        )
    )
    device_offline: AlertRule = Field(
        default_factory=lambda: AlertRule(
            admins_only=True,
            message=(
                "PitWatch has lost the {device} at {site} and is not watching "
                "the pumps. Time {time}."
            ),
        )
    )
    float_activity: AlertRule = Field(
        default_factory=lambda: AlertRule(
            enabled=False,
            severity=Severity.INFO,
            tell_when_it_clears=False,
            message="{float} at {site} went wet. Time {time}.",
        )
    )
    pump_running: AlertRule = Field(
        default_factory=lambda: AlertRule(
            enabled=False,
            severity=Severity.INFO,
            tell_when_it_clears=False,
            message="{pump} at {site} started. Time {time}.",
        )
    )

    @property
    def by_key(self) -> dict[str, AlertRule]:
        return {name: getattr(self, name) for name in ALERT_ORDER}


# The order the settings page lists them, and the only place the set of rules
# is written down. Grouped by what somebody is being told: something has gone
# wrong, something is wearing out, or something is happening.
ALERT_ORDER: tuple[str, ...] = (
    "high_water",
    "panel_alert",
    "overload",
    "contactor_no_current",
    "over_current",
    "run_too_long",
    "short_cycling",
    "nothing_has_run",
    "load_drift",
    "device_offline",
    "float_activity",
    "pump_running",
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

    # Who is answerable for this installation, as it should read on a page
    # anyone can open: "David McGowan, Sole Proprietor", "Greenwich Mews Board
    # of Managers". A carrier reviewing a messaging registration is looking for
    # a real person or business behind the number, and so is somebody deciding
    # whether a text about a pump at two in the morning is legitimate.
    operator: str = ""
    # Town and state. Deliberately not a street address, and the field is named
    # so that filling one in feels like the wrong thing to do. Where somebody
    # lives is not the carrier's question, and a public page is public forever.
    operator_locality: str = ""

    @property
    def operates_in(self) -> str:
        return self.operator_locality.strip()

    @property
    def public_pumps_at(self) -> str:
        """What to call the equipment on a page anyone can read.

        Never ``name``. That field is an address on this installation and
        probably on most others; it earns its place in an alert, where it tells
        somebody woken at two in the morning which building to drive to, and it
        has no business on a page a search engine can reach. So the public
        pages get the town, or they get nothing.
        """
        where = self.operates_in
        return f"pump equipment in {where}" if where else "pump equipment"

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
    AlertsSettings,
    ShellySettings,
    InputsSettings,
    PumpsSettings,
    SmtpSettings,
    SmsSettings,
)
