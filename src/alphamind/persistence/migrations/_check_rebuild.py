"""Shared CHECK-rebuild mechanics for SQLite migrations (ALP-915 / story 06i).

SQLite cannot ALTER a CHECK constraint in place, so widening or narrowing a
closed-set CHECK vocabulary is a table rebuild via
``batch_alter_table(recreate="always")`` — drop the named CHECK, add it back over
the target vocabulary. The two feedback-loop migrations (``c001fb0000ff``,
``b899tr0000ff``) each carried their own copy of these mechanics with divergent
signatures; this module is the one extracted home.

**Vocabulary is never centralized here.** Each migration keeps its own frozen
vocabulary tuples — the CHECK-vocab trap: a migration must reproduce the *exact*
membership current at its revision (so ``downgrade`` narrows back correctly), which
a live-enum read could not do once a later member exists. This module owns only the
mechanics (quoting, persisted-DDL inspection, the batch rebuild), not the words.

Migrations run with ``PRAGMA foreign_keys=OFF`` (see the alembic env), so the
intermediate ``DROP TABLE`` of a rebuild does not trip incoming FK constraints.

**Frozen once applied.** Once these migrations merge and apply to prod, treat this
module as frozen / append-only like the migration files that consume it — a change
here re-renders the DDL a deployed migration already ran.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``<column> IN ('a', 'b', ...)`` CHECK condition over *values*.

    Values are quoted with :func:`repr`; for the ASCII identifier vocabularies the
    migrations carry, ``repr('FOO')`` is ``'FOO'`` — a single-quoted SQL string
    literal.
    """
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def persisted_check_has_member(bind: sa.engine.Connection, table: str, member: str) -> bool:
    """True if *table*'s persisted DDL already lists *member* in a CHECK constraint.

    Reads the stored ``CREATE TABLE`` text from ``sqlite_master`` and tests for the
    quoted *member* literal, so a guarded ``upgrade`` / ``downgrade`` can skip the
    rebuild when the CHECK already has (or lacks) the member — keeping a fresh,
    metadata-built DB a no-op.
    """
    sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table},
    ).scalar_one_or_none()
    return sql is not None and repr(member) in sql


def rebuild_check(table: str, check_name: str, condition: str) -> None:
    """Rebuild *table* to reset the named CHECK constraint over *condition*.

    ``batch_alter_table(recreate="always")`` drops *check_name* and re-adds it over
    the pre-rendered *condition* string (build it with :func:`check_in`). The only
    path to changing a SQLite CHECK vocabulary; the caller is responsible for passing
    the condition that matches the target frozen vocabulary.
    """
    with op.batch_alter_table(table, recreate="always") as batch_op:
        batch_op.drop_constraint(check_name, type_="check")
        batch_op.create_check_constraint(check_name, condition)


__all__ = ["check_in", "persisted_check_has_member", "rebuild_check"]
