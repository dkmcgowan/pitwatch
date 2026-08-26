"""Signing in, and the guard in front of everything.

This is reachable from the internet behind a proxy, so the interesting tests
are the ones about what an unauthenticated request can reach, what a default
password can do, and whether guessing is cheap.
"""

from __future__ import annotations

import pytest

from pitwatch import auth
from pitwatch.middleware import PUBLIC_PATHS

NEW_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _no_leftover_throttling():
    auth.reset_throttling()
    yield
    auth.reset_throttling()


def sign_in_as_admin(client, password: str = NEW_PASSWORD):
    client.post(
        "/login", data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD}
    )
    client.post(
        "/change-password",
        data={
            "current_password": auth.DEFAULT_PASSWORD,
            "new_password": password,
            "confirm_password": password,
        },
    )
    return client


# -- what a stranger can reach -----------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/setup", "/settings", "/users", "/change-password", "/api/state"],
)
def test_everything_needs_an_account(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code in (303, 401), path
    if response.status_code == 303:
        assert response.headers["location"].startswith("/login")


def test_the_login_page_is_reachable(client):
    assert client.get("/login").status_code == 200


@pytest.mark.parametrize("path", ["/messaging-policy", "/privacy"])
def test_the_policy_pages_are_public(client, path):
    """Deliberate, and the thing most likely to be "fixed" later.

    A carrier reviewing a toll-free number registration has to be able to read
    how people opt in and out without an account. Putting these behind the login
    fails the registration, and is the wrong thing to do to somebody deciding
    whether to give you their phone number.
    """
    response = client.get(path)

    assert response.status_code == 200
    assert "STOP" in response.text or "opt" in response.text.lower()


def test_the_messaging_policy_says_what_carriers_look_for(client):
    page = client.get("/messaging-policy").text

    assert "STOP" in page
    assert "HELP" in page
    assert "Message and data rates may apply" in page
    for phrase in ("not sold", "marketing", "frequency"):
        assert phrase in page.lower()


def test_the_login_page_links_to_the_policies(client):
    """The consent terms live on their own page, which is the URL registered
    with the carrier. The login page only has to be able to reach them."""
    page = client.get("/login").text

    assert "/messaging-policy" in page
    assert "/privacy" in page


def test_the_public_list_is_short_and_deliberate():
    """A reminder to think, if this list ever grows."""
    expected = {
        "/login",
        "/logout",
        "/health",
        "/healthz",
        "/messaging-policy",
        "/privacy",
        "/set-password",
        "/favicon.ico",
    }

    assert sorted(PUBLIC_PATHS) == sorted(expected)


# -- the default account -----------------------------------------------------


def test_the_first_boot_creates_an_admin(client):
    response = client.post(
        "/login",
        data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"


def test_the_shipped_password_opens_nothing_but_the_change_page(client):
    """The whole reason shipping a known password is defensible.

    It is in the documentation, so it is known to everybody, and an account
    still using it must not be able to do anything at all.
    """
    client.post(
        "/login", data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD}
    )

    for path in ("/", "/settings", "/users", "/setup"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/change-password", path

    assert client.get("/change-password").status_code == 200


def test_changing_the_password_opens_the_rest(client):
    sign_in_as_admin(client)

    assert client.get("/").status_code == 200
    assert client.get("/settings").status_code == 200


def test_the_old_password_stops_working(client):
    sign_in_as_admin(client)
    client.post("/logout")

    response = client.post(
        "/login",
        data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 401


def test_a_new_password_has_to_be_long_enough(client):
    client.post(
        "/login", data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD}
    )

    response = client.post(
        "/change-password",
        data={
            "current_password": auth.DEFAULT_PASSWORD,
            "new_password": "short",
            "confirm_password": "short",
        },
    )

    assert response.status_code == 400
    assert "10 characters" in response.text


def test_changing_a_password_needs_the_current_one(client):
    sign_in_as_admin(client)

    response = client.post(
        "/change-password",
        data={
            "current_password": "not-the-current-password",
            "new_password": "another-long-password",
            "confirm_password": "another-long-password",
        },
    )

    assert response.status_code == 400
    assert "not the current password" in response.text


# -- signing in --------------------------------------------------------------


def test_a_wrong_password_is_refused(client):
    sign_in_as_admin(client)
    client.post("/logout")

    response = client.post("/login", data={"username": "admin", "password": "wrong-password"})

    assert response.status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_will_not_redirect_off_site(client):
    sign_in_as_admin(client)
    client.post("/logout")

    response = client.post(
        "/login",
        data={"username": "admin", "password": NEW_PASSWORD, "next": "//example.com/"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_where_you_were_going_survives_the_sign_in(client):
    sign_in_as_admin(client)
    client.post("/logout")

    landing = client.get("/settings", follow_redirects=False)
    assert landing.headers["location"] == "/login?next=/settings"

    response = client.post(
        "/login",
        data={"username": "admin", "password": NEW_PASSWORD, "next": "/settings"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings"


def test_guessing_is_throttled(client):
    """An internet facing login form without this is an invitation."""
    sign_in_as_admin(client)
    client.post("/logout")

    for _ in range(auth.MAX_ATTEMPTS):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    response = client.post("/login", data={"username": "admin", "password": NEW_PASSWORD})

    assert response.status_code == 429
    assert "Too many attempts" in response.text


def test_a_successful_sign_in_clears_the_count(client):
    sign_in_as_admin(client)
    client.post("/logout")

    for _ in range(auth.MAX_ATTEMPTS - 1):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    assert (
        client.post(
            "/login", data={"username": "admin", "password": NEW_PASSWORD}, follow_redirects=False
        ).status_code
        == 303
    )

    client.post("/logout")
    for _ in range(auth.MAX_ATTEMPTS - 1):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    assert (
        client.post(
            "/login", data={"username": "admin", "password": NEW_PASSWORD}, follow_redirects=False
        ).status_code
        == 303
    )


def test_the_api_gets_a_status_code_rather_than_a_login_page(client):
    """A login page arriving where JSON was expected reports the wrong problem."""
    response = client.get("/api/state", follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["error"]


def test_the_policies_never_print_a_placeholder_building(client):
    """Before setup there is no building name, and inventing one shows up on
    a page a carrier reads. The application is PitWatch; the building is
    whatever somebody types, and until they do it is not named at all."""
    for path in ("/messaging-policy", "/privacy"):
        page = client.get(path).text
        assert "PitWatch monitors the pumps in this building" in page, path
        assert "Ejector" not in page, path


def test_the_policies_name_the_building_once_it_is_set(client):
    sign_in_as_admin(client)
    client.post(
        "/settings/site",
        data={"site_name": "822 Greenwich St"},
    )

    page = client.get("/messaging-policy").text

    assert "the pumps at 822 Greenwich St" in page
    # The part of the building the pumps are in belongs in an alert, not in
    # prose on a policy page.
    assert "822 Greenwich St, Basement, rear" not in page


def test_the_public_pages_are_named_after_the_product(client):
    """Not after the building, which may not be set and is not the point."""
    page = client.get("/privacy").text

    assert "<title>Privacy</title>" in page
    assert "PitWatch" in page


def test_static_assets_carry_the_version(client):
    """Otherwise a browser keeps serving the stylesheet it already has.

    This bit during development in the worst way: a CSS fix shipped, was pulled,
    and appeared not to have worked, because the phone had the old file cached
    and no reason to ask for another. The version in the query string means an
    upgrade changes the URL.
    """
    for path in ("/login", "/messaging-policy"):
        page = client.get(path).text
        assert "style.css?v=" in page, path


def test_the_policy_stands_up_without_any_contact_details(client):
    """They are optional, and the page has to be complete without them.

    What actually stops the messages is replying STOP, which the carriers
    handle and which needs nobody's address. Publishing a personal mobile on a
    page anyone can read is a real cost and should not be the price of a
    compliant policy.
    """
    sign_in_as_admin(client)
    client.post("/settings/site", data={"site_name": "822 Greenwich St"})

    page = client.get("/messaging-policy").text

    assert "STOP" in page
    assert "HELP" in page
    assert "Message and data rates may apply" in page
    assert "never have to reach anybody" in page
    assert "who added you to these alerts" in page


def test_contact_details_are_shown_when_there_are_some(client):
    sign_in_as_admin(client)
    client.post(
        "/settings/site",
        data={"site_name": "822 Greenwich St", "site_contact_email": "pumps@example.com"},
    )

    page = client.get("/messaging-policy").text

    assert "pumps@example.com" in page
    # And STOP is still the first thing offered, because it is the one that
    # works without asking anybody.
    assert page.index("never have to reach anybody") < page.index("pumps@example.com")
