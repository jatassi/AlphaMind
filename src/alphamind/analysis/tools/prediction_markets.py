"""prediction_markets on-demand tool — ALP-247.

Queries ``prediction_market_contracts`` filtered by keyword / category,
joins the two latest ``prediction_market_snapshots`` per contract to
compute the 24h probability delta, and returns up to ``max_results``
contracts ordered by ``volume_24h_usd`` descending.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from alphamind._kernel.clock import Clock, RealClock
from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, parse_iso
from alphamind.persistence.models import PredictionMarketContracts, PredictionMarketSnapshots

__all__ = [
    "PredictionMarketContract",
    "PredictionMarketsInput",
    "PredictionMarketsOutput",
    "prediction_markets_factory",
]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PredictionMarketsInput(BaseModel, frozen=True):
    """Input for the prediction_markets tool.

    At least one of ``query`` or ``categories`` must be non-empty.
    Empty-both is handled at call time by returning UNAVAILABLE rather than raising.
    """

    query: str = ""
    categories: tuple[str, ...] = ()
    max_results: int = 10


class PredictionMarketContract(BaseModel, frozen=True):
    contract_id: str
    description: str
    platform: str
    category: str
    current_probability: float
    prob_24h_ago: float | None
    prob_delta_24h_pp: float | None
    volume_24h_usd: float | None
    expiration: str | None
    resolution_outcome: str | None


class PredictionMarketsOutput(ToolEnvelope, frozen=True):
    contracts: tuple[PredictionMarketContract, ...]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _query_prediction_markets(
    session: Session, inp: PredictionMarketsInput, clock: Clock
) -> PredictionMarketsOutput:
    now = clock.now()

    if not inp.query and not inp.categories:
        return PredictionMarketsOutput(
            contracts=(), data_freshness=now, quality=ToolQuality.UNAVAILABLE
        )

    conditions = []
    if inp.query:
        conditions.append(PredictionMarketContracts.description.ilike(f"%{inp.query}%"))
    if inp.categories:
        conditions.append(PredictionMarketContracts.category.in_(inp.categories))

    stmt = select(
        PredictionMarketContracts.contract_id,
        PredictionMarketContracts.platform,
        PredictionMarketContracts.description,
        PredictionMarketContracts.category,
        PredictionMarketContracts.resolution_date,
        PredictionMarketContracts.resolution_outcome,
    ).where(conditions[0] if len(conditions) == 1 else or_(*conditions))

    contract_rows = session.execute(stmt).all()

    if not contract_rows:
        return PredictionMarketsOutput(
            contracts=(), data_freshness=now, quality=ToolQuality.UNAVAILABLE
        )

    contract_ids = [row.contract_id for row in contract_rows]

    snapshot_rows = session.execute(
        select(
            PredictionMarketSnapshots.contract_id,
            PredictionMarketSnapshots.snapshot_ts,
            PredictionMarketSnapshots.yes_probability,
            PredictionMarketSnapshots.volume_24h_usd,
        )
        .where(PredictionMarketSnapshots.contract_id.in_(contract_ids))
        .order_by(
            PredictionMarketSnapshots.contract_id,
            PredictionMarketSnapshots.snapshot_ts.desc(),
        )
    ).all()

    # Keep at most 2 snapshots per contract (most-recent first) for 24h delta
    snapshots_by_contract: dict[str, list[tuple[float, float | None]]] = {}
    for snap in snapshot_rows:
        bucket = snapshots_by_contract.setdefault(snap.contract_id, [])
        if len(bucket) < 2:
            bucket.append((snap.yes_probability, snap.volume_24h_usd))

    contracts: list[tuple[float, PredictionMarketContract]] = []
    for row in contract_rows:
        snaps = snapshots_by_contract.get(row.contract_id, [])
        if not snaps:
            continue

        current_prob, volume = snaps[0]
        prob_24h_ago: float | None = None
        prob_delta: float | None = None
        if len(snaps) >= 2:
            prob_24h_ago = snaps[1][0]
            prob_delta = round(current_prob - prob_24h_ago, 6)

        contracts.append(
            (
                volume or 0.0,
                PredictionMarketContract(
                    contract_id=row.contract_id,
                    description=row.description,
                    platform=row.platform,
                    category=row.category,
                    current_probability=current_prob,
                    prob_24h_ago=prob_24h_ago,
                    prob_delta_24h_pp=prob_delta,
                    volume_24h_usd=volume,
                    expiration=row.resolution_date,
                    resolution_outcome=row.resolution_outcome,
                ),
            )
        )

    contracts.sort(key=lambda x: -x[0])
    result = tuple(c for _, c in contracts[: inp.max_results])

    freshness_row = session.execute(
        select(PredictionMarketSnapshots.ingested_at)
        .order_by(PredictionMarketSnapshots.ingested_at.desc())
        .limit(1)
    ).scalar()
    freshness = parse_iso(freshness_row) if freshness_row else now

    quality = ToolQuality.COMPLETE if result else ToolQuality.UNAVAILABLE
    return PredictionMarketsOutput(contracts=result, data_freshness=freshness, quality=quality)


def prediction_markets_factory(
    session: Session,
    *,
    clock: Clock | None = None,
) -> Callable[[PredictionMarketsInput], PredictionMarketsOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry.

    ``clock`` defaults to :class:`RealClock`; tests pass a fake to control
    the timestamp deterministically (ALP-474).
    """
    resolved_clock: Clock = clock if clock is not None else RealClock()

    def _call(inp: PredictionMarketsInput) -> PredictionMarketsOutput:
        return _query_prediction_markets(session, inp, resolved_clock)

    return _call
