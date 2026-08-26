"""Sign in, passwords, and managing who gets told.

A person in this system is one row. Whether they can sign in is whether they
have a password, and most will not: a superintendent is here to be texted, not
to look at a web page. An admin adds them with a name, an email address and a
phone number, ticks what they want to be sent, and can optionally email them a
link to set a password if they also want to watch the dashboard.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from pitwatch import auth
from pitwatch.api import forms
from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


def _context(request: Request, **extra) -> dict:
    store: SettingsStore = request.app.state.settings
    return {"site": store.site, "user": auth.current_user(request), **extra}


def _safe_next(value: str) -> str:
    """Only ever a path on this site, never somewhere else entirely."""
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


# -- signing in --------------------------------------------------------------


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/"):
    if auth.current_user(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _templates(request).TemplateResponse(
        request, "login.html", _context(request, error=None, next=_safe_next(next))
    )


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    pool = request.app.state.pool
    form = await request.form()
    username = forms.text(form, "username")
    destination = _safe_next(forms.text(form, "next", "/") or "/")

    # Throttled per user name and per client address, so that neither guessing
    # one account nor spraying many is cheap.
    client = request.client.host if request.client else "unknown"
    keys = [f"user:{username.lower()}", f"host:{client}"]
    locked = max((auth.seconds_locked_out(key) for key in keys), default=0)
    if locked:
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            _context(
                request,
                error=f"Too many attempts. Try again in {locked} seconds.",
                next=destination,
            ),
            status_code=429,
        )

    user = await auth.authenticate(pool, username, forms.text(form, "password"))
    if user is None:
        for key in keys:
            auth._record_failure(key)
        log.warning("Failed sign in for %r from %s", username, client)
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            _context(request, error="That user name and password do not match", next=destination),
            status_code=401,
        )

    for key in keys:
        auth._clear_failures(key)
    auth.sign_in(request, user)

    # "Remember me" is the difference between a session that ends with the
    # browser and one that lasts. Both are the same signed cookie; only its
    # lifetime differs, and the short one is the default.
    request.session["remember"] = forms.checkbox(form, "remember")

    if user.must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    auth.sign_out(request)
    return RedirectResponse("/login", status_code=303)


# -- passwords ---------------------------------------------------------------


@router.get("/change-password", include_in_schema=False)
async def change_password_page(request: Request, user: auth.SignedIn):
    return _templates(request).TemplateResponse(
        request,
        "change_password.html",
        _context(request, error=None, forced=user.must_change_password),
    )


@router.post("/change-password", include_in_schema=False)
async def change_password_submit(request: Request, user: auth.SignedIn):
    pool = request.app.state.pool
    form = await request.form()

    def refuse(message: str):
        return _templates(request).TemplateResponse(
            request,
            "change_password.html",
            _context(request, error=message, forced=user.must_change_password),
            status_code=400,
        )

    current = forms.text(form, "current_password")
    new = forms.text(form, "new_password")

    if await auth.authenticate(pool, user.username, current) is None:
        return refuse("That is not the current password")
    if new != forms.text(form, "confirm_password"):
        return refuse("The two new passwords do not match")
    if new == current:
        return refuse("The new password is the same as the old one")

    try:
        await auth.set_password(pool, user.id, new)
    except auth.PasswordTooShort as error:
        return refuse(str(error))

    log.info("%s changed their password", user.username)
    return RedirectResponse("/", status_code=303)


@router.get("/set-password", include_in_schema=False)
async def set_password_page(request: Request, token: str = ""):
    """Public, because somebody following an invitation has no password yet."""
    pool = request.app.state.pool
    invited = await auth.redeem_password_token(pool, token) if token else None
    return _templates(request).TemplateResponse(
        request,
        "set_password.html",
        _context(request, token=token, invited=invited, error=None),
        status_code=200 if invited else 400,
    )


@router.post("/set-password", include_in_schema=False)
async def set_password_submit(request: Request):
    pool = request.app.state.pool
    form = await request.form()
    token = forms.text(form, "token")

    invited = await auth.redeem_password_token(pool, token) if token else None
    if invited is None:
        return _templates(request).TemplateResponse(
            request,
            "set_password.html",
            _context(request, token=token, invited=None, error=None),
            status_code=400,
        )

    def refuse(message: str):
        return _templates(request).TemplateResponse(
            request,
            "set_password.html",
            _context(request, token=token, invited=invited, error=message),
            status_code=400,
        )

    new = forms.text(form, "new_password")
    if new != forms.text(form, "confirm_password"):
        return refuse("The two passwords do not match")
    try:
        await auth.set_password(pool, invited.id, new)
    except auth.PasswordTooShort as error:
        return refuse(str(error))

    # Spent only once it has actually been used, so a half finished attempt can
    # be retried from the same link.
    await auth.spend_password_token(pool, token)
    auth.sign_in(request, invited)
    log.info("%s set a password from an invitation", invited.username)
    return RedirectResponse("/", status_code=303)


# -- managing people ---------------------------------------------------------


@router.get("/users", include_in_schema=False)
async def users_page(request: Request, admin: auth.IsAdmin, saved: str | None = None):
    pool = request.app.state.pool
    return _templates(request).TemplateResponse(
        request,
        "users.html",
        _context(
            request,
            users=await auth.list_users(pool),
            saved=saved,
            error=None,
            invite_link=request.session.pop("invite_link", None),
        ),
    )


async def _render_users(request: Request, error: str):
    pool = request.app.state.pool
    return _templates(request).TemplateResponse(
        request,
        "users.html",
        _context(
            request, users=await auth.list_users(pool), saved=None, error=error, invite_link=None
        ),
        status_code=400,
    )


@router.post("/users/add", include_in_schema=False)
async def add_user(request: Request, admin: auth.IsAdmin):
    pool = request.app.state.pool
    form = await request.form()

    name = forms.text(form, "name")
    username = forms.text(form, "username").lower()
    email = forms.text(form, "email") or None
    phone = forms.text(form, "phone") or None
    notify_email = forms.checkbox(form, "notify_email")
    notify_sms = forms.checkbox(form, "notify_sms")

    if not name:
        return await _render_users(request, "A person needs a name")
    if not username:
        # Derived from the name only as a starting point, and only when the
        # admin did not care to pick one.
        username = "".join(character for character in name.lower() if character.isalnum()) or "user"
    if notify_email and not email:
        return await _render_users(request, f"{name} is set to get email but has no address")
    if notify_sms and not phone:
        return await _render_users(request, f"{name} is set to get texts but has no number")
    if phone:
        phone = sms_sender.normalize(phone)
        if not sms_sender.looks_like_a_number(phone):
            return await _render_users(request, f"{phone!r} does not look like a phone number")
    if email and not email_sender.looks_like_an_address(email):
        return await _render_users(request, f"{email!r} does not look like an email address")

    try:
        user_id = await pool.fetchval(
            """
            INSERT INTO app_user
                (username, name, email, phone, notify_email, notify_sms,
                 min_severity, is_admin, password_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL)
            RETURNING id
            """,
            username,
            name,
            email,
            phone,
            notify_email,
            notify_sms,
            forms.text(form, "min_severity", "warning") or "warning",
            forms.checkbox(form, "is_admin"),
        )
    except (ValidationError, ValueError) as error:
        return await _render_users(request, str(error))
    except Exception as error:  # noqa: BLE001 -- a duplicate is the usual one
        log.warning("Could not add %r: %s", username, error)
        return await _render_users(
            request, f"Could not add {name}. Is {username!r} or that email already used?"
        )

    log.info("%s added %s", admin.username, username)

    if forms.checkbox(form, "send_invite") and email:
        message = await _send_invitation(request, user_id, name, email)
        if message:
            request.session["invite_link"] = message

    return RedirectResponse("/users?saved=added", status_code=303)


async def _send_invitation(request: Request, user_id: int, name: str, email: str) -> str | None:
    """Email a link to set a password. Returns a fallback link if it could not.

    Falling back to showing the link matters: an admin adding people before the
    mail server is configured should not be stuck, and the alternative is an
    invitation that silently never arrives.
    """
    pool = request.app.state.pool
    store: SettingsStore = request.app.state.settings
    token = await auth.create_password_token(pool, user_id, "invite")

    base = store.site.base_url.rstrip("/") or str(request.base_url).rstrip("/")
    link = f"{base}/set-password?token={token}"

    smtp = store.smtp
    if not smtp.enabled or not smtp.host:
        return link

    try:
        await email_sender.send(
            smtp,
            email,
            "Set your password for PitWatch",
            f"Hello {name},\n\n"
            f"You have been added to PitWatch, which watches {store.site.pumps_at}. "
            f"If you would like to sign in and see their status, set a password "
            f"here:\n\n{link}\n\n"
            "The link works once and expires in three days.\n\n"
            "You do not have to. Alerts will reach you either way.\n",
        )
    except email_sender.EmailError as error:
        log.warning("Could not email an invitation to %s: %s", email, error)
        return link
    return None


@router.post("/users/{user_id}/save", include_in_schema=False)
async def save_user(request: Request, user_id: int, admin: auth.IsAdmin):
    pool = request.app.state.pool
    form = await request.form()

    email = forms.text(form, "email") or None
    phone = forms.text(form, "phone") or None
    if phone:
        phone = sms_sender.normalize(phone)

    is_admin = forms.checkbox(form, "is_admin")
    enabled = forms.checkbox(form, "enabled")

    # An install with nobody who can change anything is an install that needs
    # the database edited by hand to recover.
    if admin.id == user_id and not is_admin:
        return await _render_users(request, "You cannot remove your own administrator rights")
    if admin.id == user_id and not enabled:
        return await _render_users(request, "You cannot disable your own account")

    await pool.execute(
        """
        UPDATE app_user
        SET name = $2, email = $3, phone = $4, notify_email = $5, notify_sms = $6,
            min_severity = $7, is_admin = $8, enabled = $9
        WHERE id = $1
        """,
        user_id,
        forms.text(form, "name"),
        email,
        phone,
        forms.checkbox(form, "notify_email"),
        forms.checkbox(form, "notify_sms"),
        forms.text(form, "min_severity", "warning") or "warning",
        is_admin,
        enabled,
    )
    log.info("%s updated user %d", admin.username, user_id)
    return RedirectResponse("/users?saved=updated", status_code=303)


@router.post("/users/{user_id}/invite", include_in_schema=False)
async def invite_user(request: Request, user_id: int, admin: auth.IsAdmin):
    pool = request.app.state.pool
    user = await auth.get_user(pool, user_id)
    if user is None or not user.email:
        return await _render_users(request, "That person has no email address to send a link to")

    link = await _send_invitation(request, user.id, user.display_name, user.email)
    if link:
        request.session["invite_link"] = link
    return RedirectResponse("/users?saved=invited", status_code=303)


@router.post("/users/{user_id}/delete", include_in_schema=False)
async def delete_user(request: Request, user_id: int, admin: auth.IsAdmin):
    if admin.id == user_id:
        return await _render_users(request, "You cannot delete your own account")
    pool = request.app.state.pool
    await pool.execute("DELETE FROM app_user WHERE id = $1", user_id)
    log.info("%s deleted user %d", admin.username, user_id)
    return RedirectResponse("/users?saved=deleted", status_code=303)


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
