"""Checks on the migration files that do not need a database.

A migration that has been edited after it shipped is applied to a new install
and not to an existing one, which produces two databases with the same
schema_migration table and different schemas. That is the failure this catches:
it will not tell you a migration is correct, only that the set of them is
coherent and that the runner will handle them the way they expect.
"""

from __future__ import annotations

import re

from pitwatch.db import NO_TRANSACTION_MARKER, migration_files, split_statements


def test_migrations_are_numbered_uniquely_and_in_order():
    names = [path.name for path in migration_files()]

    assert names, "no migrations found"
    numbers = [int(re.match(r"^(\d+)_", name).group(1)) for name in names]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)


def test_continuous_aggregates_are_marked_as_needing_no_transaction():
    """Timescale refuses to create a continuous aggregate inside a transaction.

    Without the marker the migration fails at startup on a fresh install, which
    is the worst time to find out.
    """
    for path in migration_files():
        sql = path.read_text(encoding="utf-8")
        if "timescaledb.continuous" in sql:
            header = sql.split("\n\n", 1)[0]
            assert NO_TRANSACTION_MARKER in header, f"{path.name} needs the marker"


def test_no_transaction_migrations_split_into_runnable_statements():
    for path in migration_files():
        sql = path.read_text(encoding="utf-8")
        if NO_TRANSACTION_MARKER not in sql.split("\n\n", 1)[0]:
            continue
        statements = list(split_statements(sql))
        assert statements
        # A dollar quoted body would be cut in half by the naive splitter.
        assert not any("$$" in statement for statement in statements)


def test_split_statements_drops_comment_only_chunks():
    """Comments between statements are not statements.

    Handing asyncpg a chunk that is nothing but a comment is not an error, but
    it is a round trip that does nothing and it makes the log of what was
    applied misleading.
    """
    sql = "-- a leading comment;\nCREATE TABLE a (id int);\n-- a trailing one;\n"

    assert list(split_statements(sql)) == ["CREATE TABLE a (id int)"]


def test_split_statements_keeps_a_comment_attached_to_its_statement():
    sql = "-- why this table exists\nCREATE TABLE a (id int);\n"

    assert list(split_statements(sql)) == ["-- why this table exists\nCREATE TABLE a (id int)"]


def test_every_migration_parses_as_postgres():
    """Parse each file with libpg_query, the grammar Postgres itself uses.

    Catching a missing comma here is worth a dependency, because the
    alternative is finding it when a fresh install refuses to start.
    """
    import pglast

    for path in migration_files():
        try:
            statements = pglast.parse_sql(path.read_text(encoding="utf-8"))
        except pglast.parser.ParseError as error:  # pragma: no cover -- only on a broken file
            raise AssertionError(f"{path.name} does not parse: {error}") from error
        assert statements, f"{path.name} contains no statements"


def test_nothing_records_energy_or_cost():
    """A monitor, not a meter reading.

    Watt hours could only come from the meter's power figure, which in this
    installation is not about the motor at all: the voltage reference is the
    meter's own supply rather than a measured phase. A column for it is an
    invitation to fill it in with a number that looks right and is not, so
    there is not one.

    Amps and run duration are real measurements and are what the run detector
    records instead.
    """
    for path in migration_files():
        sql = path.read_text(encoding="utf-8")
        statements = [
            line
            for line in sql.splitlines()
            if not line.strip().startswith("--")
            and any(word in line.lower() for word in ("energy", "kwh", "watt_hour", "cost"))
        ]
        assert not statements, f"{path.name}: {statements}"
