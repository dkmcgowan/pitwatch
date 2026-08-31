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
    from pitwatch.schemas import ChannelMap, InputsSettings

    settings = InputsSettings(
        channels=[
            ChannelMap(channel=2, role="lead_float"),
            ChannelMap(channel=5, role="pump1_run"),
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

    from pitwatch.schemas import (
        DASHBOARD_ROLES,
        InputsSettings,
        PumpsSettings,
        ShellySettings,
        SiteSettings,
        SmsSettings,
        SmtpSettings,
    )

    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    context = {
        "site": SiteSettings(),
        "shelly": ShellySettings(),
        "inputs": InputsSettings(),
        "pumps": PumpsSettings(),
        "smtp": SmtpSettings(),
        "sms": SmsSettings(),
        "roles": DASHBOARD_ROLES,
        "user": None,
        "error": None,
        "saved": False,
    }
    context.update(overrides)
    return env.get_template("settings.html").render(**context)


def submitted(html: str) -> list[tuple[str, str]]:
    """What a browser would post from that page, left exactly as rendered."""
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
        fields.append((name.group(1), value.group(1) if value else ""))
    for select in re.finditer(r'<select [^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        chosen = re.search(r'<option value="([^"]*)"[^>]*selected', select.group(2))
        if chosen:
            fields.append((select.group(1), chosen.group(1)))
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
        ChannelMap,
        InputsSettings,
        ShellySettings,
        SiteSettings,
    )

    site = SiteSettings(
        name="822 Greenwich St",
        timezone="America/Chicago",
        base_url="https://pitwatch.example.com",
        contact_email="pumps@example.com",
        contact_phone="+12125550142",
        operator="Jane Smith, Sole Proprietor",
        operator_locality="New York, NY 10014",
        notify_delay_s=11,
        notify_cooldown_s=1234,
    )
    shelly = ShellySettings(enabled=True, host="10.0.0.5", pump1_channel=1, pump2_channel=0)
    inputs = InputsSettings(
        enabled=True,
        host="10.0.0.6",
        port=8883,
        username="panel",
        password="broker-secret",
        encrypted=True,
        topic="site/inputs",
        status_topic="site/status",
        client_id="pitwatch-822",
        debounce_ms=750,
        channels=[
            ChannelMap(channel=1, role="lead_float"),
            ChannelMap(channel=7, role="pump1_fault", invert=True),
        ],
    )
    form = FormData(submitted(render_settings(site=site, shelly=shelly, inputs=inputs)))

    assert forms.site_from(form) == site
    # The stored password is never rendered, so the round trip is given the
    # settings it is checking against, exactly as the route does. Everything
    # else on the page has to survive on what the page itself carries.
    assert forms.inputs_from(form, inputs) == inputs
    # The Shelly password is never rendered back, so it is the one field that
    # cannot survive this on its own; everything else on that section must.
    assert forms.shelly_from(form, shelly) == shelly


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

    from pitwatch.ingest.inputs import IoEvent
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
    from pitwatch.schemas import ChannelMap, InputsSettings

    return InputsSettings(
        channels=[
            ChannelMap(channel=P1, role="pump1_run"),
            ChannelMap(channel=P2, role="pump2_run"),
            ChannelMap(channel=F1, role="pump1_fault"),
            ChannelMap(channel=F2, role="pump2_fault"),
        ]
    )


def words(live):
    from pitwatch.api.live import lead_and_lag

    return lead_and_lag(wired(), live)


def test_the_display_waits_rather_than_guessing_which_pump_is_lead():
    """A fresh install has no idea. The controller alternates and does not say
    so, and picking one would be wrong half the time."""
    assert words(io()) == ("--", "--")


def test_a_running_pump_holds_lead_until_it_stops():
    """Matching the controller on the wall, which is the whole point of this
    display. The rotation flips when a pump drops out, not when it starts."""
    assert words(io((P1, True))) == ("LEAD", "LAG")
    assert words(io((P1, True), (P1, False))) == ("LAG", "LEAD")


def test_the_rotation_alternates_across_calls():
    """Pump 1 runs and hands over, then pump 2 runs and hands back."""
    assert words(io((P1, True), (P1, False))) == ("LAG", "LEAD")
    assert words(io((P1, True), (P1, False), (P2, True))) == ("LAG", "LEAD")
    assert words(io((P1, True), (P1, False), (P2, True), (P2, False))) == ("LEAD", "LAG")


def test_both_running_is_its_own_state():
    """The high water case: the pit came up past the lag float and the
    controller called both. Neither is leading anything at that point."""
    assert words(io((P1, True), (P2, True))) == ("ON", "ON")

    # And when the lag pump drops out first, the one still running is lead
    # again rather than the display jumping straight to the rotation.
    assert words(io((P1, True), (P2, True), (P2, False))) == ("LEAD", "LAG")


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
    from pitwatch.schemas import InputsSettings

    assert lead_and_lag(InputsSettings(), io((P1, True))) == ("--", "--")


def test_a_lamp_with_no_input_is_not_a_lamp_that_is_off():
    """Three states, and the middle one is the whole point. A lamp reading off
    when it means nobody wired it is a lamp that gets believed."""
    from pitwatch.api.live import panel_state
    from pitwatch.schemas import ChannelMap, InputsSettings

    inputs = InputsSettings(
        channels=[
            ChannelMap(channel=3, role="high_water"),
            ChannelMap(channel=4, role="system_alert"),
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
    from pitwatch.schemas import ChannelMap, InputsSettings

    inputs = InputsSettings(
        enabled=True,
        host="10.0.0.6",
        channels=[
            ChannelMap(channel=1, role="lead_float"),
            ChannelMap(channel=2, role="lag_float"),
            ChannelMap(channel=3, role="high_water"),
            ChannelMap(channel=4, role="system_alert", invert=True),
            ChannelMap(channel=5, role="pump1_run"),
            ChannelMap(channel=6, role="pump2_run"),
            ChannelMap(channel=7, role="pump1_fault", invert=True),
            ChannelMap(channel=8, role="pump2_fault", invert=True),
        ],
    )
    page = render_settings(inputs=inputs)

    assert forms.inputs_from(FormData(submitted(page)), inputs) == inputs


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


def test_the_dashboard_is_one_box():
    """It was six: the panel door, a card each for the two pumps, and three
    more for what the lamps had been doing. Six borders, six headings and six
    sets of padding, so most of the page was the space between the things
    somebody came to read and the answer to one question took a scroll.

    Layout is normally not worth a test. This is, because it has been described
    in prose and built from that description more than once, and shipped wrong
    both times without anything saying a word.
    """
    page = render_dashboard()

    assert page.count("<section") == 1
    # The banner and the two device indicators sit outside it. Nothing else
    # does, and no part of it is a card of its own any more.
    assert 'class="card"' not in page
    assert "history-row" not in page and "history-table" not in page


def test_the_board_reads_top_to_bottom():
    """The pumps, then the controller's own screen, then the contacts. The
    screen sits between them because it is the one thing here that is a
    sentence rather than a number, and the contacts come last because they are
    the part somebody reads only when something is lit.

    Overloads have a heading of their own rather than sitting under Alerts.
    They are a different kind of bad news: an alert means read the panel, an
    overload means a pump is off and staying off.
    """
    page = render_dashboard()

    order = [
        'data-pump="1"',
        'data-pump="2"',
        "data-lcd",
        ">Alerts",
        'data-lamp="system_alert"',
        'data-lamp="high_water"',
        ">Overloads",
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


def test_the_same_arrangement_at_every_width():
    """Two pump columns and three lists of lamps, on a phone and on a desktop.
    A layout that rearranges itself is two layouts to keep right, and the wide
    one had six boxes stacking into a column several screens tall.
    """
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    def rule(selector: str) -> str:
        return css.split(selector + " {", 1)[1].split("}", 1)[0]

    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in rule(".glance-pumps")
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in rule(".glance-lamps")

    # The phone block, which is the last thing in the board section. Nothing
    # in there moves a block; it only takes type and padding down to fit.
    phone = css.split("@media (max-width: 700px) {", 1)[1].split("/* Forms", 1)[0]
    assert "grid-template-columns" not in phone
    assert "grid-area" not in phone


def test_the_blocks_are_separated_by_rules_and_not_by_boxes():
    """A line says these answer different questions as well as a box does, and
    costs a pixel of height instead of a border, a radius and two paddings."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    between = css.split(".glance > * + * {", 1)[1].split("}", 1)[0]
    assert "border-top:" in between
    assert "padding-top:" in between

    # And between the columns inside a block, for the same reason: a number
    # under one heading must not read as the column beside it.
    assert "border-left:" in css.split(".pump-col + .pump-col {", 1)[1].split("}", 1)[0]
    assert "border-left:" in css.split(".lamp-group + .lamp-group {", 1)[1].split("}", 1)[0]


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


def test_the_history_covers_every_lamp():
    """Every contact with a lamp gets both lines."""
    from pitwatch.schemas import DASHBOARD_ROLES

    page = render_dashboard()

    for role, _ in DASHBOARD_ROLES:
        if role in ("pump1_run", "pump2_run"):
            continue  # those are the pump columns' own runs today
        assert 'data-history="' + role + '"' in page, role
    assert page.count("data-history-last") == 6
    assert page.count("data-history-count") == 6


def test_floats_are_counted_by_the_day_and_alarms_by_the_month():
    """A float closes every time the pit fills, so a day is the useful number.
    An alarm counted by the day reads zero forever and teaches somebody to stop
    looking at it."""
    import re

    page = render_dashboard()
    windows = re.findall('data-window="([a-z]+)"', page)

    # In the order the groups appear: alerts, overloads, floats.
    assert windows == ["month", "month", "today"]


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
    # nothing to say either way, and says it once rather than twice: the count
    # under an n/a of its own would be a second n/a on every one of six lamps.
    assert "counted" in history
    assert 'count.textContent = "";' in history


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


def test_the_screen_is_wide_and_shallow():
    """A panel, not a tile. It was square once, which made the lists beside it
    as tall as it was and forced the whole thing into a column on a phone. It
    is one short line, and every row of height it takes is a row the contacts
    below it do not get."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    lcd = css.split(".lcd {", 1)[1].split("}", 1)[0]

    assert "aspect-ratio" not in lcd
    assert "width: 100%;" in lcd
    assert "min-height:" in lcd
    assert "max-width:" not in lcd, "it spans the box at every width now"


def test_every_missing_pump_fact_reads_the_same_way():
    """Four fields that can each have nothing behind them, and one way of
    saying so. Left alone they drift: this had "not set", "not in 24 h", a
    bare dash and a sentence, all on one card."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    # The three that draw a pump column, and nothing else. The panel lamps
    # below them keep their own words: "not set" there means no input is
    # assigned, which is a different thing from having no reading.
    card = js.split("function renderPump", 1)[1].split("function buildInputs", 1)[0]

    assert "function setFact(" in js
    for phrase in ("not enough runs yet", "not in 24 h", '"not set"'):
        assert phrase not in card, phrase
    # Every fact goes through it, so there is nowhere for another spelling of
    # "no data yet" to appear.
    assert card.count("setFact(") >= 4

    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    assert ".fact .none {" in css


def test_a_pump_column_carries_everything_about_that_pump():
    """Amps now, when it last ran, how often today, and how it has been
    running. All facts about one motor, so they live in one column, and the
    two columns sit side by side the way the pumps do."""
    page = render_dashboard()

    for marker in (
        "data-amps",
        "data-fact-last",
        "data-fact-runs",
        "data-typical",
        "data-drift",
        "data-pump-lamp",
    ):
        assert page.count(marker) == 2, marker

    column = page.split('data-pump="1"', 1)[1].split('data-pump="2"', 1)[0]
    assert column.count("<dt>") == 4


def test_a_running_pump_is_shown_by_the_lamp_and_the_amps():
    """No pill. The amps are right there and turn green, and the lamp beside
    the name says the same thing to somebody looking from further away."""
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    assert "data-run-pill" not in page
    assert ".pump-col.running .amps" in css
    assert ".pump-lamp.on {" in css


def test_every_long_note_is_a_dialog_opened_from_beside_its_heading():
    """A native dialog shown as a modal renders in the top layer, so it cannot
    push the numbers around or end up behind something whatever the stacking
    looks like, and Escape closes it without being told to."""
    page = render_dashboard()

    # Five buttons and five notes: the three groups of lamps and the two pump
    # columns, the column being written once in a loop.
    assert page.count("data-info=") == 5
    assert page.count("<dialog") == 5
    assert page.count("</dialog>") == 5

    # Each button names a note that exists.
    import re

    for key in re.findall(r'data-info="([^"]+)"', page):
        assert 'id="note-' + key + '"' in page, key

    # And the words are still there, just not on the page.
    assert "middle reading of every one" in page
    assert "How often the pit has filled" in page


def test_a_note_can_always_be_closed():
    """A note that needs a target found before it will go away is a note that
    gets left open. Clicking anywhere closes it, including inside: there is
    nothing in there to interact with."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
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
    # The handle sits beside the heading it belongs to.
    assert page.count('class="info-mark"') == 5


def test_the_current_reading_is_labelled_and_not_the_biggest_thing_on_the_page():
    """A number on its own does not say what it is a number of, and this one is
    zero most of the time."""
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    # The label itself, not the word where the note explains what it means.
    assert page.count("<dt>Current</dt>") == 2

    def size(selector: str) -> float:
        rule = css.split(selector + " {", 1)[1].split("}", 1)[0]
        return float(rule.split("font-size:", 1)[1].split("rem", 1)[0].strip())

    # A little larger than the facts under it, and only a little. What a pump
    # has been doing over weeks has more to say than what it is doing this
    # second.
    assert size(".fact dd") < size(".amps") <= 1.1


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

    from pitwatch.ingest.shelly import EmSample
    from pitwatch.ingest.sink import LiveState

    live = LiveState()
    base = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)

    def reading(seconds: int, amps: float) -> None:
        live.update(
            EmSample(
                ts=base + timedelta(seconds=seconds),
                channel=0,
                current=amps,
                voltage=None,
                act_power=None,
                aprt_power=None,
                pf=None,
                freq=None,
            )
        )

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


def test_the_run_count_is_described_as_a_floor():
    """It was described as a tally, on the strength of an inference that does
    not hold: two readings above the running threshold with no zero between
    them do not prove the pump never stopped, because a zero that was never
    reported is not a zero that never happened.

    The operator who has stood in front of the panel says the pumps run in
    short bursts and nothing longer, so runs close together are arriving here
    looking like one.
    """
    # Compared against what the page says, not against how the template wraps.
    prose = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", render_dashboard()))

    assert "floor rather than a tally" in prose
    assert "at least this many rather than exactly this many" in prose


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
    assert links == ["/", "/users", "/alerts", "/settings", "/profile"], links
    assert len(links) == len(set(links))


def test_the_settings_page_asks_for_a_broker_and_not_for_a_poll_interval():
    """The device changed and the shape of the question changed with it.

    A poll interval left on this page would be a setting that does nothing, and
    a setting that does nothing is worse than a missing one: somebody tunes it
    and believes they have changed something.
    """
    page = render_settings()

    for name in ("inputs_host", "inputs_port", "inputs_username", "inputs_password"):
        assert f'name="{name}"' in page, name
    for name in ("inputs_topic", "inputs_status_topic", "inputs_client_id"):
        assert f'name="{name}"' in page, name

    for gone in ("inputs_poll_ms", "inputs_unit_id", "inputs_timeout_s"):
        assert gone not in page, gone

    # The one piece of processing that survived the change of protocol, because
    # contacts bounce whatever is carrying the news of it.
    assert 'name="inputs_debounce_ms"' in page


def test_the_stored_broker_password_is_never_rendered():
    from pitwatch.schemas import InputsSettings

    page = render_settings(
        inputs=InputsSettings(enabled=True, host="10.0.0.6", password="broker-secret")
    )

    assert "broker-secret" not in page


def test_the_lamps_are_chosen_on_the_input_that_carries_them():
    """There is no lamp section any more, and no lamp page before that. An
    input row says what the panel put on it, and that is what lights the lamp.
    Two lists to keep in step was the thing worth deleting."""
    page = render_settings()

    assert "channel_3_role" in page, "the choice is on the input row"
    for role, _ in [("high_water", 0), ("pump1_run", 0), ("system_alert", 0)]:
        assert f'value="{role}"' in page, role

    # Nothing left of the second list, nor of a page that only pointed at one.
    assert 'action="/settings/dashboard"' not in page
    assert "role_high_water" not in page
    assert "Dashboard lamps" not in page
    assert 'href="/settings/alerts"' not in page
