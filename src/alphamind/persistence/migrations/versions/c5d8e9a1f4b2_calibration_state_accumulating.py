"""calibration_state_bootstrap_to_accumulating

Revision ID: c5d8e9a1f4b2
Revises: b3e6f8a2c4d7
Create Date: 2026-05-18 16:30:00.000000

Renames the ``bootstrap`` calibration-state value to ``accumulating`` across
the four distillation tables enumerated in ``_TABLES_WITH_CHECK`` below, per
ALP-540. The vocabulary split distinguishes "collector healthy, just need
more time" (``accumulating``) from "zero observations / collector failure"
(``unavailable``), eliminating the operator-confusing collapsed-state
``bootstrap``.

SQLite enforces CHECK at row-write time, so the OLD constraint must be
dropped before the UPDATE writes ``'accumulating'`` — and symmetrically on
downgrade the NEW constraint must be dropped before the reverse UPDATE writes
``'bootstrap'``. The CHECK is replaced via ``batch_alter_table`` (table
rebuild); the column itself is unchanged.

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


def _drop_check(*, table: str, constraint: str) -> None:
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_constraint(constraint, type_="check")


def _create_check(*, table: str, constraint: str, sql: str) -> None:
    with op.batch_alter_table(table) as batch_op:
        batch_op.create_check_constraint(constraint, sql)


def upgrade() -> None:
    """Re-label bootstrap → accumulating and update the CHECK constraints."""
    # Three phases, ordered to satisfy SQLite's row-write-time CHECK enforcement:
    # (1) drop OLD CHECK on every table, (2) UPDATE values, (3) create NEW CHECK.
    # Collapsing into a single per-table block would force the UPDATE inside
    # batch_alter_table, where op.execute targets the shadow table — no-op.
    for table, constraint in _TABLES_WITH_CHECK:
        _drop_check(table=table, constraint=constraint)
    for table, _ in _TABLES_WITH_CHECK:
        op.execute(
            sa.text(
                f"UPDATE {table} SET calibration_state = 'accumulating' "
                "WHERE calibration_state = 'bootstrap'"
            )
        )
    for table, constraint in _TABLES_WITH_CHECK:
        _create_check(table=table, constraint=constraint, sql=_NEW_CHECK_SQL)


def downgrade() -> None:
    """Revert: accumulating → bootstrap and reinstate the old CHECK constraints."""
    # Mirror of upgrade(): drop NEW CHECK → UPDATE values → create OLD CHECK.
    for table, constraint in _TABLES_WITH_CHECK:
        _drop_check(table=table, constraint=constraint)
    for table, _ in _TABLES_WITH_CHECK:
        op.execute(
            sa.text(
                f"UPDATE {table} SET calibration_state = 'bootstrap' "
                "WHERE calibration_state = 'accumulating'"
            )
        )
    for table, constraint in _TABLES_WITH_CHECK:
        _create_check(table=table, constraint=constraint, sql=_OLD_CHECK_SQL)
