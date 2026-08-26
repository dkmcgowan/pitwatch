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
            waveshare=store.waveshare,
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
        waveshare = forms.waveshare_from(form)
        pumps = forms.pumps_from(form)
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "setup.html",
            _context(
                request,
                shelly=store.shelly,
                waveshare=store.waveshare,
                pumps=store.pumps,
                error=_readable(error),
            ),
            status_code=400,
        )

    for value in (site, shelly, waveshare, pumps):
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
            waveshare=store.waveshare,
            pumps=store.pumps,
            smtp=store.smtp,
            sms=store.sms,
            saved=saved,
            error=None,
        ),
    )


# -- the dashboard ----------------------------------------------------------
#
# Its own page rather than another card on the settings page, because it is a
# different job. The settings page describes the wiring; this describes the
# display, and the two are edited at different times by people thinking about
# different things. Administrators only, like everything under /settings.


@router.get("/settings/dashboard", include_in_schema=False)
async def dashboard_settings_page(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = request.app.state.settings
    return _templates(request).TemplateResponse(
        request,
        "dashboard_settings.html",
        _context(
            request,
            dashboard=store.dashboard,
            waveshare=store.waveshare,
            saved=request.query_params.get("saved") is not None,
            error=None,
        ),
    )


@router.post("/settings/dashboard", include_in_schema=False)
async def dashboard_settings_save(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = request.app.state.settings
    form = await request.form()
    try:
        await store.put(forms.dashboard_from(form))
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "dashboard_settings.html",
            _context(
                request,
                dashboard=store.dashboard,
                waveshare=store.waveshare,
                saved=False,
                error=_readable(error),
            ),
            status_code=400,
        )
    log.info("%s updated the dashboard lamps", admin.username)
    return RedirectResponse("/settings/dashboard?saved=1", status_code=303)


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
            case "waveshare":
                await store.put(forms.waveshare_from(form))
            case "pumps":
                await store.put(forms.pumps_from(form))
            case "smtp":
                await store.put(forms.smtp_from(form, store.smtp))
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
                waveshare=store.waveshare,
                pumps=store.pumps,
                smtp=store.smtp,
                sms=store.sms,
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
