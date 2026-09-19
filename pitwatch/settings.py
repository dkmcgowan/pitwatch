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
    AiSettings,
    AlertsSettings,
    ChatSettings,
    GroundwaterSettings,
    MqttSettings,
    PanelButtonSettings,
    PumpsSettings,
    SiteSettings,
    SmsSettings,
    SmtpSettings,
    TideSettings,
    WeatherSettings,
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
        # PitWatch's own: the mail server, the Twilio account, the model key.
        self._cache: dict[str, dict] = {}
        # One building's, keyed by site: its panel, its pumps, its rules.
        self._by_site: dict[int, dict[str, dict]] = {}
        # Which building this store is answering about. One today, and the
        # thing a request would set once there are several.
        self._site_id: int | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[asyncio.Queue[str]] = []

    @property
    def site_id(self) -> int | None:
        """The building being answered about, or None before load."""
        return self._site_id

    def for_site(self, site_id: int) -> SettingsStore:
        """A view of the same store answering about a different building.

        Shares the caches and the pool deliberately: two views of one store are
        two readings of the same configuration, and a second copy would be a
        second thing to keep current.
        """
        other = SettingsStore.__new__(SettingsStore)
        other.__dict__ = dict(self.__dict__)
        other._site_id = site_id
        return other

    async def load(self) -> None:
        rows = await self._pool.fetch("SELECT key, value FROM setting")
        self._cache = {row["key"]: json.loads(row["value"]) for row in rows}

        self._by_site = {}
        for row in await self._pool.fetch("SELECT site_id, key, value FROM site_setting"):
            self._by_site.setdefault(row["site_id"], {})[row["key"]] = json.loads(row["value"])

        # The one site, until something sets it per request. `min` rather than
        # any, so a restart lands on the same building every time.
        sites = await self._pool.fetch("SELECT id FROM site ORDER BY id")
        self._site_id = sites[0]["id"] if sites else None

        log.info(
            "Loaded %d application setting(s) and %d site(s)",
            len(self._cache),
            len(sites),
        )

    def get(self, model: type[ModelT]) -> ModelT:
        """Return a settings model, filling in defaults for anything unset.

        Values that fail validation, which normally means a field changed shape
        between versions, fall back to defaults rather than stopping the
        application. A monitor that will not start because one SMTP field is now
        an integer is a monitor that is not watching the pump.
        """
        raw = self._raw_for(model)
        try:
            return model.model_validate(raw)
        except ValueError as error:
            log.warning("Setting %r did not validate, using defaults: %s", model.KEY, error)  # type: ignore[attr-defined]
            return model()

    def _raw_for(self, model: type[BaseModel]) -> dict:
        """Which of the two caches a model is read from.

        Declared on the model rather than worked out here, because a setting
        read from the wrong table is one silently shared between buildings or
        silently not shared at all, and neither shows up as an error.
        """
        key: str = model.KEY  # type: ignore[attr-defined]
        if getattr(model, "SCOPE", "app") == "app":
            return self._cache.get(key, {})
        if self._site_id is None:
            return {}
        return self._by_site.get(self._site_id, {}).get(key, {})

    async def put(self, value: BaseModel) -> None:
        key: str = value.KEY  # type: ignore[attr-defined]
        payload = value.model_dump(mode="json")
        scope = getattr(type(value), "SCOPE", "app")

        async with self._lock:
            if scope == "app":
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
            else:
                if self._site_id is None:
                    raise RuntimeError(f"Cannot save {key!r}: no site is selected")
                await self._pool.execute(
                    """
                    INSERT INTO site_setting (site_id, key, value, updated_at)
                    VALUES ($1, $2, $3::jsonb, now())
                    ON CONFLICT (site_id, key)
                    DO UPDATE SET value = excluded.value, updated_at = now()
                    """,
                    self._site_id,
                    key,
                    json.dumps(payload),
                )
                self._by_site.setdefault(self._site_id, {})[key] = payload
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
    def mqtt(self) -> MqttSettings:
        return self.get(MqttSettings)

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

    @property
    def chat(self) -> ChatSettings:
        """What this building says about itself, for the model to read."""
        return self.get(ChatSettings)

    @property
    def ai(self) -> AiSettings:
        """The account a summary is written through, which is PitWatch's."""
        return self.get(AiSettings)

    @property
    def weather(self) -> WeatherSettings:
        return self.get(WeatherSettings)

    @property
    def tide(self) -> TideSettings:
        return self.get(TideSettings)

    @property
    def groundwater(self) -> GroundwaterSettings:
        return self.get(GroundwaterSettings)

    @property
    def panel_button(self) -> PanelButtonSettings:
        return self.get(PanelButtonSettings)

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

    # The connection and nothing else, left switched off.
    #
    # Knowing where a broker would be is not the same as knowing anything is
    # publishing to it. Seeded on, a fresh install would start by reporting a
    # fault about hardware still in its box, and the first thing anybody learns
    # is that the red light does not mean anything.
    #
    # No sources either. What each input carries is a claim about how somebody
    # wired a panel, and which topic a meter publishes on is a claim about how
    # somebody configured it. Guessing either would put lamps on a dashboard
    # describing equipment nobody has connected.
    if config.seed_broker_host:
        await store.put(
            MqttSettings(
                host=config.seed_broker_host.strip(),
                port=config.seed_broker_port or 1883,
                username=(config.seed_broker_username or "").strip(),
                password=config.seed_broker_password or "",
            )
        )
        log.info("Seeded the broker connection from the environment")
