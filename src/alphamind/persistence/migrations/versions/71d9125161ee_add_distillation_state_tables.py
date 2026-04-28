"""add_distillation_state_tables

Revision ID: 71d9125161ee
Revises: 0c13bbb17336
Create Date: 2026-04-27 00:00:00.000000

Adds the six Class B rolling-state tables consumed by every per-category
distillation computation (story 02-distillation-layer/03):

- ``distillation_ticker_baseline``
- ``distillation_pair_lag``
- ``distillation_contract_history``
- ``distillation_event_history``
- ``distillation_regime_state``
- ``distillation_composite_state``
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "71d9125161ee"
down_revision: str | Sequence[str] | None = "0c13bbb17336"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the six distillation state tables."""
    op.create_table(
        "distillation_ticker_baseline",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("baseline_kind", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=False),
        sa.Column("stdev", sa.Float(), nullable=False),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("calibration_state", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("ticker", "baseline_kind", "as_of"),
        sa.CheckConstraint(
            "baseline_kind IN ('volume', 'atr', 'spread', 'sentiment')",
            name="ck_distillation_ticker_baseline_baseline_kind",
        ),
        sa.CheckConstraint(
            "calibration_state IN ('calibrated', 'bootstrap', 'unavailable')",
            name="ck_distillation_ticker_baseline_calibration_state",
        ),
    )
    op.create_index(
        "ix_distillation_ticker_baseline_ticker_kind_as_of",
        "distillation_ticker_baseline",
        ["ticker", "baseline_kind", "as_of"],
        unique=False,
    )

    op.create_table(
        "distillation_pair_lag",
        sa.Column("lead_ticker", sa.Text(), nullable=False),
        sa.Column("lag_ticker", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("lead_lag_days_estimate", sa.Float(), nullable=False),
        sa.Column("n_pair_events", sa.Integer(), nullable=False),
        sa.Column("last_overdue_flag", sa.Integer(), nullable=False),
        sa.Column("calibration_state", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["lead_ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lag_ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("lead_ticker", "lag_ticker", "as_of"),
        sa.CheckConstraint(
            "calibration_state IN ('calibrated', 'bootstrap', 'unavailable')",
            name="ck_distillation_pair_lag_calibration_state",
        ),
    )
    op.create_index(
        "ix_distillation_pair_lag_lead_lag_as_of",
        "distillation_pair_lag",
        ["lead_ticker", "lag_ticker", "as_of"],
        unique=False,
    )

    op.create_table(
        "distillation_contract_history",
        sa.Column("contract_id", sa.Text(), nullable=False),
        sa.Column("snapshot_ts", sa.Text(), nullable=False),
        sa.Column("yes_probability", sa.Float(), nullable=False),
        sa.Column("delta_pp_since_prior", sa.Float(), nullable=False),
        sa.Column("liquidity_usd", sa.Float(), nullable=False),
        sa.Column("calibration_state", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["prediction_market_contracts.contract_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("contract_id", "snapshot_ts"),
        sa.CheckConstraint(
            "calibration_state IN ('calibrated', 'bootstrap', 'unavailable')",
            name="ck_distillation_contract_history_calibration_state",
        ),
    )
    op.create_index(
        "ix_distillation_contract_history_contract_ts",
        "distillation_contract_history",
        ["contract_id", "snapshot_ts"],
        unique=False,
    )

    op.create_table(
        "distillation_event_history",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("magnitude_atr_multiple", sa.Float(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("outcome_observed_at", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("ticker", "event_kind", "event_ts"),
        sa.CheckConstraint(
            "event_kind IN ('gap', 'extended_hours')",
            name="ck_distillation_event_history_event_kind",
        ),
    )
    op.create_index(
        "ix_distillation_event_history_ticker_kind_ts",
        "distillation_event_history",
        ["ticker", "event_kind", "event_ts"],
        unique=False,
    )

    op.create_table(
        "distillation_regime_state",
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("regime_label", sa.Text(), nullable=False),
        sa.Column("vix_level", sa.Float(), nullable=False),
        sa.Column("term_structure_basis", sa.Float(), nullable=False),
        sa.Column("vvix_percentile", sa.Float(), nullable=False),
        sa.Column("realized_vol", sa.Float(), nullable=False),
        sa.Column("indicator_agreement_count", sa.Integer(), nullable=False),
        sa.Column("invocations_held", sa.Integer(), nullable=False),
        sa.Column("transition_state", sa.Text(), nullable=False),
        sa.Column("prior_label", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("as_of"),
        sa.CheckConstraint(
            "regime_label IN ('low_vol_compression', 'vol_expansion', "
            "'crisis_spike', 'vol_normalization')",
            name="ck_distillation_regime_state_regime_label",
        ),
        sa.CheckConstraint(
            "transition_state IN ('stable', 'early-weak', 'early-strong', 'confirmed')",
            name="ck_distillation_regime_state_transition_state",
        ),
    )
    op.create_index(
        "ix_distillation_regime_state_as_of",
        "distillation_regime_state",
        ["as_of"],
        unique=False,
    )

    op.create_table(
        "distillation_composite_state",
        sa.Column("composite_kind", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("composite_value", sa.Float(), nullable=False),
        sa.Column("component_breakdown_json", sa.Text(), nullable=False),
        sa.Column("percentile_60d", sa.Float(), nullable=False),
        sa.Column("alert_active", sa.Integer(), nullable=False),
        sa.Column("calibration_state", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("composite_kind", "as_of"),
        sa.CheckConstraint(
            "composite_kind IN ('funding_stress', 'market_liquidity')",
            name="ck_distillation_composite_state_composite_kind",
        ),
        sa.CheckConstraint(
            "calibration_state IN ('calibrated', 'bootstrap', 'unavailable')",
            name="ck_distillation_composite_state_calibration_state",
        ),
    )
    op.create_index(
        "ix_distillation_composite_state_kind_as_of",
        "distillation_composite_state",
        ["composite_kind", "as_of"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the six distillation state tables."""
    op.drop_index(
        "ix_distillation_composite_state_kind_as_of",
        table_name="distillation_composite_state",
    )
    op.drop_table("distillation_composite_state")
    op.drop_index(
        "ix_distillation_regime_state_as_of",
        table_name="distillation_regime_state",
    )
    op.drop_table("distillation_regime_state")
    op.drop_index(
        "ix_distillation_event_history_ticker_kind_ts",
        table_name="distillation_event_history",
    )
    op.drop_table("distillation_event_history")
    op.drop_index(
        "ix_distillation_contract_history_contract_ts",
        table_name="distillation_contract_history",
    )
    op.drop_table("distillation_contract_history")
    op.drop_index(
        "ix_distillation_pair_lag_lead_lag_as_of",
        table_name="distillation_pair_lag",
    )
    op.drop_table("distillation_pair_lag")
    op.drop_index(
        "ix_distillation_ticker_baseline_ticker_kind_as_of",
        table_name="distillation_ticker_baseline",
    )
    op.drop_table("distillation_ticker_baseline")
