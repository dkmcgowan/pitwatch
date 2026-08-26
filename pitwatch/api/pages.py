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
from pitwatch.schemas import (
    SIGNAL_LABELS,
    Signal,
)
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter()


def _context(request: Request, **extra) -> dict:
    store: SettingsStore = request.app.state.settings
    return {
        "site": store.site,
        "user": auth.current_user(request),
        "signals": [(signal.value, SIGNAL_LABELS[signal]) for signal in Signal],
        **extra,
    }


def _templates(request: Request):
    return request.app.state.templates


# -- setup ------------------------------------------------------------------


@router.get("/setup", include_in_schema=False)
async def setup_page(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = request.app.state.settings
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
