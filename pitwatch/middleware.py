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

from pitwatch import auth, csrf

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

        # Checked before anything else looks at the request, and on public
        # paths too: the login form is public and is exactly the form somebody
        # would want to make you submit.
        if request.method not in csrf.SAFE_METHODS and not await csrf.is_valid(request):
            log.warning("Rejected a %s to %s with no valid CSRF token", request.method, path)
            return _refuse_csrf(request)

        if is_public(path):
            return await call_next(request)

        if request.state.user is None:
            return _refuse(request)

        # A password change, here or anywhere else, ends every other session
        # for that account. Without this a stolen cookie survives the thing
        # somebody does precisely because they think it was stolen.
        if request.session.get(auth.SESSION_FINGERPRINT_KEY) != request.state.user.fingerprint:
            log.info("Session for %s no longer matches its password", request.state.user.username)
            auth.sign_out(request)
            request.state.user = None
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

    Decided by path rather than by the Accept header. Browsers do send
    text/html, but plenty of other clients send */* and mean a page, so
    trusting the header turned ordinary page loads into 401s. The paths under
    /api and /ws are the only things the front end calls with fetch, and they
    are the only ones that want a code rather than a login page arriving where
    JSON was expected.
    """
    return not request.url.path.startswith(("/api/", "/ws/"))


def _refuse_csrf(request: Request):
    """Deliberately unhelpful about what was missing.

    Somebody who reached here through a page this application rendered has a
    stale session and wants to sign in again. Anybody else does not need
    instructions.
    """
    if not _wants_html(request):
        return JSONResponse({"error": "The session has expired. Reload and try again."}, 403)
    return RedirectResponse("/login?stale=1", status_code=303)


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


class SecurityHeaders(BaseHTTPMiddleware):
    """Headers that cost nothing and close off whole categories of attack.

    A proxy could set these instead, and often does. Setting them here means
    they are true wherever this runs, including on somebody's laptop and behind
    a proxy nobody configured for it.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers

        # Nothing here is ever meant to be inside somebody else's page. This is
        # the modern spelling; X-Frame-Options is the one older browsers read.
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        headers.setdefault("X-Frame-Options", "DENY")
        # Stop a browser deciding for itself that something is a script.
        headers.setdefault("X-Content-Type-Options", "nosniff")
        # An invitation link in a Referer header would hand somebody else a
        # working credential, so referrers stay on this site.
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # None of this is a browser API this application has any use for.
        headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        return response


# Everything is served from this origin, and the one websocket connects back to
# it. There are no third party scripts, fonts, frames or images anywhere, which
# is what makes a policy this tight possible.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self' ws: wss:",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]
)
