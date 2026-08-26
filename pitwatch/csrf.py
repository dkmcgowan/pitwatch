"""Cross site request forgery protection.

The session cookie is already `SameSite=Lax`, which stops another site's page
posting this application's forms with your cookie attached, and on a modern
browser that is the bulk of the protection. It is not all of it:

* Lax is about *sites*, not origins. Anything else on the same registrable
  domain counts as same site, so a compromised or hostile page on a neighboring
  subdomain is not covered by it at all.
* It is a browser behavior rather than something this application enforces, so
  it is exactly as good as the browser in front of it.

So there is a token as well. It lives in the session, is required on every
unsafe request, and is compared in constant time. Belt and braces on an admin
panel that is reachable from the internet is the right trade.

GET, HEAD and OPTIONS are exempt because they are supposed to be safe. If a
handler is ever added that changes something on a GET, that is the bug, not this.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from starlette.requests import Request

log = logging.getLogger(__name__)

SESSION_KEY = "csrf"
FIELD_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def token_for(request: Request) -> str:
    """The token for this session, minting one the first time it is asked for.

    Per session rather than per form. A per form token would also defend against
    a form being replayed, which is not a threat here, and would break the back
    button in a way people rightly find infuriating.
    """
    existing = request.session.get(SESSION_KEY)
    if isinstance(existing, str) and existing:
        return existing
    minted = secrets.token_urlsafe(32)
    request.session[SESSION_KEY] = minted
    return minted


def rotate(request: Request) -> None:
    """Issue a new token. Called when who you are changes."""
    request.session[SESSION_KEY] = secrets.token_urlsafe(32)


async def submitted_token(request: Request) -> str:
    """Read the token off the request, from a form field or a header.

    The header exists for the fetch calls the front end makes. They send a
    FormData built from a real form, so they carry the hidden field already, but
    accepting either means a future JSON call is not a special case.
    """
    header = request.headers.get(HEADER_NAME)
    if header:
        return header

    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(FIELD_NAME)
        if isinstance(value, str):
            return value
    return ""


async def is_valid(request: Request) -> bool:
    expected = request.session.get(SESSION_KEY)
    if not isinstance(expected, str) or not expected:
        # No token was ever issued for this session, so nothing can match. That
        # is a request that did not come from a page this application rendered.
        return False
    return hmac.compare_digest(expected, await submitted_token(request))
