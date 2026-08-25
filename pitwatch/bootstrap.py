"""Everything that has to happen before the web server is built.

There is exactly one thing: the session key. Starlette's session middleware
takes its key when the middleware is added, and the stored key does not exist
until the database is reachable, so a short lived connection resolves it first.
Migrations run here too, which means a container that cannot migrate fails at
startup with the real error rather than serving 503s and hiding it in a health
check.
"""

from __future__ import annotations

import logging

from pitwatch.config import Config
from pitwatch.db import connect, migrate
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)


async def resolve_secret_key(config: Config) -> str:
    pool = await connect(config)
    try:
        await migrate(pool)
        store = SettingsStore(pool)
        await store.load()
        return await store.secret_key(config)
    finally:
        await pool.close()
