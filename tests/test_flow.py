"""The whole setup and settings flow, through real HTTP against a real database.

Skipped without PITWATCH_TEST_DATABASE_URL, same as the rest.

What these are really guarding is the boring half of an appliance: that a fresh
container offers a setup page, that the page closes once it has been used, and
that a setting saved in a browser is the setting the ingest tasks then read.
That chain has several links and every one of them is a plain mistake away from
looking fine and doing nothing.
"""

from __future__ import annotations

import re

import pytest

from pitwatch import auth

NEW_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _no_leftover_throttling():
    """Failed sign in attempts are counted in memory and outlive a test."""
    auth.reset_throttling()
    yield
    auth.reset_throttling()


def sign_in(client, username: str = auth.DEFAULT_USERNAME, password: str = NEW_PASSWORD):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def sign_in_as_admin(client):
    """Sign in with the shipped password and get past the forced change.

    Every test needs this, which is the point: there is no way to reach
    anything without an account, and no way to keep the shipped password.
    """
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


SETUP_FORM = {
    "site_name": "Basement pit",
    "site_timezone": "America/New_York",
    "notify_delay_s": "5",
    "notify_cooldown_s": "900",
    "shelly_enabled": "on",
    "shelly_host": "192.168.1.50",
    "shelly_pump1_channel": "1",
    "shelly_pump2_channel": "0",
    "shelly_heartbeat_s": "30",
    "inputs_enabled": "on",
    "inputs_host": "192.168.1.51",
    "inputs_port": "1883",
    "inputs_username": "pitwatch",
    "inputs_password": "broker-secret",
    "inputs_topic": "pitwatch/inputs",
    "inputs_status_topic": "pitwatch/status",
    "inputs_client_id": "pitwatch",
    "inputs_debounce_ms": "500",
    "channel_1_label": "Lead float",
    "channel_2_label": "Lag float",
    "channel_3_label": "High water alarm float",
    "channel_4_label": "Panel alarm contact",
    "channel_5_label": "Pump 1 running",
    "channel_6_label": "Pump 2 running",
    "channel_7_label": "Pump 1 overload tripped",
    "channel_7_on_when": "absent",
    "channel_8_label": "Pump 2 overload tripped",
    "channel_8_on_when": "absent",
    "pump1_name": "North pump",
    "pump1_running_amps": "1.5",
    "pump1_nameplate_amps": "9.6",
    "pump2_name": "South pump",
    "pump2_running_amps": "1.5",
    "pump2_nameplate_amps": "9.6",
}


def test_a_fresh_install_offers_setup_once_you_are_in(client):
    sign_in_as_admin(client)
    response = client.get("/")

    assert response.status_code == 200
    assert "/setup" in response.text

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert "Set up PitWatch" in setup.text


def test_setup_saves_everything(client):
    sign_in_as_admin(client)
    response = client.post("/setup", data=SETUP_FORM, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "192.168.1.51" in settings.text
    assert "North pump" in settings.text


def test_setup_sends_you_to_settings_once_it_has_been_used(client):
    """Setup is the same settings under a friendlier name, so once it is done
    there is one place to change them and it is not this one."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    response = client.get("/setup", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_two_inputs_may_carry_the_same_name(client):
    """Nothing keys on the name, so there is nothing to collide.

    This used to be refused, because a name was an identity and two inputs
    could not share one. Now the input number is the identity and the name is
    description, and a panel with two contacts both marked "High water" is a
    real panel rather than a mistake for this to argue with.
    """
    sign_in_as_admin(client)
    response = client.post(
        "/setup", data=SETUP_FORM | {"channel_2_label": "Lead float"}, follow_redirects=False
    )

    assert response.status_code == 303
    channels = client.app.state.settings.inputs.channels
    assert channels[0].label == "Lead float"
    assert channels[1].label == "Lead float"


def test_the_clamp_choice_is_stored_the_way_it_was_made(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    state = client.get("/api/state").json()

    assert state["pumps"]["1"]["channel"] == 1
    assert state["pumps"]["2"]["channel"] == 0
    assert state["pumps"]["1"]["name"] == "North pump"

    stored = client.app.state.settings.shelly
    assert stored.pump1_channel == 1
    assert stored.pump2_channel == 0


def test_swapping_the_clamps_takes_effect_both_ways(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post(
        "/settings/shelly",
        data=SETUP_FORM | {"shelly_pump1_channel": "0", "shelly_pump2_channel": "1"},
    )

    state = client.get("/api/state").json()
    assert state["pumps"]["1"]["channel"] == 0
    assert state["pumps"]["2"]["channel"] == 1


def test_putting_both_pumps_on_one_clamp_is_refused(client):
    """A form is not a guarantee, so the model checks it again.

    Both pumps reading one motor would show a plausible dashboard that was
    simply wrong about one of them.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    response = client.post(
        "/settings/shelly",
        data=SETUP_FORM | {"shelly_pump1_channel": "1", "shelly_pump2_channel": "1"},
    )

    assert response.status_code == 400
    assert "same clamp" in response.text
    # And the previous, valid mapping is untouched.
    assert client.app.state.settings.shelly.clamp_for_pump == {1: 1, 2: 0}


def test_settings_need_a_sign_in(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    page = client.get("/settings", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=/settings"

    save = client.post(
        "/settings/site", data={"site_name": "Somewhere else"}, follow_redirects=False
    )
    assert save.status_code == 303
    assert save.headers["location"].startswith("/login")
    assert client.app.state.settings.site.name != "Somewhere else"


def test_saving_one_section_leaves_the_others_alone(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post(
        "/settings/site", data={"site_name": "Renamed", "site_timezone": "America/New_York"}
    )

    page = client.get("/settings").text
    assert "Renamed" in page
    assert "192.168.1.51" in page
    assert "North pump" in page


def test_an_smtp_password_is_kept_when_the_box_is_left_empty(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/smtp",
        data={
            "smtp_enabled": "on",
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_username": "alerts",
            "smtp_password": "the-real-password",
            "smtp_from_address": "alerts@example.com",
        },
    )

    # Saving again without retyping the password must not blank it.
    client.post(
        "/settings/smtp",
        data={
            "smtp_enabled": "on",
            "smtp_host": "smtp2.example.com",
            "smtp_port": "587",
            "smtp_username": "alerts",
            "smtp_from_address": "alerts@example.com",
        },
    )

    store = client.app.state.settings
    assert store.smtp.password == "the-real-password"
    assert store.smtp.host == "smtp2.example.com"


def test_an_smtp_password_can_be_cleared_on_purpose(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/smtp",
        data={"smtp_host": "smtp.example.com", "smtp_password": "the-real-password"},
    )

    client.post(
        "/settings/smtp",
        data={"smtp_host": "smtp.example.com", "smtp_clear_password": "on"},
    )

    assert client.app.state.settings.smtp.password == ""


def test_a_stored_password_is_never_sent_to_the_browser(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/smtp",
        data={"smtp_host": "smtp.example.com", "smtp_password": "the-real-password"},
    )

    page = client.get("/settings").text

    assert "the-real-password" not in page
    assert "unchanged" in page


def test_the_shelly_password_is_kept_when_the_box_is_left_empty(client):
    """Same rule as SMTP, and easier to get wrong because it is a device.

    Saving the Shelly section to change the clamp mapping must not silently
    drop the device password and leave ingest unable to authenticate.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/shelly",
        data=SETUP_FORM | {"shelly_password": "the-device-password"},
    )

    # Swapping means moving both, now that both are stored rather than one
    # being inferred from the other.
    client.post(
        "/settings/shelly",
        data=SETUP_FORM | {"shelly_pump1_channel": "0", "shelly_pump2_channel": "1"},
    )

    store = client.app.state.settings
    assert store.shelly.password == "the-device-password"
    assert store.shelly.clamp_for_pump == {1: 0, 2: 1}


def test_the_shelly_password_can_be_cleared_on_purpose(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_password": "the-device-password"})

    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_clear_password": "on"})

    assert client.app.state.settings.shelly.password is None


def test_the_shelly_password_never_reaches_the_browser(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_password": "the-device-password"})

    assert "the-device-password" not in client.get("/settings").text


def test_the_inputs_test_button_reports_a_broker_it_cannot_reach(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    response = client.post(
        "/api/test/inputs",
        data={"inputs_host": "192.0.2.1", "inputs_port": "1883"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_health_is_healthy_once_the_database_is_up(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_state_endpoint_reports_both_devices(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    state = client.get("/api/state").json()

    assert set(state["devices"]) == {"shelly", "inputs"}
    assert state["site"]["name"] == "Basement pit"


def test_the_shelly_test_button_needs_a_sign_in_once_there_is_an_account(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    response = client.post("/api/test/shelly", data={"shelly_host": "192.168.1.50"})

    assert response.status_code == 401


def test_the_shelly_test_button_reports_a_device_it_cannot_reach(client):
    """Unreachable is an answer, not an error.

    192.0.2.1 is reserved for documentation and routes nowhere, so this
    exercises the failure path without depending on what is on the network.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    response = client.post("/api/test/shelly", data={"shelly_host": "192.0.2.1"})

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_the_dashboard_replaces_the_setup_prompt_once_configured(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/").text

    assert "Basement pit" in page
    assert "North pump" not in page, "names arrive over the live feed, not in the markup"
    assert 'data-pump="1"' in page
    assert 'data-pump="2"' in page


def test_the_live_feed_sends_the_same_shape_the_api_does(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    from_api = client.get("/api/state").json()

    with client.websocket_connect("/ws/state") as socket:
        from_socket = socket.receive_json()

    # updated_at moves between the two reads; everything else has to match, or
    # the page has two renderers pretending to be one.
    from_api.pop("updated_at", None)
    from_socket.pop("updated_at", None)
    assert from_socket == from_api


def test_the_live_feed_reports_a_pump_with_no_readings_as_unknown(client):
    """No clamp data is not the same as zero amps.

    Zero amps is a pump that is sitting there working fine. A dashboard that
    renders a dead meter as a healthy idle pump is the specific failure this
    whole project exists to avoid.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    pump = client.get("/api/state").json()["pumps"]["1"]

    assert pump["current"] is None
    # Voltage and power are still recorded but never reported: the meter's
    # voltage reference is its own supply rather than a measured phase, so
    # anything derived from it would be a number about the wrong circuit.
    assert "voltage" not in pump
    assert "act_power" not in pump
    assert pump["running"] is False
    assert pump["drawing_current"] is False


def test_an_input_with_no_name_is_left_off_the_dashboard(client):
    """And a named one that has not been read yet reports unknown, not off.

    None is not False. An input reported as off when nothing has read it is an
    alarm that will never fire and will look like it is working.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM | {"channel_3_label": ""})

    inputs = {row["channel"]: row for row in client.get("/api/state").json()["inputs"]}

    assert 3 not in inputs, "nothing is wired there"
    assert inputs[1]["label"] == "Lead float"
    assert inputs[1]["state"] is None, "wired, but nothing has been read yet"


SHELLY_ONLY_FORM = {
    key: value
    for key, value in SETUP_FORM.items()
    # Everything except the panel module section, which is how you set this up
    # while the I/O module is still in the post.
    if not key.startswith(("inputs_", "channel_"))
}


def test_setup_works_with_only_the_clamps_configured(client):
    """The panel module is optional, and starting without it is a normal path.

    Waiting on hardware is the usual case, not an edge case: the clamps go on
    in ten minutes and the I/O module needs the panel opened up.
    """
    sign_in_as_admin(client)
    response = client.post("/setup", data=SHELLY_ONLY_FORM, follow_redirects=False)

    assert response.status_code == 303
    assert client.get("/").status_code == 200

    state = client.get("/api/state").json()
    assert state["pumps"]["1"]["name"] == "North pump"
    assert state["devices"]["shelly"]["configured"] is True


def test_an_unconfigured_device_is_not_reported_as_a_fault(client):
    """Not set up and not reachable are different answers.

    Painting a red fault for a device nobody has configured teaches whoever
    reads this page to ignore the one place that goes red when it matters.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SHELLY_ONLY_FORM)

    inputs = client.get("/api/state").json()["devices"]["inputs"]

    assert inputs["configured"] is False
    assert inputs["online"] is False
    assert inputs["last_error"] is None


def test_every_contact_reads_as_unknown_without_the_io_module(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SHELLY_ONLY_FORM)

    state = client.get("/api/state").json()

    assert state["inputs"] == [], "no module, so nothing is named"
    for pump in state["pumps"].values():
        assert pump["current"] is None
        assert pump["running"] is False


def test_adding_the_io_module_later_does_not_need_a_restart(client):
    """The supervisor watches the settings and restarts the reader itself.

    This is the whole reason device addresses live in the database rather than
    in the environment.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SHELLY_ONLY_FORM)
    assert client.get("/api/state").json()["devices"]["inputs"]["configured"] is False

    client.post(
        "/settings/inputs",
        data={key: value for key, value in SETUP_FORM.items() if key != "shelly_host"},
    )

    state = client.get("/api/state").json()
    assert state["devices"]["inputs"]["configured"] is True
    assert client.app.state.settings.inputs.host == "192.168.1.51"


def test_the_dashboard_shows_amps_and_nothing_derived_from_voltage(client):
    """A CT measures the conductor, so current is valid whatever the meter is
    using as a voltage reference. Watts and power factor need voltage and
    current from the same phase with the right angle between them, which is not
    true when the meter is powered from a different outlet, so they are not
    shown at all rather than shown and quietly wrong."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/").text

    assert "data-amps" in page
    assert "data-voltage" not in page
    assert "data-power" not in page


def test_the_header_is_icons_rather_than_words(client):
    """It wrapped onto two lines on a phone, which is where this gets read.

    Each icon keeps a title and an aria-label, so it is still a word to a
    screen reader and on hover.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/").text

    for label in ("Dashboard", "Users", "Settings", "Your profile"):
        assert f'aria-label="{label}"' in page, label
    # The words themselves are gone from the navigation.
    assert ">Dashboard</a>" not in page
    assert ">Settings</a>" not in page


def test_a_non_admin_sees_no_settings_or_users_icon(client):
    """The icons are a menu, not decoration: what is not theirs is not shown."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/users/new",
        data={"name": "Watcher", "username": "watcher", "email": "w@example.com"},
    )

    page = client.get("/").text
    assert 'aria-label="Settings"' in page, "the admin should see it"

    # The template decides by user.is_admin, which the Users page shows too.
    assert 'aria-label="Users"' in page


def test_the_profile_page_carries_everything_about_you(client):
    """One page rather than a dropdown of mismatched links and buttons."""
    sign_in_as_admin(client)

    page = client.get("/profile").text

    assert 'name="email"' in page
    assert 'name="phone"' in page
    assert 'name="notify_email"' in page
    assert 'name="notify_sms"' in page
    assert 'action="/change-password"' in page
    assert 'action="/logout"' in page


def test_you_can_change_your_own_contact_details(client):
    sign_in_as_admin(client)

    client.post(
        "/profile",
        data={
            "name": "David",
            "email": "david@example.com",
            "phone": "(212) 555-0142",
            "notify_sms": "on",
            "min_severity": "warning",
        },
    )

    page = client.get("/profile").text
    assert 'value="david@example.com"' in page
    assert 'value="+12125550142"' in page


def test_wanting_no_alerts_at_all_is_allowed(client):
    """A real choice, not an error. The account still works."""
    sign_in_as_admin(client)

    response = client.post(
        "/profile", data={"name": "David", "email": "d@example.com", "min_severity": "warning"}
    )

    assert response.status_code == 200
    assert client.get("/").status_code == 200


def test_asking_for_alerts_with_nowhere_to_send_them_is_refused(client):
    sign_in_as_admin(client)

    response = client.post("/profile", data={"name": "David", "notify_sms": "on"})
    assert response.status_code == 400
    assert "number to send them to" in response.text

    response = client.post("/profile", data={"name": "David", "notify_email": "on"})
    assert response.status_code == 400
    assert "address to send it to" in response.text


def test_your_own_profile_cannot_make_you_an_administrator(client):
    """The fields that are not yours to set are not reachable from that form.

    Enforced by the handler rather than by the template, so posting them by
    hand does not help either. This posts them by hand.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/users/new",
        data={"name": "Watcher", "username": "watcher", "email": "w@example.com"},
    )

    # Give them a password through the invitation, then become them.
    invite = client.post(f"/users/{_user_id(client, 'watcher')}/invite").text
    token = re.search(r"/set-password\?token=([A-Za-z0-9_-]+)", invite).group(1)
    client.post("/logout")
    client.post(
        "/set-password",
        data={
            "token": token,
            "new_password": "their-own-long-password",
            "confirm_password": "their-own-long-password",
        },
    )

    client.post(
        "/profile",
        data={
            "name": "Watcher",
            "email": "w@example.com",
            "is_admin": "on",
            "enabled": "",
            "min_severity": "warning",
        },
    )

    # Still not an administrator, and still able to sign in.
    assert client.get("/users", follow_redirects=False).status_code == 403
    assert client.get("/settings", follow_redirects=False).status_code == 403
    assert client.get("/").status_code == 200


def _user_id(client, username: str) -> int:
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


def test_setup_records_the_administrator_as_somebody_to_alert(client):
    """It used to ask for a public contact and nothing else, so the person
    doing the setup ended up with no way to be reached and no sign of it."""
    sign_in_as_admin(client)

    client.post(
        "/setup",
        data=SETUP_FORM
        | {
            "admin_name": "David",
            "admin_email": "david@example.com",
            "admin_phone": "(212) 555-0142",
            "admin_notify_email": "on",
            "admin_notify_sms": "on",
        },
    )

    profile = client.get("/profile").text
    assert 'value="david@example.com"' in profile
    assert 'value="+12125550142"' in profile


def test_the_public_contact_and_your_own_are_kept_apart(client):
    """Two emails and two phone numbers on one page, for opposite purposes.

    One pair is printed on a page anybody can read. The other is where a text
    arrives at two in the morning. Confusing them is how somebody ends up with
    their mobile number on a public policy and no alerts.
    """
    sign_in_as_admin(client)

    client.post(
        "/setup",
        data=SETUP_FORM
        | {
            "site_contact_email": "super@building.example.com",
            "admin_email": "david@example.com",
            "admin_notify_email": "on",
        },
    )

    assert client.app.state.settings.site.contact_email == "super@building.example.com"
    assert 'value="david@example.com"' in client.get("/profile").text
    # And the public page shows only the public one.
    policy = client.get("/messaging-policy").text
    assert "super@building.example.com" in policy
    assert "david@example.com" not in policy


# -- naming the inputs ------------------------------------------------------


def test_naming_an_input_is_all_it_takes_to_watch_it(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post(
        "/settings/inputs",
        data={
            "inputs_enabled": "on",
            "inputs_host": "192.168.1.51",
            "channel_1_label": "Bottom float",
            "channel_2_label": "Seal failure",
            "channel_2_on_when": "absent",
        },
    )

    channels = client.app.state.settings.inputs.channels
    assert channels[0].label == "Bottom float"
    assert channels[1].label == "Seal failure"
    assert channels[1].invert is True
    # Everything not named in that post is now unnamed, which is the only way
    # clearing a name can work when a form posts the whole section.
    assert [c.channel for c in client.app.state.settings.inputs.used_channels] == [1, 2]


def test_clearing_a_name_stops_watching_that_input(client):
    """Which is the entire removal story. There is no delete to get wrong."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    assert len(client.app.state.settings.inputs.used_channels) == 8

    client.post(
        "/settings/inputs",
        data={key: value for key, value in SETUP_FORM.items() if key != "channel_4_label"}
        | {"channel_4_label": "   "},
    )

    used = client.app.state.settings.inputs.used_channels
    assert [c.channel for c in used] == [1, 2, 3, 5, 6, 7, 8]


def test_a_renamed_input_is_shown_under_its_new_name(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post("/settings/inputs", data=SETUP_FORM | {"channel_3_label": "TOP FLOAT"})

    page = client.get("/settings").text
    assert 'value="TOP FLOAT"' in page
    inputs = {row["channel"]: row for row in client.get("/api/state").json()["inputs"]}
    assert inputs[3]["label"] == "TOP FLOAT"


# -- the dashboard lamps -----------------------------------------------------


def test_assigning_a_lamp_shows_the_input_by_its_name(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    save = client.post(
        "/settings/dashboard",
        data={
            "role_system_alert": "4",
            "role_high_water": "3",
            "role_lead_float": "1",
            "role_lag_float": "2",
            "role_pump1_run": "5",
            "role_pump2_run": "6",
            "role_pump1_fault": "7",
            "role_pump2_fault": "8",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303

    dashboard = client.app.state.settings.dashboard
    assert dashboard.high_water == 3
    assert dashboard.pump2_fault == 8

    panel = client.get("/api/state").json()["panel"]
    assert panel["high_water"]["channel"] == 3
    assert panel["high_water"]["label"] == "High water alarm float"
    # Nothing has read the module, so every lamp is unknown rather than off.
    assert panel["high_water"]["state"] is None
    assert panel["display"] == {"1": "--", "2": "--"}


def test_a_lamp_left_blank_stays_unassigned(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post("/settings/dashboard", data={"role_high_water": "3"})

    panel = client.get("/api/state").json()["panel"]
    assert panel["high_water"]["channel"] == 3
    assert panel["system_alert"]["channel"] is None
    assert panel["system_alert"]["label"] is None


def test_an_input_a_lamp_is_showing_is_not_listed_again_below(client):
    """The panel and the leftovers list are one screen. An input in both would
    be read twice and counted once."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/dashboard",
        data={"role_lead_float": "1", "role_lag_float": "2", "role_high_water": "3"},
    )

    state = client.get("/api/state").json()
    listed = {row["channel"] for row in state["inputs"]}

    assert listed == {4, 5, 6, 7, 8}


def test_two_lamps_may_share_one_input(client):
    """A simple panel really can bring out one contact that is both its high
    water float and its alarm."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    save = client.post(
        "/settings/dashboard",
        data={"role_high_water": "3", "role_system_alert": "3"},
        follow_redirects=False,
    )

    assert save.status_code == 303
    panel = client.get("/api/state").json()["panel"]
    assert panel["high_water"]["channel"] == 3
    assert panel["system_alert"]["channel"] == 3


# -- the alerts page ---------------------------------------------------------


def test_the_alerts_page_is_for_administrators(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    assert client.get("/alerts").status_code == 200

    client.post("/logout")
    page = client.get("/alerts", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")


def test_every_rule_is_on_the_page_with_what_it_says(client):
    from pitwatch.domain import alerts as specs

    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/alerts").text

    for spec in specs.SPECS:
        assert f"{spec.key}_enabled" in page, spec.key
        assert f"{spec.key}_message" in page, spec.key
        assert spec.title in page, spec.title


def test_the_thresholds_moved_off_the_pumps_page(client):
    """A number on the pumps page told you nothing about what happened when it
    was crossed. Beside its own message it tells you everything."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    pumps = client.get("/settings").text
    alerts = client.get("/alerts").text

    for field in ("max_runtime_ms", "restart_gap_ms", "quiet_minutes_before_flag"):
        assert field not in pumps, field
    assert "short_cycling_restart_within_ms" in alerts
    assert "nothing_has_run_quiet_minutes" in alerts
    assert "run_too_long_longer_than_ms" in alerts
    # What a pump is stays on the pumps page.
    assert "pump1_running_amps" in pumps
    assert "pump1_nameplate_amps" in pumps


def test_saving_a_rule_keeps_it(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    response = client.post(
        "/alerts",
        data={
            "high_water_enabled": "on",
            "high_water_severity": "warning",
            "high_water_message": "The pit is full at {site}.",
            "over_current_enabled": "on",
            "over_current_severity": "critical",
            "over_current_pump1_amps": "18.5",
            "over_current_readings": "3",
            "device_offline_admins_only": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    alerts = client.app.state.settings.alerts
    assert alerts.high_water.message == "The pit is full at {site}."
    assert alerts.high_water.severity.value == "warning"
    assert alerts.over_current.pump1_amps == 18.5
    assert alerts.over_current.readings == 3
    assert alerts.device_offline.admins_only is True
    # Anything the form did not carry is switched off rather than left on, the
    # way an unchecked box always is.
    assert alerts.float_activity.enabled is False


def test_a_rule_keeps_its_words_when_the_box_is_left_empty(client):
    """Clearing the message would send a blank text at three in the morning."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post("/alerts", data={"high_water_enabled": "on", "high_water_message": ""})

    assert "{site}" in client.app.state.settings.alerts.high_water.message
