"""Sign in, passwords, and managing who gets told.

A person in this system is one row. Whether they can sign in is whether they
have a password, and most will not: a superintendent is here to be texted, not
to look at a web page. An admin adds them with a name, an email address and a
phone number, ticks what they want to be sent, and can optionally email them a
link to set a password if they also want to watch the dashboard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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


# -- your own account --------------------------------------------------------


@router.get("/profile", include_in_schema=False)
async def profile_page(request: Request, user: auth.SignedIn, saved: str | None = None):
    return _templates(request).TemplateResponse(
        request,
        "profile.html",
        _context(request, saved=saved, error=None, password_error=None),
    )


@router.post("/profile", include_in_schema=False)
async def profile_save(request: Request, user: auth.SignedIn):
    """Edit your own details, and only the ones that are yours to edit.

    Deliberately a separate handler from the admin one rather than the same
    query with a different id. Whether somebody is an administrator, and whether
    their account works at all, are not on this form and cannot be reached from
    it, which is a property of the code rather than of the template.
    """
    pool = request.app.state.pool
    form = await request.form()

    def refuse(message: str):
        return _templates(request).TemplateResponse(
            request,
            "profile.html",
            _context(request, saved=None, error=message, password_error=None),
            status_code=400,
        )

    name = forms.text(form, "name")
    email = forms.text(form, "email") or None
    phone = forms.text(form, "phone") or None
    notify_email = forms.checkbox(form, "notify_email")
    notify_sms = forms.checkbox(form, "notify_sms")

    if not name:
        return refuse("A name is needed, so an alert can be addressed to somebody")
    if email and not email_sender.looks_like_an_address(email):
        return refuse(f"{email!r} does not look like an email address")
    if phone:
        phone = sms_sender.normalize(phone)
        if not sms_sender.looks_like_a_number(phone):
            return refuse(f"{phone!r} does not look like a phone number")
    if notify_email and not email:
        return refuse("Turn on email and there has to be an address to send it to")
    if notify_sms and not phone:
        return refuse("Turn on text messages and there has to be a number to send them to")

    try:
        await pool.execute(
            """
            UPDATE app_user
            SET name = $2, email = $3, phone = $4,
                notify_email = $5, notify_sms = $6, min_severity = $7
            WHERE id = $1
            """,
            user.id,
            name,
            email,
            phone,
            notify_email,
            notify_sms,
            forms.text(form, "min_severity", "warning") or "warning",
        )
    except Exception as error:  # noqa: BLE001 -- a taken address is the usual one
        log.warning("Could not save the profile for %s: %s", user.username, error)
        return refuse("Could not save that. Is the email address already used by somebody else?")

    log.info("%s updated their own details", user.username)
    return RedirectResponse("/profile?saved=1", status_code=303)


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
        # While the shipped password is still in force there is nowhere else to
        # be, so the error stays on the dedicated page. Afterwards the form
        # lives on the profile page and so should the answer.
        if user.must_change_password:
            return _templates(request).TemplateResponse(
                request,
                "change_password.html",
                _context(request, error=message, forced=True),
                status_code=400,
            )
        return _templates(request).TemplateResponse(
            request,
            "profile.html",
            _context(request, saved=None, error=None, password_error=message),
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

    # Sign in again on the new password. Every session for this account is now
    # stale, which is the point, and that includes this one: the browser doing
    # the changing should not be thrown out for it.
    refreshed = await auth.get_user(pool, user.id)
    if refreshed is not None:
        auth.sign_in(request, refreshed)

    log.info("%s changed their password", user.username)
    # Straight to the dashboard the first time, because that is somebody who has
    # just been made to change a shipped password and wants to get on with it.
    return RedirectResponse("/" if user.must_change_password else "/profile?saved=1", 303)


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

    # Read the person back, because the password that was just set changed the
    # fingerprint the session is checked against. Signing in with the copy from
    # before the change hands out a session that is stale the moment it is made.
    invited = await auth.get_user(pool, invited.id) or invited
    auth.sign_in(request, invited)
    log.info("%s set a password from an invitation", invited.username)
    return RedirectResponse("/", status_code=303)


# -- managing users ----------------------------------------------------------
#
# A list, and a form only when somebody asked for one. Every account used to be
# an open form on the same page: several editable copies of one shape, no way
# to see at a glance who gets what, and a Save button per row that looked like
# it might save all of them.


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


async def _list_with_error(request: Request, error: str):
    pool = request.app.state.pool
    return _templates(request).TemplateResponse(
        request,
        "users.html",
        _context(
            request, users=await auth.list_users(pool), saved=None, error=error, invite_link=None
        ),
        status_code=400,
    )


def _form_page(request: Request, person, error: str | None = None, status: int = 200):
    """The add form, or the edit form for one account. Same template either way."""
    return _templates(request).TemplateResponse(
        request,
        "user_form.html",
        _context(
            request,
            person=person,
            action=f"/users/{person.id}/edit" if person else "/users/new",
            error=error,
        ),
        status_code=status,
    )


@dataclass(frozen=True, slots=True)
class _Details:
    """The fields that adding and editing both ask for, once checked."""

    name: str
    email: str | None
    phone: str | None
    notify_email: bool
    notify_sms: bool
    min_severity: str
    is_admin: bool


def _details_from(form) -> _Details | str:
    """Read the shared fields, or return the sentence to show instead of them."""
    name = forms.text(form, "name")
    email = forms.text(form, "email") or None
    phone = forms.text(form, "phone") or None
    notify_email = forms.checkbox(form, "notify_email")
    notify_sms = forms.checkbox(form, "notify_sms")

    if not name:
        return "A user needs a name"
    if phone:
        phone = sms_sender.normalize(phone)
        if not sms_sender.looks_like_a_number(phone):
            return f"{phone!r} does not look like a phone number"
    if email and not email_sender.looks_like_an_address(email):
        return f"{email!r} does not look like an email address"
    # Checked here as well as in the browser, where the boxes are switched off
    # and unavailable without an address to send to. A form is not a guarantee,
    # and a notification setting that reads as on and delivers nothing is worse
    # than one that is plainly off.
    if notify_email and not email:
        return f"{name} is set to get email but has no address"
    if notify_sms and not phone:
        return f"{name} is set to get texts but has no number"

    return _Details(
        name=name,
        email=email,
        phone=phone,
        notify_email=notify_email,
        notify_sms=notify_sms,
        min_severity=forms.text(form, "min_severity", "warning") or "warning",
        is_admin=forms.checkbox(form, "is_admin"),
    )


@router.get("/users/new", include_in_schema=False)
async def new_user_page(request: Request, admin: auth.IsAdmin):
    return _form_page(request, person=None)


@router.post("/users/new", include_in_schema=False)
async def add_user(request: Request, admin: auth.IsAdmin):
    pool = request.app.state.pool
    form = await request.form()

    details = _details_from(form)
    if isinstance(details, str):
        return _form_page(request, person=None, error=details, status=400)

    username = forms.text(form, "username").lower()
    if not username:
        # Derived from the name only as a starting point, and only when the
        # administrator did not care to pick one.
        username = "".join(c for c in details.name.lower() if c.isalnum()) or "user"

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
            details.name,
            details.email,
            details.phone,
            details.notify_email,
            details.notify_sms,
            details.min_severity,
            details.is_admin,
        )
    except (ValidationError, ValueError) as error:
        return _form_page(request, person=None, error=str(error), status=400)
    except Exception as error:  # noqa: BLE001 -- a duplicate is the usual one
        log.warning("Could not add %r: %s", username, error)
        return _form_page(
            request,
            person=None,
            error=f"Could not add {details.name}. Is {username!r} or that email already used?",
            status=400,
        )

    log.info("%s added %s", admin.username, username)

    if forms.checkbox(form, "send_invite") and details.email:
        message = await _send_invitation(request, user_id, details.name, details.email)
        if message:
            request.session["invite_link"] = message

    return RedirectResponse("/users?saved=added", status_code=303)


@router.get("/users/{user_id}/edit", include_in_schema=False)
async def edit_user_page(request: Request, user_id: int, admin: auth.IsAdmin):
    person = await auth.get_user(request.app.state.pool, user_id)
    if person is None:
        return RedirectResponse("/users", status_code=303)
    return _form_page(request, person=person)


@router.post("/users/{user_id}/edit", include_in_schema=False)
async def save_user(request: Request, user_id: int, admin: auth.IsAdmin):
    pool = request.app.state.pool
    person = await auth.get_user(pool, user_id)
    if person is None:
        return RedirectResponse("/users", status_code=303)

    form = await request.form()
    details = _details_from(form)
    if isinstance(details, str):
        return _form_page(request, person=person, error=details, status=400)

    enabled = forms.checkbox(form, "enabled")

    # An install with nobody who can change anything is an install that needs
    # the database edited by hand to recover.
    if admin.id == user_id and not details.is_admin:
        return _form_page(
            request,
            person=person,
            error="You cannot remove your own administrator rights",
            status=400,
        )
    if admin.id == user_id and not enabled:
        return _form_page(
            request, person=person, error="You cannot disable your own account", status=400
        )

    await pool.execute(
        """
        UPDATE app_user
        SET name = $2, email = $3, phone = $4, notify_email = $5, notify_sms = $6,
            min_severity = $7, is_admin = $8, enabled = $9
        WHERE id = $1
        """,
        user_id,
        details.name,
        details.email,
        details.phone,
        details.notify_email,
        details.notify_sms,
        details.min_severity,
        details.is_admin,
        enabled,
    )
    log.info("%s updated user %d", admin.username, user_id)
    return RedirectResponse("/users?saved=updated", status_code=303)


@router.post("/users/{user_id}/toggle", include_in_schema=False)
async def toggle_user(request: Request, user_id: int, admin: auth.IsAdmin):
    """Active or not, from the list, without opening the form."""
    if admin.id == user_id:
        return await _list_with_error(request, "You cannot disable your own account")
    pool = request.app.state.pool
    person = await auth.get_user(pool, user_id)
    if person is None:
        return RedirectResponse("/users", status_code=303)

    await pool.execute("UPDATE app_user SET enabled = NOT enabled WHERE id = $1", user_id)
    log.info(
        "%s %s %s", admin.username, "disabled" if person.enabled else "enabled", person.username
    )
    return RedirectResponse("/users?saved=updated", status_code=303)


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


@router.post("/users/{user_id}/admin", include_in_schema=False)
async def toggle_admin(request: Request, user_id: int, admin: auth.IsAdmin):
    """Administrator or not, from the list.

    One click either way, on a page only administrators can reach. Opening a
    form to change one checkbox and pressing Save is more ceremony than the
    change deserves, and the change is reversible by the same click.
    """
    if admin.id == user_id:
        return await _list_with_error(request, "You cannot remove your own administrator rights")
    pool = request.app.state.pool
    person = await auth.get_user(pool, user_id)
    if person is None:
        return RedirectResponse("/users", status_code=303)

    await pool.execute("UPDATE app_user SET is_admin = NOT is_admin WHERE id = $1", user_id)
    log.info(
        "%s made %s %s",
        admin.username,
        person.username,
        "an ordinary user" if person.is_admin else "an administrator",
    )
    return RedirectResponse("/users?saved=updated", status_code=303)


@router.post("/users/{user_id}/invite", include_in_schema=False)
async def invite_user(request: Request, user_id: int, admin: auth.IsAdmin):
    pool = request.app.state.pool
    user = await auth.get_user(pool, user_id)
    if user is None or not user.email:
        return await _list_with_error(request, "That user has no email address to send a link to")

    link = await _send_invitation(request, user.id, user.display_name, user.email)
    if link:
        request.session["invite_link"] = link
    return RedirectResponse("/users?saved=invited", status_code=303)


@router.post("/users/{user_id}/delete", include_in_schema=False)
async def delete_user(request: Request, user_id: int, admin: auth.IsAdmin):
    if admin.id == user_id:
        return await _list_with_error(request, "You cannot delete your own account")
    pool = request.app.state.pool
    await pool.execute("DELETE FROM app_user WHERE id = $1", user_id)
    log.info("%s deleted user %d", admin.username, user_id)
    return RedirectResponse("/users?saved=deleted", status_code=303)


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
