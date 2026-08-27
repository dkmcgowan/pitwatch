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


def test_only_the_database_password_is_required():
    """Everything else has a default, so a first run is one edit.

    A compose file that refuses to start over a setting somebody has no opinion
    about yet is a bad first five minutes.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

    assert set(re.findall(r"\$\{([A-Z_]+):\?", compose)) == {"POSTGRES_PASSWORD"}


def test_env_holds_only_what_has_no_sensible_default():
    """One file to edit, one line in it.

    Everything else lives beside a comment in the compose file, where somebody
    changing it can see what it does. A .env full of settings nobody has an
    opinion about is a longer first five minutes for no benefit.
    """
    root = Path(__file__).parent.parent
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")

    substituted = set(re.findall(r"\$\{([A-Z_]+)", compose))
    assert substituted == {"POSTGRES_PASSWORD"}

    offered = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    assert offered == {"POSTGRES_PASSWORD"}


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


def test_only_named_inputs_are_shown():
    """An unnamed input is one nothing is wired to. Eight rows of "not wired" is
    not a dashboard, it is a settings page nobody asked to see."""
    from pitwatch.schemas import ChannelMap, WaveshareSettings

    settings = WaveshareSettings(
        channels=[
            ChannelMap(channel=2, label="Lead float"),
            ChannelMap(channel=5, label="Seal failure"),
        ]
    )

    assert [c.channel for c in settings.used_channels] == [2, 5]
    assert [c.label for c in settings.used_channels] == ["Lead float", "Seal failure"]


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
        PumpsSettings,
        ShellySettings,
        SiteSettings,
        SmsSettings,
        SmtpSettings,
        WaveshareSettings,
    )

    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    context = {
        "site": SiteSettings(),
        "shelly": ShellySettings(),
        "waveshare": WaveshareSettings(),
        "pumps": PumpsSettings(),
        "smtp": SmtpSettings(),
        "sms": SmsSettings(),
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
        PumpSettings,
        PumpsSettings,
        ShellySettings,
        SiteSettings,
        WaveshareSettings,
    )

    site = SiteSettings(
        name="822 Greenwich St",
        timezone="America/Chicago",
        base_url="https://pitwatch.example.com",
        contact_email="pumps@example.com",
        contact_phone="+12125550142",
        notify_delay_s=11,
        notify_cooldown_s=1234,
    )
    shelly = ShellySettings(enabled=True, host="10.0.0.5", pump1_channel=1, pump2_channel=0)
    waveshare = WaveshareSettings(
        enabled=True,
        host="10.0.0.6",
        port=5020,
        unit_id=7,
        poll_ms=350,
        debounce_ms=750,
        channels=[
            ChannelMap(channel=1, label="Bottom float"),
            ChannelMap(channel=7, label="Pump 1 overload", invert=True),
        ],
    )
    pumps = PumpsSettings(
        pump1=PumpSettings(
            name="North",
            running_amps=1.5,
            nameplate_amps=9.6,
            overcurrent_amps=18.0,
            overcurrent_readings=3,
        ),
        pump2=PumpSettings(name="South", running_amps=1.6),
        max_runtime_ms=12_000,
        restart_gap_ms=4000,
        restart_streak=6,
        quiet_minutes_before_flag=300,
    )

    form = FormData(
        submitted(render_settings(site=site, shelly=shelly, waveshare=waveshare, pumps=pumps))
    )

    assert forms.site_from(form) == site
    assert forms.waveshare_from(form) == waveshare
    assert forms.pumps_from(form) == pumps
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

    from pitwatch.ingest.sink import LiveIo
    from pitwatch.ingest.waveshare import IoEvent

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


WIRED = {
    "pump1_run": 5,
    "pump2_run": 6,
    "pump1_fault": 7,
    "pump2_fault": 8,
}
P1, P2, F1, F2 = 5, 6, 7, 8


def words(live):
    from pitwatch.api.live import lead_and_lag
    from pitwatch.schemas import DashboardSettings

    return lead_and_lag(DashboardSettings(**WIRED), live)


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
    from pitwatch.schemas import DashboardSettings

    assert lead_and_lag(DashboardSettings(), io((P1, True))) == ("--", "--")


def test_a_lamp_with_no_input_is_not_a_lamp_that_is_off():
    """Three states, and the middle one is the whole point. A lamp reading off
    when it means nobody wired it is a lamp that gets believed."""
    from pitwatch.api.live import panel_state
    from pitwatch.schemas import ChannelMap, DashboardSettings, WaveshareSettings

    waveshare = WaveshareSettings(channels=[ChannelMap(channel=3, label="Top float")])
    panel = panel_state(
        DashboardSettings(high_water=3, system_alert=4),
        waveshare,
        io((3, True)),
    )

    assert panel["high_water"]["state"] is True
    assert panel["high_water"]["label"] == "Top float"
    # Assigned, but nothing has ever read it.
    assert panel["system_alert"]["state"] is None
    assert panel["system_alert"]["channel"] == 4
    # Not assigned at all.
    assert panel["lead_float"]["channel"] is None
    assert panel["lead_float"]["state"] is None


def test_saving_the_dashboard_page_unchanged_changes_nothing():
    """Same round trip as the settings page: what the form renders is what the
    parser reads."""
    from jinja2 import Environment, FileSystemLoader
    from starlette.datastructures import FormData

    from pitwatch.api import forms
    from pitwatch.schemas import DASHBOARD_ROLES, ChannelMap, DashboardSettings, WaveshareSettings

    dashboard = DashboardSettings(
        system_alert=4,
        high_water=3,
        lead_float=1,
        lag_float=2,
        pump1_run=5,
        pump2_run=6,
        pump1_fault=7,
        pump2_fault=8,
    )
    env = Environment(loader=FileSystemLoader("pitwatch/templates"), autoescape=True)
    env.globals["csrf_token"] = lambda: "token"
    env.globals["version"] = "test"
    html = env.get_template("dashboard_settings.html").render(
        site=None,
        user=None,
        roles=DASHBOARD_ROLES,
        dashboard=dashboard,
        waveshare=WaveshareSettings(channels=[ChannelMap(channel=3, label="Top float")]),
        saved=False,
        error=None,
    )

    assert forms.dashboard_from(FormData(submitted(html))) == dashboard


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


def test_the_panel_is_four_blocks_across():
    """Alerts, overloads, the screen, floats. Overloads have their own heading
    because they are a different kind of bad news: an alert means read the
    panel, an overload means a pump is off and staying off.

    Layout is normally not worth a test. This is, because it has been described
    in prose and built from that description more than once, and shipped wrong
    both times without anything saying a word.
    """
    page = render_dashboard()

    order = [
        ">Alerts",
        'data-lamp="system_alert"',
        'data-lamp="high_water"',
        ">Overloads",
        'data-lamp="pump1_fault"',
        'data-lamp="pump2_fault"',
        "door-middle",
        "data-lcd",
        ">Floats",
        'data-lamp="lead_float"',
        'data-lamp="lag_float"',
    ]
    found = [page.index(token) for token in order]

    assert found == sorted(found), "the panel is out of order: " + str(
        list(zip(order, found, strict=True))
    )


def test_a_lamp_says_one_thing():
    """Lit or not. It used to carry a line of text under it reading "not set"
    or "no data", which is a sentence where an indicator should be.

    An unassigned lamp still draws dimmer, which is the one piece of that
    distinction worth keeping without words: dark because nobody wired it reads
    differently from dark because the contact is open.
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


def test_the_overloads_have_a_block_of_their_own():
    """They were under Alerts, which put two kinds of bad news under one word.
    An alert means read the panel; an overload means a pump is off."""
    page = render_dashboard()

    assert ">OL1<" in page and ">OL2<" in page

    alerts = page.split("door-alerts", 1)[1].split("door-overloads", 1)[0]
    overloads = page.split("door-overloads", 1)[1].split("door-middle", 1)[0]

    assert "system_alert" in alerts and "high_water" in alerts
    assert "pump1_fault" not in alerts and "pump2_fault" not in alerts
    assert "pump1_fault" in overloads and "pump2_fault" in overloads


def test_a_pump_card_carries_its_own_lamp():
    """Green while it is running, top right, where a panel would put it."""
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    assert page.count("data-pump-lamp") == 2
    assert ".pump-lamp.on {" in css


def test_the_history_cards_cover_every_lamp():
    """Every contact with a lamp gets a row, because a lamp that is off now
    looks the same whether it went twenty times today or never."""
    from pitwatch.schemas import DASHBOARD_ROLES

    page = render_dashboard()

    for role, _ in DASHBOARD_ROLES:
        if role in ("pump1_run", "pump2_run"):
            continue  # those are the pump cards' own runs today
        assert 'data-history="' + role + '"' in page, role
    assert page.count("data-history-last") == 6
    assert page.count("data-history-count") == 6


def test_the_history_cards_read_in_the_same_order_as_the_lamps():
    """Alerts, overloads, floats, both times. Two orders for the same three
    things is one more thing to hold in your head."""
    import re as _re

    page = render_dashboard()
    cards = _re.findall(r'<section class="card history">\s*<h2>([A-Za-z]+)', page)

    assert cards == ["Alerts", "Overloads", "Floats"]


def test_floats_are_counted_by_the_day_and_alarms_by_the_month():
    """A float closes every time the pit fills, so a day is the useful number.
    An alarm counted by the day reads zero forever and teaches somebody to stop
    looking at it."""
    import re

    page = render_dashboard()
    windows = re.findall('data-window="([a-z]+)"', page)

    # In the order the cards appear, which now matches the lamps above them:
    # alerts, overloads, floats.
    assert windows == ["month", "month", "today"]


def test_the_run_contacts_have_no_lamp_on_the_panel():
    """A pump that is running says so on its own card, in amps. A lamp
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


def test_the_screen_is_a_wide_panel_flanked_by_rules():
    """A panel, not a tile. It was square, which made the two side lists as
    tall as it and forced the whole thing into a column on a phone."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    def rule(selector: str) -> str:
        return css.split(selector + " {", 1)[1].split("}", 1)[0]

    lcd = rule(".lcd")
    assert "aspect-ratio" not in lcd
    assert "width: 100%;" in lcd
    assert "min-height:" in lcd
    # The sides are sized to their own contents and the middle takes the rest,
    # which is what leaves room to be wide without guessing at fractions.
    assert "grid-template-columns: auto auto minmax(0, 1fr) auto;" in rule(".door-grid")

    middle = rule(".door-middle")
    assert "border-left:" in middle and "border-right:" in middle
    # And it has to stretch. A centered grid item shrinks to fit its contents,
    # so the screen's width: 100% resolved against the width of its own text
    # and the column it had been given went unused.
    assert "justify-self: stretch;" in middle
    # Each side is sized to its own widest lamp, so the bulbs in a stack line
    # up under each other.
    assert "width: max-content;" in rule(".door-side")


def test_every_missing_pump_fact_reads_the_same_way():
    """Four fields that can each have nothing behind them, and one way of
    saying so. Left alone they drift: this had "not set", "not in 24 h", a
    bare dash and a sentence, all on one card."""
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")
    # The three that draw a pump card, and nothing else. The panel lamps below
    # them keep their own words: "not set" there means no input is assigned,
    # which is a different thing from having no reading.
    card = js.split("function renderPump", 1)[1].split("function buildInputs", 1)[0]

    assert "function setFact(" in js
    for phrase in ("not enough runs yet", "not in 24 h", '"not set"'):
        assert phrase not in card, phrase
    # Every one of the four goes through it, so there is nowhere for a fifth
    # spelling to appear.
    assert card.count("setFact(") >= 5

    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    assert ".detail dd.none {" in css


def test_a_phone_puts_the_three_lists_side_by_side():
    """In the same order as the wide view: alerts, overloads, floats, with the
    screen full width underneath. Stacking them made a column several screens
    tall, and a different arrangement on a phone is a second layout to keep
    right rather than the same one at another size.
    """
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    # Everything after the phone breakpoint opens. The placements below appear
    # nowhere else in the file, so there is no need to find where it closes.
    phone = css.split("@media (max-width: 700px) {", 1)[1]

    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in phone
    assert "grid-area: 1 / 1;" in phone, "alerts first"
    assert "grid-area: 1 / 2;" in phone, "overloads second"
    assert "grid-area: 1 / 3;" in phone, "floats third"
    assert "grid-area: 2 / 1 / 3 / -1;" in phone, "the screen spans the row below"


def test_a_section_label_is_not_a_caption_on_the_first_lamp():
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    assert ".door-side .door-heading { margin-bottom:" in css


def test_the_pump_facts_are_two_by_two():
    """Left to itself the grid fitted as many columns as would go, which put
    four facts in a row on a wide card and three plus a lonely fourth on a
    medium one."""
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")
    detail = css.split(".detail {", 1)[1].split("}", 1)[0]

    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in detail

    page = render_dashboard()
    card = page.split('data-pump="1"', 1)[1].split("</article>", 1)[0]
    assert card.count("<dt>") == 4


def test_a_pump_card_carries_everything_about_that_pump():
    """Amps now, when it last ran, how often today, and how it has been
    running. All facts about one motor, so they live together."""
    page = render_dashboard()

    for marker in (
        "data-amps",
        "data-fact-last",
        "data-fact-runs",
        "data-typical",
        "data-nameplate",
        "data-drift",
    ):
        assert page.count(marker) == 2, marker


def test_a_running_pump_is_shown_by_the_card_and_not_by_a_pill():
    """The amps are right there and the card outlines itself green. A word
    saying the same thing is a third way to say it."""
    page = render_dashboard()
    css = Path("pitwatch/static/style.css").read_text(encoding="utf-8")

    assert "data-run-pill" not in page
    assert ".pump.running" in css


def test_every_long_note_is_a_dialog_opened_from_beside_its_heading():
    """A native dialog shown as a modal renders in the top layer, so it cannot
    push a card around or end up behind one whatever the stacking looks like,
    and Escape closes it without being told to."""
    page = render_dashboard()

    # Five buttons and five notes on the page: three history cards and two
    # pumps, the pump card being written once in a loop.
    assert page.count("data-info=") == 5
    assert page.count("<dialog") == 5
    assert page.count("</dialog>") == 5

    # Each button names a note that exists.
    import re

    for key in re.findall(r'data-info="([^"]+)"', page):
        assert 'id="note-' + key + '"' in page, key

    # And the words are still there, just not on the card.
    assert "full load amps printed on the motor" in page
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


def test_the_note_does_not_sit_in_the_flow_of_the_card():
    """It used to, first at the top and then at the foot. Both pushed the
    numbers around when it opened, which is what a card is for."""
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
    assert page.count('class="readout-label">Current<') == 2

    def size(selector: str) -> float:
        rule = css.split(selector + " {", 1)[1].split("}", 1)[0]
        return float(rule.split("font-size:", 1)[1].split("rem", 1)[0].strip())

    # Bigger than the facts under it, and by less than it used to be.
    assert size(".amps") <= 1.75
    assert size(".amps") > 1.0


def test_todays_run_count_carries_an_ordinary_day_beside_it():
    """Eighty-nine is a lot or a Tuesday depending on what the month looks
    like, and only one of those is worth getting out of bed for."""
    page = render_dashboard()
    js = Path("pitwatch/static/dashboard.js").read_text(encoding="utf-8")

    assert page.count("data-fact-average") == 2
    assert "daily_average" in js


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
