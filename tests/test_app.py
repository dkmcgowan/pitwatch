"""The application builds and answers, without a database behind it.

These do not start the lifespan, so there is no pool and no settings store.
That is the point: the container has to survive Postgres being slow to start or
briefly gone, and a process that cannot even build its routes in that state
gives you nothing to look at when you need it most.
"""

from __future__ import annotations

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
    client = build()

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


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


def test_the_compose_file_maps_the_host_port_to_the_container_default():
    """The two port variables have to stay distinct, and stay consistent.

    PITWATCH_PORT is the application's own bind port and is set to 8080 in the
    image. PITWATCH_HOST_PORT is the compose host mapping. If the compose file
    ever used PITWATCH_PORT for the host side, passing it through to the app
    would map a port the app was no longer listening on, and the container would
    look healthy from inside while being unreachable from outside.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

    assert '"${PITWATCH_HOST_PORT:-8080}:8080"' in compose
    assert "${PITWATCH_PORT" not in compose


def test_the_image_health_check_follows_the_bind_port():
    """The health check has to use the same port the application binds.

    Hardcoding 8080 there would make a container run with PITWATCH_PORT set
    report itself unhealthy forever, and compose would keep restarting it.
    """
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "PITWATCH_PORT=8080" in dockerfile
    assert "${PITWATCH_PORT}/healthz" in dockerfile


def test_the_host_network_compose_does_not_reach_the_database_by_service_name():
    """Host networking takes the container off the compose network.

    An app on host networking cannot resolve `db`, so this file has to point at
    127.0.0.1 and the database has to publish there. Getting that wrong is a
    container that starts, retries the database forever, and never says why in
    a way anyone would connect to networking.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.host.yml").read_text(encoding="utf-8")

    assert "network_mode: host" in compose
    assert "@db:5432" not in compose
    # Loopback only. Postgres has no business being on the LAN.
    assert '"127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"' in compose
    # There is no port mapping to move, so the bind port is the one that counts.
    # The file may still mention PITWATCH_HOST_PORT in a comment explaining that
    # it does nothing here, which is worth saying; what it must not do is
    # substitute it.
    assert "PITWATCH_PORT: ${PITWATCH_PORT:-8080}" in compose
    assert "${PITWATCH_HOST_PORT" not in compose


def test_the_host_network_compose_publishes_and_connects_on_the_same_port():
    """The one thing that breaks if the database port is made settable.

    On host networking the published port is not just for outside tools, it is
    the port the application itself dials. Parameterising the mapping and
    leaving 5432 hardcoded in the connection string would work perfectly until
    somebody set the variable, and then fail as a container that starts and
    retries the database forever.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.host.yml").read_text(encoding="utf-8")

    mapping = '"127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"'
    dsn = "@127.0.0.1:${POSTGRES_HOST_PORT:-5432}/pitwatch"

    assert mapping in compose
    assert dsn in compose
    # Neither reference may hardcode the port while the other reads a variable.
    assert "@127.0.0.1:5432/" not in compose


def test_the_bridge_compose_reaches_the_database_by_service_name():
    """Publishing is optional there and the application must never depend on it.

    Container to container the port is always 5432 whatever is published on the
    host, so an install that never uncomments the ports block still works.
    """
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

    assert "@db:5432/pitwatch" in compose
    # The publish block stays commented out by default.
    assert '\n    #   - "127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"' in compose
    assert '\n    ports:\n      - "127.0.0.1:' not in compose


def test_both_compose_files_share_a_project_and_volume_name():
    """Switching between bridge and host networking must keep the history.

    They are the same install run two ways, not two installs. A different
    project name or a different volume name in either file would silently start
    a second, empty database, and the first sign of it would be a dashboard
    that had forgotten everything.
    """
    root = Path(__file__).parent.parent
    bridge = (root / "docker-compose.yml").read_text(encoding="utf-8")
    host = (root / "docker-compose.host.yml").read_text(encoding="utf-8")

    for compose in (bridge, host):
        assert "\nname: pitwatch\n" in compose
        assert "\nvolumes:\n  pitwatch-db:\n" in compose
        assert "- pitwatch-db:/var/lib/postgresql/data" in compose


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
