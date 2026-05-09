"""tighten state-persistence FKs

Revision ID: e9d2c4f7b3a1
Revises: d4a8e9f2c1b3
Create Date: 2026-05-08 12:00:00.000000

ALP-119 follow-up integration migration. Each per-table mapping module
documented "FKs land in a follow-up integration migration once those
tables ship"; the substrate's parent tables now all ship, so this
migration honors those promises.

Adds the deferred FK constraints — every link is ``ON DELETE RESTRICT``
so the audit trail cannot be broken by an in-place delete that would
orphan a child row. Per the design doc, deletes are an operator-only
out-of-band concern.

SQLite cannot ``ALTER TABLE ADD CONSTRAINT FOREIGN KEY``; ``op.batch_alter_table``
with ``recreate='always'`` issues the standard SQLite table-rebuild
(create new with constraint, copy rows, drop old, rename) atomically per
table. The rebuilds run table-at-a-time, so the cyclic
``orders.bracket_id`` ↔ ``brackets.entry_order_id`` link is straightforward:
each table's neighbor is unchanged at the moment of its rebuild.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9d2c4f7b3a1"
down_revision: str | Sequence[str] | None = "d4a8e9f2c1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Each per-table FK list is structured as a sequence of
# (constraint_name, columns, target_table, target_columns) tuples.
# ``ondelete`` is uniformly RESTRICT; ``initially_deferred`` is left at
# the SQLite default — the parent rows of every child write already exist
# when the child INSERT runs because Phase 1 / Phase 2 always seed the
# parent first inside the same transaction.
_POSITIONS_FKS = (
    ("fk_positions_thesis_id", ["thesis_id"], "theses", ["thesis_id"]),
    ("fk_positions_bracket_id", ["bracket_id"], "brackets", ["bracket_id"]),
    (
        "fk_positions_parent_position_id",
        ["parent_position_id"],
        "positions",
        ["position_id"],
    ),
)
_THESES_FKS = (("fk_theses_position_id", ["position_id"], "positions", ["position_id"]),)
_ORDERS_FKS = (
    ("fk_orders_position_id", ["position_id"], "positions", ["position_id"]),
    ("fk_orders_bracket_id", ["bracket_id"], "brackets", ["bracket_id"]),
)
_BRACKETS_FKS = (
    ("fk_brackets_position_id", ["position_id"], "positions", ["position_id"]),
    ("fk_brackets_entry_order_id", ["entry_order_id"], "orders", ["order_id"]),
)
_BRACKET_LEGS_FKS = (("fk_bracket_legs_order_id", ["order_id"], "orders", ["order_id"]),)
_ACTIVITY_LOG_FKS = (
    ("fk_activity_log_position_id", ["position_id"], "positions", ["position_id"]),
    ("fk_activity_log_order_id", ["order_id"], "orders", ["order_id"]),
    ("fk_activity_log_thesis_id", ["thesis_id"], "theses", ["thesis_id"]),
)

# Order matters: process tables in an order that minimizes the size of any
# single rebuild but tolerates the cyclic orders↔brackets link. Each
# rebuild is independent (the others' rows are unchanged at that point), so
# any topological order works as long as it covers every entry.
_TIGHTENING_ORDER = (
    ("positions", _POSITIONS_FKS),
    ("theses", _THESES_FKS),
    ("orders", _ORDERS_FKS),
    ("brackets", _BRACKETS_FKS),
    ("bracket_legs", _BRACKET_LEGS_FKS),
    ("activity_log", _ACTIVITY_LOG_FKS),
)


def upgrade() -> None:
    """Add the deferred FKs in one rebuild per table."""
    for table_name, fk_specs in _TIGHTENING_ORDER:
        with op.batch_alter_table(table_name, recreate="always") as batch_op:
            for constraint_name, columns, target_table, target_columns in fk_specs:
                batch_op.create_foreign_key(
                    constraint_name,
                    target_table,
                    columns,
                    target_columns,
                    ondelete="RESTRICT",
                )


def downgrade() -> None:
    """Drop every FK this revision added — reverse rebuild order."""
    for table_name, fk_specs in reversed(_TIGHTENING_ORDER):
        with op.batch_alter_table(table_name, recreate="always") as batch_op:
            for constraint_name, _columns, _target_table, _target_columns in fk_specs:
                batch_op.drop_constraint(constraint_name, type_="foreignkey")
