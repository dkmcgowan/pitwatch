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
from ipaddress import ip_address
from typing import ClassVar, Literal
from urllib.parse import urlsplit

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

    The key, where there is one, is somebody's account and this is the only
    place it is stored. It is never rendered back to the browser, the same as
    every other secret here. There need not be one: the base URL can point at a
    model on this network, and then nothing leaves the building at all.
    """

    KEY: ClassVar[str] = "summary"

    description: str = Field(default="", max_length=4000)

    # Run one every day without being asked, at this time on the building's own
    # clock. Off by default: something that calls out to a model on a schedule
    # should be a thing somebody switched on.
    daily: bool = False
    daily_at: str = "07:00"
    # And send what it says to whoever takes information level news. Email
    # only, which is not a setting: a health check is four paragraphs of prose
    # and four paragraphs of prose is several text messages, arriving daily, on
    # a channel that exists here for two in the morning.
    notify: bool = False

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

    @field_validator("daily_at")
    @classmethod
    def a_time_of_day(cls, value: str) -> str:
        """Twenty four hour, because it is stored rather than read aloud."""
        try:
            hour, minute = (int(part) for part in value.strip().split(":", 1))
        except ValueError:
            raise ValueError("Write the time as HH:MM, like 07:00.") from None
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("Write the time as HH:MM, like 07:00.")
        return f"{hour:02d}:{minute:02d}"

    @property
    def daily_hour_and_minute(self) -> tuple[int, int]:
        hour, minute = self.daily_at.split(":", 1)
        return int(hour), int(minute)

    @property
    def ready(self) -> bool:
        """Enough to ask.

        A model, somewhere to ask it, and a key **unless the somewhere is on
        this network**. A model running beside this usually wants no key, and
        requiring one meant an installation pointed at its own hardware saw a
        page saying "add an OpenAI key" and a button that never appeared.

        The address is what decides it, not a checkbox, because the address is
        the fact: everything out on the internet wants to know who is asking and
        nothing on a private network here does. It also keeps a fresh install
        honest, where the model and the address are filled in by default and the
        key is the one thing nobody has typed yet.
        """
        return bool(self.model and self.base_url and (self.api_key or self.asks_this_network))

    @property
    def asks_this_network(self) -> bool:
        """Whether the API address is somewhere on this side of the router.

        Loopback, the three private ranges, and a name with no dots in it or a
        local suffix, which is what a machine on a LAN is called. Anything this
        cannot place is treated as the internet, which is the safe way round:
        the cost of being wrong here is a page asking for a key that was not
        needed, and the other way round is a button that fails at the far end of
        a request.
        """
        host = (urlsplit(self.base_url).hostname or "").strip().lower()
        if not host:
            return False
        try:
            return ip_address(host).is_private or ip_address(host).is_loopback
        except ValueError:
            pass
        return host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".home"))


class SmsSettings(BaseModel):
    KEY: ClassVar[str] = "sms"

    enabled: bool = False
    # Where the message actually goes.
    #
    # The carrier email gateway was the third option and is gone. It sent a
    # short email to an address like 5551234567@vtext.com, which costs nothing
    # and needs no registration, and is delivered entirely at the carrier's
    # convenience: minutes, hours, or never, with no delivery receipt and no
    # way to tell the difference. That is an acceptable trade for a reminder
    # and not for a flood alarm, and leaving it on the page invited somebody to
    # pick it because it was the one with no paperwork.
    # Amazon SNS was the second option and is gone too. It was written, it had
    # a settings page, it had tests, and not one message was ever sent through
    # it, because the account it was written against never left the SMS
    # sandbox. That is the difference between a fallback and a guess.
    provider: Literal["twilio"] = "twilio"

    #
    # The account SID is not a credential. It names the account in the URL
    # every request is sent to, so it is required whichever way the request is
    # signed, and it is the one Twilio identifier that is safe on screen.
    twilio_account_sid: str = ""
    # The secret half, written like a password and never sent back to the
    # browser. It is the account's own auth token when no API key is set, and
    # the API key's secret when one is.
    twilio_auth_token: str = ""
    # An API key, which is what Twilio recommends over the account auth token
    # and for good reason: it can be revoked on its own, so a leaked one costs
    # a rotation rather than a locked account, and revoking it cannot take the
    # console down with it. When this is set it becomes the user half of the
    # basic auth pair and the account SID stays in the URL, which is the part
    # that catches people out: an API key SID is not a replacement for the
    # account SID, it is a second thing alongside it.
    twilio_key_sid: str = ""
    # One of these, and the messaging service wins where both are set. A
    # registered campaign is attached to a messaging service rather than to a
    # number, which is what A2P 10DLC approval actually gives you, and sending
    # from the bare number afterwards would be sending unregistered traffic
    # down the road that was registered.
    twilio_messaging_service_sid: str = ""
    twilio_from: str = ""


class ClampSource(BaseModel):
    """One current clamp: where its readings arrive, and how to ask for one.

    No profile. A clamp reading is a number, which is the whole of what this is
    for, so the only question is where in the body to find it.
    """

    pump: int = Field(ge=1, le=2)
    topic: str = Field(default="", max_length=300)
    # Where in the body the number is. Dots step into nested objects. Empty
    # takes the body itself, for a device that publishes a bare number.
    path: str = Field(default="", max_length=200)
    # Asking, for a meter that goes quiet while a motor runs steady. Optional:
    # a source asks only when it has a topic and a payload.
    ask_topic: str = Field(default="", max_length=300)
    ask_payload: str = Field(default="", max_length=1000)
    # Where the answer lands, which is usually not the topic asked on. Where a
    # meter decides the reply topic from something in the request, two clamps
    # asking one meter need two of them: a reply carries no sign of what it is
    # answering, so two sources reading one topic at one path would each match
    # every answer.
    reply_topic: str = Field(default="", max_length=300)
    reply_path: str = Field(default="", max_length=200)
    # How often to ask, while the pump is running. There is no switch for
    # whether to ask only while running, because that was a setting with one
    # sensible value: on a pump monitor, a topic to ask on means ask while the
    # pump is turning. An empty ask topic is how you say "do not ask".
    ask_every_s: float = Field(default=1.0, gt=0, le=3600)

    @property
    def role(self) -> str:
        return f"clamp{self.pump}"

    @property
    def channel(self) -> int:
        """Which channel these readings are stored under.

        Derived rather than configured. It was a setting for exactly one
        reason: readings already in the database were filed under whatever
        numbers the meter gave its clamps, and renumbering them would have left
        last month's amps describing the other pump. The readings were wiped on
        2026-09-08, so there is nothing left to stay compatible with, and a box
        whose only correct value is the obvious one is a box that can go.
        """
        return self.pump - 1

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    @property
    def asks(self) -> bool:
        return bool(self.ask_topic and self.ask_payload)

    @property
    def answers_on(self) -> str:
        return self.reply_topic or self.topic

    @property
    def answer_path(self) -> str:
        return self.reply_path or self.path


class ContactInput(BaseModel):
    """One contact on the panel, and the topic that carries it.

    One topic per contact rather than eight in one body. The combined body was
    what one module happened to publish and it cost a parser that had to guess
    how somebody had spelled eight keys. A contact on its own topic is on or
    off, which is all a contact ever is.
    """

    channel: int = Field(ge=1, le=8)
    role: str = Field(default="", max_length=40)
    topic: str = Field(default="", max_length=300)
    # Only for a device that wraps it: `state`, or `value.on`. Empty reads the
    # body itself, which is what a module publishing `1` or `on` sends.
    path: str = Field(default="", max_length=200)
    # Which way round the wire works. A dry contact wired normally closed, or a
    # live signal that holds voltage while all is well and drops it on the
    # event, both mean the opposite of what they read. Panel alarm and overload
    # contacts are often built the second way on purpose, so a cut wire reads
    # the same as a fault.
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
        """Whether this input has been told what it means and where to listen.

        Both, now. Under the combined body an input was read whatever it was
        called, so a role was enough; with a topic each, an input nobody has
        given a topic is one nothing will ever arrive for.
        """
        return bool(self.role and self.topic)

    @property
    def title(self) -> str:
        for role, label in DASHBOARD_ROLES:
            if role == self.role:
                return label
        return f"DI{self.channel}"


class HealthSource(BaseModel):
    """A device saying it is still there.

    Its own section because it is its own question. A clamp topic answers what
    the pump drew; this answers whether the thing that would have told us is
    still plugged in, and the two fail separately.

    Silence is the test, not the broker's last will. Measured on the real panel
    on 2026-09-07: an unplugged meter kept its `online` topic true for the whole
    outage, then published false a tenth of a second before it published true
    again. That is a session takeover at reconnect, not a death notice.
    """

    name: str = Field(default="", max_length=60)
    topic: str = Field(default="", max_length=300)
    # Silence for about two and a half of these and it is reported offline.
    # Zero never holds silence against it, which is right for a device that was
    # never asked to speak on a schedule.
    expect_s: int = Field(default=0, ge=0, le=86_400)

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    @property
    def title(self) -> str:
        return self.name or self.topic or "device"


class MqttSettings(BaseModel):
    """The broker, and everything PitWatch listens to on it.

    One connection and one place to configure it. There were two device
    sections before this, each with its own address and its own idea of what
    being online meant, and only one of them was MQTT: the meter was read over
    a websocket PitWatch opened *to the device*.

    That direction is the thing this changed. A pull design needs a route from
    the application to every device, which works on a LAN and stops the moment
    the application is anywhere else. Every device dialing out to one broker
    needs one reachable address.

    Three kinds of thing to listen to, because a pump panel asks three kinds of
    question. What a pump is drawing is a number. What a contact is doing is on
    or off. Whether a device is still there is neither, and is answered by it
    having said anything lately. Each kind gets its own section rather than one
    list of sources with a profile to pick, because the profile was a question
    with only ever one right answer per kind.
    """

    KEY: ClassVar[str] = "mqtt"

    enabled: bool = False

    # The broker. Bundled alongside this application by default, which is why
    # the default is loopback: the devices dial in from the network and this
    # reads from the same machine.
    host: str = "127.0.0.1"
    port: int = Field(default=1883, ge=1, le=65535)
    username: str = ""
    password: str = ""
    encrypted: bool = False

    # How this client identifies itself. Two clients sharing an id knock each
    # other off, which is not hypothetical: it is the mechanism that published
    # the meter's will during the outage test.
    client_id: str = Field(default="pitwatch", min_length=1, max_length=64)

    # How long a contact state has to hold before it counts as a change.
    # Applies to every contact, because a run contact and the float above it
    # debounced differently would record a call that started before the pump.
    debounce_ms: int = Field(default=0, ge=0, le=30_000)

    clamps: list[ClampSource] = Field(default_factory=list)
    inputs: list[ContactInput] = Field(default_factory=list)
    health: list[HealthSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_in_the_rows(self) -> MqttSettings:
        """Two clamps, eight inputs, two health checks, whatever was saved.

        A page showing only what somebody had already configured would have no
        row to configure the next one in, and a fresh install would show an
        empty box where the whole of ingest is meant to be.
        """
        by_pump = {clamp.pump: clamp for clamp in self.clamps}
        self.clamps = [by_pump.get(pump, ClampSource(pump=pump)) for pump in (1, 2)]

        by_channel = {one.channel: one for one in self.inputs}
        self.inputs = [
            by_channel.get(number, ContactInput(channel=number)) for number in range(1, 9)
        ]

        self.health = list(self.health[:4])
        # Named for the job, and the same words the dashboard uses. "Panel
        # module" and "Inputs" for the same thing on two pages is two things to
        # somebody reading them.
        for name in ("Inputs", "Meter")[len(self.health) :]:
            self.health.append(HealthSource(name=name))
        return self

    @model_validator(mode="after")
    def one_input_per_role(self) -> MqttSettings:
        """Two inputs claiming to be the high float is a panel nobody can read."""
        seen: dict[str, int] = {}
        for one in self.inputs:
            if not one.role:
                continue
            if one.role in seen:
                raise ValueError(
                    f"Inputs {seen[one.role]} and {one.channel} both say they carry "
                    f"{one.title}. Each one lives on a single input."
                )
            seen[one.role] = one.channel
        return self

    @model_validator(mode="after")
    def answers_are_told_apart(self) -> MqttSettings:
        """Two clamps cannot read their answers off one topic and one path.

        An MQTT message carries no sender and no sign of what it is answering.
        The topic is the whole of its address, so two clamps reading the reply
        at the same path both match every answer, and one reading is filed
        under both pumps. That shipped once: it would also have convinced the
        history page that the second pump had a clamp fitted and drawn a line
        for a CT that is not installed.

        Sharing a topic is fine where the paths differ, because then only one
        finds anything in a given body.
        """
        asking = [clamp for clamp in self.clamps if clamp.asks]
        if len(asking) == 2 and (asking[0].answers_on, asking[0].answer_path) == (
            asking[1].answers_on,
            asking[1].answer_path,
        ):
            raise ValueError(
                f"Both clamps read their answer from {asking[0].answers_on!r} at "
                f"{asking[0].answer_path!r}. One reply would be recorded as both. "
                f"Ask with a different src so each gets its own reply topic."
            )
        return self

    # -- what the rest of the application asks --------------------------------

    @property
    def used_clamps(self) -> list[ClampSource]:
        return [clamp for clamp in self.clamps if clamp.configured]

    @property
    def used_inputs(self) -> list[ContactInput]:
        return [one for one in self.inputs if one.used]

    @property
    def used_health(self) -> list[HealthSource]:
        return [one for one in self.health if one.configured]

    @property
    def used_channels(self) -> list[ContactInput]:
        """What the dashboard draws a lamp for."""
        return self.used_inputs

    @property
    def channels(self) -> list[ContactInput]:
        """Every input, configured or not. The settings page draws all eight."""
        return self.inputs

    def channel_for(self, role: str) -> int | None:
        for one in self.inputs:
            if one.role == role and one.topic:
                return one.channel
        return None

    def label_for(self, channel: int) -> str:
        """What to call an input, falling back to its terminal marking."""
        for one in self.inputs:
            if one.channel == channel:
                return one.title
        return f"DI{channel}"

    def input_at(self, channel: int) -> ContactInput | None:
        for one in self.inputs:
            if one.channel == channel:
                return one
        return None

    @property
    def clamp_for_pump(self) -> dict[int, int]:
        """Which recorded channel holds each pump's readings."""
        return {clamp.pump: clamp.channel for clamp in self.clamps}

    def clamp_of(self, pump: int) -> ClampSource | None:
        for clamp in self.clamps:
            if clamp.pump == pump:
                return clamp
        return None


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

    # Who hears it. A separate question from how urgent it is: "the meter
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

    # How long to wait for a better explanation.
    #
    # Two seconds, because the check it is waiting for is "is another alert
    # already open", and that is a question about whether a row exists rather
    # than one that needs time to pass. The overload or the high float trips
    # first or in the same instant, so by the time this is evaluated the
    # detailed alert is usually already raised and this one is correctly
    # suppressed without any wait at all. The hold only has to cover the
    # debounce and a message hop, not a human's reaction.
    hold_s: int = Field(default=2, ge=0, le=300)


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

    # A minute, which is five times the twelve seconds the reference pit takes.
    #
    # It had no default at all and was off, because before the contacts were
    # wired a duration could only be guessed at from current. It is measured
    # now, so the rule can ship with a number and switched on.
    longer_than_ms: int | None = Field(default=60_000, ge=1_000, le=86_400_000)


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

    # Six hours, not two.
    #
    # Two was a guess made before anything had been measured. The reference pit
    # calls every thirty to forty five minutes through the night and every
    # seven to fifteen in the morning, so two hours of silence does look wrong
    # there -- but a quiet week, an empty building or a dry spell would trip it
    # repeatedly, and a rule that cries wolf is a rule somebody turns off. Six
    # hours is eight times the longest gap ever recorded there and still
    # catches a panel that died overnight.
    #
    # This is the rule most in need of tuning per building, and the settings
    # page says so.
    quiet_minutes: int | None = Field(default=360, ge=5, le=525_600)


class RunDriftRule(AlertRule):
    """A pump taking longer to shift the same pit.

    The duration's half of the load drift, and on hardware with run contacts it
    is the better half: a duration is measured from the panel's own signal and
    is exact, where amps are whatever the meter happened to report. A worn
    impeller, a partial blockage or a check valve starting to pass all show
    here first, and none of them shows in any single run.
    """

    # Seconds longer than the weeks before. Five on a pit that runs for twelve
    # is a run half again as long, which is well past anything a float's
    # position accounts for.
    longer_by_s: float | None = Field(default=5.0, gt=0, le=3600)


class BothPumpsRule(AlertRule):
    """The controller called the lag pump as well.

    Not the same event as the high water float, which is why it is not the same
    rule. The float can be wet without the controller deciding it needs both,
    and on a panel where the high float is not wired this is the only thing
    that says the pit is beating one pump.
    """


class PumpIdleRule(AlertRule):
    """One pump has not run while the other has been working.

    A duplex panel alternates, so the two counts should stay close. One pump
    sitting out while the other does everything is a pump that is not starting:
    a tripped overload nobody saw, a failed contactor coil, a seized motor.

    This matters more where the overload contacts are wired normally open, as
    they are on the reference panel, because then a broken sense wire reads as
    "no overload" forever and the trip itself is invisible. The pump going
    quiet is the symptom that is left.
    """

    idle_hours: int | None = Field(default=24, ge=1, le=8760)


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
    run_drift: RunDriftRule = Field(
        default_factory=lambda: RunDriftRule(
            severity=Severity.INFO,
            tell_when_it_clears=False,
            message=(
                "{pump} at {site} is taking {seconds} s to run, up from {was} s a few weeks ago."
            ),
        )
    )
    both_pumps: BothPumpsRule = Field(
        default_factory=lambda: BothPumpsRule(
            severity=Severity.WARNING,
            message=(
                "Both pumps are running at {site}: one could not keep up with the pit. Time {time}."
            ),
        )
    )
    pump_idle: PumpIdleRule = Field(
        default_factory=lambda: PumpIdleRule(
            severity=Severity.WARNING,
            message=(
                "{pump} at {site} has not run in {hours} h while the other one "
                "has. It may not be starting."
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
    "both_pumps",
    "pump_idle",
    "run_drift",
    "load_drift",
    "device_offline",
    "float_activity",
    "pump_running",
)


class SiteSettings(BaseModel):
    KEY: ClassVar[str] = "site"

    # The building. An address or a name, whichever somebody woken at two in the
    # morning would recognize: "123 Main St".
    #
    # This is not the name of the application, which is always PitWatch. It is
    # which pumps, and it goes in the subject line of every alert. It has no
    # default, because a placeholder here would end up printed on the public
    # policy pages, and "Ejector pit uses PitWatch to monitor" reads exactly as
    # badly as it sounds.
    name: str = ""
    timezone: str = "America/New_York"

    # Where the pit is, as a street address, so the rain over it can be looked
    # up. Separate from ``name`` on purpose even though on this installation
    # they read almost the same: ``name`` is a label chosen to be recognized at
    # two in the morning and could reasonably be "Main Street, rear
    # building", while this one has to be something a geocoder can find.
    #
    # Only ever sent to the geocoder, and only when somebody presses the button
    # on the settings page. The recurring weather calls send the coordinates
    # below instead, rounded.
    address: str = ""
    # What the geocoder made of it. Stored rather than looked up each time, so
    # the address goes over the wire once rather than every quarter of an hour,
    # and so that somebody who would rather not type an address at all can put
    # coordinates straight in.
    #
    # Rounded to two decimal places when they are saved, which is about a
    # kilometer. Every rainfall model in play is coarser than that, so the
    # rounding costs nothing that could be measured and stops the request
    # pointing at a building.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # What the geocoder called the place it found, kept so the settings page
    # can show what was matched. A lookup that quietly found the right street
    # in the wrong state is the failure worth catching, and it is only catchable
    # by printing the answer.
    located: str = ""

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    # The address this is reachable at from outside, used to build invitation
    # links. Behind a reverse proxy the application cannot work this out for
    # itself: it sees the proxy's idea of the request, not the name somebody
    # typed. Leave it empty and links are built from the incoming request,
    # which is right often enough but not always.
    base_url: str = ""
    # Shown on the public terms and conditions, because a carrier reviewing a
    # toll-free registration wants a way to contact somebody, and so does a
    # recipient who wants to be taken off the list.
    contact_email: str = ""
    contact_phone: str = ""

    # Who is answerable for this installation, as it should read on a page
    # anyone can open: "David McGowan, Sole Proprietor", "Main Street Board
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
        """A phrase for prose: "the pumps at 123 Main St"."""
        return f"the pumps at {self.where}" if self.where else "the pumps in this building"

    # A delay before sending and a cooldown between repeats used to live here.
    # Both were rendered, parsed and stored, and read by no code anywhere,
    # which is worse than missing: somebody tunes one and believes they have
    # changed something. Waiting belongs to each rule's own hold, because the
    # right wait differs per rule, and repeats are already impossible without a
    # timer. One open alert per rule is a unique index, so a float that stays
    # up is one message rather than one per sweep, and clearing it is what
    # makes the next trip worth sending.


class WeatherSettings(BaseModel):
    """Rain over the pit, from Open-Meteo.

    There is nothing to authenticate. Open-Meteo serves NOAA's own HRRR and GFS
    output with no key and no sign up under ten thousand calls a day for
    non-commercial use, and this polls four times an hour. That absence is the
    reason it was chosen over the alternatives: every other credential in this
    application costs a password box, a rule about never sending it back to the
    browser, a way to clear it and a test that all three hold. This costs a
    latitude and a longitude.

    So the only settings are whether to ask at all and what unit to answer in.
    The real switch is whether the site has coordinates: without them there is
    nothing to ask about, and the poller says so and sleeps.
    """

    KEY: ClassVar[str] = "weather"

    enabled: bool = True
    # Inches or millimeters. Stored in millimeters either way; this decides
    # only what the pages print. Inches by default because the people reading
    # this one are in New York, and a tenth of an inch of rain is a sentence
    # they already understand.
    units: str = Field(default="in", pattern="^(in|mm)$")


SETTING_MODELS: tuple[type[BaseModel], ...] = (
    SiteSettings,
    WeatherSettings,
    MqttSettings,
    AlertsSettings,
    PumpsSettings,
    SmtpSettings,
    SmsSettings,
)
