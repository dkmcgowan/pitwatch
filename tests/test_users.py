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
from pathlib import Path

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
    """Sign in as the admin, whether or not this test has done it before.

    The first call has to use the shipped password and then change it, because
    that account can go nowhere until it does. Every later call has to use the
    new one. Getting that wrong does not fail loudly: the sign in quietly does
    not happen, the next request redirects to the login page, and the test then
    asserts against a page that never did what it asked for.
    """
    first = client.post(
        "/login",
        data={"username": auth.DEFAULT_USERNAME, "password": auth.DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    if first.status_code == 303:
        client.post(
            "/change-password",
            data={
                "current_password": auth.DEFAULT_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
    else:
        signed_in = client.post(
            "/login",
            data={"username": auth.DEFAULT_USERNAME, "password": NEW_PASSWORD},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303, "could not sign back in as the admin"
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
    # By the account the row is about, not by reading the words in it. Scanning
    # the whole page for a name and then reaching backwards for an id happily
    # starts in one row and finishes in the next, which returns the wrong id
    # and then edits the wrong account. That is exactly what an earlier version
    # of this did, and the test that noticed was the one where the wrong
    # account turned out to be the admin.
    for row in page.split("<tr")[1:]:
        if f'data-username="{username}"' not in row:
            continue
        # The edit link specifically. Taking the first "/users/" in the row and
        # reading to "/edit" worked until a second form appeared above it, and
        # then quietly read across both of them.
        return int(re.search(r"/users/(\d+)/edit", row).group(1))
    raise AssertionError(f"{username} is not on the users page")


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

    client.post("/users/new", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text
    assert "Building Super" in page
    assert "super@example.com" in page
    # Typed the way a person types it, stored the way AWS needs it.
    assert "+12125550142" in page


def test_someone_added_can_be_reached_without_ever_signing_in(client):
    """Most people here never sign in. They are here to be texted."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})

    # They are on the list, and reachable, without a password.
    assert 'data-username="super"' in client.get("/users").text

    # Whether they can sign in is on the edit form now rather than in a column.
    # The list is who they are and what they get.
    user_id = user_id_of(client, "super")
    form = client.get(f"/users/{user_id}/edit").text
    assert "Send an invitation" in form, "no password yet, so an invitation is offered"


def test_a_person_set_to_be_texted_needs_a_number(client):
    sign_in_as_admin(client)

    response = client.post(
        "/users/new", data={"name": "No Phone", "notify_sms": "on", "min_severity": "warning"}
    )

    assert response.status_code == 400
    assert "no number" in response.text


def test_a_person_set_to_be_emailed_needs_an_address(client):
    sign_in_as_admin(client)

    response = client.post(
        "/users/new", data={"name": "No Mail", "notify_email": "on", "min_severity": "warning"}
    )

    assert response.status_code == 400
    assert "no address" in response.text


def test_a_phone_number_that_is_not_one_is_refused(client):
    sign_in_as_admin(client)

    response = client.post(
        "/users/new", data={"name": "Wrong", "phone": "nonsense", "notify_sms": "on"}
    )

    assert response.status_code == 400
    assert "phone number" in response.text


def test_editing_someone_keeps_the_change(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    client.post(
        f"/users/{user_id}/edit",
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
        f"/users/{admin_id}/edit", data={"name": "Administrator", "enabled": "on"}
    )
    assert response.status_code == 400
    assert "administrator rights" in response.text

    response = client.post(
        f"/users/{admin_id}/edit", data={"name": "Administrator", "is_admin": "on"}
    )
    assert response.status_code == 400
    assert "disable your own account" in response.text

    response = client.post(f"/users/{admin_id}/delete")
    assert response.status_code == 400
    assert "delete your own account" in response.text


def test_only_an_admin_can_manage_people_or_settings(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
    # Checked for the signed in layout rather than for a 200, because the root
    # answers 200 to a signed out visitor too: it is the public home page.
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "public-nav" not in dashboard.text


# -- invitations -------------------------------------------------------------


def test_an_invitation_sets_a_password_and_signs_them_in(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
    assert client.get("/profile").status_code == 200


def test_an_invitation_link_only_works_once(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
        f"/users/{user_id}/edit", data={"name": "Building Super", "email": "super@example.com"}
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
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    shown = client.post(f"/users/{user_id}/invite").text
    assert "/set-password?token=" in shown

    assert "/set-password?token=" not in client.get("/users").text


def test_a_signed_in_non_admin_cannot_reach_the_dashboard_lamps(client):
    """Being able to sign in is not being able to configure the place.

    Somebody who only gets alerts has a real account and a real session, which
    is exactly the case a route guarded by nothing but "is signed in" would
    let through.
    """
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
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
    assert client.get("/profile").status_code == 200, "they are signed in"

    # What an input carries is part of the settings page, so the page to be
    # kept out of is that one.
    page = client.get("/settings", follow_redirects=False)
    assert page.status_code in (303, 403), page.status_code

    save = client.post(
        "/settings/inputs", data={"channel_3_role": "high_water"}, follow_redirects=False
    )
    assert save.status_code in (303, 403)

    # And alerts, which has its own place in the header now.
    assert client.get("/alerts", follow_redirects=False).status_code in (303, 403)
    assert client.app.state.settings.inputs.channel_for("high_water") is None


# -- the list, and the forms that are not on it ------------------------------


def test_the_users_page_is_a_list_and_not_a_pile_of_forms(client):
    """It used to render every account as an open form on one page. Several
    editable copies of one shape, no way to see at a glance who gets what, and
    a Save button per row that looked like it might save all of them."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text

    assert "<table" in page
    for column in ("Name", "Email", "Mobile", "Alerts"):
        assert f">{column}</th>" in page, column
    # Who they are and what they get, and nothing else. User name, sign in
    # state, role and status were all here and made the table wider than the
    # page. They are on the edit form, which is where somebody goes to change
    # them anyway.
    for gone in ("User name", "Sign in", "Status"):
        assert f">{gone}</th>" not in page, gone
    # Role is back, as one box rather than nine characters of "Administrator".
    assert ">Admin</th>" in page
    # One editable form per account is exactly what this replaced, so the only
    # forms here are the single actions on a row. Counting them was the old
    # check and it broke the moment a third action appeared; naming the fields
    # that must not be here says what it means.
    for field in ('name="name"', 'name="email"', 'name="phone"', 'name="min_severity"'):
        assert field not in page, field
    assert 'href="/users/new"' in page


def test_the_list_shows_what_each_account_gets(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})

    row = ""
    for chunk in client.get("/users").text.split("<tr"):
        if 'data-username="super"' in chunk:
            row = chunk
    assert row, "the account is not in the table"

    assert "super@example.com" in row
    assert "Email, Text" in row, "both channels are on for this one"
    # Active or not is the icon offered rather than a column: an account that
    # is on can be disabled, one that is off can be enabled.
    assert "#icon-disable" in row
    assert "#icon-enable" not in row


def test_adding_happens_on_its_own_page_and_comes_back_to_the_list(client):
    sign_in_as_admin(client)

    assert client.get("/users/new").status_code == 200

    response = client.post("/users/new", data=SUPER | {"send_invite": ""}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/users?saved=added"
    assert "super" in client.get("/users").text


def test_a_rejected_add_comes_back_to_the_form_and_not_the_list(client):
    """With what was typed still in it. Bouncing to a list and making somebody
    start again is how a typo costs five fields."""
    sign_in_as_admin(client)

    page = client.post(
        "/users/new",
        data={"name": "No Mail", "notify_email": "on", "min_severity": "warning"},
    )

    assert page.status_code == 400
    assert "has no address" in page.text
    assert 'action="/users/new"' in page.text, "still the form"
    assert "<table" not in page.text, "not the list"


def test_editing_happens_on_its_own_page_with_a_way_out(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    page = client.get(f"/users/{user_id}/edit")

    assert page.status_code == 200
    assert "Building Super" in page.text
    assert f'action="/users/{user_id}/edit"' in page.text
    assert 'href="/users">Cancel<' in page.text, "a way out that changes nothing"
    # The sign in name is the one thing that cannot be changed here.
    assert 'name="username"' not in page.text


def test_saving_an_edit_returns_to_the_list(client):
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    response = client.post(
        f"/users/{user_id}/edit",
        data={"name": "Renamed", "email": "super@example.com", "min_severity": "warning"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/users?saved=updated"
    assert "Renamed" in client.get("/users").text


def test_active_can_be_flipped_from_the_list(client):
    """Without opening the form, which is the common case: somebody is on
    holiday and should stop being texted at three in the morning."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    # There is no Status column any more. Which icon the row offers is how the
    # state shows: an account that is on can be disabled, one that is off can
    # be enabled.
    client.post(f"/users/{user_id}/toggle", follow_redirects=False)
    page = client.get("/users").text
    assert 'title="Enable Building Super"' in page
    assert "row-off" in page or 'class="off"' in page, "and the row is dimmed"

    client.post(f"/users/{user_id}/toggle", follow_redirects=False)
    page = client.get("/users").text
    assert 'title="Disable Building Super"' in page
    assert 'class="off"' not in page


def test_you_cannot_disable_yourself_from_the_list_either(client):
    """The button is not offered, and the server refuses it anyway. An install
    with nobody who can change anything needs the database edited by hand."""
    sign_in_as_admin(client)
    admin_id = user_id_of(client, auth.DEFAULT_USERNAME)

    page = client.get("/users").text
    assert f'action="/users/{admin_id}/toggle"' not in page
    assert f'action="/users/{admin_id}/delete"' not in page

    refused = client.post(f"/users/{admin_id}/toggle")
    assert refused.status_code == 400
    assert "cannot disable your own account" in refused.text


def test_a_notification_box_needs_somewhere_to_send(client):
    """Checked in the browser and again here. A box ticked for email on an
    account with no address reads as configured and delivers nothing."""
    sign_in_as_admin(client)

    page = client.get("/users/new").text
    assert 'data-requires="notify_email"' in page
    assert 'data-requires="notify_sms"' in page

    refused = client.post(
        "/users/new", data={"name": "No Phone", "notify_sms": "on", "min_severity": "warning"}
    )
    assert refused.status_code == 400
    assert "has no number" in refused.text


def test_the_row_actions_are_icons(client):
    """Three words per row was most of what made the table too wide. An icon
    with a title and a label says the same thing in a quarter of the space."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text

    for icon in ("#icon-pencil", "#icon-disable", "#icon-trash"):
        assert icon in page, icon
    # Every one still says what it is, for a screen reader and on hover.
    assert 'aria-label="Edit Building Super"' in page
    assert 'aria-label="Delete Building Super"' in page
    assert 'title="Disable Building Super"' in page


def test_disabling_flips_which_icon_is_offered(client):
    """Status has no column any more. Which icon is there is how it shows."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    client.post(f"/users/{user_id}/toggle")

    page = client.get("/users").text
    assert 'title="Enable Building Super"' in page
    assert 'title="Disable Building Super"' not in page


def test_deleting_asks_first(client):
    """A real dialog rather than the browser's confirm box, which cannot say
    which account it means in the page's own voice."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text

    assert 'data-confirm="Delete Building Super?' in page
    assert '<dialog class="note confirm" id="confirm">' in page
    assert "data-confirm-yes" in page and "data-confirm-no" in page


def test_administrator_is_a_box_that_can_be_ticked(client):
    """One click either way, on a page only administrators can reach. Opening a
    form to change one checkbox is more ceremony than the change deserves."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    row = ""
    for chunk in client.get("/users").text.split("<tr"):
        if 'data-username="super"' in chunk:
            row = chunk
    assert f'action="/users/{user_id}/admin"' in row
    assert "data-autosubmit" in row
    assert "checked" not in row.split("data-autosubmit", 1)[1].split(">", 1)[0]

    client.post(f"/users/{user_id}/admin", follow_redirects=False)

    row = ""
    for chunk in client.get("/users").text.split("<tr"):
        if 'data-username="super"' in chunk:
            row = chunk
    assert "checked" in row.split("data-autosubmit", 1)[1].split(">", 1)[0]


def test_you_cannot_take_away_your_own_administrator_rights(client):
    """The box is ticked and fixed, and the server refuses it as well. An
    install with nobody who can change anything needs the database edited by
    hand to recover."""
    sign_in_as_admin(client)
    admin_id = user_id_of(client, auth.DEFAULT_USERNAME)

    row = ""
    for chunk in client.get("/users").text.split("<tr"):
        if f'data-username="{auth.DEFAULT_USERNAME}"' in chunk:
            row = chunk
    assert "disabled" in row
    assert f'action="/users/{admin_id}/admin"' not in row

    refused = client.post(f"/users/{admin_id}/admin")
    assert refused.status_code == 400
    assert "cannot remove your own administrator rights" in refused.text


def test_the_admin_box_works_without_scripting(client):
    """A checkbox posts nothing until something submits the form, so the markup
    carries a real button. The script hides it and lets the box do the work."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})

    page = client.get("/users").text
    js = (Path("pitwatch/static/setup.js")).read_text(encoding="utf-8")

    assert "data-autosubmit-go" in page, "a real submit button in the markup"
    assert "button.hidden = true;" in js, "hidden once the script is running"


def test_the_password_link_button_sits_in_the_form_it_belongs_to(client):
    """It was a card of its own below the save button, which read as a second
    thing to do rather than part of editing this person.

    It cannot be a nested form, because forms do not nest and it posts
    somewhere else. So the button lives in the section and points at an empty
    form by id. Worth a test: if that id ever stops matching, the button
    silently submits the edit form instead and quietly saves the person rather
    than sending them a link.
    """
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    page = client.get(f"/users/{user_id}/edit").text

    assert page.count('<section class="card"') == 1, "one card, not two"
    assert 'form="password-link"' in page
    assert 'id="password-link"' in page
    assert f'action="/users/{user_id}/invite"' in page
    # In the section rather than after it.
    assert page.index("Send an invitation") < page.index(">Save<")


def test_the_password_link_still_sends_from_its_new_home(client):
    """Moving where a button is drawn is exactly the kind of change that can
    leave it pointing at nothing."""
    sign_in_as_admin(client)
    client.post("/users/new", data=SUPER | {"send_invite": ""})
    user_id = user_id_of(client, "super")

    sent = client.post(f"/users/{user_id}/invite")

    assert sent.status_code == 200
    assert "/set-password?token=" in sent.text
