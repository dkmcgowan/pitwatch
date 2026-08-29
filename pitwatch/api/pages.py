"""Page routes: setup, sign in, settings.

The dashboard is not here. It reads live data and lives in its own module.

One decision worth stating: the setup page is reachable without signing in, but
only while there is no account. Once one exists, the route redirects. That is
what makes the first boot usable at all without shipping a default password,
and it closes as soon as it has been used. Anyone who can reach the port during
that window can claim the install, which is the same window every appliance
has, and is why the README does not suggest putting this on the internet.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from pitwatch import auth
from pitwatch.api import forms
from pitwatch.domain import alerts as alert_specs
from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import DASHBOARD_ROLES
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter()


def _context(request: Request, **extra) -> dict:
    store: SettingsStore = request.app.state.settings
    return {
        "site": store.site,
        "user": auth.current_user(request),
        "roles": DASHBOARD_ROLES,
        **extra,
    }


def _templates(request: Request):
    return request.app.state.templates


# -- setup ------------------------------------------------------------------


@router.get("/setup", include_in_schema=False)
async def setup_page(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = request.app.state.settings
    if await store.is_setup_complete():
        return RedirectResponse("/settings", status_code=303)
    return _templates(request).TemplateResponse(
        request,
        "setup.html",
        _context(
            request,
            shelly=store.shelly,
            inputs=store.inputs,
            pumps=store.pumps,
            error=None,
        ),
    )


@router.post("/setup", include_in_schema=False)
async def setup_submit(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = request.app.state.settings
    form = await request.form()

    try:
        site = forms.site_from(form)
        shelly = forms.shelly_from(form, store.shelly)
        inputs = forms.inputs_from(form, store.inputs)
        pumps = forms.pumps_from(form)
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "setup.html",
            _context(
                request,
                shelly=store.shelly,
                inputs=store.inputs,
                pumps=store.pumps,
                error=_readable(error),
            ),
            status_code=400,
        )

    for value in (site, shelly, inputs, pumps):
        await store.put(value)

    # The person doing the setup is the first person alerts should reach, and
    # asking again on a profile page afterwards is asking twice.
    await _save_own_details(request, admin, form)

    await store.mark_setup_complete()
    log.info("Setup completed by %s", admin.username)
    return RedirectResponse("/", status_code=303)


# -- settings ---------------------------------------------------------------


@router.get("/settings", include_in_schema=False)
async def settings_page(request: Request, admin: auth.IsAdmin, saved: str | None = None):
    store: SettingsStore = request.app.state.settings
    return _templates(request).TemplateResponse(
        request,
        "settings.html",
        _context(
            request,
            shelly=store.shelly,
            inputs=store.inputs,
            pumps=store.pumps,
            smtp=store.smtp,
            sms=store.sms,
            dashboard=store.dashboard,
            saved=saved,
            error=None,
        ),
    )


# -- alerts ------------------------------------------------------------------
#
# Its own place in the header rather than a room off the settings page. A dozen
# rules each carrying a level, an audience, a message and sometimes a threshold
# is not a section of anything, and a settings page whose job is to send you
# somewhere else is a menu pretending to be a page.
#
# It owns every threshold that raises an alert. A number on the pumps page
# tells you nothing about what happens when it is crossed; the same number
# beside its own message tells you everything.


@router.get("/alerts", include_in_schema=False)
async def alerts_page(request: Request, admin: auth.IsAdmin, saved: str | None = None):
    store: SettingsStore = request.app.state.settings
    return _templates(request).TemplateResponse(
        request,
        "alerts.html",
        _context(
            request,
            specs=alert_specs.SPECS,
            rules=store.alerts.by_key,
            saved=saved is not None,
            error=None,
        ),
    )


@router.post("/alerts", include_in_schema=False)
async def alerts_save(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = request.app.state.settings
    form = await request.form()
    try:
        await store.put(forms.alerts_from(form, store.alerts))
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "alerts.html",
            _context(
                request,
                specs=alert_specs.SPECS,
                rules=store.alerts.by_key,
                saved=False,
                error=_readable(error),
            ),
            status_code=400,
        )
    log.info("%s updated the alert rules", admin.username)
    return RedirectResponse("/alerts?saved=1", status_code=303)


@router.post("/settings/{section}", include_in_schema=False)
async def settings_save(request: Request, section: str, admin: auth.IsAdmin) -> HTMLResponse:
    store: SettingsStore = request.app.state.settings
    form = await request.form()

    try:
        match section:
            case "site":
                await store.put(forms.site_from(form))
            case "shelly":
                await store.put(forms.shelly_from(form, store.shelly))
            case "inputs":
                await store.put(forms.inputs_from(form, store.inputs))
            case "pumps":
                await store.put(forms.pumps_from(form))
            case "smtp":
                await store.put(forms.smtp_from(form, store.smtp))
            case "dashboard":
                await store.put(forms.dashboard_from(form))
            case "sms":
                await store.put(forms.sms_from(form, store.sms))
            case _:
                return RedirectResponse("/settings", status_code=303)
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "settings.html",
            _context(
                request,
                shelly=store.shelly,
                inputs=store.inputs,
                pumps=store.pumps,
                smtp=store.smtp,
                sms=store.sms,
                dashboard=store.dashboard,
                saved=None,
                error=_readable(error),
            ),
            status_code=400,
        )

    log.info("%s saved the %s settings", admin.username, section)
    return RedirectResponse(f"/settings?saved={section}", status_code=303)


async def _save_own_details(request: Request, admin: auth.User, form) -> None:
    """Record the administrator's own contact details from the setup form.

    Quietly skipped if what was typed does not make sense, because failing the
    whole of setup over a mistyped phone number would be a poor trade. The
    profile page says so properly.
    """
    email = forms.text(form, "admin_email") or None
    phone = forms.text(form, "admin_phone") or None
    if phone:
        phone = sms_sender.normalize(phone)
        if not sms_sender.looks_like_a_number(phone):
            log.warning("Ignoring an unusable phone number from setup")
            phone = None
    if email and not email_sender.looks_like_an_address(email):
        log.warning("Ignoring an unusable email address from setup")
        email = None

    await request.app.state.pool.execute(
        """
        UPDATE app_user
        SET name = COALESCE(NULLIF($2, ''), name),
            email = COALESCE($3, email),
            phone = COALESCE($4, phone),
            notify_email = $5,
            notify_sms = $6
        WHERE id = $1
        """,
        admin.id,
        forms.text(form, "admin_name"),
        email,
        phone,
        forms.checkbox(form, "admin_notify_email") and bool(email),
        forms.checkbox(form, "admin_notify_sms") and bool(phone),
    )


def _readable(error: Exception) -> str:
    """One line a person can act on, out of a pydantic error or a plain one."""
    if isinstance(error, ValidationError):
        problems = []
        for detail in error.errors():
            location = ".".join(str(part) for part in detail["loc"]) or "value"
            problems.append(f"{location}: {detail['msg']}")
        return "; ".join(problems[:3])
    return str(error)


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
