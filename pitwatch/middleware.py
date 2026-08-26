"""The guard that makes signing in the default rather than something to remember.

Doing this per route is how a route eventually gets added without it. So the
rule is inverted: everything needs an account unless it is on the list below,
and the list is short enough to read in one go and argue with.

What is public, and why each one:

* **The login page.** Obviously.
* **Static files.** The stylesheet for the login page, and nothing that is not
  already in the repository.
* **The health check.** A load balancer cannot sign in.
* **The messaging policy and privacy pages.** These are the ones that look like
  a mistake and are not. A carrier reviewing a toll-free number registration has
  to be able to read how people opt in and out of texts, without an account, and
  so does anybody deciding whether to hand over their phone number. Putting
  consent terms behind a login is both a failed registration and the wrong thing
  to do.
* **The set password page.** Somebody following an invitation link has no
  password yet, which is the entire point of the link.
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from pitwatch import auth

log = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/healthz",
        "/messaging-policy",
        "/privacy",
        "/set-password",
        "/favicon.ico",
    }
)

PUBLIC_PREFIXES = ("/static/",)

# Where a signed in user is sent when their password is the one the container
# was shipped with. Everything else is refused until they have changed it.
CHANGE_PASSWORD_PATH = "/change-password"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class RequireSignIn(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        path = request.url.path

        pool = getattr(request.app.state, "pool", None)
        user_id = auth.signed_in_user_id(request)
        if pool is not None and user_id is not None:
            user = await auth.get_user(pool, user_id)
            # A disabled or deleted account stops working on its next request
            # rather than when its cookie eventually expires.
            if user is not None and user.enabled:
                request.state.user = user
            else:
                auth.sign_out(request)

        if is_public(path):
            return await call_next(request)

        if request.state.user is None:
            return _refuse(request)

        # A default password is a password everybody knows, so an account still
        # using one can go to exactly one place.
        if request.state.user.must_change_password and path != CHANGE_PASSWORD_PATH:
            if _wants_html(request):
                return RedirectResponse(CHANGE_PASSWORD_PATH, status_code=303)
            return JSONResponse(
                {"error": "Change the password on this account first"}, status_code=403
            )

        return await call_next(request)


def _wants_html(request: Request) -> bool:
    """Whether to redirect or to answer with a status code.

    A browser following a link wants the login page. Anything the front end
    asked for wants a code it can act on, because a login page arriving where
    JSON was expected parses as gibberish and reports the wrong problem.
    """
    if request.url.path.startswith(("/api/", "/ws/")):
        return False
    return "text/html" in request.headers.get("accept", "")


def _refuse(request: Request):
    if not _wants_html(request):
        return JSONResponse({"error": "Sign in first"}, status_code=401)

    destination = request.url.path
    if request.url.query:
        destination = f"{destination}?{request.url.query}"
    # Only ever a path on this site. Reflecting an absolute URL here would make
    # the login page an open redirect.
    if not destination.startswith("/") or destination.startswith("//"):
        destination = "/"
    return RedirectResponse(f"/login?next={destination}", status_code=303)
