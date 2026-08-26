"""Database connection pool and the migration runner.

The runner is intentionally about as simple as one can be: SQL files in
pitwatch/migrations, applied in name order, each recorded in schema_migration so
it is applied once. There is no down migration. Rolling a schema back on a box
in a boiler room is not a thing anyone is going to do at three in the morning;
restoring the volume from a backup is.

Each file runs inside a transaction unless its first lines contain the marker
"pitwatch: no-transaction", which the Timescale continuous aggregates need
because they cannot be created inside one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from pitwatch.config import Config

log = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).parent / "migrations"
NO_TRANSACTION_MARKER = "pitwatch: no-transaction"


async def connect(config: Config, *, attempts: int = 30) -> asyncpg.Pool:
    """Open the pool, waiting for the database to come up.

    In a compose stack the app container regularly starts before Postgres is
    accepting connections, even with a health check, so a first connection
    failure is expected rather than fatal.
    """
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            pool = await asyncpg.create_pool(
                dsn=config.dsn,
                min_size=config.database_pool_min,
                max_size=config.database_pool_max,
                command_timeout=30,
            )
        except (OSError, asyncpg.PostgresError) as error:
            last_error = error
            log.info("Database not ready yet (attempt %d/%d): %s", attempt, attempts, error)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)
            continue
        if pool is None:  # pragma: no cover -- asyncpg only returns None on a bug
            raise RuntimeError("asyncpg returned no pool")
        log.info("Connected to the database")
        return pool
    raise RuntimeError(f"Could not reach the database after {attempts} attempts") from last_error


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def split_statements(sql: str) -> Iterable[str]:
    """Split a migration into statements for the no-transaction path.

    Naive on purpose. It splits on semicolons that end a line, which holds for
    every migration in this project because none of them define a function body
    or use a dollar quoted string. If one ever does, give it its own file that
    runs in a transaction, or teach this to skip dollar quotes.
    """
    for chunk in sql.split(";\n"):
        statement = chunk.strip()
        if statement and not all(
            line.strip().startswith("--") for line in statement.splitlines() if line.strip()
        ):
            yield statement


async def migrate(pool: asyncpg.Pool) -> list[str]:
    """Apply any migration that has not run. Returns the names applied."""
    async with pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                name        text PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        done = {row["name"] for row in await connection.fetch("SELECT name FROM schema_migration")}

        applied = []
        for path in migration_files():
            if path.name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            log.info("Applying migration %s", path.name)
            if NO_TRANSACTION_MARKER in sql.split("\n\n", 1)[0]:
                for statement in split_statements(sql):
                    await connection.execute(statement)
                await connection.execute(
                    "INSERT INTO schema_migration (name) VALUES ($1)", path.name
                )
            else:
                async with connection.transaction():
                    await connection.execute(sql)
                    await connection.execute(
                        "INSERT INTO schema_migration (name) VALUES ($1)", path.name
                    )
            applied.append(path.name)

    if applied:
        log.info("Applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        log.info("Database schema is current")
    return applied


async def update_timescale_extension(config: Config) -> str | None:
    """Bring the installed extension up to the version the image ships.

    The compose files float the database image on `latest-pg17`, so pulling a
    newer one arrives with newer Timescale binaries while the database still has
    the old extension version registered. Postgres does not reconcile that
    itself, and the mismatch surfaces later as functions that are missing or
    behave oddly, which is a miserable thing to debug.

    Two constraints make this its own connection rather than part of migrate():
    the statement cannot run inside a transaction, and Timescale requires it to
    be the first thing a session does, before anything has touched the
    extension.

    Returns the version it moved to, or None if there was nothing to do.
    """
    connection = await asyncpg.connect(dsn=config.dsn)
    try:
        installed = await connection.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
        )
        # Not installed yet means a fresh database, where migration 001 is about
        # to create it at the current version anyway.
        if installed is None:
            return None

        available = await connection.fetchval(
            "SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb'"
        )
        if not available or available == installed:
            return None

        log.info("Updating the timescaledb extension from %s to %s", installed, available)
        await connection.execute("ALTER EXTENSION timescaledb UPDATE")
        return str(available)
    except asyncpg.PostgresError as error:
        # Worth continuing. The application may well work fine on the older
        # extension, and refusing to start over this would take the monitoring
        # away for something that is usually cosmetic.
        log.warning(
            "Could not update the timescaledb extension: %s. "
            "If something later fails oddly, run ALTER EXTENSION timescaledb UPDATE by hand.",
            error,
        )
        return None
    finally:
        await connection.close()


@asynccontextmanager
async def lifespan_pool(config: Config) -> AsyncIterator[asyncpg.Pool]:
    # Before the pool, so that no pooled connection has touched the extension
    # by the time the update runs.
    await update_timescale_extension(config)
    pool = await connect(config)
    try:
        await migrate(pool)
        yield pool
    finally:
        await pool.close()
