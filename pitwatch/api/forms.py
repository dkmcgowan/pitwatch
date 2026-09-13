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

from pitwatch.ingest import weather
from pitwatch.schemas import (
    ALERT_ORDER,
    AlertsSettings,
    ClampSource,
    ContactInput,
    HealthSource,
    MqttSettings,
    PanelButtonSettings,
    PumpSettings,
    PumpsSettings,
    SiteSettings,
    SmsSettings,
    SmtpSettings,
    SummarySettings,
    TideSettings,
    WeatherSettings,
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


def coordinate(form: FormData, field: str, limit: float) -> float | None:
    """A latitude or a longitude, or None when the box is empty.

    None rather than zero. Zero degrees is a real place in the Gulf of Guinea,
    and a site that quietly ends up there would be given somebody else's rain
    rather than an error.
    """
    raw = text(form, field)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not -limit <= value <= limit:
        return None
    # Rounded on the way in rather than on the way out, so what is stored is
    # what gets sent and the settings page shows the reader exactly what leaves
    # the building.
    return weather.rounded(value)


def site_from(form: FormData) -> SiteSettings:
    return SiteSettings(
        name=text(form, "site_name"),
        timezone=text(form, "site_timezone", "America/New_York") or "America/New_York",
        address=text(form, "site_address"),
        latitude=coordinate(form, "site_latitude", 90),
        longitude=coordinate(form, "site_longitude", 180),
        located=text(form, "site_located"),
        base_url=text(form, "site_base_url").rstrip("/"),
        contact_email=text(form, "site_contact_email"),
        contact_phone=text(form, "site_contact_phone"),
        operator=text(form, "site_operator"),
        operator_locality=text(form, "site_operator_locality"),
    )


def weather_from(form: FormData) -> WeatherSettings:
    return WeatherSettings(
        enabled=checkbox(form, "weather_enabled"),
        units=text(form, "weather_units", "in") or "in",
    )


def tide_from(form: FormData) -> TideSettings:
    return TideSettings(
        enabled=checkbox(form, "tide_enabled"),
        station=text(form, "tide_station"),
        station_name=text(form, "tide_station_name"),
        units=text(form, "tide_units", "ft") or "ft",
    )


def panel_button_from(form: FormData) -> PanelButtonSettings:
    return PanelButtonSettings(
        enabled=checkbox(form, "panel_button_enabled"),
        topic=text(form, "panel_button_topic"),
        switch_id=integer(form, "panel_button_switch_id", 0),
        silence_ms=integer(form, "panel_button_silence_ms", 400),
        reset_ms=integer(form, "panel_button_reset_ms", 3500),
        auto_recover=checkbox(form, "panel_button_auto_recover"),
        max_trips=integer(form, "panel_button_max_trips", 3),
        within_minutes=integer(form, "panel_button_within_minutes", 60),
    )


def clamp_from(form: FormData, pump: int) -> ClampSource:
    """One clamp's row. A reading is a number, so there is nothing to pick."""
    return ClampSource(
        pump=pump,
        topic=text(form, f"clamp{pump}_topic"),
        path=text(form, f"clamp{pump}_path"),
        ask_topic=text(form, f"clamp{pump}_ask_topic"),
        ask_payload=text(form, f"clamp{pump}_ask_payload"),
        reply_topic=text(form, f"clamp{pump}_reply_topic"),
        reply_path=text(form, f"clamp{pump}_reply_path"),
        ask_every_s=number(form, f"clamp{pump}_ask_every_s", 1.0),
    )


def contact_from(form: FormData, channel: int) -> ContactInput:
    """One input's row: what it carries, where it arrives, and which way round."""
    return ContactInput(
        channel=channel,
        # What the panel put on this input, chosen from what the dashboard can
        # draw. Blank means nothing has said.
        role=text(form, f"input_{channel}_role"),
        topic=text(form, f"input_{channel}_topic"),
        path=text(form, f"input_{channel}_path"),
        # A select rather than a checkbox, because "invert" asks you to think
        # backwards and this asks you what the panel does.
        invert=text(form, f"input_{channel}_on_when") == "absent",
    )


def health_from(form: FormData, index: int) -> HealthSource:
    return HealthSource(
        name=text(form, f"health_{index}_name"),
        topic=text(form, f"health_{index}_topic"),
        expect_s=integer(form, f"health_{index}_expect_s", 0),
    )


def mqtt_from(form: FormData, existing: MqttSettings | None = None) -> MqttSettings:
    """The broker, the clamps, the inputs and the health checks.

    One form where there were two device sections, because there is one
    connection now and the sections were named after the hardware.
    """
    # Same rule as the SMTP password: the stored one is never sent to the
    # browser, so an empty box means unchanged rather than cleared.
    password = optional_text(form, "mqtt_password")
    if password is None and existing is not None and not checkbox(form, "mqtt_clear_password"):
        password = existing.password

    return MqttSettings(
        enabled=checkbox(form, "mqtt_enabled"),
        host=text(form, "mqtt_host"),
        port=integer(form, "mqtt_port", 1883),
        username=text(form, "mqtt_username"),
        password=password or "",
        encrypted=checkbox(form, "mqtt_encrypted"),
        client_id=text(form, "mqtt_client_id", "pitwatch") or "pitwatch",
        debounce_ms=integer(form, "mqtt_debounce_ms", 0),
        clamps=[clamp_from(form, pump) for pump in (1, 2)],
        inputs=[contact_from(form, channel) for channel in range(1, 9)],
        health=[health_from(form, index) for index in (0, 1)],
    )


def _pump_from(form: FormData, prefix: str, fallback_name: str) -> PumpSettings:
    return PumpSettings(name=text(form, f"{prefix}_name", fallback_name) or fallback_name)


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


def summary_from(form: FormData, existing: SummarySettings) -> SummarySettings:
    # Same rule as every other secret: never rendered back to the browser, so an
    # empty box means leave it alone and there is a checkbox for clearing it.
    key = text(form, "summary_api_key")
    if checkbox(form, "summary_clear_key"):
        key = ""
    elif not key:
        key = existing.api_key

    return SummarySettings(
        description=text(form, "summary_description"),
        schedule=text(form, "summary_schedule", existing.schedule) or existing.schedule,
        schedule_window=(
            text(form, "summary_schedule_window", existing.schedule_window)
            or existing.schedule_window
        ),
        schedule_at=text(form, "summary_schedule_at", existing.schedule_at) or existing.schedule_at,
        notify=checkbox(form, "summary_notify"),
        api_key=key,
        model=text(form, "summary_model", existing.model) or existing.model,
        base_url=text(form, "summary_base_url", existing.base_url) or existing.base_url,
    )


def _kept_secret(form: FormData, field: str, clear_field: str, existing: str) -> str:
    """Same rule as every other secret: never rendered back to the browser, so
    an empty box means leave it alone and there is a checkbox for clearing it.

    Without the checkbox there is no way to remove one at all, since the empty
    box that would say so is also what an untouched box looks like."""
    typed = text(form, field)
    if checkbox(form, clear_field):
        return ""
    return typed or existing


def sms_from(form: FormData, existing: SmsSettings) -> SmsSettings:
    return SmsSettings(
        enabled=checkbox(form, "sms_enabled"),
        twilio_account_sid=text(form, "sms_twilio_account_sid"),
        twilio_key_sid=text(form, "sms_twilio_key_sid"),
        twilio_auth_token=_kept_secret(
            form,
            "sms_twilio_auth_token",
            "sms_clear_twilio_token",
            existing.twilio_auth_token,
        ),
        twilio_messaging_service_sid=text(form, "sms_twilio_messaging_service_sid"),
        twilio_from=text(form, "sms_twilio_from"),
    )
