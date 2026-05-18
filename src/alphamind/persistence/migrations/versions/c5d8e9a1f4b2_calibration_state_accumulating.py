"""calibration_state_bootstrap_to_accumulating

Revision ID: c5d8e9a1f4b2
Revises: b3e6f8a2c4d7
Create Date: 2026-05-18 16:30:00.000000

Renames the ``bootstrap`` calibration-state value to ``accumulating`` across
the four distillation tables, per ALP-540. The vocabulary split distinguishes
"collector healthy, just need more time" (``accumulating``) from "zero
observations / collector failure" (``unavailable``), eliminating the
operator-confusing collapsed-state ``bootstrap``.

Per-table changes:

- ``distillation_ticker_baseline`` — UPDATE bootstrap → accumulating;
  recreate CHECK to accept the new vocabulary.
- ``distillation_pair_lag`` — same.
- ``distillation_contract_history`` — same.
- ``distillation_composite_state`` — same.

The UPDATE precedes the CHECK recreation so existing rows pass the new
constraint at validation time. SQLite recreates CHECK constraints via
``batch_alter_table`` — the column itself is unchanged.

This migration is data-preserving: every historical ``bootstrap`` row is
re-labelled ``accumulating`` because the prior decision rule only emitted
``bootstrap`` when ``0 < observed < required`` (or for some defensive
fallbacks; the per-series writer's invariant kept ``unavailable`` reserved
for ``value=None``). The new decision rule will correctly produce
``unavailable`` for zero-observation rows on subsequent invocations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d8e9a1f4b2"
down_revision: str | Sequence[str] | None = "b3e6f8a2c4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_CHECK_SQL = "calibration_state IN ('calibrated', 'accumulating', 'unavailable')"
_OLD_CHECK_SQL = "calibration_state IN ('calibrated', 'bootstrap', 'unavailable')"


_TABLES_WITH_CHECK: tuple[tuple[str, str], ...] = (
    ("distillation_ticker_baseline", "ck_distillation_ticker_baseline_calibration_state"),
    ("distillation_pair_lag", "ck_distillation_pair_lag_calibration_state"),
    ("distillation_contract_history", "ck_distillation_contract_history_calibration_state"),
    ("distillation_composite_state", "ck_distillation_composite_state_calibration_state"),
)


def _rename_check(*, table: str, constraint: str, old_sql: str, new_sql: str) -> None:
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_constraint(constraint, type_="check")
        batch_op.create_check_constraint(constraint, new_sql)
    _ = old_sql  # intentionally unused — recorded for symmetry with downgrade


def upgrade() -> None:
    """Re-label bootstrap → accumulating and update the CHECK constraints."""
    for table, _ in _TABLES_WITH_CHECK:
        op.execute(
            sa.text(
                f"UPDATE {table} SET calibration_state = 'accumulating' "
                "WHERE calibration_state = 'bootstrap'"
            )
        )
    for table, constraint in _TABLES_WITH_CHECK:
        _rename_check(
            table=table,
            constraint=constraint,
            old_sql=_OLD_CHECK_SQL,
            new_sql=_NEW_CHECK_SQL,
        )


def downgrade() -> None:
    """Revert: accumulating → bootstrap and reinstate the old CHECK constraints."""
    for table, constraint in _TABLES_WITH_CHECK:
        _rename_check(
            table=table,
            constraint=constraint,
            old_sql=_NEW_CHECK_SQL,
            new_sql=_OLD_CHECK_SQL,
        )
    for table, _ in _TABLES_WITH_CHECK:
        op.execute(
            sa.text(
                f"UPDATE {table} SET calibration_state = 'bootstrap' "
                "WHERE calibration_state = 'accumulating'"
            )
        )
