"""Fixtures, including the ones that need a real TimescaleDB.

Anything that touches the database is skipped unless PITWATCH_TEST_DATABASE_URL
points at one. CI sets it against a Timescale service container, so the
migrations, the settings store and the whole HTTP flow are exercised against the
real thing on every push. Locally, `docker compose up -d db` and exporting the
same variable gets the same coverage.

The alternative, mocking asyncpg, would test that the mock behaves like the mock.
The migrations are the part most likely to be wrong and the part a fake cannot
check at all.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

DATABASE_URL_ENV = "PITWATCH_TEST_DATABASE_URL"
REQUIRE_ENV = "PITWATCH_REQUIRE_DATABASE"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if url:
        return url
    # Skipping is right on a laptop with no database and wrong in CI, where it
    # would be a silent hole rather than a failure. CI sets the second variable.
    if os.environ.get(REQUIRE_ENV):
        raise RuntimeError(
            f"{REQUIRE_ENV} is set but {DATABASE_URL_ENV} is not, so the database "
            "tests would have been skipped"
        )
    pytest.skip(f"set {DATABASE_URL_ENV} to run the database tests")


@pytest.fixture
async def pool(database_url):
    """A pool against an empty, freshly migrated database.

    The schema is torn down and rebuilt per test. That is slower than
    truncating tables, and it is the only way to keep the migration path itself
    under test rather than testing it once and then trusting it.
    """
    import asyncpg

    from pitwatch.db import migrate

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=4)
    assert pool is not None
    try:
        async with pool.acquire() as connection:
            # The extension owns schemas of its own, so it goes first or the
            # drop leaves catalog rows pointing at tables that are gone.
            await connection.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
            await connection.execute("DROP SCHEMA public CASCADE")
            await connection.execute("CREATE SCHEMA public")
        await migrate(pool)
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def store(pool):
    from pitwatch.settings import SettingsStore

    store = SettingsStore(pool)
    await store.load()
    return store


@pytest.fixture
def config(database_url):
    from pitwatch.config import Config

    return Config(database_url=database_url, secret_key="test-key-for-the-test-suite")


def session_of(client) -> dict:
    """Read the session out of the cookie the client is holding.

    The cookie is signed rather than encrypted, so the payload can be read
    without the key. Only a test would want to; the point here is to find the
    CSRF token without an extra page fetch before every post.
    """
    raw = client.cookies.get("pitwatch_session")
    if not raw:
        return {}
    payload = raw.split(".")[0]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


class SignedRequestClient(TestClient):
    """A TestClient that carries a CSRF token, the way a browser does.

    Every form the application renders has the token in it, so a test that had
    to add it by hand to each post would be testing the test. Pass csrf=False to
    post without one, which is what the tests that check the guard do.
    """

    def post(self, url, *, csrf=True, **kwargs):
        if csrf:
            data = kwargs.get("data")
            if data is None or (isinstance(data, dict) and "csrf_token" not in data):
                kwargs["data"] = {**(data or {}), "csrf_token": self.csrf_token()}
        return super().post(url, **kwargs)

    def csrf_token(self) -> str:
        token = session_of(self).get("csrf")
        if not token:
            # No session yet, so nothing has issued one. Loading the sign in
            # page is what a browser would have done first anyway.
            self.get("/login")
            token = session_of(self).get("csrf")
        return token or ""


@pytest.fixture
def client(config, pool):
    """A TestClient with the lifespan running, so ingest and settings are up.

    The pool fixture is depended on rather than used, so that the schema is
    rebuilt before the application connects to it.
    """
    from pitwatch.app import create_app

    with SignedRequestClient(create_app(config, secret_key=config.secret_key)) as client:
        yield client
