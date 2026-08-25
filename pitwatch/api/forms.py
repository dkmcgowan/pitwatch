"""Turning HTML form posts into settings models.

Browsers post strings, and an empty text box posts an empty string rather than
nothing at all. Pydantic is strict about both, correctly, so the translation
happens here instead of scattering coercions through the route handlers.

The rule throughout: an empty box means "not set", not "zero". The difference
matters. An overcurrent threshold of zero would alert on every run; an
overcurrent threshold that is not set means do not run that check.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData

from pitwatch.schemas import (
    ChannelMap,
    PumpSettings,
    PumpsSettings,
    ShellySettings,
    Signal,
    SiteSettings,
    SmsSettings,
    SmtpSettings,
    WaveshareSettings,
)


def text(form: FormData, name: str, default: str = "") -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else default


def optional_text(form: FormData, name: str) -> str | None:
    value = text(form, name)
    return value or None


def checkbox(form: FormData, name: str) -> bool:
    """An unchecked box posts nothing at all, which is how it reads as False."""
    return form.get(name) is not None


def number(form: FormData, name: str, default: float) -> float:
    value = text(form, name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{name} has to be a number") from error


def optional_number(form: FormData, name: str) -> float | None:
    value = text(form, name)
    if not value:
        return None
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{name} has to be a number") from error


def integer(form: FormData, name: str, default: int) -> int:
    return int(number(form, name, default))


def site_from(form: FormData) -> SiteSettings:
    return SiteSettings(
        name=text(form, "site_name", "Ejector pit") or "Ejector pit",
        location=text(form, "site_location"),
        timezone=text(form, "site_timezone", "America/New_York") or "America/New_York",
        notify_delay_s=integer(form, "notify_delay_s", 30),
        notify_cooldown_s=integer(form, "notify_cooldown_s", 900),
    )


def shelly_from(form: FormData) -> ShellySettings:
    pump1_channel = integer(form, "shelly_pump1_channel", 0)
    return ShellySettings(
        enabled=checkbox(form, "shelly_enabled"),
        host=text(form, "shelly_host"),
        mode=text(form, "shelly_mode", "client") or "client",
        password=optional_text(form, "shelly_password"),
        pump1_channel=pump1_channel,
        # One choice, not two. Asking twice invites a form where both pumps are
        # on clamp 0, and the second answer is always the first one inverted.
        pump2_channel=1 - pump1_channel,
        heartbeat_s=integer(form, "shelly_heartbeat_s", 30),
    )


def waveshare_from(form: FormData) -> WaveshareSettings:
    channels = []
    for number_ in range(1, 9):
        channels.append(
            ChannelMap(
                channel=number_,
                signal=Signal(text(form, f"channel_{number_}_signal", "unused") or "unused"),
                normally_closed=checkbox(form, f"channel_{number_}_nc"),
                debounce_ms=integer(form, f"channel_{number_}_debounce", 500),
            )
        )
    return WaveshareSettings(
        enabled=checkbox(form, "waveshare_enabled"),
        host=text(form, "waveshare_host"),
        port=integer(form, "waveshare_port", 502),
        unit_id=integer(form, "waveshare_unit_id", 1),
        poll_ms=integer(form, "waveshare_poll_ms", 200),
        timeout_s=number(form, "waveshare_timeout_s", 3.0),
        channels=channels,
    )


def _pump_from(form: FormData, prefix: str, fallback_name: str) -> PumpSettings:
    return PumpSettings(
        name=text(form, f"{prefix}_name", fallback_name) or fallback_name,
        running_amps=number(form, f"{prefix}_running_amps", 1.0),
        nameplate_amps=optional_number(form, f"{prefix}_nameplate_amps"),
        overcurrent_amps=optional_number(form, f"{prefix}_overcurrent_amps"),
        overcurrent_hold_s=integer(form, f"{prefix}_overcurrent_hold_s", 15),
        undercurrent_amps=optional_number(form, f"{prefix}_undercurrent_amps"),
    )


def pumps_from(form: FormData) -> PumpsSettings:
    return PumpsSettings(
        pump1=_pump_from(form, "pump1", "Pump 1"),
        pump2=_pump_from(form, "pump2", "Pump 2"),
        inrush_ignore_s=number(form, "inrush_ignore_s", 2.0),
        stop_hold_s=number(form, "stop_hold_s", 3.0),
        max_runtime_s=integer(form, "max_runtime_s", 600),
        max_starts_per_hour=integer(form, "max_starts_per_hour", 20),
        quiet_hours_before_flag=integer(form, "quiet_hours_before_flag", 72),
    )


def smtp_from(form: FormData, existing: SmtpSettings) -> SmtpSettings:
    # A password box is rendered empty even when one is stored, so that the
    # stored value is never sent to a browser. An empty box therefore means
    # "leave it alone", and there is a separate checkbox for clearing it.
    password = text(form, "smtp_password")
    if checkbox(form, "smtp_clear_password"):
        password = ""
    elif not password:
        password = existing.password

    return SmtpSettings(
        enabled=checkbox(form, "smtp_enabled"),
        host=text(form, "smtp_host"),
        port=integer(form, "smtp_port", 587),
        username=text(form, "smtp_username"),
        password=password,
        security=text(form, "smtp_security", "starttls") or "starttls",
        from_address=text(form, "smtp_from_address"),
        from_name=text(form, "smtp_from_name", "PitWatch") or "PitWatch",
    )


def sms_from(form: FormData, existing: SmsSettings) -> SmsSettings:
    token = text(form, "sms_auth_token")
    if checkbox(form, "sms_clear_token"):
        token = ""
    elif not token:
        token = existing.auth_token

    return SmsSettings(
        enabled=checkbox(form, "sms_enabled"),
        provider=text(form, "sms_provider", "twilio") or "twilio",
        account_sid=text(form, "sms_account_sid"),
        auth_token=token,
        from_number=text(form, "sms_from_number"),
        gateway_domain=text(form, "sms_gateway_domain"),
    )


def recipients_from(form: FormData) -> list[dict[str, Any]]:
    """Read the repeating recipient rows.

    Rows are numbered by the template rather than being a list, because a row
    that is deleted in the browser leaves a gap and a positional list would
    then reassign everyone else's details to the wrong person.
    """
    recipients = []
    for key in form:
        if not key.startswith("recipient_") or not key.endswith("_name"):
            continue
        index = key.removeprefix("recipient_").removesuffix("_name")
        name = text(form, key)
        email = text(form, f"recipient_{index}_email")
        phone = text(form, f"recipient_{index}_phone")
        if not name or (not email and not phone):
            continue
        recipients.append(
            {
                "name": name,
                "email": email or None,
                "phone": phone or None,
                "min_severity": text(form, f"recipient_{index}_min_severity", "warning")
                or "warning",
                "enabled": checkbox(form, f"recipient_{index}_enabled"),
            }
        )
    return recipients
