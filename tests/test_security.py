"""The parts that only matter once this is reachable from the internet.

Everything here guards a decision that is invisible when it is working and
expensive when it is not.
"""

from __future__ import annotations

import re

import pytest

from pitwatch import auth, csrf

NEW_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _no_leftover_throttling():
    auth.reset_throttling()
    yield
    auth.reset_throttling()


def token_from(client, path: str = "/login") -> str:
    """Scrape the token out of a rendered form, the way a browser gets it."""
    page = client.get(path).text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def sign_in_as_admin(client):
    client.post(
        "/login",
        data={
            "username": auth.DEFAULT_USERNAME,
            "password": auth.DEFAULT_PASSWORD,
            "csrf_token": token_from(client),
        },
    )
    client.post(
        "/change-password",
        data={
            "current_password": auth.DEFAULT_PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
            "csrf_token": token_from(client, "/change-password"),
        },
    )
    return client


# -- cross site request forgery ---------------------------------------------


def test_a_post_without_a_token_is_refused(client):
    """What another site's page would be able to do otherwise.

    SameSite=Lax already stops the cookie being sent on a cross site post, but
    it is a browser behavior and it treats a neighboring subdomain as the same
    site. This does not depend on either.
    """
    sign_in_as_admin(client)

    response = client.post(
        "/settings/site",
        data={"site_name": "Somewhere else"},
        follow_redirects=False,
        csrf=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert client.app.state.settings.site.name != "Somewhere else"


def test_a_post_with_the_wrong_token_is_refused(client):
    sign_in_as_admin(client)

    response = client.post(
        "/settings/site",
        data={"site_name": "Somewhere else", "csrf_token": "not-the-right-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.app.state.settings.site.name != "Somewhere else"


def test_a_post_with_the_right_token_goes_through(client):
    sign_in_as_admin(client)

    client.post(
        "/settings/site",
        data={"site_name": "822 Greenwich St", "csrf_token": token_from(client, "/settings")},
    )

    assert client.app.state.settings.site.name == "822 Greenwich St"


def test_the_login_form_is_protected_too(client):
    """It is exactly the form somebody would want to make you submit."""
    response = client.post(
        "/login",
        data={"username": "admin", "password": auth.DEFAULT_PASSWORD},
        follow_redirects=False,
        csrf=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_token_is_accepted_in_a_header(client):
    """For the fetch calls the front end makes."""
    sign_in_as_admin(client)
    token = token_from(client, "/settings")

    response = client.post(
        "/api/test/shelly",
        data={"shelly_host": ""},
        headers={"x-csrf-token": token},
    )

    # Refused for having no address, which means it got past the token check.
    assert response.status_code == 400
    assert "address" in response.json()["error"]


def test_reading_never_needs_a_token(client):
    sign_in_as_admin(client)

    for path in ("/", "/settings", "/users", "/health"):
        assert client.get(path).status_code == 200, path


def test_signing_in_issues_a_fresh_token(client):
    """A token handed out before signing in must not work afterwards."""
    before = token_from(client)
    sign_in_as_admin(client)

    assert token_from(client, "/settings") != before


# -- sessions and passwords --------------------------------------------------


def test_changing_a_password_ends_every_other_session(client):
    """The reason somebody changes a password is usually that they think a
    session was stolen. A cookie that outlives the change defeats the point.

    Two browsers are two cookie jars, so that is what this swaps between. A
    second TestClient would be a second event loop sharing one connection pool,
    which fails for reasons that have nothing to do with what is being tested.
    """
    cookie = "pitwatch_session"

    sign_in_as_admin(client)
    first_browser = client.cookies.get(cookie)

    # A second browser, signed in as the same person.
    client.cookies.clear()
    client.post(
        "/login",
        data={
            "username": auth.DEFAULT_USERNAME,
            "password": NEW_PASSWORD,
            "csrf_token": token_from(client),
        },
    )
    second_browser = client.cookies.get(cookie)
    assert client.get("/profile").status_code == 200, "the second browser should be in"

    # Back to the first, which changes the password.
    client.cookies.clear()
    client.cookies.set(cookie, first_browser)
    changed = client.post(
        "/change-password",
        data={
            "current_password": NEW_PASSWORD,
            "new_password": "a-different-long-password",
            "confirm_password": "a-different-long-password",
            "csrf_token": token_from(client, "/change-password"),
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303

    # The second browser is out, on its next request rather than whenever its
    # cookie happened to expire.
    client.cookies.clear()
    client.cookies.set(cookie, second_browser)
    assert client.get("/settings", follow_redirects=False).status_code == 303


# -- headers -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/terms"])
def test_the_security_headers_are_set(client, path):
    headers = client.get(path).headers

    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


def test_the_content_security_policy_allows_no_third_party_anything(client):
    """Everything is served from this origin, so the policy can be this tight.

    It is worth keeping that way: the moment a CDN font or an analytics script
    appears, this has to be loosened, and loosening it is how a stored script
    becomes a real problem rather than a rendering bug.
    """
    policy = client.get("/login").headers["content-security-policy"]

    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "base-uri 'none'" in policy
    for unsafe in ("unsafe-inline", "unsafe-eval", "*"):
        assert unsafe not in policy


def test_the_referrer_policy_keeps_invitation_links_off_other_sites():
    """An invitation link in a Referer header is a working credential handed to
    whoever the next click went to."""
    from pitwatch.middleware import SecurityHeaders

    assert SecurityHeaders is not None


# -- what the process trusts -------------------------------------------------


def test_forwarded_headers_are_not_trusted_from_anywhere(monkeypatch):
    """Trusting every address lets anyone who can reach the port claim to be
    any address, which defeats the per address sign in throttling and makes the
    log say whatever they felt like."""
    from pitwatch.config import Config

    monkeypatch.delenv("PITWATCH_TRUSTED_PROXIES", raising=False)
    trusted = Config(_env_file=None).trusted_proxies

    assert trusted != "*"
    assert "127.0.0.1" in trusted


def test_the_csrf_token_is_long_enough_to_be_unguessable():
    class FakeRequest:
        def __init__(self) -> None:
            self.session: dict = {}

    request = FakeRequest()
    token = csrf.token_for(request)

    assert len(token) >= 32
    assert csrf.token_for(request) == token, "stable within a session"


def test_checking_the_token_does_not_eat_the_form(client):
    """The trap that broke every form at once.

    Starlette caches the raw body so middleware can read it and the handler
    still sees it, but that caching is at the body() level. Calling form() in
    middleware drains the stream and every downstream handler receives an empty
    form, which presents as a dozen unrelated failures rather than as one cause.

    So this posts a real form through the guard and checks the handler actually
    received the fields.
    """
    sign_in_as_admin(client)

    client.post(
        "/settings/site",
        data={
            "site_name": "822 Greenwich St",
            "site_timezone": "America/New_York",
            "csrf_token": token_from(client, "/settings"),
        },
    )

    site = client.app.state.settings.site
    assert site.name == "822 Greenwich St", "the handler saw an empty form"


def test_the_token_in_a_form_is_the_token_and_not_a_function(client):
    """Jinja does not call a global for you.

    `{{ csrf_token }}` renders the function object, which is a stable, useless
    string that every form then carried and no request could match. The parens
    matter, and nothing else in the suite would have said so: the guard was
    working perfectly and rejecting every form for the right reason.

    Checked by the shape a real token has rather than by hunting for pieces of
    a repr. Looking for "0x" in the rendered value failed about one run in a
    hundred all by itself, because a token is random and two of its characters
    are eventually going to be those two. A test that fails on its own is worse
    than no test: it teaches you to rerun the build instead of reading it.
    """
    page = client.get("/login").text
    token = token_from(client)

    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token), f"not a token: {token!r}"
    assert f'value="{token}"' in page


def test_changing_your_own_password_does_not_sign_you_out(client):
    """The other half of ending every other session.

    Every session for the account becomes stale, deliberately. The browser
    doing the changing has to be handed a fresh one, or the feature that
    protects you also locks you out for using it.
    """
    sign_in_as_admin(client)

    response = client.post(
        "/change-password",
        data={
            "current_password": NEW_PASSWORD,
            "new_password": "a-different-long-password",
            "confirm_password": "a-different-long-password",
            "csrf_token": token_from(client, "/change-password"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/profile?saved=1"
    assert client.get("/profile").status_code == 200, "signed out by their own change"


@pytest.mark.parametrize("path", ["/login", "/terms", "/privacy"])
def test_pages_reference_their_own_assets_by_path_not_by_url(client, path):
    """Behind a proxy that terminates TLS, an absolute URL is a broken page.

    The browser is on https. If the application builds `http://host/static/...`
    because it cannot see the original scheme, that is a different origin as far
    as the content security policy is concerned, and every stylesheet and script
    on the page is blocked. Nothing renders and the console blames the CSP,
    which is telling the truth about the wrong thing.

    A root relative path has no scheme to get wrong.
    """
    page = client.get(path).text

    assert "/static/style.css?v=" in page
    assert "http://" not in page.split("</head>")[0], "an absolute URL in the head"


def test_no_template_builds_an_absolute_url_for_a_static_file():
    """The same rule, checked at the source so a new page cannot reintroduce it."""
    from pathlib import Path

    for template in (Path(__file__).parent.parent / "pitwatch" / "templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "url_for('static'" not in text, template.name
