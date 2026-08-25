"""The shape of everything that gets configured.

Each model here is stored as one JSON row in the setting table, under the key in
its ``KEY`` attribute. Validation lives in the model, so a value that reaches
the database has already been checked, and a value read back from an older
version of the application picks up new fields as defaults rather than failing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


class Signal(StrEnum):
    """What a Waveshare digital input is wired to.

    These names are the vocabulary the rest of the application speaks. A rule
    asks for HIGH_WATER, not for channel 3, so rewiring the panel is a settings
    change and not a code change.
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


SIGNAL_LABELS: dict[Signal, str] = {
    Signal.UNUSED: "Not connected",
    Signal.LEAD_FLOAT: "Lead float",
    Signal.LAG_FLOAT: "Lag float",
    Signal.HIGH_WATER: "High water alarm float",
    Signal.PANEL_ALARM: "Panel alarm contact",
    Signal.PUMP1_RUN: "Pump 1 running",
    Signal.PUMP2_RUN: "Pump 2 running",
    Signal.PUMP1_OVERLOAD: "Pump 1 overload tripped",
    Signal.PUMP2_OVERLOAD: "Pump 2 overload tripped",
}


class ChannelMap(BaseModel):
    """One Waveshare digital input.

    ``normally_closed`` is the setting that is easiest to get wrong and worst to
    get wrong. A normally closed contact reads as closed when nothing is
    happening and opens on the event, so without this flag every alarm would be
    permanently on and go quiet at exactly the moment it mattered. Panels are
    not consistent about which way round they wire an overload relay, so it is
    per channel rather than global.
    """

    channel: int = Field(ge=1, le=8)
    signal: Signal = Signal.UNUSED
    normally_closed: bool = False
    # Contacts bounce, and float switches bounce for longer than most. A state
    # has to hold for this long before it counts as a change.
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
    channels: list[ChannelMap] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_and_check_channels(self) -> WaveshareSettings:
        by_channel = {channel.channel: channel for channel in self.channels}
        self.channels = [
            by_channel.get(number, ChannelMap(channel=number)) for number in range(1, 9)
        ]
        assigned = [c.signal for c in self.channels if c.signal is not Signal.UNUSED]
        duplicates = {s for s in assigned if assigned.count(s) > 1}
        if duplicates:
            names = ", ".join(sorted(SIGNAL_LABELS[s] for s in duplicates))
            raise ValueError(f"Each signal can only be on one channel. Repeated: {names}")
        return self

    def channel_for(self, signal: Signal) -> ChannelMap | None:
        return next((c for c in self.channels if c.signal is signal), None)


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
    # Which em1 instance is on which pump. The clamps are interchangeable and
    # nothing on the device says which motor it is around, so this is asked
    # rather than assumed.
    pump1_channel: int = Field(default=0, ge=0, le=1)
    pump2_channel: int = Field(default=1, ge=0, le=1)
    # The device pushes on change. This poll exists only to notice that it has
    # stopped pushing, which a silent socket does not tell us.
    heartbeat_s: int = Field(default=30, ge=5, le=600)

    @model_validator(mode="after")
    def channels_differ(self) -> ShellySettings:
        if self.pump1_channel == self.pump2_channel:
            raise ValueError("The two pumps cannot share one clamp")
        return self


class PumpSettings(BaseModel):
    """Per pump electrical expectations.

    ``running_amps`` is the line between off and on. It is not zero: a clamp
    reads a little noise, and a contactor coil or a control transformer on the
    same conductor reads more than a little.
    """

    name: str = "Pump"
    running_amps: float = Field(default=1.0, ge=0)
    # Full load amps off the motor nameplate. Everything below is a multiple of
    # it, so entering it correctly is worth more than tuning the rest.
    nameplate_amps: float | None = Field(default=None, gt=0)
    # A run drawing more than this for longer than overcurrent_hold_s is an
    # overload the panel has not tripped on yet.
    overcurrent_amps: float | None = Field(default=None, gt=0)
    overcurrent_hold_s: int = Field(default=15, ge=1, le=3600)
    # Drawing far less than usual means the impeller is spinning in air, or the
    # coupling has gone, or a phase is missing.
    undercurrent_amps: float | None = Field(default=None, ge=0)


class PumpsSettings(BaseModel):
    KEY: ClassVar[str] = "pumps"

    pump1: PumpSettings = Field(default_factory=lambda: PumpSettings(name="Pump 1"))
    pump2: PumpSettings = Field(default_factory=lambda: PumpSettings(name="Pump 2"))

    # A motor draws six to eight times its running current for a fraction of a
    # second at start. Averaging that into the run makes a healthy pump look
    # overloaded, so the first seconds are dropped from every average. The peak
    # is still recorded separately.
    inrush_ignore_s: float = Field(default=2.0, ge=0, le=30)
    # Current has to fall below the running threshold for this long before a run
    # counts as finished, so a momentary dip does not split one run into two.
    stop_hold_s: float = Field(default=3.0, ge=0.5, le=120)
    # Longer than this and the pump is running on a stuck float or against a
    # blockage, neither of which ends well.
    max_runtime_s: int = Field(default=600, ge=30, le=86_400)
    # More starts than this in an hour means the check valve is passing and the
    # pit is refilling from the discharge pipe.
    max_starts_per_hour: int = Field(default=20, ge=1, le=500)
    # No run at all in this many hours is either a very dry week or a dead
    # sensor. Worth a quiet flag either way.
    quiet_hours_before_flag: int = Field(default=72, ge=1, le=8760)

    def for_pump(self, pump: int) -> PumpSettings:
        return self.pump1 if pump == 1 else self.pump2


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
    # 'twilio' sends through Twilio. 'email_gateway' sends a short email to a
    # carrier address such as 5551234567@vtext.com, which costs nothing and is
    # delivered at the carrier's convenience. Which is right depends on whether
    # a delayed flood alarm is acceptable.
    provider: str = Field(default="twilio", pattern="^(twilio|email_gateway)$")
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    # Only used by email_gateway, for example "vtext.com".
    gateway_domain: str = ""


class SiteSettings(BaseModel):
    KEY: ClassVar[str] = "site"

    name: str = "Ejector pit"
    # Free text, put in every alert. "123 Example St, basement, rear" saves a
    # phone call at two in the morning.
    location: str = ""
    timezone: str = "America/New_York"
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
