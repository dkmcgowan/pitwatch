"""The application builds and answers, without a database behind it.

These do not start the lifespan, so there is no pool and no settings store.
That is the point: the container has to survive Postgres being slow to start or
briefly gone, and a process that cannot even build its routes in that state
gives you nothing to look at when you need it most.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pitwatch import __version__
from pitwatch.app import create_app
from pitwatch.config import Config
from pitwatch.summary import Offer


def build() -> TestClient:
    # Not entered as a context manager, so lifespan never runs.
    return TestClient(create_app(Config(secret_key="test"), secret_key="test"))


def test_health_reports_starting_before_the_pool_exists():
    response = build().get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "starting"}


def test_static_files_are_served():
    response = build().get("/static/style.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_the_api_documentation_is_not_exposed():
    """Turned off, and behind the sign in guard as well.

    Either alone would do. Asserting "not reachable" rather than a specific
    code means this keeps meaning the same thing if the guard changes which one
    it answers with.
    """
    client = build()

    # Without following the redirect: the guard sends a browser to /login, and
    # this application has no lifespan running, so there is nothing to render a
    # page with. What matters is that none of these ever answer with the docs.
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code != 200, path


def test_version_is_a_release_number():
    parts = __version__.split(".")

    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_the_bind_port_can_be_set_from_the_environment(monkeypatch):
    """Only matters for host networking and for running without Docker.

    In the normal compose setup the container stays on 8080 and PITWATCH_HOST_PORT
    moves the host side instead.
    """
    monkeypatch.setenv("PITWATCH_PORT", "9090")

    assert Config().port == 9090


def test_the_bind_port_defaults_to_the_one_the_image_health_check_uses(monkeypatch):
    # Read neither the environment nor a local .env, so this asserts the
    # built-in default rather than whatever the developer happens to have set.
    monkeypatch.delenv("PITWATCH_PORT", raising=False)

    assert Config(_env_file=None).port == 8080


def test_the_image_health_check_follows_the_bind_port():
    """The health check has to use the same port the application binds.

    Hardcoding 8080 there would make a container run with PITWATCH_PORT set
    report itself unhealthy forever, and compose would keep restarting it.
    """
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "PITWATCH_PORT=8080" in dockerfile
    assert "${PITWATCH_PORT}/healthz" in dockerfile


def test_health_is_plain_ok_and_touches_nothing():
    """The load balancer check.

    Built without the lifespan, so there is no database at all here. It still
    answers 200, which is the point: a proxy polling every couple of seconds
    must not turn into a query per poll.
    """
    response = build().get("/health")

    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")


def test_health_answers_head_as_well_as_get():
    """Some checkers use HEAD. Starlette gives it to us with GET, and it would
    be quietly lost if this ever became an explicitly method-limited route."""
    response = build().head("/health")

    assert response.status_code == 200


def test_health_and_healthz_answer_different_questions():
    """/health is liveness and /healthz is readiness.

    Without a database, /health is still 200 because the process is serving,
    and /healthz is 503 because it cannot do its job. Collapsing the two would
    mean either hammering Postgres from the load balancer or never noticing it
    was gone.
    """
    client = build()

    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 503


def test_the_database_image_pins_the_postgres_major_version():
    """The examples float the database image, and that pin is what makes it safe.

    Plain `latest` would follow Postgres majors. Postgres will not start on a
    data directory written by an older major, so the day that tag moved the
    database would stop coming up and the way back would be a dump and restore
    rather than a rollback. `-pg17` cannot do that.

    Not the `-oss` build either: compression, retention policies and continuous
    aggregate refresh are Community License features it lacks, so migration 003
    fails on it.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

    tags = [
        line.strip().split("timescale/timescaledb:", 1)[1].strip()
        for line in compose.splitlines()
        if line.strip().startswith("image: timescale/timescaledb:")
    ]

    assert tags, "the compose file should declare the database image"
    for tag in tags:
        assert tag.endswith("-pg17"), f"{tag} does not pin the Postgres major"


def test_the_readme_does_not_carry_a_copy_of_the_compose_file():
    """It did, and the copy went stale within a day.

    Somebody pasting a compose block out of a README gets whatever was true
    when it was written. Telling them to fetch the file cannot drift.
    """
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")

    install = readme[readme.index("## Install") : readme.index("## Accounts")]
    assert "services:" not in install, "the README is duplicating the compose file again"
    assert "docker-compose.yml" in install


def test_only_the_passwords_are_required():
    """Everything else has a default, so a first run is two edits.

    A compose file that refuses to start over a setting somebody has no opinion
    about yet is a bad first five minutes. A password is the exception, and it
    is the exception in both directions: there is no default worth having, and
    a broker that started without one would be a broker anyone on the LAN could
    publish a high water alarm to.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

    required = set(re.findall(r"\$\{([A-Z_]+):\?", compose))
    assert required == {"POSTGRES_PASSWORD", "MQTT_PASSWORD"}


def test_env_holds_only_what_has_no_sensible_default():
    """One file to edit, and only what has to be in it.

    Everything else lives beside a comment in the compose file, where somebody
    changing it can see what it does. A .env full of settings nobody has an
    opinion about is a longer first five minutes for no benefit. The broker's
    user name and port are here only because they are typed into the panel
    module as well, and a value that has to match something outside this
    machine is worth having in one obvious place.
    """
    root = Path(__file__).parent.parent
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")

    expected = {"POSTGRES_PASSWORD", "MQTT_PASSWORD", "MQTT_USERNAME", "MQTT_PORT"}
    assert set(re.findall(r"\$\{([A-Z_]+)", compose)) == expected

    offered = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    assert offered == expected


def test_the_broker_ships_with_the_application():
    """The panel module has to publish somewhere, and asking somebody to stand
    up a broker before they can see a float move is a worse first hour than
    running one more container.

    It is on the host's network for a reason that is easy to undo by accident:
    unlike the database, something outside this machine has to connect to it.
    A broker on loopback is a broker the module cannot reach.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

    broker = compose[compose.index("  mqtt:") : compose.index("  app:")]
    assert "eclipse-mosquitto" in broker
    assert "network_mode: host" in broker
    assert "127.0.0.1:" not in broker

    # Anonymous access would let anyone on the LAN publish a high water alarm,
    # or a quiet one over the top of a real one.
    assert "allow_anonymous false" in broker
    assert "mosquitto_passwd" in broker

    # The password file is regenerated from .env on every start, and it lives
    # in a volume, so it is there on the second one. mosquitto_passwd will not
    # create over an existing file, so the first restart failed to start the
    # broker at all until this removed it first.
    assert broker.index("rm -f /mosquitto/data/passwd") < broker.index("mosquitto_passwd -b")


def test_there_is_one_compose_file():
    """Two of them meant two things to keep in step, and they did not stay in
    step. The one that is left runs on the host's network, which is the
    arrangement least likely to be in the way of talking to a meter on a LAN."""
    root = Path(__file__).parent.parent

    assert (root / "docker-compose.yml").exists()
    assert not (root / "docker-compose.host.yml").exists()

    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "network_mode: host" in compose
    assert "@127.0.0.1:5432/pitwatch" in compose


# -- what the panel card is asked to show -----------------------------------


def test_only_inputs_carrying_something_are_shown():
    """Eight rows of "nothing" is not a dashboard, it is a settings page nobody
    asked to see. An input with no role is still read and still recorded; it
    just has no lamp to appear in."""
    from pitwatch.schemas import ContactInput, MqttSettings

    settings = MqttSettings(
        inputs=[
            ContactInput(channel=2, role="lead_float", topic="pit/in/2"),
            ContactInput(channel=5, role="pump1_run", topic="pit/in/5"),
        ]
    )

    assert [c.channel for c in settings.used_channels] == [2, 5]
    assert [c.title for c in settings.used_channels] == ["Lead float", "Pump 1 running"]


# -- the page and the parser have to agree ----------------------------------
#
# Every setting makes the same round trip: a template renders a field, a
# browser posts it, a forms.*_from reads it back. A field the parser does not
# read still renders, still accepts what you type, and still says Saved. It
# just quietly keeps the old value, and nothing anywhere reports a problem.
#
# That has now happened twice, so it is a test rather than a habit. No database
# needed: this is templates and parsers, and both are pure.


def render_settings(**overrides) -> str:
    from jinja2 import Environment, FileSystemLoader

    from pitwatch.app import TIMEZONES
    from pitwatch.domain.diagnostics import Report as Diagnostics
    from pitwatch.schemas import (
        DASHBOARD_ROLES,
        MqttSettings,
        PumpsSettings,
        SiteSettings,
        SmsSettings,
        SmtpSettings,
        SummarySettings,
        WeatherSettings,
    )

    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    # The real list rather than a stub: an empty one renders a select with no
    # options, which submits nothing, and this file's round trip test would
    # then be checking that a timezone survives being dropped.
    env.globals["timezones"] = TIMEZONES
    context = {
        "site": SiteSettings(),
        "weather": WeatherSettings(),
        "mqtt": MqttSettings(),
        "pumps": PumpsSettings(),
        "smtp": SmtpSettings(),
        "sms": SmsSettings(),
        "summary": SummarySettings(),
        "roles": DASHBOARD_ROLES,
        # Nothing to say without a database, which is the state this renderer
        # runs in. The section renders its "nothing configured" line.
        "diagnostics": Diagnostics(),
        "user": None,
        "error": None,
        "saved": False,
    }
    context.update(overrides)
    return env.get_template("settings.html").render(**context)


def submitted(html: str) -> list[tuple[str, str]]:
    """What a browser would post from that page, left exactly as rendered.

    Entities are decoded, because a browser decodes them. A value containing a
    quote is written into the attribute as &#34; and posted back as a quote,
    and reading the attribute literally would report a round trip failure for a
    page that works. The ask payload is JSON, so this is not hypothetical.
    """
    import html as entities

    fields: list[tuple[str, str]] = []
    for tag in re.finditer(r"<input [^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag.group())
        if not name:
            continue
        kind = re.search(r'type="([^"]+)"', tag.group())
        kind = kind.group(1) if kind else "text"
        if kind == "checkbox":
            # An unchecked box posts nothing at all.
            if "checked" in tag.group():
                fields.append((name.group(1), "on"))
            continue
        value = re.search(r'value="([^"]*)"', tag.group())
        fields.append((name.group(1), entities.unescape(value.group(1)) if value else ""))
    for select in re.finditer(r'<select [^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        chosen = re.search(r'<option value="([^"]*)"[^>]*selected', select.group(2))
        if chosen:
            fields.append((select.group(1), entities.unescape(chosen.group(1))))
    return fields


def test_saving_the_settings_page_unchanged_changes_nothing():
    """Render every section with values that are not the defaults, post the page
    back exactly as rendered, and get the same settings out.

    This is the check that catches a field the parser never learned to read.
    Such a field renders, accepts what you type, and reports Saved, while
    keeping the old value and reporting nothing.
    """
    from starlette.datastructures import FormData

    from pitwatch.api import forms
    from pitwatch.schemas import (
        ClampSource,
        ContactInput,
        HealthSource,
        MqttSettings,
        SiteSettings,
    )

    site = SiteSettings(
        name="123 Main St",
        timezone="America/Chicago",
        base_url="https://pitwatch.example.com",
        contact_email="pumps@example.com",
        contact_phone="+12125550142",
        operator="Jane Smith, Sole Proprietor",
        operator_locality="Anytown, NY 12345",
    )
    mqtt = MqttSettings(
        enabled=True,
        host="10.0.0.6",
        port=8883,
        username="panel",
        password="broker-secret",
        encrypted=True,
        client_id="pitwatch-123",
        debounce_ms=750,
        clamps=[
            ClampSource(
                pump=1,
                topic="meter/status/em1:0",
                path="current",
                ask_topic="meter/rpc",
                ask_payload='{"method":"EM1.GetStatus","src":"pitwatch-c1"}',
                reply_topic="pitwatch-c1/rpc",
                reply_path="result.current",
                ask_while_running=True,
                ask_every_s=1.0,
            ),
            ClampSource(pump=2, topic="meter/status/em1:1", path="current"),
        ],
        health=[
            HealthSource(name="Panel module", topic="site/heartbeat", expect_s=90),
            HealthSource(name="Meter", topic="site/meter", expect_s=45),
        ],
        inputs=[
            ContactInput(channel=1, role="lead_float", topic="pit/in/1"),
            ContactInput(channel=7, role="pump1_fault", topic="pit/in/7", invert=True),
        ],
    )
    form = FormData(submitted(render_settings(site=site, mqtt=mqtt)))

    assert forms.site_from(form) == site
    # The stored password is never rendered, so the round trip is given the
    # settings it is checking against, exactly as the route does. Everything
    # else on the page has to survive on what the page itself carries.
    assert forms.mqtt_from(form, mqtt) == mqtt


# -- the panel door ----------------------------------------------------------
#
# The two words in the middle are the only thing on this dashboard that is
# derived rather than read, so they are the only thing that can be confidently
# wrong. Every case below is one a real panel reaches.


def io(*steps):
    """A live state built from contacts opening and closing, a second apart.

    Each step is (channel, state). Order is the order things happened, which is
    what the rotation depends on, so these read like a morning at the pit.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.ingest.contacts import IoEvent
    from pitwatch.ingest.sink import LiveIo

    live = LiveIo()
    base = datetime(2026, 8, 26, 3, 0, tzinfo=UTC)
    for order, (channel, state) in enumerate(steps):
        live.update(
            IoEvent(
                ts=base + timedelta(seconds=order),
                channel=channel,
                label=f"DI{channel}",
                state=state,
                raw=state,
            )
        )
    return live


P1, P2, F1, F2 = 5, 6, 7, 8


def wired():
    from pitwatch.schemas import ContactInput, MqttSettings

    return MqttSettings(
        inputs=[
            ContactInput(channel=P1, role="pump1_run", topic="pit/in/x"),
            ContactInput(channel=P2, role="pump2_run", topic="pit/in/x"),
            ContactInput(channel=F1, role="pump1_fault", topic="pit/in/x"),
            ContactInput(channel=F2, role="pump2_fault", topic="pit/in/x"),
        ]
    )


def words(live):
    from pitwatch.api.live import lead_and_lag

    return lead_and_lag(wired(), live)


def test_the_display_waits_rather_than_guessing_which_pump_is_lead():
    """A fresh install has no idea. The controller alternates and does not say
    so, and picking one would be wrong half the time."""
    assert words(io()) == ("--", "--")


def test_a_running_pump_is_on_and_the_other_is_already_lead():
    """Matching the controller on the wall, which is the whole point of this
    display.

    Corrected against the real panel on 2026-09-05. This used to claim a
    running pump held LEAD for the length of its run and that the rotation
    flipped when it dropped out. Watching it, the rotation flips the moment a
    pump starts: pump 1 reads ON and pump 2 reads LEAD there and then, because
    pump 2 is what answers the next call. The old reading had the two words
    disagreeing with the panel door for the whole of every run, which is
    precisely when somebody is standing in front of both.
    """
    assert words(io((P1, True))) == ("ON", "LEAD")
    assert words(io((P1, True), (P1, False))) == ("LAG", "LEAD")

    # And the same the other way round.
    assert words(io((P2, True))) == ("LEAD", "ON")
    assert words(io((P2, True), (P2, False))) == ("LEAD", "LAG")


def test_the_rotation_alternates_across_calls():
    """Pump 1 runs and hands over, then pump 2 runs and hands back."""
    assert words(io((P1, True), (P1, False))) == ("LAG", "LEAD")
    # Pump 2 picks up the call it was lead for, so it reads ON and pump 1 is
    # lead again from the moment it starts.
    assert words(io((P1, True), (P1, False), (P2, True))) == ("LEAD", "ON")
    assert words(io((P1, True), (P1, False), (P2, True), (P2, False))) == ("LEAD", "LAG")


def test_both_running_is_its_own_state():
    """The high water case: the pit came up past the lag float and the
    controller called both. Neither is leading anything at that point."""
    assert words(io((P1, True), (P2, True))) == ("ON", "ON")

    # And when the second pump drops out first, the one still running goes
    # back to reading ON rather than the display jumping to the rotation.
    assert words(io((P1, True), (P2, True), (P2, False))) == ("ON", "LEAD")


def test_one_pump_having_never_run_still_answers():
    assert words(io((P1, True), (P1, False))) == ("LAG", "LEAD")
    assert words(io((P2, True), (P2, False))) == ("LEAD", "LAG")


def test_an_overload_outranks_everything_else():
    """A tripped pump is not lag waiting its turn, it is out. The other one is
    lead because it is the only one left, whatever the rotation said."""
    # Pump 2 ran last, so pump 1 would be lead. Its overload says otherwise.
    assert words(io((P2, True), (P2, False), (F1, True))) == ("FAIL", "LEAD")
    assert words(io((P1, True), (P1, False), (F2, True))) == ("LEAD", "FAIL")
    # Even mid run, which is exactly when an overload trips.
    assert words(io((P1, True), (F1, True))) == ("FAIL", "LEAD")


def test_both_overloads_tripped_is_its_own_display():
    assert words(io((F1, True), (F2, True))) == ("FAIL", "FAIL")


def test_unassigned_run_inputs_answer_nothing():
    """Rather than reading as "neither has ever run", which looks the same on
    screen and means something entirely different."""
    from pitwatch.api.live import lead_and_lag
    from pitwatch.schemas import MqttSettings

    assert lead_and_lag(MqttSettings(), io((P1, True))) == ("--", "--")


def test_a_lamp_with_no_input_is_not_a_lamp_that_is_off():
    """Three states, and the middle one is the whole point. A lamp reading off
    when it means nobody wired it is a lamp that gets believed."""
    from pitwatch.api.live import panel_state
    from pitwatch.schemas import ContactInput, MqttSettings

    inputs = MqttSettings(
        inputs=[
            ContactInput(channel=3, role="high_water", topic="pit/in/3"),
            ContactInput(channel=4, role="system_alert", topic="pit/in/4"),
        ]
    )
    panel = panel_state(inputs, io((3, True)))

    assert panel["high_water"]["state"] is True
    assert panel["high_water"]["label"] == "High water"
    # Assigned, but nothing has ever read it.
    assert panel["system_alert"]["state"] is None
    assert panel["system_alert"]["channel"] == 4
    # Not assigned at all.
    assert panel["lead_float"]["channel"] is None
    assert panel["lead_float"]["state"] is None


def test_the_lamp_mapping_makes_the_round_trip_with_the_inputs():
    """It has no page of its own now. Choosing what an input carries is what
    lights the lamp, so it rides along with the rest of the input settings and
    makes the same round trip everything else on that page makes."""
    from starlette.datastructures import FormData

    from pitwatch.api import forms
    from pitwatch.schemas import ContactInput, MqttSettings

    mqtt = MqttSettings(
        enabled=True,
        host="10.0.0.6",
        inputs=[
            ContactInput(channel=1, role="lead_float", topic="pit/in/1"),
            ContactInput(channel=2, role="lag_float", topic="pit/in/2"),
            ContactInput(channel=3, role="high_water", topic="pit/in/3"),
            ContactInput(channel=4, role="system_alert", topic="pit/in/4", invert=True),
            ContactInput(channel=5, role="pump1_run", topic="pit/in/5"),
            ContactInput(channel=6, role="pump2_run", topic="pit/in/6"),
            ContactInput(channel=7, role="pump1_fault", topic="pit/in/7", invert=True),
            ContactInput(channel=8, role="pump2_fault", topic="pit/in/8", invert=True),
        ],
    )
    page = render_settings(mqtt=mqtt)

    assert forms.mqtt_from(FormData(submitted(page)), mqtt) == mqtt


# -- what a pump has been drawing --------------------------------------------


def test_drift_needs_both_windows_to_mean_anything():
    """One number is not information. Sixteen amps is fine or alarming entirely
    depending on what it was last month, so with no baseline there is no
    answer rather than a reassuring zero."""
    from pitwatch.domain.history import Typical

    assert Typical(median=16.2, earlier_median=None).drift is None
    assert Typical(median=None, earlier_median=15.8).drift is None
    assert Typical().drift is None
    assert Typical(median=16.2, earlier_median=15.8).drift == pytest.approx(0.4)


def test_a_median_from_three_readings_is_not_reported():
    """Below the floor it describes two runs and a coincidence. Saying nothing
    is better than saying something measured off a handful of samples, because
    the number will be believed either way."""
    from pitwatch.domain import history

    assert history.MIN_SAMPLES >= 30


def test_the_windows_do_not_overlap_and_the_recent_one_is_the_shorter():
    """The query splits one scan at the boundary, so an overlap would count the
    same readings on both sides and flatten every drift toward zero."""
    from pitwatch.domain import history

    assert history.RECENT < history.EARLIER


def test_the_history_query_only_counts_readings_taken_while_running():
    """Averaging in the hours a pump spends switched off produces a number near
    zero that moves with the weather, which is a rain gauge."""
    from pitwatch.domain.history import QUERY

    assert "current >= $2" in QUERY
    assert "percentile_cont(0.5)" in QUERY


# -- how the panel is laid out ----------------------------------------------
#
# Layout is normally not worth a test. This is, because it was described in
# prose, built from that description, and shipped sitting beside the screen
# instead of stacked on it, and nothing said a word. The order things appear in
# down the page is a claim, so it gets checked like one.


def render_dashboard() -> str:
    from jinja2 import Environment, FileSystemLoader

    from pitwatch.schemas import SiteSettings

    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    return env.get_template("dashboard.html").render(site=SiteSettings(name="A pit"), user=None)


def test_the_dashboard_is_five_sections():
    """A pump, the other pump, then alerts, floats and rain.

    It was six boxes once, then one box holding everything at a size that fit a
    phone without scrolling. The one box fit and could not be read: two pump
    columns in half a screen each, three lists of lamps in a third each, and
    the people who read this page are standing in a boiler room and are not
    twenty five. Full width sections and a scroll is the trade, and the scroll
    is the cheap half of it.

    Then the overloads gave up their own section and became two more rows under
    Alerts, which is what they are: an alarm, counted by the month, read by
    somebody looking for the one thing that is lit rather than for a heading.

    Rain came last and sits under the floats, because it is the cause and they
    are the effect. It is also the only card that looks forward.

    Layout is normally not worth a test. This is, because it has been described
    in prose and built from that description more than once, and shipped wrong
    both times without anything saying a word.
    """
    page = render_dashboard()

    assert page.count("<section") == 5
    # The banner and the two device indicators sit outside them, and nothing
    # else does.
    assert page.count('class="board-card') == 5
    # Rain is last, under the water it explains.
    assert page.index("rain-card") > page.index("water-card")
    assert "history-row" not in page and "history-table" not in page
    # And nothing left of the section that went, in the markup or the
    # stylesheet, so it cannot come back by halves.
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    assert ">Overloads" not in page and "overload-card" not in page
    assert "overload-card" not in css


def test_the_box_says_everything_in_three_words_or_less():
    """A column here is a third of a phone wide. A sentence in one is a
    paragraph, so nothing in the box is one: every line is a heading, a short
    label or a number, and the prose lives behind the i where there is room
    for it.

    The line naming the pump an overload has stopped went with the rest. OL1
    and OL2 already say which pump, and the screen says FAIL beside its name.
    """
    import re

    page = render_dashboard()
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert "data-panel-note" not in page and "data-panel-note" not in js

    board = page.split('<div class="board"', 1)[1]
    board = re.sub(r"<dialog.*?</dialog>", "", board, flags=re.S)

    for text in re.findall(r">([^<>]+)<", board):
        words = text.split()
        assert len(words) <= 3, text


def test_nothing_sits_below_the_lamps():
    """Eight contacts on the panel and eight inputs on the module, so every one
    of them carries a lamp or a run signal and the list of leftovers was always
    empty. An input with its meaning taken away is still read and recorded; it
    just has nowhere on the dashboard to be shown, which is what taking the
    meaning away asked for."""
    page = render_dashboard()
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    assert "data-inputs" not in page
    assert "Other inputs" not in page
    # And nothing left over to draw them with, so it cannot come back by
    # halves.
    assert "renderInputs" not in js and "setPill" not in js
    assert ".pill" not in css and ".float" not in css


def test_the_board_does_not_stretch_across_a_desktop():
    """The page is wide enough for a table of users and this is not one. Two
    pump columns and three lists of lamps pulled across a desktop put a label
    at one end of a line and its number at the other. The banner and the device
    dots take the same cap so the three of them line up down one edge."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    # The first .links rule in the file is the one that caps the three of
    # them together; the one further down only lays the dots out.
    capped = css.split(".links {", 1)[1].split("}", 1)[0]

    assert "max-width:" in capped
    assert "margin-left: auto;" in capped and "margin-right: auto;" in capped


def test_the_board_reads_top_to_bottom():
    """Pump 1, pump 2, then the contacts, which come last because they are the
    part somebody reads only when something is lit.

    The controller's screen used to sit between them, a green panel across the
    page reading "P1:LEAD  P2:LAG". Those two words are beside the two pumps
    now, which is where somebody looking at a pump was going to look for them.

    The overloads are the last two rows of Alerts rather than a section of
    their own. They are the worst of the four, and the panel's own word for
    that pump already says FAIL beside its name.
    """
    page = render_dashboard()

    order = [
        'data-pump="1"',
        'data-pump="2"',
        ">Alerts",
        'data-lamp="system_alert"',
        'data-lamp="high_water"',
        'data-lamp="pump1_fault"',
        'data-lamp="pump2_fault"',
        ">Floats",
        'data-lamp="lead_float"',
        'data-lamp="lag_float"',
    ]
    found = [page.index(token) for token in order]

    assert found == sorted(found), "the board is out of order: " + str(
        list(zip(order, found, strict=True))
    )


def test_the_narrow_screen_gives_up_padding_and_not_type():
    """One section per row at every width, and the same type at every width.

    The phone block used to bring the headings, the labels, the counts and the
    bulbs all down a size, on the screen where they were already smallest. That
    is backwards: the phone in a basement is the hard case, and what it can
    afford to give up is the room around the words, not the words.
    """
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    def rule(selector: str) -> str:
        return css.split(selector + " {", 1)[1].split("}", 1)[0]

    # A stack, not a grid of columns: nothing here is beside anything else.
    assert "grid-template-columns" not in rule(".board")

    # The phone block itself, read to its own closing brace rather than to
    # whatever section happens to follow it. Anchoring on the next comment made
    # this fail the day something unrelated was written underneath it.
    phone = css.split("@media (max-width: 700px) {", 1)[1].split("\n}", 1)[0]
    assert "font-size" not in phone
    assert "grid-template-columns" not in phone
    assert "grid-area" not in phone


def test_a_section_is_a_box_and_a_row_is_a_rule():
    """A rule between blocks was enough while they were columns inside one box.
    Across four full width sections it is not: the eye needs to know where a
    section starts more than it needs the pixel of height back.

    Inside a section it is the other way about. A label at one end of a line
    this wide and its number at the other want a track to be read along, and a
    rule is that track."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    box = css.split(".board-card {", 1)[1].split("}", 1)[0]
    assert "border:" in box and "border-radius:" in box

    for selector in (".fact {", ".lamp {"):
        assert "border-top:" in css.split(selector, 1)[1].split("}", 1)[0], selector


def test_a_lamp_carries_what_it_has_been_doing():
    """A lamp on its own cannot say the second part: one that is off looks
    exactly the same whether it went twenty times today or has never gone at
    all. That used to be three cards below the fold and is two small lines
    under the bulb now.

    What it does not carry is a sentence. It used to read "not set" or "no
    data" under the bulb, which is prose where an indicator should be; an
    unassigned lamp draws dimmer instead.
    """
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert "lamp-state" not in page
    assert "lamp-state" not in css
    # Nothing reaches for the element any more either, so it cannot come back
    # by halves: markup without the writer, or a writer with no markup.
    assert "data-lamp-state" not in js
    assert ".lamp.unset .bulb" in css

    # Every lamp is also the row that says what it has been doing, so a lamp
    # and the lines under it read the same payload and cannot disagree.
    import re

    for role in re.findall(r'data-lamp="([a-z_0-9]+)"', page):
        lamp = page.split('data-lamp="' + role + '"', 1)[1].split("</div>", 1)[0]
        assert 'data-history="' + role + '"' in lamp, role
        assert "data-history-last" in lamp, role
        assert "data-history-count" in lamp, role


def test_both_lines_beside_a_lamp_are_the_same_size():
    """When it last went and how often it has been going. Two answers to two
    questions, so they are set alike.

    The count used to be two thirds the size of the line above it, which made
    the pair read as an answer with a footnote under it, and made a row with
    nothing behind it an n/a on top of a smaller n/a. Muted against not muted
    is the whole difference now, which is the same difference a label and its
    number have on a pump.
    """
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    def rule(selector: str) -> str:
        # A selector may share its block with the pump rows, which take the
        # same shape on purpose, so match it before a brace or before a comma.
        for token in (selector + " {", selector + ",\n"):
            if token in css:
                return css.split(token, 1)[1].split("}", 1)[0]
        raise AssertionError(f"no rule for {selector}")

    def size(selector: str) -> str:
        return rule(selector).split("font-size:", 1)[1].split(";", 1)[0].strip()

    assert size(".lamp-count") == size(".lamp-last") == size(".lamp-title")
    # And the difference that is left: one is the answer, the other is quieter.
    assert "var(--text-muted)" in rule(".lamp-count")
    assert "var(--text)" in rule(".lamp-last")


def test_the_overloads_are_rows_of_the_alerts_section():
    """An overload is an alert. It is a worse one, and the difference is that
    it has already taken a pump away rather than asking somebody to go and
    read the panel, but a heading of its own is a heading between somebody
    whose phone just went off and the row that is lit.

    So Alerts is four rows: the panel's alarm, the high water float, and an
    overload for each pump. All four are counted over the same month, which is
    what made them mergeable in the first place.
    """
    page = render_dashboard()

    section = '<section class="board-card lamp-card alert-card" data-window="month"'
    assert section in page
    alerts = page.split(section, 1)[1].split("</section>", 1)[0]

    for role in ("system_alert", "high_water", "pump1_fault", "pump2_fault"):
        assert 'data-lamp="' + role + '"' in alerts, role

    # The overloads keep everything they used to say, on the way in: which
    # pump it is, and the note that says the panel has already cut that motor.
    assert "Pump 1 overload" in alerts and "Pump 2 overload" in alerts
    assert "will not run" in alerts


def test_the_alerts_section_says_whether_anything_is_up():
    """Four dark bulbs already say nothing is raised. They say it by being four
    things somebody has to read and find dark, which is a page that has to be
    checked rather than one that reports, and being told is the whole reason
    anybody opens this on a phone.

    So the four rows are added up beside the heading, in the same badge the
    pumps carry the controller's word in.
    """
    page = render_dashboard()
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    alerts = page.split('aria-label="Alerts"', 1)[1].split("</section>", 1)[0]
    assert "data-alert-summary" in alerts
    # It starts as a dash, like the pumps' own badge: nothing has arrived yet
    # and a guess would be wrong.
    assert 'class="status status-none"' in alerts

    # The four it adds up are the four rows of that section and nothing else.
    roles = js.split("const ALERT_ROLES = ", 1)[1].split("]", 1)[0]
    for role in ("system_alert", "high_water", "pump1_fault", "pump2_fault"):
        assert role in roles, role
    assert "lead_float" not in roles and "lag_float" not in roles

    # Quiet when clear and red when not, and a word either way rather than a
    # color on its own.
    assert '"All clear"' in js
    assert '" active"' in js
    assert ".status-clear {" in css and ".status-alarm {" in css


def test_the_summary_counts_only_what_is_wired():
    """An alert with no input assigned is not being watched, and folding it
    into "all clear" would be the page claiming something nobody wired. It is
    the same rule the lamps follow: null is not false."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    summary = js.split("function renderAlertSummary", 1)[1].split("function renderLinks", 1)[0]

    # Wired is the filter, and an empty set is a dash rather than a clear.
    assert "lamp.channel" in summary
    assert '"--"' in summary
    assert "lamp.state === true" in summary


def test_a_float_going_is_the_equipment_working():
    """Red is an alarm and green is the equipment doing its job, and a float
    closing is the second one: a pit that fills and gets pumped out is a pit
    working.

    They were amber, on the reasoning that water rising is a warning. It is
    Tuesday. Amber for the ordinary thing left the page with no color that
    meant fine, and nothing else was using it."""
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    floats = page.split('aria-label="Floats"', 1)[1].split("</section>", 1)[0]
    assert floats.count("lamp lamp-green") == 2

    alerts = page.split('aria-label="Alerts"', 1)[1].split("</section>", 1)[0]
    # Four alarms and the pit winning, which is the fifth thing worth a red
    # bulb in that section even though no wire reports it.
    assert alerts.count("lamp lamp-red") == 5

    # Two colors on the lamps, and nothing left of the third.
    assert "lamp-amber" not in css and "lamp-amber" not in page


def test_the_history_covers_every_lamp():
    """Every contact with a lamp gets both lines."""
    from pitwatch.schemas import DASHBOARD_ROLES

    page = render_dashboard()

    for role, _ in DASHBOARD_ROLES:
        if role in ("pump1_run", "pump2_run"):
            continue  # those are the pump columns' own runs today
        assert 'data-history="' + role + '"' in page, role

    # Six contacts, plus the one row that is not a contact: both pumps running
    # at once, which is two of them read together.
    assert 'data-history="both_pumps"' in page
    assert page.count("data-history-last") == 7
    assert page.count("data-history-count") == 7


def test_floats_are_counted_by_the_day_and_alarms_by_the_month():
    """A float closes every time the pit fills, so a day is the useful number.
    An alarm counted by the day reads zero forever and teaches somebody to stop
    looking at it."""
    import re

    page = render_dashboard()
    windows = re.findall('data-window="([a-z]+)"', page)

    # In the order the groups appear: alerts, then floats. The overloads are
    # rows of the alerts section now and are counted on its month.
    assert windows == ["month", "today"]


def test_a_count_says_which_window_it_counted():
    """Two windows on one row of lamps, so a bare number would read as one.
    Alerts are counted by the month and floats by the day, and the difference
    is invisible unless the count says so."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert '{ today: "today", month: "this month" }' in js
    # Read off the group the lamp is in, so the markup carries the window and
    # the script does not have to know which lamp is which.
    assert 'row.closest("[data-window]")' in js


def test_a_contact_that_has_never_closed_says_so():
    """Never is an answer. n/a is the absence of one, and reading n/a on a
    float that has been watched all month and has not moved is what teaches
    somebody to distrust the rest of the page."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    history = js.split("function renderHistory", 1)[1].split("function renderInputs", 1)[0]

    assert '"never"' in history
    # And only when there is a count behind it. An input nobody has wired has
    # nothing to say either way, and says so on both lines: one line under a
    # lamp and two under the one beside it is a row that does not line up.
    assert "counted" in history
    assert 'count.textContent = "";' not in history


def test_the_run_contacts_have_no_lamp_on_the_panel():
    """A pump that is running says so in its own column, in amps. A lamp
    repeating that is one more thing to read for nothing.

    The assignments stay. They are what the screen reads to work out which pump
    is lead, and they are still on the settings page.
    """
    from pitwatch.schemas import DASHBOARD_ROLES

    page = render_dashboard()

    assert 'data-lamp="pump1_run"' not in page
    assert 'data-lamp="pump2_run"' not in page
    assert "pump1_run" in dict(DASHBOARD_ROLES)
    assert "pump2_run" in dict(DASHBOARD_ROLES)


def test_the_panels_word_sits_beside_the_pump_it_is_about():
    """LEAD, LAG, ON and FAIL are the controller's own words, and somebody who
    has stood in front of that panel already knows how to read them.

    They used to be a green screen across the middle of the page reading
    "P1:LEAD  P2:LAG", drawn to look like the display on the door. That was a
    picture of a display rather than a display: a band of the page spent on two
    words, with neither word anywhere near the pump it described. Nothing is
    left of it, in the markup or the stylesheet, so it cannot come back by
    halves.
    """
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert "data-lcd" not in page and "lcd" not in css and "lcd" not in js

    # One badge per pump, and it starts saying nothing rather than guessing.
    # Counted inside the pump sections: the alerts summary is the same shape
    # of badge in the same starting state, and it is not one of these.
    assert page.count("data-status") == 2
    pumps = page.split('data-pump="1"', 1)[1].split('aria-label="Alerts"', 1)[0]
    assert pumps.count('class="status status-none"') == 2

    # The three words worth a color of their own. Lag is the ordinary state and
    # keeps the plain badge, and every one of them is a word as well, which is
    # the rule the rest of this page follows.
    #
    # Matched without the brace: fail shares its rule with the alerts summary's
    # own red, and a selector in a list is still a selector.
    for word in ("lead", "on", "fail"):
        assert ".status-" + word in css, word
    assert "MEANS = {" in js and '"status-" + word.toLowerCase()' in js


def test_every_missing_pump_fact_reads_the_same_way():
    """Four fields that can each have nothing behind them, and one way of
    saying so. Left alone they drift: this had "not set", "not in 24 h", a
    bare dash and a sentence, all on one card."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    # The three that draw a pump column, and nothing else.
    # Down to where the panel starts. The lamps keep their own words: "not
    # set" there means no input is assigned, which is a different thing from
    # having no reading.
    card = js.split("function renderPump", 1)[1].split("// The panel door.", 1)[0]

    assert "function setFact(" in js
    for phrase in ("not enough runs yet", "not in 24 h", '"not set"'):
        assert phrase not in card, phrase
    # Every fact goes through it, so there is nowhere for another spelling of
    # "no data yet" to appear.
    assert card.count("setFact(") >= 4

    # Load now included. It answered "--" of its own for a while, which is a
    # different thing said in a different typeface at the top of a column of
    # three n/a, and it is the first row of the first section on a fresh
    # install.
    assert '"--"' not in card
    assert 'setFact(card.querySelector("[data-amps]")' in card

    # One rule, and it reaches the lamps as well as the facts. A field with
    # nothing behind it reads the same on all four sections, in the type of
    # the reading it stands in for rather than a lighter one of its own.
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    selectors = css.split(".fact .none", 1)[1].split("{", 1)[0]
    assert ".lamp-last.none" in selectors and ".lamp-count.none" in selectors


def test_a_pump_section_carries_everything_about_that_pump():
    """What it is doing this second, when it last ran, how often today, how it
    has been running, and the word the controller has for it. All facts about
    one motor, so they live in one section, and the second pump gets the same
    section under it rather than half the width beside it."""
    page = render_dashboard()

    for marker in (
        "data-amps",
        "data-fact-last",
        "data-fact-runs",
        "data-typical",
        "data-drift",
        "data-status",
    ):
        assert page.count(marker) == 2, marker

    section = page.split('data-pump="1"', 1)[1].split('data-pump="2"', 1)[0]
    assert section.count("<dt>") == 2


def test_a_pump_is_two_rows_and_not_four():
    """Load now and typical load were a line each, and they are the same
    measurement at two moments: what the clamp reads this second, which on a
    pit that runs for seconds at a time is zero nearly every time anybody
    looks, and what it reads while the pump is working. Zero had half the row
    and the number worth watching was underneath it.

    Last run, runs today and the ordinary day were three lines asking one
    question. All of it fits a 390 pixel phone on two lines, which was the
    whole argument: the pump was the tallest thing on the page and most of the
    height was labels.
    """
    page = render_dashboard()

    section = page.split('data-pump="1"', 1)[1].split('data-pump="2"', 1)[0]
    assert "<dt>Load</dt>" in section and "<dt>Runs</dt>" in section
    assert section.count('class="fact"') == 2

    # The live reading and the typical one share the load row.
    load = section.split("<dt>Load</dt>", 1)[1].split("</div>", 1)[0]
    assert "data-amps" in load and "data-typical" in load and "data-drift" in load

    # When it last went, how many times today, and an ordinary day, in that
    # order, share the runs row.
    runs = section.split("<dt>Runs</dt>", 1)[1].split("</div>", 1)[0]
    for marker in ("data-fact-last", "data-fact-runs", "data-fact-average"):
        assert marker in runs, marker
    order = [runs.index(marker) for marker in ("data-fact-last", "data-fact-runs")]
    assert order == sorted(order)


def test_every_row_on_the_board_is_the_same_two_lines():
    """A dt on the left and a pair of lines on the right, in every section.

    The pumps were one line with the second half tucked in beside it, so four
    sections down a page read as two typographies. Both lines say n/a when
    there is nothing behind them, for the same reason the lamp rows do: a row
    one line tall next to one that is two stops the sections lining up.
    """
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    # One definition of the shape, shared rather than copied, so the two kinds
    # of row cannot drift apart.
    assert ".lamp-when,\n.fact dd {" in css
    assert ".lamp-last,\n.fact .answer {" in css
    assert ".lamp-count,\n.fact .aside {" in css

    for section in ("<dt>Load</dt>", "<dt>Runs</dt>"):
        row = page.split(section, 1)[1].split("</div>", 1)[0]
        assert 'class="answer"' in row, section
        assert 'class="aside"' in row, section

    # Nothing on a pump row hides itself any more; the second line answers n/a
    # like every other second line.
    assert "data-runs-sep" not in page and "data-runs-sep" not in js
    assert "runs.hidden" not in js
    typical = js.split("function renderTypical", 1)[1].split("// The panel door.", 1)[0]
    assert "setFact(value" in typical and "value.hidden" not in typical


def test_runs_today_means_today():
    """Since midnight where the pit is, not the last twenty four hours.

    It was a rolling day, which is a defensible number and is not the one the
    word promises. At nine in the morning a count that still holds last night's
    storm is the page telling somebody something they will read as wrong, and
    being right about a window nobody asked for is not worth that.
    """
    from pitwatch.domain import history

    assert not hasattr(history, "RUN_WINDOW")
    # Midnight in the site's timezone, drawn in the database so that the day
    # and the rows it filters are decided at the same instant.
    # Both sources draw the day the same way: the contacts, which is what a
    # wired panel reads, and the clamp fallback for one without.
    for query in (history.CONTACT_RUNS_QUERY, history.RUNS_QUERY):
        assert "date_trunc('day', now() AT TIME ZONE" in query
        assert "interval" not in query.split("FILTER", 1)[1].split(")", 1)[0]

    # And the timezone reaches it from the site's own settings rather than the
    # server's clock. It is part of both cache keys, because a cached count is
    # a count of somebody's day.
    live = Path("pitwatch/api/live.py").read_text(encoding="utf-8")
    assert "store.site.timezone" in live
    source = Path("pitwatch/domain/history.py").read_text(encoding="utf-8")
    for key in (
        'key = ("clamp", channel, running_amps, timezone)',
        'key = ("contact", pump, timezone)',
    ):
        assert key in source, key


def test_a_running_pump_is_shown_by_the_whole_section():
    """No pill and no dot. The section outlines and tints itself, its icon goes
    green and so do the amps, which is the same answer at three distances: from
    across the room, from a glance, and from reading it.

    The dot beside the name went with the redesign. It was thirteen pixels
    saying what a whole tinted section now says."""
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert "data-run-pill" not in page
    assert "data-pump-lamp" not in page and "data-pump-lamp" not in js
    assert ".pump-lamp" not in css

    assert ".pump-card.running {" in css
    assert ".pump-card.running .amps" in css
    # The icon follows the section's own color token, which is what paints it
    # the rest of the time too, so the running color cannot drift away from
    # the resting one or leave the tint behind it the wrong hue.
    assert "--icon: var(--ok);" in css.split(".pump-card.running {", 1)[1].split("}", 1)[0]
    assert "var(--icon" in css.split(".card-icon {", 1)[1].split("}", 1)[0]


def test_every_long_note_is_a_dialog_opened_from_beside_its_heading():
    """A native dialog shown as a modal renders in the top layer, so it cannot
    push the numbers around or end up behind something whatever the stacking
    looks like, and Escape closes it without being told to."""
    page = render_dashboard()

    # Five buttons and five notes: the two groups of lamps, the rain, and the
    # two pump columns, the column being written once in a loop.
    assert page.count("data-info=") == 5
    assert page.count("<dialog") == 5
    assert page.count("</dialog>") == 5

    # Each button names a note that exists.
    import re

    for key in re.findall(r'data-info="([^"]+)"', page):
        assert 'id="note-' + key + '"' in page, key

    # And the words are still there, just not on the page.
    assert "less the starting surge" in page
    assert "How often the pit has filled" in page


def test_the_dashboard_calls_nothing_it_does_not_define():
    """Every renderer the page calls has to exist. This is not hypothetical:
    deleting the block that drew the list of leftover inputs took the two
    device indicators out with it, because they sat between it and the next
    function. The call to them stayed.

    Nothing said a word. The websocket swallows an error from a frame on
    purpose, so a bad one cannot tear down a working socket, which meant the
    exception was caught and the whole render was abandoned every time: the
    dots never moved off unknown and the page kept the banner saying it was not
    connected. Both looked like the feed was down.
    """
    import re

    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    code = re.sub(r"//.*", "", js)

    # Bare calls only. Anything reached through a dot belongs to the browser or
    # to a value, and this file is not where it would be defined.
    called = set(re.findall(r"(?<![.\w$])([a-z][\w$]*)\s*\(", code))
    defined = set(re.findall(r"function\s+([\w$]+)\s*\(", code))
    keywords = {"catch", "for", "function", "if", "return", "switch", "while"}
    globals_ = {"fetch"}

    assert called - defined - keywords - globals_ == set()


def test_only_the_parse_is_forgiven():
    """A malformed frame is not a reason to tear down a working socket. A
    mistake in the renderer is not a malformed frame, and one catch around both
    is how a missing function survived two releases: every frame threw, every
    throw was swallowed, and the page sat there saying it was not connected."""
    import re

    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    handler = js.split('socket.addEventListener("message"', 1)[1].split("});", 1)[0]
    # Without the comment, which says catch several times over.
    handler = re.sub(r"//.*", "", handler)

    assert "JSON.parse" in handler.split("catch", 1)[0], "the parse is inside the try"
    assert "render(state);" in handler.split("catch", 1)[1], "the render is not"


def test_a_note_can_always_be_closed():
    """A note that needs a target found before it will go away is a note that
    gets left open. Clicking anywhere closes it, including inside: there is
    nothing in there to interact with."""
    # Its own file, loaded by every page. It was the dashboard's until the
    # history page needed the same four lines.
    js = Path("pitwatch/static/notes.js").read_text(encoding="utf-8")
    notes = js.split("function wireNotes", 1)[1].split("wireNotes();", 1)[0]

    assert "showModal()" in notes, "modal, so only one at a time and Escape works"
    assert "note.close()" in notes
    # The listener is on the dialog itself, so a click anywhere inside bubbles
    # to it. A backdrop-only check would leave the note open on a phone, where
    # there is barely any backdrop to hit.
    assert 'note.addEventListener("click"' in notes
    # And a browser without dialog support hides the button rather than
    # offering something that cannot be closed.
    assert "button.hidden = true;" in notes


def test_the_note_does_not_sit_in_the_flow_of_the_page():
    """It used to, first at the top of a card and then at the foot. Both pushed
    the numbers around when it opened."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    page = render_dashboard()

    assert "card-note" not in css and "card-note" not in page
    assert "dialog.note::backdrop" in css
    # The handle sits beside the heading it belongs to, on each of the five.
    assert page.count('class="info-mark"') == 5


def test_the_live_reading_is_labelled_and_no_bigger_than_anything_else():
    """A number on its own does not say what it is a number of, and this one is
    zero most of the time. It was set three times the size of the label beside
    it on a phone, by a rule in the block that sizes the header icons, left
    over from when it was meant to be the headline.

    Load, not current. Current is exactly what it is and exactly why it is no
    use as a label: the typical load sits on the same line now, and the two are
    the same measurement at two different moments.

    Not "load now" either, for the same reason. The row carries both the
    reading from this second and the one from this week, so a label that says
    now is wrong about half of what is under it.
    """
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    # The label itself, not the word where the note explains what it means.
    assert page.count("<dt>Load</dt>") == 2

    # One size for the whole list, set on the row. Nothing inside it may set
    # another, wherever in the file it is written: that is how this got to
    # 2.4rem and stayed there.
    assert "font-size:" in css.split(".fact {", 1)[1].split("}", 1)[0]
    for selector in (".amps", ".unit", ".fact dt", ".fact dd"):
        for rule in css.split(selector + " {")[1:]:
            assert "font-size" not in rule.split("}", 1)[0], selector


def test_todays_run_count_carries_an_ordinary_day_beside_it():
    """Eighty-nine is a lot or a Tuesday depending on what the month looks
    like, and only one of those is worth getting out of bed for."""
    page = render_dashboard()
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert page.count("data-fact-average") == 2
    assert "daily_average" in js

    # The count and the note beside it are separate elements. They were not:
    # the span sat inside the element the count is written into, and writing
    # textContent removes every child, so the ordinary day was destroyed on the
    # first render and never appeared again.
    for value, beside in (("data-fact-runs", "data-fact-average"), ("data-typical", "data-drift")):
        holder = page.split(value, 1)[1].split("</span>", 1)[0]
        assert beside not in holder, value


def test_the_last_run_clock_does_not_wait_for_the_query():
    """The run count is cached for a minute, which is right for a count and
    useless for a clock: a pump stops and the dashboard goes on saying it last
    ran sixteen minutes ago until the cache turns over.

    The live state knows exactly when the current rose, so it wins.
    """
    from datetime import UTC, datetime, timedelta

    from pitwatch.api.live import _with_live_rise
    from pitwatch.domain.history import Recent

    stale = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    fresh = stale + timedelta(minutes=16)

    assert _with_live_rise(Recent(runs=4, last_start=stale), fresh)["last_start"] == (
        fresh.isoformat()
    )
    # Older news does not overwrite newer.
    assert _with_live_rise(Recent(runs=4, last_start=fresh), stale)["last_start"] == (
        fresh.isoformat()
    )
    # And nothing in memory yet leaves the query's answer alone.
    assert _with_live_rise(Recent(runs=4, last_start=stale), None)["last_start"] == (
        stale.isoformat()
    )
    # The count is never guessed at, because two runs inside one cache window
    # would make a local increment quietly wrong.
    assert _with_live_rise(Recent(runs=4, last_start=stale), fresh)["runs"] == 4


def test_the_live_state_records_a_rise_and_not_a_level():
    """It does not have the running threshold and does not need it: an idle
    clamp on this meter reads 0.000 exactly, so anything at all is a start."""
    from datetime import UTC, datetime, timedelta

    from pitwatch.ingest.readings import EmSample
    from pitwatch.ingest.sink import LiveState

    live = LiveState()
    base = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)

    def reading(seconds: int, amps: float) -> None:
        live.update(EmSample(ts=base + timedelta(seconds=seconds), channel=0, current=amps))

    reading(0, 0.0)
    assert live.rose_at(0) is None

    reading(10, 16.4)
    assert live.rose_at(0) == base + timedelta(seconds=10)

    # Still running is not a second start.
    reading(20, 15.2)
    assert live.rose_at(0) == base + timedelta(seconds=10)

    reading(30, 0.0)
    reading(40, 16.1)
    assert live.rose_at(0) == base + timedelta(seconds=40)


def test_the_run_count_says_which_source_it_came_from():
    """A count off the panel's run contact is a tally. A count off the clamp is
    a floor, and the page has to say which one it is showing.

    The clamp reading was described as a tally once, on the strength of an
    inference that does not hold: two readings above the running threshold with
    no zero between them do not prove the pump never stopped, because a zero
    that was never reported is not a zero that never happened. The contacts
    have none of that problem, and are the source wherever they are wired.
    """
    # Compared against what the page says, not against how the template wraps.
    prose = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", render_dashboard()))

    assert "measured from the panel's own run contact" in prose
    assert "a tally rather than an estimate" in prose
    # And the fallback still admits what it is, including why no duration is
    # offered when the clamp is all there is.
    assert "count is a floor rather than a tally" in prose
    assert "two close runs can arrive as one" in prose


def test_the_overload_note_names_all_four_selector_positions():
    """H, A, HO and AO. Somebody standing at the panel is looking at a selector
    with four positions on it, not two."""
    page = render_dashboard()

    for position in (">H<", ">A<", ">O<", ">HO<", ">AO<"):
        assert position in page, position
    assert "red button" in page


# -- the alerts page and the parser have to agree ---------------------------


def render_alerts(rules) -> str:
    from jinja2 import Environment, FileSystemLoader

    from pitwatch.domain import alerts as specs

    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    return env.get_template("alerts.html").render(
        specs=specs.SPECS, rules=rules, site=None, user=None, saved=False, error=None
    )


def test_saving_the_alerts_page_unchanged_changes_nothing():
    """Twelve rules, each with a level, an audience, a message and sometimes a
    threshold. That is a lot of fields for one of them to be rendered and never
    read back, which shows as a setting that accepts what you type, says Saved,
    and keeps the old value.
    """
    from starlette.datastructures import FormData

    from pitwatch.api import forms
    from pitwatch.schemas import ALERT_ORDER, AlertsSettings

    before = AlertsSettings()
    before.high_water.severity = "warning"
    before.high_water.message = "Water is up at {site}, {pumps_state}."
    before.panel_alert.hold_s = 9
    before.over_current.pump1_amps = 18.5
    before.over_current.pump2_amps = 17.0
    before.over_current.readings = 3
    before.short_cycling.restart_within_ms = 30_000
    before.short_cycling.times_in_a_row = 6
    before.nothing_has_run.quiet_minutes = 90
    before.load_drift.climb_amps = 0.6
    before.run_too_long.longer_than_ms = 12_000
    before.device_offline.admins_only = True
    before.float_activity.enabled = True

    posted = submitted(render_alerts(before.by_key))
    # A checkbox that is off posts nothing, and a textarea is not an input, so
    # the message boxes have to be collected separately.
    page = render_alerts(before.by_key)
    for area in re.finditer(r'<textarea\b[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', page, re.S):
        posted.append((area.group(1), area.group(2).strip()))

    after = forms.alerts_from(FormData(posted), AlertsSettings())

    for key in ALERT_ORDER:
        assert getattr(after, key) == getattr(before, key), key


def test_a_message_with_an_unknown_placeholder_still_sends():
    """A typo in a message should produce a slightly odd alert, not a silent
    one. str.format would raise on the first unknown name and the alert would
    never arrive."""
    from pitwatch.domain.alerts import fill

    assert fill("{pump} at {site}", {"pump": "Pump 1", "site": "A pit"}) == "Pump 1 at A pit"
    assert fill("{nonsense} at {site}", {"site": "A pit"}) == "{nonsense} at A pit"


def test_every_placeholder_a_rule_offers_is_one_it_can_fill():
    """The page lists what each rule fills in. A name on that list that the
    rule never provides is a promise the message keeps in braces."""
    from pitwatch.domain import alerts as specs

    for spec in specs.SPECS:
        for name in spec.placeholders:
            assert name.startswith("{") and name.endswith("}"), name
        # Everything says where it came from, because an alert that does not
        # name the building is one somebody has to go and work out.
        assert "{site}" in spec.placeholders, spec.key


def test_the_header_has_one_of_each_icon():
    """A script that inserts a link and is run twice inserts it twice, which is
    how this page briefly had two bells."""
    import collections
    import re
    from pathlib import Path as _Path

    icons = _Path("pitwatch/templates/_icons.html").read_text(encoding="utf-8")
    names = re.findall(r'<g id="([a-z-]+)"', icons)
    repeated = [name for name, count in collections.Counter(names).items() if count > 1]
    assert not repeated, repeated

    base = _Path("pitwatch/templates/base.html").read_text(encoding="utf-8")
    links = re.findall(r'<a href="(/[a-z]*)" class="icon-link', base)
    assert links == [
        "/",
        "/history",
        "/summary",
        "/users",
        "/alerts",
        "/settings",
        "/profile",
    ], links
    assert len(links) == len(set(links))


def test_the_settings_page_asks_for_a_broker_and_not_for_a_poll_interval():
    """The device changed and the shape of the question changed with it.

    A poll interval left on this page would be a setting that does nothing, and
    a setting that does nothing is worse than a missing one: somebody tunes it
    and believes they have changed something.
    """
    page = render_settings()

    for name in ("mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password"):
        assert f'name="{name}"' in page, name
    for name in ("input_1_topic", "input_1_role", "mqtt_client_id"):
        assert f'name="{name}"' in page, name

    for gone in ("inputs_poll_ms", "inputs_unit_id", "inputs_timeout_s"):
        assert gone not in page, gone

    # The status topic went with the last will. It was the device's own word
    # for whether it was there, and measured on the real panel it stayed true
    # through an outage and announced the device was gone a tenth of a second
    # before announcing it was back. Silence is the test now, and every source
    # carries its own interval.
    assert "status_topic" not in page
    for pump in (1, 2):
        assert f'name="clamp{pump}_topic"' in page, pump
        assert f'name="clamp{pump}_path"' in page, pump
    for index in (0, 1):
        assert f'name="health_{index}_topic"' in page, index
        assert f'name="health_{index}_expect_s"' in page, index
    # And a topic per contact rather than one body carrying eight.
    for channel in range(1, 9):
        assert f'name="input_{channel}_topic"' in page, channel

    # The one piece of processing that survived the change of protocol, because
    # contacts bounce whatever is carrying the news of it.
    assert 'name="mqtt_debounce_ms"' in page


def test_the_stored_broker_password_is_never_rendered():
    from pitwatch.schemas import MqttSettings

    page = render_settings(
        inputs=MqttSettings(enabled=True, host="10.0.0.6", password="broker-secret")
    )

    assert "broker-secret" not in page


def test_the_lamps_are_chosen_on_the_input_that_carries_them():
    """There is no lamp section any more, and no lamp page before that. An
    input row says what the panel put on it, and that is what lights the lamp.
    Two lists to keep in step was the thing worth deleting."""
    page = render_settings()

    assert "input_3_role" in page, "the choice is on the input row"
    for role, _ in [("high_water", 0), ("pump1_run", 0), ("system_alert", 0)]:
        assert f'value="{role}"' in page, role

    # Nothing left of the second list, nor of a page that only pointed at one.
    assert 'action="/settings/dashboard"' not in page
    assert "role_high_water" not in page
    assert "Dashboard lamps" not in page
    assert 'href="/settings/alerts"' not in page


def render_page(name: str, **context) -> str:
    from jinja2 import Environment, FileSystemLoader

    from pitwatch.app import TIMEZONES
    from pitwatch.schemas import MqttSettings, SiteSettings, WeatherSettings

    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    env.globals["timezones"] = TIMEZONES
    context.setdefault("site", SiteSettings(name="A pit"))
    context.setdefault("weather", WeatherSettings())
    context.setdefault("mqtt", MqttSettings())
    context.setdefault("user", None)
    return env.get_template(name).render(**context)


def test_the_name_stays_in_the_header_on_a_phone():
    """It was hidden there, to buy room for seven icons. The icons could give
    up a size instead, and the name is the one place a stranger reading this
    finds out what it is.

    It gives way before the icons do, though: on a screen too narrow for both,
    a header that wraps onto two rows costs every page a row of height, and a
    name that ends in an ellipsis costs nothing anybody needs.
    """
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    base = Path("pitwatch/templates/base.html").read_text(encoding="utf-8")
    public = Path("pitwatch/templates/public.html").read_text(encoding="utf-8")

    # The last of the two phone blocks, which is the one the header is in, up
    # to the end of the file's last rule.
    phone = css.rsplit("@media (max-width: 30rem) {", 1)[1].split(".login-single", 1)[0]
    assert "font-size: 0;" not in phone, "the name is sized down, not switched off"
    assert ".brand { font-size:" in phone

    # Both layouts carry the same header, and it is one file: the mark used to
    # be a blue dot written out in each of them, which is two places for a logo
    # to be changed in one of.
    brand = Path("pitwatch/templates/_brand.html").read_text(encoding="utf-8")
    assert '{% include "_brand.html" %}' in base
    assert '{% include "_brand.html" %}' in public
    assert 'class="brand-name"' in brand and 'class="brand-logo"' in brand
    assert "brand-mark" not in css
    truncated = css.split(".brand-name {", 1)[1].split("}", 1)[0]
    assert "text-overflow: ellipsis;" in truncated
    assert "flex: none;" in css.split(".topbar nav {", 1)[1].split("}", 1)[0]


def test_the_two_footers_are_the_same_shape():
    """A visitor who reads the public pages and then signs in should not feel
    like the footer moved. Both are two groups: who this is on the left, where
    to read the rest on the right, ending in the version.

    Neither carries a link home. The mark in the header is the link home, and
    the public footer had one of those plus a group of its own for the version,
    which left three groups spread across a desktop and none of them lined up
    with anything.
    """
    base = Path("pitwatch/templates/base.html").read_text(encoding="utf-8")
    public = Path("pitwatch/templates/public.html").read_text(encoding="utf-8")

    public_footer = public.split("<footer", 1)[1]
    base_footer = base.split("<footer", 1)[1]

    assert '<a href="/">Home</a>' not in public_footer
    assert "footer-links" not in public_footer and "footer-version" not in public_footer

    for link in ("/terms", "/privacy"):
        assert link in base_footer, link
        assert link in public_footer, link
    # Contact is public only. There is nobody to contact from inside the
    # application; you are already the operator.
    assert "/contact" in public_footer
    assert "/contact" not in base_footer

    # Two children each, which is what puts the second one against the right
    # hand edge.
    for footer in (base_footer, public_footer):
        assert footer.count("PitWatch {{ version }}") == 1


def test_the_public_footer_ends_with_the_version():
    """Rendered rather than read, because the shape that matters is the one a
    browser gets."""
    from pitwatch.schemas import SiteSettings

    page = render_page(
        "home.html",
        site=SiteSettings(operator="A person", contact_email="hello@example.com"),
    )
    footer = page.split("<footer", 1)[1]

    assert "A person" in footer
    assert footer.index("Terms &amp; Conditions") < footer.index("PitWatch test")
    assert ">Home<" not in footer


def test_every_chart_can_be_read_at_a_moment():
    """A shape says something happened on Tuesday. Somebody opening this page
    wants to know what it was, so a line follows the cursor and the numbers
    under it are written above the chart rather than into a tooltip a thumb
    would be covering."""
    page = render_page("history.html")
    js = Path("pitwatch/static/history.js").read_text(encoding="utf-8")
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    for chart in ("calls", "runs", "load", "hours"):
        assert 'data-readout="' + chart + '"' in page, chart

    # Pointer events, so a finger and a mouse are the same code.
    assert 'addEventListener("pointermove"' in js
    assert 'addEventListener("pointerleave"' in js
    # And the page still scrolls under a finger going up and down.
    assert "touch-action: pan-y" in css
    # The line keeps its height whether or not it has anything in it, so
    # touching a chart does not push the page around.
    assert "min-height:" in css.split(".chart-readout {", 1)[1].split("}", 1)[0]


def test_nothing_on_this_page_writes_an_inline_style():
    """The content security policy allows styles from this origin and nothing
    else, and a style attribute is not from an origin. A script coloring an
    element in that way is blocked silently: the key dots came out colorless on
    the live site, which is a chart with a key that identifies nothing."""
    js = Path("pitwatch/static/history.js").read_text(encoding="utf-8")

    assert ".style." not in js
    # SVG carries its colors as presentation attributes, which are not styles.
    assert "fill: SERIES" in js


def test_the_key_dot_scales_to_the_size_the_stylesheet_asks_for():
    """An SVG with no viewBox does not scale, it clips. The dot is drawn in a
    ten pixel box and the stylesheet asks for two thirds of a rem."""
    js = Path("pitwatch/static/history.js").read_text(encoding="utf-8")
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    dot = js.split("function key(", 1)[1].split("function drawAll", 1)[0]
    assert 'viewBox: "0 0 10 10"' in dot
    assert "width: 0.65rem" in css.split(".key-dot {", 1)[1].split("}", 1)[0]


def test_the_heard_column_says_what_arrived_and_not_what_to_do_about_it():
    """It carried a note under the sentence, which ran on from it with no space
    because they were two inline spans: "nothing has arrivedCheck the topic."
    Spacing it fixed the run-on and left a cell wrapping to two lines for
    advice that is one glance away in the column beside it."""
    from pitwatch.domain.diagnostics import Report, Row

    quiet = Row(
        name="Input 7",
        carries="Pump 1 overload",
        topic="pit/in/7",
        heard=False,
        detail="nothing has arrived",
        note="Check the topic.",
    )
    heard = Row(
        name="Pump 2",
        carries="Current",
        topic="meter/em1:1",
        heard=True,
        detail="5 readings in the last day, peak 0.00 A",
        note="Every reading is zero, which is what an unfitted clamp looks like.",
    )
    page = render_settings(diagnostics=Report(inputs=[quiet], clamps=[heard]))

    assert "nothing has arrived" in page
    # No advice in the cell, for either row. What to do about a silent source
    # is one line and it is the topic in the column beside it; the clamp
    # reading nothing but zero is named in the list at the top, where there is
    # room for the whole sentence.
    assert "Check the topic" not in page
    assert "unfitted clamp" not in page
    # And a topic and a timestamp are each one thing, not two lines.
    assert page.count('class="nowrap"') == 4


def test_an_input_that_has_not_gone_reads_the_same_whatever_the_reason():
    """Never and zero, whether or not a message has ever arrived.

    This drew the distinction for a while: an input read all month that had
    stayed open said "never", and one nothing had ever arrived for said n/a,
    because a quiet contact and a wrong topic are not the same thing.

    True, and the wrong page for it. Somebody reading this one wants to know
    whether the pit is alright, and to them an overload that has not tripped
    and an overload PitWatch has not heard from mean the same thing: nothing
    has happened. A second vocabulary for a state that exists only in the hours
    after somebody wires a panel is a puzzle for every reader after that.

    The distinction lives in Diagnostics on the settings page now, which is
    where somebody can do something about it.
    """
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    history = js.split("function renderHistory", 1)[1].split("function renderLinks", 1)[0]

    assert 'setFact(last, "never")' in history, "one answer, not two"
    assert '(times || 0) + " "' in history, "a count of zero rather than nothing"
    # The flag that chose between the two vocabularies is gone rather than
    # merely unused, which is the difference between a decision and a leftover.
    assert "counted" not in history.replace("counted by the", "")


def test_a_lamp_with_nothing_behind_it_still_has_two_lines():
    """One line under a lamp and two under the one beside it is a row that does
    not line up, which is what happens the day some inputs are wired and some
    are not."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    history = js.split("function renderHistory", 1)[1].split("function renderLinks", 1)[0]

    # Both lines go through setFact, which is the one place that writes n/a.
    assert history.count("setFact(") == 3
    assert 'count.textContent = ""' not in history


def test_the_history_page_draws_every_chart_over_one_window():
    """Four charts and one row of buttons that moves all of them. Four windows
    with their own selectors is four charts that can be looking at four
    different weeks.

    The order is the order the questions get asked: how often, how long for,
    how hard, and at what time of day.
    """
    page = render_page("history.html")

    charts = ("calls", "runs", "load", "hours")
    for chart in charts:
        assert 'data-chart="' + chart + '"' in page, chart
        assert 'data-empty="' + chart + '"' in page, chart

    places = [page.index('data-chart="' + chart + '"') for chart in charts]
    assert places == sorted(places), "the charts are in the order the page describes"

    import re

    windows = re.findall(r'data-window="([0-9a-z]+)"', page)
    assert windows == ["today", "7d", "30d"]
    # One of them is on when the page opens, and it is the one the API defaults
    # to. Two that disagree means the page opens showing a week and saying a
    # day.
    from pitwatch.domain.series import DEFAULT_WINDOW

    assert 'data-window="' + DEFAULT_WINDOW + '" aria-pressed="true"' in page


def test_the_rain_is_drawn_from_its_own_series_and_not_from_the_calls():
    """Rain is drawn in its own pass rather than as a branch inside the loop
    over the calls.

    Drawn from the calls, a day that rained and did not fill the pit has no bar
    to hang from and the rain silently is not drawn. That is one of the two
    readings the chart exists to give: a deep rain bar with a tall call bar
    under it says the pit is doing its job, and a wet day the pit shrugged off
    is the other half of the same question.
    """
    js = Path("pitwatch/static/history.js").read_text(encoding="utf-8")
    calls = js.split("function drawCalls", 1)[1].split("function runsOf", 1)[0]

    assert "(data.rain || []).forEach" in calls
    # And the two loops place a bucket the same way, or a rain bar and the call
    # bar under it would not line up, which would make the chart say something
    # that is not true.
    assert calls.count("bucketAt(") == 3, "one definition and one call per loop"


def test_the_rain_hangs_from_the_ceiling_rather_than_sharing_an_axis():
    """Rain and what it produced share a time axis and nothing else. Two series
    growing from one baseline invites reading one against the other, which is
    exactly the comparison that is not available: they are different units.

    A stormwater chart hangs the rain from the top for this reason, and the
    thing worth seeing survives it, because a wet day still sits directly above
    a busy one.
    """
    js = Path("pitwatch/static/history.js").read_text(encoding="utf-8")

    # The plot is pushed down to leave the gap the rain hangs in.
    assert "headroom" in js
    frame = js.split("function frame(", 1)[1].split("function timeAxis", 1)[0]
    assert "PAD.top + (options.headroom || 0)" in frame


def test_every_window_the_page_offers_is_one_the_query_knows():
    """A button for a window the server does not have quietly falls back to the
    default, so the page would answer a different question than the one that
    was pressed."""
    import re

    from pitwatch.domain.series import WINDOWS

    page = render_page("history.html")
    for key in re.findall(r'data-window="([0-9a-z]+)"', page):
        assert key in WINDOWS, key


def test_the_charts_are_drawn_here_and_not_fetched_from_anywhere():
    """The content security policy on this application allows scripts from this
    origin and nothing else, so a charting library from a CDN would not load at
    all. It is a few polylines; they are drawn by hand."""
    js = Path("pitwatch/static/history.js").read_text(encoding="utf-8")

    assert "createElementNS" in js
    assert "cdn" not in js.lower()
    # Text from the settings page goes in as text. A contact somebody named is
    # a contact somebody could name with a script tag.
    assert "innerHTML" not in js


def test_the_summary_page_says_what_it_needs_before_it_offers_the_button():
    """A button that spends money and fails is worse than no button."""
    nothing = render_page(
        "summary.html",
        last=None,
        age="",
        ready=False,
        context="",
        offer=Offer(False, "Add an OpenAI key and a model on the settings page first."),
        earlier=[],
        error=None,
    )

    assert "New summary" not in nothing
    assert "settings" in nothing

    ready = render_page(
        "summary.html",
        last=None,
        age="",
        ready=True,
        context="",
        offer=Offer(True),
        earlier=[],
        error=None,
    )
    assert "New summary" in ready
    # The empty box is how the page says nothing has been written about the
    # building. A sentence saying so, printed above a box labelled with what to
    # put in it, is the same thing said twice.
    assert 'name="summary_description"' in ready
    assert "What it knows about this building" in ready


def test_a_written_summary_is_rendered_as_text_with_its_age():
    """It came from a model. Whatever it says goes on the page as text, and the
    page says when it was written: a paragraph about a pump means something
    different if it is a week old."""
    page = render_page(
        "summary.html",
        last={
            "body": "Both pumps look <normal>.",
            "model": "gpt-4o-mini",
            "window_key": "7d",
            "written_by": "david",
        },
        age="3 min ago",
        ready=True,
        context="Two pumps in a pit.",
        offer=Offer(False, "This one has read the same week."),
        earlier=[],
        error=None,
    )

    assert "3 min ago" in page
    assert "david" in page and "gpt-4o-mini" in page
    # Escaped, not rendered.
    assert "&lt;normal&gt;" in page
    assert "<normal>" not in page


def test_the_openai_key_is_never_rendered_back():
    """Same rule as the broker password and the AWS secret. An empty box means
    leave it alone, and there is a checkbox for clearing it."""
    from pitwatch.schemas import SummarySettings

    page = render_settings(summary=SummarySettings(api_key="sk-secret-value", description="A pit"))

    assert "sk-secret-value" not in page
    assert "summary_clear_key" in page
    assert "unchanged" in page
    # The description is not a secret and does come back, or editing it would
    # mean retyping it.
    assert "A pit" in page


def test_the_summary_sends_the_description_and_the_numbers_and_nothing_else():
    """No address, no site name, no account names. Nobody needs a street
    address to say whether a pump is drawing more than it did last week."""
    from pitwatch.schemas import SummarySettings
    from pitwatch.summary import messages

    numbers = {"pumps": [{"pump": 1, "name": "Pump 1", "runs_this_week": 12}]}
    payload = messages(SummarySettings(description="Two pumps in a pit."), numbers)

    assert [part["role"] for part in payload] == ["system", "user"]
    body = payload[1]["content"]
    assert "Two pumps in a pit." in body
    assert "runs_this_week" in body
    for leaked in ("123", "Main St", "admin"):
        assert leaked not in body, leaked


def test_a_summary_needs_a_key_before_it_asks_anything():
    """And says so in a sentence somebody can act on rather than failing at the
    far end of a request."""
    import asyncio

    from pitwatch.schemas import SummarySettings
    from pitwatch.summary import SummaryError, ask

    with pytest.raises(SummaryError) as raised:
        asyncio.run(ask(SummarySettings(), [{"role": "user", "content": "hello"}]))

    assert "settings page" in str(raised.value)


def test_the_notes_are_wired_from_one_file_for_every_page():
    """Three pages carry an i beside a heading now. One copy of the four lines
    that opens it, loaded by the layout."""
    base = Path("pitwatch/templates/base.html").read_text(encoding="utf-8")
    dashboard = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert "/static/notes.js" in base
    assert "function wireNotes" not in dashboard
    assert Path("pitwatch/static/notes.js").exists()


# -- the alert history page --------------------------------------------------


def _alert(title, detail, severity="critical", lasted="open", bad=True, when="6 Sep 18:34"):
    return {
        "severity": severity,
        "title": title,
        "detail": detail,
        "raised_local": when,
        "lasted": lasted,
        "bad": bad,
    }


def test_the_history_separates_what_is_open_from_what_is_over():
    """Whether anything is wrong right now, and what this pit has been doing,
    are different questions. The second is much the longer list, and mixed
    together the urgent one is answered somewhere in the middle of it."""
    page = render_page(
        "alert_history.html",
        tab="history",
        open=[_alert("High water", "The pit is full.")],
        past=[_alert("Overload tripped", "Pump 1 tripped.", lasted="1.0 h", bad=False)],
        messages=[],
    )

    assert page.index("Open now") < page.index("High water")
    assert page.index("High water") < page.index("Overload tripped")
    assert "1.0 h" in page, "how long the finished one lasted"


def test_an_empty_history_is_a_sentence_rather_than_an_empty_table():
    """On a panel that has behaved itself this is the normal state, and it
    deserves a sentence rather than furniture with no rows in it."""
    page = render_page("alert_history.html", tab="history", open=[], past=[], messages=[])

    assert "Nothing is raised" in page
    assert "Nothing has been raised yet" in page
    assert "<table" not in page


def test_the_history_says_when_a_message_did_not_get_through():
    """An alert nobody was told about is the failure this page exists to make
    visible, and it is invisible among the alerts themselves: a raised one
    looks the same whether it reached a phone or died in a mail server."""
    page = render_page(
        "alert_history.html",
        tab="history",
        open=[],
        past=[],
        messages=[
            {
                "channel": "sms",
                "target": "+12125550142",
                "status": "failed",
                "error": "connection refused",
                "detail": "",
                "when_local": "6 Sep 16:52",
            }
        ],
    )

    assert "failed" in page and "connection refused" in page
    assert "+12125550142" in page


def test_the_severity_is_a_bar_rather_than_a_column_of_words():
    """A column repeating the word critical is read once and then skipped. A
    color at the edge of the row is still doing its job on the twentieth."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    for level, token in (("critical", "--crit"), ("warning", "--warn"), ("info", "--water")):
        rule = css.split(f".alert.sev-{level} {{", 1)[1].split("}", 1)[0]
        assert token in rule, level

    # And the rows are not a table, because a three column table of prose
    # cannot fit a phone without clipping the third one.
    page = render_page("alert_history.html", tab="history", open=[], past=[], messages=[])
    assert "table-scroll" not in page


def test_today_is_today_and_not_the_last_twenty_four_hours():
    """The dashboard has counted "today" since it was built, so a history page
    answering "the last 24 hours" under the same word was two pages disagreeing
    about the same building. The span is measured back from the building's own
    midnight, which means it is whatever o'clock it is there."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from pitwatch.domain import series

    zone = "America/New_York"
    now = datetime.now(UTC)
    midnight = now.astimezone(ZoneInfo(zone)).replace(hour=0, minute=0, second=0, microsecond=0)

    window = series.window_for("today", zone)

    assert window.span <= timedelta(hours=25), "never longer than a day, DST included"
    elapsed = now - midnight.astimezone(UTC)
    assert abs(window.span - max(elapsed, timedelta(minutes=1))) < timedelta(seconds=5)

    # And a floor, so a page opened a second after midnight asks for something
    # a chart can hold rather than an interval of nothing.
    assert window.span >= timedelta(minutes=1)

    # The other two do not move.
    assert series.window_for("7d", zone).span == timedelta(days=7)
    assert series.window_for("30d", zone).span == timedelta(days=30)


def test_every_window_carries_the_english_it_is_read_in():
    """Gluing a preposition onto a label in the browser gives "The last Today"
    and "3 calls in Today". Each phrase is written out instead."""
    from pitwatch.domain.series import WINDOWS

    today = WINDOWS["today"]
    assert (today.title, today.heading, today.over, today.within) == (
        "Today",
        "Today",
        "today",
        "today",
    )

    week = WINDOWS["7d"]
    assert week.heading == "The last 7 days"
    assert "Calls for water " + week.over == "Calls for water over the last 7 days"
    assert "3 calls " + week.within == "3 calls in the last 7 days"

    for window in WINDOWS.values():
        assert not window.heading.startswith("The last The")


def test_the_summary_page_says_what_it_is_and_offers_a_refresh():
    """It is an AI summary and the page should say so, the way the settings
    section that configures it does. And the button is pressed again and again
    on a page that already has one, so it reads as another rather than a first."""
    page = render_page(
        "summary.html",
        ready=True,
        last=None,
        age="",
        context="",
        offer=Offer(True),
        earlier=[],
        error=None,
    )

    assert "AI Summary" in page
    assert ">New summary<" in page

    # The note used to say it knows nothing that is not on this page, which is
    # not true: it is sent a week of figures the page never draws.
    note = page.split('id="note-summary"', 1)[1]
    assert "not on this page" not in note
    assert "reading of the numbers" not in note
    # What is worth keeping from it: this costs money and only happens on a
    # press, and the building is not named to the model.
    assert "presses the button" in note and "no address" in note


def test_an_earlier_summary_is_shown_with_the_words_it_was_written_from():
    """The list is the history. Nothing new is stored for it: every summary has
    been kept since the table was made, and since 018 each one carries the
    description it was given, so this is a view onto rows that already exist."""
    page = render_page(
        "summary.html",
        ready=True,
        last=None,
        age="",
        context="Two pumps in a pit.",
        offer=Offer(True),
        earlier=[
            {
                "id": 12,
                "when_local": "3 Sep 9:14 AM",
                "who": "david",
                "model": "gpt-4o-mini",
                "body": "Both pumps look normal.",
                "context": "Two pumps and a check valve replaced in the spring.",
                "restorable": True,
                "same": False,
            },
            {
                "id": 4,
                "when_local": "27 Aug 8:02 AM",
                "who": "david",
                "model": "gpt-4o-mini",
                "body": "Nothing worth acting on.",
                "context": "",
                "restorable": False,
                "same": False,
            },
        ],
        error=None,
    )

    assert "3 Sep 9:14 AM" in page
    assert "check valve replaced in the spring" in page
    assert 'action="/summary/restore"' in page
    assert 'value="12"' in page

    # The one written before the words were kept offers no restore, because
    # restoring nothing would wipe the description and call it a restore.
    assert 'value="4"' not in page
    assert "not kept" in page


def test_the_words_already_in_the_box_are_not_offered_back():
    """A button that puts back what is already there is a button that does
    nothing, and pressing it would still count as a change."""
    page = render_page(
        "summary.html",
        ready=True,
        last=None,
        age="",
        context="Two pumps in a pit.",
        offer=Offer(True),
        earlier=[
            {
                "id": 12,
                "when_local": "3 Sep 9:14 AM",
                "who": "david",
                "model": "gpt-4o-mini",
                "body": "Both pumps look normal.",
                "context": "Two pumps in a pit.",
                "restorable": True,
                "same": True,
            }
        ],
        error=None,
    )

    assert 'action="/summary/restore"' not in page
    assert "the words in the box now" in page
