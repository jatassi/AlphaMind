"""IO shell for the prediction-market deltas classifier (ALP-487).

Owns every DB read the deltas compute needs. The pure compute lives in
:mod:`.prediction_market_deltas_compute`. The reads project through the
pilot-scoped
:class:`alphamind.distillation._repository.DistillationRepository`
Protocol; no raw ``Session`` use here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from alphamind.distillation._repository import (
    ContractCurrentStateRow,
    ContractHistoryEntry,
    ContractMetadataRow,
    DistillationRepository,
)
from alphamind.distillation.qualitative.prediction_market_deltas_compute import (
    PredictionMarketDeltasInputs,
)


def _parse_iso_utc(ts: str) -> datetime:
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts[:-1]).replace(tzinfo=UTC)
    return datetime.fromisoformat(ts)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_prediction_market_deltas_inputs(
    repository: DistillationRepository,
    *,
    contract_scope: Sequence[str],
    as_of: str,
    history_days: int,
) -> PredictionMarketDeltasInputs:
    """Pre-load every per-contract input the deltas compute consumes."""
    scope = tuple(contract_scope)
    freshness_ts = _parse_iso_utc(as_of)
    if not scope:
        return PredictionMarketDeltasInputs(
            contract_scope=(),
            freshness_ts=freshness_ts,
            current_state_by_contract={},
            history_by_contract={},
            metadata_by_contract={},
            volume_liquidity_by_contract={},
        )

    history_range_start = _format_iso_utc(freshness_ts - timedelta(days=history_days))
    current_state_by_contract: dict[str, ContractCurrentStateRow | None] = {}
    history_by_contract: dict[str, tuple[ContractHistoryEntry, ...]] = {}
    metadata_by_contract: dict[str, ContractMetadataRow | None] = {}
    volume_liquidity_by_contract: dict[str, tuple[float, float]] = {}

    for contract_id in scope:
        current = repository.load_contract_current_state(contract_id=contract_id, as_of=as_of)
        current_state_by_contract[contract_id] = current
        if current is None:
            # No current state means the contract is skipped by the compute;
            # pre-loading the other fields would be wasted IO.
            continue
        history_by_contract[contract_id] = repository.load_contract_history(
            contract_id=contract_id, range_start=history_range_start, range_end=as_of
        )
        metadata_by_contract[contract_id] = repository.load_contract_metadata(
            contract_id=contract_id
        )
        volume_liquidity_by_contract[contract_id] = (
            repository.load_contract_24h_volume_and_liquidity(contract_id=contract_id, as_of=as_of)
        )

    return PredictionMarketDeltasInputs(
        contract_scope=scope,
        freshness_ts=freshness_ts,
        current_state_by_contract=current_state_by_contract,
        history_by_contract=history_by_contract,
        metadata_by_contract=metadata_by_contract,
        volume_liquidity_by_contract=volume_liquidity_by_contract,
    )


__all__ = ["load_prediction_market_deltas_inputs"]
