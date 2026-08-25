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


@asynccontextmanager
async def lifespan_pool(config: Config) -> AsyncIterator[asyncpg.Pool]:
    pool = await connect(config)
    try:
        await migrate(pool)
        yield pool
    finally:
        await pool.close()
