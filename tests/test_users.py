"""Managing people, and the invitation flow.

A person here is one row: somebody who gets told when the pit floods, and who
may or may not ever sign in. That unification is the point, so most of these
check the two halves stay one thing.

Everything goes through HTTP rather than reaching into the database. Partly
because the pool belongs to the test client's event loop and poking it from
outside is a fight, and mostly because a test that bypasses the application
stops noticing when the application stops doing what it says.
"""

from __future__ import annotations

import re

import pytest

from pitwatch import auth

NEW_PASSWORD = "a-long-enough-password"

SUPER = {
    "name": "Building Super",
    "username": "super",
    "email": "super@example.com",
    "phone": "(212) 555-0142",
    "notify_email": "on",
    "notify_sms": "on",
    "min_severity": "warning",
}


@pytest.fixture(autouse=True)
def _no_leftover_throttling():
    auth.reset_throttling()
    yield
    auth.reset_throttling()


def sign_in_as_admin(client):
    client.post(
        "/login", data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD}
    )
    client.post(
        "/change-password",
        data={
            "current_password": auth.DEFAULT_PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    return client


def user_id_of(client, username: str) -> int:
    """Find somebody's id from the page, the way a browser would.

    Split into forms first rather than searching the whole page. A lazy match
    across the page happily starts at one person's form and runs on to the next
    person's name, which returns the wrong id and then edits the wrong person.
    That is exactly what it did, and the test that noticed was the one where
    the wrong person turned out to be the admin.
    """
    page = client.get("/users").text
    for chunk in page.split('<form method="post" action="/users/')[1:]:
        identifier, _, rest = chunk.partition("/save")
        if f"<code>{username}</code>" in rest.split('<form method="post"')[0]:
            return int(identifier)
    raise AssertionError(f"{username} is not on the people page")


def invitation_link(client, user_id: int) -> str:
    """Ask for an invitation and read the link back off the page.

    With no mail server configured the application shows the link instead of
    emailing it, which is the fallback that stops an admin being stuck before
    SMTP is set up. Using it here means that path is tested too.
    """
    # Read it off the response to the POST, which the client follows through to
    # the people page. The link is shown once and cleared, the way a flash
    # message should be, so fetching the page a second time would find it gone.
    page = client.post(f"/users/{user_id}/invite").text
    match = re.search(r"(https?://\S+?/set-password\?token=[A-Za-z0-9_-]+)", page)
    assert match, "no invitation link was offered"
    return match.group(1)


def token_from(link: str) -> str:
    return link.split("token=", 1)[1]


# -- adding people -----------------------------------------------------------


def test_adding_someone_records_how_to_reach_them(client):
    sign_in_as_admin(client)

    client.post("/users/add", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text
    assert "Building Super" in page
    assert "super@example.com" in page
    # Typed the way a person types it, stored the way AWS needs it.
    assert "+12125550142" in page


def test_someone_added_can_be_reached_without_ever_signing_in(client):
    """Most people here never sign in. They are here to be texted."""
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text

    assert "once a password is set" in page, "should be shown as having no password"


def test_a_person_set_to_be_texted_needs_a_number(client):
    sign_in_as_admin(client)

    response = client.post(
        "/users/add", data={"name": "No Phone", "notify_sms": "on", "min_severity": "warning"}
    )

    assert response.status_code == 400
    assert "no number" in response.text


def test_a_person_set_to_be_emailed_needs_an_address(client):
    sign_in_as_admin(client)

    response = client.post(
        "/users/add", data={"name": "No Mail", "notify_email": "on", "min_severity": "warning"}
    )

    assert response.status_code == 400
    assert "no address" in response.text


def test_a_phone_number_that_is_not_one_is_refused(client):
    sign_in_as_admin(client)

    response = client.post(
        "/users/add", data={"name": "Wrong", "phone": "nonsense", "notify_sms": "on"}
    )

    assert response.status_code == 400
    assert "phone number" in response.text


def test_editing_someone_keeps_the_change(client):
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    client.post(
        f"/users/{user_id}/save",
        data={
            "name": "The Super",
            "email": "super@example.com",
            "phone": "2125550199",
            "notify_sms": "on",
            "enabled": "on",
        },
    )

    page = client.get("/users").text
    assert "The Super" in page
    assert "+12125550199" in page


# -- not locking yourself out ------------------------------------------------


def test_an_admin_cannot_lock_themselves_out(client):
    """An install with nobody who can change anything needs the database
    editing by hand to recover, which is not a state to reach by accident."""
    sign_in_as_admin(client)
    admin_id = user_id_of(client, auth.DEFAULT_USERNAME)

    response = client.post(
        f"/users/{admin_id}/save", data={"name": "Administrator", "enabled": "on"}
    )
    assert response.status_code == 400
    assert "administrator rights" in response.text

    response = client.post(
        f"/users/{admin_id}/save", data={"name": "Administrator", "is_admin": "on"}
    )
    assert response.status_code == 400
    assert "disable your own account" in response.text

    response = client.post(f"/users/{admin_id}/delete")
    assert response.status_code == 400
    assert "delete your own account" in response.text


def test_only_an_admin_can_manage_people_or_settings(client):
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    link = invitation_link(client, user_id)
    client.post("/logout")
    client.post(
        "/set-password",
        data={
            "token": token_from(link),
            "new_password": "their-own-long-password",
            "confirm_password": "their-own-long-password",
        },
    )

    assert client.get("/users", follow_redirects=False).status_code == 403
    assert client.get("/settings", follow_redirects=False).status_code == 403
    assert client.post("/settings/site", data={"site_name": "Nope"}).status_code == 403
    # The dashboard, which is what they were given an account for, works.
    assert client.get("/").status_code == 200


# -- invitations -------------------------------------------------------------


def test_an_invitation_sets_a_password_and_signs_them_in(client):
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    link = invitation_link(client, user_id_of(client, "super"))
    client.post("/logout")

    assert client.get(f"/set-password?token={token_from(link)}").status_code == 200

    response = client.post(
        "/set-password",
        data={
            "token": token_from(link),
            "new_password": "their-own-long-password",
            "confirm_password": "their-own-long-password",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.get("/").status_code == 200


def test_an_invitation_link_only_works_once(client):
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    link = invitation_link(client, user_id_of(client, "super"))
    client.post("/logout")

    client.post(
        "/set-password",
        data={
            "token": token_from(link),
            "new_password": "their-own-long-password",
            "confirm_password": "their-own-long-password",
        },
    )
    client.post("/logout")

    response = client.get(f"/set-password?token={token_from(link)}")

    assert response.status_code == 400
    assert "does not work" in response.text


def test_issuing_a_new_invitation_kills_the_old_one(client):
    """A resent invitation must not leave the first link live."""
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    first = invitation_link(client, user_id)
    second = invitation_link(client, user_id)

    assert client.get(f"/set-password?token={token_from(first)}").status_code == 400
    assert client.get(f"/set-password?token={token_from(second)}").status_code == 200


def test_a_made_up_token_is_refused(client):
    response = client.get("/set-password?token=not-a-real-token")

    assert response.status_code == 400
    assert "does not work" in response.text


def test_a_mismatched_password_does_not_spend_the_link(client):
    """A typo should not cost somebody their invitation."""
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    link = invitation_link(client, user_id_of(client, "super"))
    client.post("/logout")

    response = client.post(
        "/set-password",
        data={
            "token": token_from(link),
            "new_password": "their-own-long-password",
            "confirm_password": "mistyped-the-second-one",
        },
    )
    assert response.status_code == 400

    assert client.get(f"/set-password?token={token_from(link)}").status_code == 200


def test_a_disabled_person_cannot_sign_in(client):
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")
    link = invitation_link(client, user_id)
    client.post("/logout")
    client.post(
        "/set-password",
        data={
            "token": token_from(link),
            "new_password": "their-own-long-password",
            "confirm_password": "their-own-long-password",
        },
    )
    client.post("/logout")

    sign_in_as_admin(client)
    # Saving without the enabled box ticked turns them off.
    client.post(
        f"/users/{user_id}/save", data={"name": "Building Super", "email": "super@example.com"}
    )
    client.post("/logout")

    response = client.post(
        "/login", data={"username": "super", "password": "their-own-long-password"}
    )

    assert response.status_code == 401


def test_the_invitation_link_is_shown_once_and_then_cleared(client):
    """It is a flash message, not something left lying on the page.

    An invitation link is a bearer credential for somebody's account. Leaving
    it rendered on every later page load would make it as durable as the page
    itself.
    """
    sign_in_as_admin(client)
    client.post("/users/add", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    shown = client.post(f"/users/{user_id}/invite").text
    assert "/set-password?token=" in shown

    assert "/set-password?token=" not in client.get("/users").text
