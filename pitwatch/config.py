"""Process configuration, read from the environment once at startup.

This is deliberately small. It holds the things that have to be known before a
database connection exists: where the database is, what port to listen on, and
the key that signs session cookies. Everything else, including both device
addresses and the whole channel mapping, lives in the database and is edited in
the browser. See pitwatch/settings.py.

The PITWATCH_SEED_* variables are the exception, and they are a convenience for
a headless first boot rather than a second place to configure the application.
They are written into the settings table once, when it is empty, and ignored
from then on. Changing one later does nothing; change it in the settings page.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PITWATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: PostgresDsn = Field(
        default="postgresql://pitwatch:pitwatch@db:5432/pitwatch",
        description="Where the TimescaleDB instance lives.",
    )
    database_pool_min: int = 2
    database_pool_max: int = 10

    # A container has to listen on all its interfaces to be reachable at all.
    host: str = "0.0.0.0"
    port: int = 8080

    # Signs the session cookie. Generated and stored in the database on first
    # boot if it is not set here, so an install that never sets it still gets
    # sessions that survive a restart.
    secret_key: str | None = None

    # Which addresses are allowed to say, via X-Forwarded-For, who the client
    # really is. Only the proxy should be believed.
    #
    # This used to be "*", which trusts anybody. Behind a proxy that is fine
    # until the application's own port is also reachable, and then anyone who
    # can reach it can claim to be any address they like: per address sign in
    # throttling stops working and the log names whoever they felt like naming.
    # A comma separated list, or "*" to go back to trusting everything.
    trusted_proxies: str = "127.0.0.1,::1"

    # Set this when a proxy in front is terminating TLS, which it should be if
    # this is reachable from anywhere but your own network. It marks the session
    # cookie Secure, so a browser will not send it over plain HTTP.
    secure_cookies: bool = False
    # How long "stay signed in" lasts. A session without it ends with the
    # browser.
    session_days: int = 30

    log_level: str = "INFO"

    # The timezone the dashboard renders in. Timestamps are stored in UTC.
    timezone: str = "America/New_York"

    # Seeds, applied only when the settings table is empty. See the module
    # docstring.
    seed_shelly_host: str | None = None
    seed_waveshare_host: str | None = None
    seed_admin_username: str | None = None
    seed_admin_password: str | None = None

    @property
    def dsn(self) -> str:
        return str(self.database_url)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
