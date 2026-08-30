"""Reading and writing the configuration that lives in the database.

Everything the wizard and the settings pages edit goes through here. The store
keeps a cached copy so that a hot path, such as deciding whether a current
reading counts as a run, is not a database round trip, and it publishes a change
event so the running ingest tasks can pick up a new device address without the
container being restarted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import TypeVar

import asyncpg
from pydantic import BaseModel

from pitwatch.config import Config
from pitwatch.schemas import (
    AlertsSettings,
    InputsSettings,
    PumpsSettings,
    ShellySettings,
    SiteSettings,
    SmsSettings,
    SmtpSettings,
)

log = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

# Not a user setting. Stored alongside them because it has to survive a restart
# and there is nowhere better for it.
SECRET_KEY_SETTING = "internal.secret_key"
SETUP_COMPLETE_SETTING = "internal.setup_complete"


class SettingsStore:
    """Typed access to the setting table, with a cache and a change signal."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._cache: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._listeners: list[asyncio.Queue[str]] = []

    async def load(self) -> None:
        rows = await self._pool.fetch("SELECT key, value FROM setting")
        self._cache = {row["key"]: json.loads(row["value"]) for row in rows}
        log.info("Loaded %d setting(s)", len(self._cache))

    def get(self, model: type[ModelT]) -> ModelT:
        """Return a settings model, filling in defaults for anything unset.

        Values that fail validation, which normally means a field changed shape
        between versions, fall back to defaults rather than stopping the
        application. A monitor that will not start because one SMTP field is now
        an integer is a monitor that is not watching the pump.
        """
        raw = self._cache.get(model.KEY, {})  # type: ignore[attr-defined]
        try:
            return model.model_validate(raw)
        except ValueError as error:
            log.warning("Setting %r did not validate, using defaults: %s", model.KEY, error)  # type: ignore[attr-defined]
            return model()

    async def put(self, value: BaseModel) -> None:
        key: str = value.KEY  # type: ignore[attr-defined]
        payload = value.model_dump(mode="json")
        async with self._lock:
            await self._pool.execute(
                """
                INSERT INTO setting (key, value, updated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
                """,
                key,
                json.dumps(payload),
            )
            self._cache[key] = payload
        self._publish(key)

    async def get_raw(self, key: str, default: object = None) -> object:
        if key in self._cache:
            return self._cache[key]
        row = await self._pool.fetchval("SELECT value FROM setting WHERE key = $1", key)
        if row is None:
            return default
        self._cache[key] = json.loads(row)
        return self._cache[key]

    async def put_raw(self, key: str, value: object) -> None:
        async with self._lock:
            await self._pool.execute(
                """
                INSERT INTO setting (key, value, updated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
                """,
                key,
                json.dumps(value),
            )
            self._cache[key] = value  # type: ignore[assignment]
        self._publish(key)

    # -- change notification ------------------------------------------------
    #
    # An ingest task subscribes and restarts its connection when the settings it
    # cares about change. Queues are unbounded but only ever hold key names, and
    # a slow reader just coalesces on its own next loop.

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        if queue in self._listeners:
            self._listeners.remove(queue)

    def _publish(self, key: str) -> None:
        for queue in self._listeners:
            queue.put_nowait(key)

    # -- convenience --------------------------------------------------------

    @property
    def site(self) -> SiteSettings:
        return self.get(SiteSettings)

    @property
    def shelly(self) -> ShellySettings:
        return self.get(ShellySettings)

    @property
    def inputs(self) -> InputsSettings:
        return self.get(InputsSettings)

    @property
    def pumps(self) -> PumpsSettings:
        return self.get(PumpsSettings)

    @property
    def alerts(self) -> AlertsSettings:
        return self.get(AlertsSettings)

    @property
    def smtp(self) -> SmtpSettings:
        return self.get(SmtpSettings)

    @property
    def sms(self) -> SmsSettings:
        return self.get(SmsSettings)

    async def is_setup_complete(self) -> bool:
        return bool(await self.get_raw(SETUP_COMPLETE_SETTING, False))

    async def mark_setup_complete(self) -> None:
        await self.put_raw(SETUP_COMPLETE_SETTING, True)

    async def secret_key(self, config: Config) -> str:
        """The key that signs session cookies.

        Taken from the environment when it is set there, so a deployment can
        control it. Otherwise generated once and kept, which means sessions
        survive a restart on an install that never set the variable.
        """
        if config.secret_key:
            return config.secret_key
        existing = await self.get_raw(SECRET_KEY_SETTING)
        if isinstance(existing, str) and existing:
            return existing
        generated = secrets.token_urlsafe(48)
        await self.put_raw(SECRET_KEY_SETTING, generated)
        log.info("Generated a session key and stored it in the database")
        return generated


# What DI1 through DI8 are called when the environment seeds an install, in the
# order a duplex ejector panel usually brings them out. A starting point for
# somebody who has not opened the settings page yet, and nothing more: these are
# labels, so anything here is wrong only in the sense of being unhelpful.


async def seed_from_environment(store: SettingsStore, config: Config) -> None:
    """Write the PITWATCH_SEED_* values, once, into an empty settings table.

    This exists so a stack can be brought up from a compose file alone and
    already be talking to both devices. It never overwrites a setting that has
    been saved, so editing a seed variable later does nothing; the settings page
    is the place to change one.
    """
    if await store.is_setup_complete():
        return

    # Both of these seed an address and leave the device switched off.
    #
    # Knowing where something would be is not the same as knowing it is there.
    # A device seeded on means a fresh install starts by reporting a fault
    # about hardware that is still in its box, and the first thing anybody
    # learns is that the red light does not mean anything.
    if config.seed_shelly_host and not store.shelly.host:
        await store.put(ShellySettings(host=config.seed_shelly_host.strip()))
        log.info("Seeded the Shelly address from the environment")

    if config.seed_broker_host:
        # The connection only. What each input carries is a claim about how
        # somebody wired a panel, and guessing that would put eight lamps on
        # the dashboard describing a module nobody has connected yet.
        await store.put(
            InputsSettings(
                host=config.seed_broker_host.strip(),
                port=config.seed_broker_port or 1883,
                username=(config.seed_broker_username or "").strip(),
                password=config.seed_broker_password or "",
            )
        )
        log.info("Seeded the broker connection from the environment")
