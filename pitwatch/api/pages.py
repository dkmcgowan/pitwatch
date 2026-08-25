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
async def setup_page(request: Request):
    pool = request.app.state.pool
    if await auth.any_user_exists(pool):
        return RedirectResponse("/settings", status_code=303)

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
async def setup_submit(request: Request):
    pool = request.app.state.pool
    store: SettingsStore = request.app.state.settings
    if await auth.any_user_exists(pool):
        return RedirectResponse("/settings", status_code=303)

    form = await request.form()
    username = forms.text(form, "username", "admin") or "admin"
    password = forms.text(form, "password")
    confirm = forms.text(form, "password_confirm")

    try:
        if password != confirm:
            raise ValueError("The two passwords do not match")
        site = forms.site_from(form)
        shelly = forms.shelly_from(form)
        waveshare = forms.waveshare_from(form)
        pumps = forms.pumps_from(form)
        # Hashing last, after everything else has validated, so a rejected form
        # does not cost a deliberately slow hash for nothing.
        await auth.create_user(pool, username, password)
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
    auth.sign_in(request, username)
    log.info("Setup completed by %s", username)
    return RedirectResponse("/", status_code=303)


# -- sign in ----------------------------------------------------------------


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/"):
    pool = request.app.state.pool
    if not await auth.any_user_exists(pool):
        return RedirectResponse("/setup", status_code=303)
    return _templates(request).TemplateResponse(
        request, "login.html", _context(request, error=None, next=next)
    )


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    pool = request.app.state.pool
    form = await request.form()
    destination = forms.text(form, "next", "/") or "/"
    # An open redirect here would be a small hole in a small application, but
    # it costs one line to not have one.
    if not destination.startswith("/") or destination.startswith("//"):
        destination = "/"

    username = await auth.authenticate(
        pool, forms.text(form, "username"), forms.text(form, "password")
    )
    if username is None:
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            _context(request, error="That user name and password do not match", next=destination),
            status_code=401,
        )
    auth.sign_in(request, username)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    auth.sign_out(request)
    return RedirectResponse("/", status_code=303)


# -- settings ---------------------------------------------------------------


@router.get("/settings", include_in_schema=False)
async def settings_page(request: Request, saved: str | None = None):
    pool = request.app.state.pool
    if not await auth.any_user_exists(pool):
        return RedirectResponse("/setup", status_code=303)
    if auth.current_user(request) is None:
        return RedirectResponse("/login?next=/settings", status_code=303)

    store: SettingsStore = request.app.state.settings
    recipients = await pool.fetch("SELECT * FROM recipient ORDER BY id")
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
            recipients=recipients,
            saved=saved,
            error=None,
        ),
    )


@router.post("/settings/{section}", include_in_schema=False)
async def settings_save(request: Request, section: str, user: auth.SignedIn) -> HTMLResponse:
    store: SettingsStore = request.app.state.settings
    pool = request.app.state.pool
    form = await request.form()

    try:
        match section:
            case "site":
                await store.put(forms.site_from(form))
            case "shelly":
                await store.put(forms.shelly_from(form))
            case "waveshare":
                await store.put(forms.waveshare_from(form))
            case "pumps":
                await store.put(forms.pumps_from(form))
            case "smtp":
                await store.put(forms.smtp_from(form, store.smtp))
            case "sms":
                await store.put(forms.sms_from(form, store.sms))
            case "recipients":
                await _save_recipients(pool, form)
            case _:
                return RedirectResponse("/settings", status_code=303)
    except (ValueError, ValidationError) as error:
        recipients = await pool.fetch("SELECT * FROM recipient ORDER BY id")
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
                recipients=recipients,
                saved=None,
                error=_readable(error),
            ),
            status_code=400,
        )

    log.info("%s saved the %s settings", user, section)
    return RedirectResponse(f"/settings?saved={section}", status_code=303)


async def _save_recipients(pool, form) -> None:
    """Replace the recipient list wholesale.

    Rewriting the table rather than diffing it means a row deleted in the
    browser is a row deleted here, with no orphan left behind that keeps
    getting paged. Notifications reference an alert, not a recipient, so
    nothing points at these rows afterwards.
    """
    recipients = forms.recipients_from(form)
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("DELETE FROM recipient")
        for entry in recipients:
            await connection.execute(
                """
                INSERT INTO recipient (name, email, phone, min_severity, enabled)
                VALUES ($1, $2, $3, $4, $5)
                """,
                entry["name"],
                entry["email"],
                entry["phone"],
                entry["min_severity"],
                entry["enabled"],
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
