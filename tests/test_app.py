"""The application builds and answers, without a database behind it.

These do not start the lifespan, so there is no pool and no settings store.
That is the point: the container has to survive Postgres being slow to start or
briefly gone, and a process that cannot even build its routes in that state
gives you nothing to look at when you need it most.
"""

from __future__ import annotations

import re
from pathlib import Path

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
