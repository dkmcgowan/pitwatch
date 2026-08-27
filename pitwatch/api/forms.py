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
    ALERT_ORDER,
    DASHBOARD_ROLES,
    AlertsSettings,
    ChannelMap,
    DashboardSettings,
    PumpSettings,
    PumpsSettings,
    ShellySettings,
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
        notify_delay_s=integer(form, "notify_delay_s", 5),
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


def waveshare_from(form: FormData) -> WaveshareSettings:
    """The I/O module and what each of its eight inputs is called.

    A blank name means nothing is wired to that input. There is nothing else to
    read: the input number is the identity, so there is no separate list of
    names to keep in step with it and no way for the two to disagree.
    """
    channels = [
        ChannelMap(
            channel=number_,
            label=text(form, f"channel_{number_}_label"),
            # A select rather than a checkbox, because "invert" asks you to
            # think backwards and this asks you what the panel does.
            invert=text(form, f"channel_{number_}_on_when") == "absent",
        )
        for number_ in range(1, 9)
    ]
    return WaveshareSettings(
        enabled=checkbox(form, "waveshare_enabled"),
        host=text(form, "waveshare_host"),
        port=integer(form, "waveshare_port", 502),
        unit_id=integer(form, "waveshare_unit_id", 1),
        poll_ms=integer(form, "waveshare_poll_ms", 200),
        timeout_s=number(form, "waveshare_timeout_s", 3.0),
        debounce_ms=integer(form, "waveshare_debounce_ms", 500),
        channels=channels,
    )


def dashboard_from(form: FormData) -> DashboardSettings:
    """Which input drives which lamp. An empty box means that lamp is unassigned."""
    return DashboardSettings(
        **{role: optional_integer(form, f"role_{role}") for role, _ in DASHBOARD_ROLES}
    )


def _pump_from(form: FormData, prefix: str, fallback_name: str) -> PumpSettings:
    return PumpSettings(
        name=text(form, f"{prefix}_name", fallback_name) or fallback_name,
        running_amps=number(form, f"{prefix}_running_amps", 1.0),
        nameplate_amps=optional_number(form, f"{prefix}_nameplate_amps"),
    )


def pumps_from(form: FormData) -> PumpsSettings:
    return PumpsSettings(
        pump1=_pump_from(form, "pump1", "Pump 1"),
        pump2=_pump_from(form, "pump2", "Pump 2"),
    )


def alerts_from(form: FormData, existing: AlertsSettings) -> AlertsSettings:
    """Every rule, read back off the one page that owns them.

    Each rule posts its fields under its own key, so nothing here has to know
    which rule is which beyond the extra numbers a few of them carry.
    """
    rules: dict[str, object] = {}
    for key in ALERT_ORDER:
        current = getattr(existing, key)
        values: dict[str, object] = {
            "enabled": checkbox(form, f"{key}_enabled"),
            "severity": text(form, f"{key}_severity", "warning") or "warning",
            "admins_only": checkbox(form, f"{key}_admins_only"),
            "message": text(form, f"{key}_message") or current.message,
            "tell_when_it_clears": checkbox(form, f"{key}_tell_when_it_clears"),
        }
        # The handful of rules that carry a threshold. Read by name off the
        # model rather than from a list here, so adding a field to a rule does
        # not mean remembering to add it in a second place.
        for name in type(current).model_fields:
            if name in values:
                continue
            field = f"{key}_{name}"
            annotation = type(current).model_fields[name].annotation
            if annotation in (int, float):
                values[name] = number(form, field, getattr(current, name))
            else:
                values[name] = optional_number(form, field)
        rules[key] = type(current)(**values)
    return AlertsSettings(**rules)


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
