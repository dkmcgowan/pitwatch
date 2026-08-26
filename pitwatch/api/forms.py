"""Turning HTML form posts into settings models.

Browsers post strings, and an empty text box posts an empty string rather than
nothing at all. Pydantic is strict about both, correctly, so the translation
happens here instead of scattering coercions through the route handlers.

The rule throughout: an empty box means "not set", not "zero". The difference
matters. An overcurrent threshold of zero would alert on every run; an
overcurrent threshold that is not set means do not run that check.
"""

from __future__ import annotations

from starlette.datastructures import FormData

from pitwatch.schemas import (
    UNUSED,
    ChannelMap,
    PumpSettings,
    PumpsSettings,
    ShellySettings,
    SignalDef,
    SiteSettings,
    SmsSettings,
    SmtpSettings,
    WaveshareSettings,
    default_signals,
    signal_key,
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


def optional_integer(form: FormData, name: str) -> int | None:
    value = optional_number(form, name)
    return int(value) if value is not None else None


def site_from(form: FormData) -> SiteSettings:
    return SiteSettings(
        name=text(form, "site_name"),
        timezone=text(form, "site_timezone", "America/New_York") or "America/New_York",
        base_url=text(form, "site_base_url").rstrip("/"),
        contact_email=text(form, "site_contact_email"),
        contact_phone=text(form, "site_contact_phone"),
        notify_delay_s=integer(form, "notify_delay_s", 30),
        notify_cooldown_s=integer(form, "notify_cooldown_s", 900),
    )


def shelly_from(form: FormData, existing: ShellySettings | None = None) -> ShellySettings:
    # Same rule as the SMTP password: the stored device password is never sent
    # to a browser, so an empty box means leave it alone.
    password = optional_text(form, "shelly_password")
    if password is None and existing is not None and not checkbox(form, "shelly_clear_password"):
        password = existing.password

    return ShellySettings(
        enabled=checkbox(form, "shelly_enabled"),
        host=text(form, "shelly_host"),
        mode=text(form, "shelly_mode", "client") or "client",
        password=password,
        # Both are asked for and both are stored. The form keeps them apart in
        # the browser; the model checks it again here, because a form is not a
        # guarantee.
        pump1_channel=integer(form, "shelly_pump1_channel", 0),
        pump2_channel=integer(form, "shelly_pump2_channel", 1),
        heartbeat_s=integer(form, "shelly_heartbeat_s", 30),
    )


def signals_from(form: FormData, existing: WaveshareSettings | None = None) -> list[SignalDef]:
    """The signal catalog as the form has it.

    The rows are paired by position: each one posts a signal_key and a
    signal_label. A row that is already saved posts its key back unchanged, so
    renaming it changes only what it is called and leaves every reading already
    recorded under that key still pointing at it.

    A row whose name has been emptied is dropped. That is deliberately all
    removing one is: there is no separate delete request to get wrong, and it
    works with JavaScript off.
    """
    labels = form.getlist("signal_label")
    if not labels:
        # A form that does not carry the catalog at all, rather than one
        # carrying an empty catalog. Leave what is saved alone.
        return list(existing.signals) if existing is not None else default_signals()

    keys = form.getlist("signal_key")
    signals: list[SignalDef] = []
    taken: set[str] = set()
    for index, raw_label in enumerate(labels):
        label = str(raw_label).strip()
        if not label:
            continue
        posted = str(keys[index]).strip() if index < len(keys) else ""
        # Run it through the same cleaner either way. It leaves a key that is
        # already valid alone, and it means nothing a browser posts can reach
        # the database as a key that will not validate.
        key = signal_key(posted or label)
        if key == UNUSED or key in taken:
            # Two rows landing on one key, which duplicating a name will do.
            # Numbering the second is kinder than refusing the save and leaving
            # somebody to work out which two collided.
            base = "signal" if key == UNUSED else key
            suffix = 2
            while f"{base}_{suffix}" in taken:
                suffix += 1
            key = f"{base}_{suffix}"
        taken.add(key)
        signals.append(SignalDef(key=key, label=label))
    return signals


def waveshare_from(form: FormData, existing: WaveshareSettings | None = None) -> WaveshareSettings:
    channels = []
    for number_ in range(1, 9):
        channels.append(
            ChannelMap(
                channel=number_,
                signal=text(form, f"channel_{number_}_signal", UNUSED) or UNUSED,
                invert=checkbox(form, f"channel_{number_}_invert"),
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
        signals=signals_from(form, existing),
        channels=channels,
    )


def _pump_from(form: FormData, prefix: str, fallback_name: str) -> PumpSettings:
    return PumpSettings(
        name=text(form, f"{prefix}_name", fallback_name) or fallback_name,
        running_amps=number(form, f"{prefix}_running_amps", 1.0),
        nameplate_amps=optional_number(form, f"{prefix}_nameplate_amps"),
        overcurrent_amps=optional_number(form, f"{prefix}_overcurrent_amps"),
        overcurrent_hold_ms=integer(form, f"{prefix}_overcurrent_hold_ms", 1500),
    )


def pumps_from(form: FormData) -> PumpsSettings:
    return PumpsSettings(
        pump1=_pump_from(form, "pump1", "Pump 1"),
        pump2=_pump_from(form, "pump2", "Pump 2"),
        inrush_ignore_ms=integer(form, "inrush_ignore_ms", 800),
        # Every threshold that raises an alert is optional, and empty means do
        # not run that check. Nothing here treats an empty box as a zero.
        max_runtime_ms=optional_integer(form, "max_runtime_ms"),
        restart_gap_ms=optional_integer(form, "restart_gap_ms"),
        restart_streak=integer(form, "restart_streak", 4),
        quiet_minutes_before_flag=optional_integer(form, "quiet_minutes_before_flag"),
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
    # Same rule as every other secret: never rendered back to the browser, so an
    # empty box means leave it alone and there is a checkbox for clearing it.
    secret = text(form, "sms_aws_secret_access_key")
    if checkbox(form, "sms_clear_secret"):
        secret = ""
    elif not secret:
        secret = existing.aws_secret_access_key

    return SmsSettings(
        enabled=checkbox(form, "sms_enabled"),
        provider=text(form, "sms_provider", "sns") or "sns",
        aws_region=text(form, "sms_aws_region", "us-east-1") or "us-east-1",
        aws_access_key_id=text(form, "sms_aws_access_key_id"),
        aws_secret_access_key=secret,
        origination_number=text(form, "sms_origination_number"),
        sender_id=text(form, "sms_sender_id"),
        gateway_domain=text(form, "sms_gateway_domain"),
    )
