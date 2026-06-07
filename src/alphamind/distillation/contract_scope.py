"""
Resolve the in-scope prediction-market contract list for a distillation run.

The orchestrator threads the resolved tuple to both
:func:`alphamind.distillation.baselines.refresh_contract_history` (class-B
refresh) and
:func:`alphamind.distillation.qualitative._loaders.load_qualitative_inputs`
(per-category indicator compute, which threads it into the
prediction-market deltas loader) — both consumers MUST receive the same
tuple, otherwise the writer ingests one set while the reader reports on
another.

Scope is computed from ``config.prediction_market.tracked_categories``:
each category's ``min_volume_24h_usd`` override (or the
``tracked_default_min_volume_24h_usd`` default) is applied against the
latest ``prediction_market_snapshots.volume_24h_usd`` per contract.
Contracts whose ``resolution_date`` has passed ``as_of`` are excluded.

ISO 8601 ``Z``-suffix lex comparison is safe — both Polymarket and Kalshi
emit consistent UTC formats, and SQLite's TEXT ordering matches calendar
ordering for that format.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.persistence.models import (
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)


def _format_as_of(as_of: datetime) -> str:
    """Format a tz-aware datetime as ISO 8601 ``Z``-suffixed UTC."""
    return as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_prediction_market_scope(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> tuple[str, ...]:
    """Return the contract IDs in scope for this distillation invocation.

    Empty ``tracked_categories`` short-circuits to an empty tuple — the
    orchestrator threads ``()`` through and both consumers become no-ops.
    Otherwise:

    * SQL filters on ``category IN tracked_categories`` plus
      ``resolution_date IS NULL OR resolution_date > as_of``.
    * Latest ``prediction_market_snapshots.volume_24h_usd`` per contract is
      joined via a window-ranked subquery.
    * Contracts with no snapshot or volume below the per-category floor
      (or the default when no override is set) are dropped.

    Returns the surviving contract IDs in deterministic ascending order.
    """
    pm = config.prediction_market
    if not pm.tracked_categories:
        return ()

    as_of_iso = _format_as_of(as_of)
    categories = tuple(pm.tracked_categories.keys())

    # Latest snapshot per contract via window function.
    ranked = (
        select(
            PredictionMarketSnapshots.contract_id.label("contract_id"),
            PredictionMarketSnapshots.volume_24h_usd.label("volume_24h_usd"),
            func.row_number()
            .over(
                partition_by=PredictionMarketSnapshots.contract_id,
                order_by=PredictionMarketSnapshots.snapshot_ts.desc(),
            )
            .label("rn"),
        )
    ).subquery()
    latest_snap = (
        select(ranked.c.contract_id, ranked.c.volume_24h_usd).where(ranked.c.rn == 1).subquery()
    )

    rows = session.execute(
        select(
            PredictionMarketContracts.contract_id,
            PredictionMarketContracts.category,
            latest_snap.c.volume_24h_usd,
        )
        .join(
            latest_snap,
            PredictionMarketContracts.contract_id == latest_snap.c.contract_id,
        )
        .where(
            PredictionMarketContracts.category.in_(categories),
            or_(
                PredictionMarketContracts.resolution_date.is_(None),
                PredictionMarketContracts.resolution_date > as_of_iso,
            ),
        )
    ).all()

    default_floor = float(pm.tracked_default_min_volume_24h_usd)
    in_scope: list[str] = []
    for row in rows:
        override = pm.tracked_categories[row.category].min_volume_24h_usd
        floor = float(override) if override is not None else default_floor
        volume = row.volume_24h_usd
        if volume is None:
            continue
        if float(volume) >= floor:
            in_scope.append(row.contract_id)
    return tuple(sorted(in_scope))
