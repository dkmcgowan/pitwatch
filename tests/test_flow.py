"""The whole setup and settings flow, through real HTTP against a real database.

Skipped without PITWATCH_TEST_DATABASE_URL, same as the rest.

What these are really guarding is the boring half of an appliance: that a fresh
container offers a setup page, that the page closes once it has been used, and
that a setting saved in a browser is the setting the ingest tasks then read.
That chain has several links and every one of them is a plain mistake away from
looking fine and doing nothing.
"""

from __future__ import annotations

SETUP_FORM = {
    "username": "admin",
    "password": "a-long-enough-password",
    "password_confirm": "a-long-enough-password",
    "site_name": "Basement pit",
    "site_location": "123 Example St, rear",
    "site_timezone": "America/New_York",
    "notify_delay_s": "30",
    "notify_cooldown_s": "900",
    "shelly_enabled": "on",
    "shelly_host": "192.168.1.50",
    "shelly_pump1_channel": "1",
    "shelly_heartbeat_s": "30",
    "waveshare_enabled": "on",
    "waveshare_host": "192.168.1.51",
    "waveshare_port": "502",
    "waveshare_unit_id": "1",
    "waveshare_poll_ms": "200",
    "waveshare_timeout_s": "3",
    "channel_1_signal": "lead_float",
    "channel_1_debounce": "500",
    "channel_2_signal": "lag_float",
    "channel_2_debounce": "500",
    "channel_3_signal": "high_water",
    "channel_3_debounce": "500",
    "channel_4_signal": "panel_alarm",
    "channel_4_debounce": "500",
    "channel_5_signal": "pump1_run",
    "channel_5_debounce": "200",
    "channel_6_signal": "pump2_run",
    "channel_6_debounce": "200",
    "channel_7_signal": "pump1_overload",
    "channel_7_invert": "on",
    "channel_7_debounce": "200",
    "channel_8_signal": "pump2_overload",
    "channel_8_invert": "on",
    "channel_8_debounce": "200",
    "pump1_name": "North pump",
    "pump1_running_amps": "1.5",
    "pump1_nameplate_amps": "9.6",
    "pump2_name": "South pump",
    "pump2_running_amps": "1.5",
    "pump2_nameplate_amps": "9.6",
    "inrush_ignore_s": "2",
    "stop_hold_s": "3",
    "max_runtime_s": "600",
    "max_starts_per_hour": "20",
    "quiet_hours_before_flag": "72",
}


def test_a_fresh_install_offers_setup(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "/setup" in response.text

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert "Set up PitWatch" in setup.text


def test_settings_redirect_to_setup_before_there_is_an_account(client):
    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_saves_everything_and_signs_you_in(client):
    response = client.post("/setup", data=SETUP_FORM, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "192.168.1.51" in settings.text
    assert "North pump" in settings.text


def test_setup_closes_once_it_has_been_used(client):
    client.post("/setup", data=SETUP_FORM)

    response = client.get("/setup", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_setup_rejects_mismatched_passwords_and_creates_nothing(client):
    bad = SETUP_FORM | {"password_confirm": "something-else-entirely"}

    response = client.post("/setup", data=bad)

    assert response.status_code == 400
    assert "do not match" in response.text
    # Still open, so the mistake can be corrected.
    assert client.get("/setup", follow_redirects=False).status_code == 200


def test_setup_rejects_a_short_password(client):
    response = client.post(
        "/setup", data=SETUP_FORM | {"password": "short", "password_confirm": "short"}
    )

    assert response.status_code == 400
    assert "10 characters" in response.text


def test_setup_rejects_a_signal_wired_to_two_channels(client):
    response = client.post("/setup", data=SETUP_FORM | {"channel_2_signal": "lead_float"})

    assert response.status_code == 400
    assert "only be on one channel" in response.text


def test_the_clamp_choice_is_stored_the_way_it_was_made(client):
    client.post("/setup", data=SETUP_FORM)

    state = client.get("/api/state").json()

    assert state["pumps"]["1"]["channel"] == 1
    assert state["pumps"]["2"]["channel"] == 0
    assert state["pumps"]["1"]["name"] == "North pump"


def test_settings_need_a_sign_in(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    page = client.get("/settings", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=/settings"

    save = client.post("/settings/site", data={"site_name": "Somewhere else"})
    assert save.status_code == 401


def test_signing_back_in_works(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    response = client.post(
        "/login",
        data={"username": "admin", "password": "a-long-enough-password", "next": "/settings"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_a_wrong_password_does_not_sign_you_in(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    response = client.post("/login", data={"username": "admin", "password": "wrong-password"})

    assert response.status_code == 401
    assert client.get("/settings", follow_redirects=False).headers["location"].startswith("/login")


def test_login_will_not_redirect_off_site(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "a-long-enough-password",
            "next": "//example.com/",
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_saving_one_section_leaves_the_others_alone(client):
    client.post("/setup", data=SETUP_FORM)

    client.post(
        "/settings/site", data={"site_name": "Renamed", "site_timezone": "America/New_York"}
    )

    page = client.get("/settings").text
    assert "Renamed" in page
    assert "192.168.1.51" in page
    assert "North pump" in page


def test_an_smtp_password_is_kept_when_the_box_is_left_empty(client):
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
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/smtp",
        data={"smtp_host": "smtp.example.com", "smtp_password": "the-real-password"},
    )

    page = client.get("/settings").text

    assert "the-real-password" not in page
    assert "unchanged" in page


def test_recipients_are_saved_and_removed(client):
    client.post("/setup", data=SETUP_FORM)

    client.post(
        "/settings/recipients",
        data={
            "recipient_1_name": "David",
            "recipient_1_email": "david@example.com",
            "recipient_1_min_severity": "info",
            "recipient_1_enabled": "on",
            "recipient_2_name": "Super",
            "recipient_2_phone": "+15551234567",
            "recipient_2_min_severity": "critical",
        },
    )
    # Asserting on the value attribute rather than on the number appearing
    # anywhere in the page. The empty rows carry example numbers as
    # placeholders, and a bare substring search finds those too, which reads as
    # a saved recipient that is not there.
    page = client.get("/settings").text
    assert 'value="david@example.com"' in page
    assert 'value="+15551234567"' in page

    # Clearing a name removes that row.
    client.post(
        "/settings/recipients",
        data={
            "recipient_1_name": "David",
            "recipient_1_email": "david@example.com",
            "recipient_1_min_severity": "info",
            "recipient_1_enabled": "on",
            "recipient_2_name": "",
            "recipient_2_phone": "+15551234567",
        },
    )
    page = client.get("/settings").text
    assert 'value="david@example.com"' in page
    assert 'value="+15551234567"' not in page


def test_a_recipient_with_no_way_to_be_reached_is_dropped(client):
    client.post("/setup", data=SETUP_FORM)

    client.post("/settings/recipients", data={"recipient_1_name": "Nobody"})

    assert 'value="Nobody"' not in client.get("/settings").text


def test_the_shelly_password_is_kept_when_the_box_is_left_empty(client):
    """Same rule as SMTP, and easier to get wrong because it is a device.

    Saving the Shelly section to change the clamp mapping must not silently
    drop the device password and leave ingest unable to authenticate.
    """
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/shelly",
        data=SETUP_FORM | {"shelly_password": "the-device-password"},
    )

    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_pump1_channel": "0"})

    store = client.app.state.settings
    assert store.shelly.password == "the-device-password"
    assert store.shelly.pump1_channel == 0


def test_the_shelly_password_can_be_cleared_on_purpose(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_password": "the-device-password"})

    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_clear_password": "on"})

    assert client.app.state.settings.shelly.password is None


def test_the_shelly_password_never_reaches_the_browser(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/shelly", data=SETUP_FORM | {"shelly_password": "the-device-password"})

    assert "the-device-password" not in client.get("/settings").text


def test_the_waveshare_test_button_reports_a_module_it_cannot_reach(client):
    client.post("/setup", data=SETUP_FORM)

    response = client.post(
        "/api/test/waveshare",
        data={"waveshare_host": "192.0.2.1", "waveshare_timeout_s": "1"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_health_is_healthy_once_the_database_is_up(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_state_endpoint_reports_both_devices(client):
    client.post("/setup", data=SETUP_FORM)

    state = client.get("/api/state").json()

    assert set(state["devices"]) == {"shelly", "waveshare"}
    assert state["site"]["name"] == "Basement pit"


def test_the_shelly_test_button_needs_a_sign_in_once_there_is_an_account(client):
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    response = client.post("/api/test/shelly", data={"shelly_host": "192.168.1.50"})

    assert response.status_code == 401


def test_the_shelly_test_button_reports_a_device_it_cannot_reach(client):
    """Unreachable is an answer, not an error.

    192.0.2.1 is reserved for documentation and routes nowhere, so this
    exercises the failure path without depending on what is on the network.
    """
    client.post("/setup", data=SETUP_FORM)

    response = client.post("/api/test/shelly", data={"shelly_host": "192.0.2.1"})

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_the_dashboard_replaces_the_setup_prompt_once_configured(client):
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/").text

    assert "Basement pit" in page
    assert "North pump" not in page, "names arrive over the live feed, not in the markup"
    assert 'data-pump="1"' in page
    assert 'data-pump="2"' in page


def test_the_dashboard_is_readable_without_signing_in(client):
    """Deliberate. Reading is open, changing anything is not."""
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")

    assert client.get("/").status_code == 200


def test_the_live_feed_sends_the_same_shape_the_api_does(client):
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
    client.post("/setup", data=SETUP_FORM)

    pump = client.get("/api/state").json()["pumps"]["1"]

    assert pump["current"] is None
    assert pump["running"] is False
    assert pump["drawing_current"] is False
    assert pump["run_contact"] is None


def test_unwired_signals_report_as_unknown_rather_than_off(client):
    form = SETUP_FORM | {"channel_3_signal": "unused"}
    client.post("/setup", data=form)

    floats = client.get("/api/state").json()["floats"]

    assert floats["high_water"]["state"] is None
    assert floats["lead_float"]["state"] is None, "wired, but nothing has been read yet"
