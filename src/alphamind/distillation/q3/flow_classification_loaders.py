"""IO shell for q3 options flow classification (ALP-467).

Owns every DB read the flow-classification pipeline needs. The pure
compute lives in :mod:`.flow_classification_compute`. The orchestrator
constructs one :class:`DistillationRepository` at the composition root
and threads it through these loaders before the pure compute runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from alphamind.distillation._repository import (
    DistillationRepository,
    TickerADVRow,
)
from alphamind.distillation.q3.flow_classification_compute import (
    FlowClassificationInputs,
    PerContractSnapshotPair,
    PutFlowIntentInputs,
)


def load_flow_classification_inputs(
    repository: DistillationRepository,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
) -> FlowClassificationInputs:
    """Pre-load every contract + snapshot pair the compute needs.

    Mirrors the original :func:`classify_options_flow` queries but front-loads
    them so the compute slice is pure. Contracts with no today-snapshot are
    still surfaced (with ``today_snapshot=None``) so the compute's
    "skip-zero-volume" decision is made in one place.

    Today + prior snapshots are loaded per-underlying in two index-friendly
    batched queries via
    :meth:`DistillationRepository.load_options_snapshot_pairs_for_underlying`,
    avoiding the N+1 per-contract lookup that previously degraded to a
    partial scan over the 13.5M-row snapshots table.
    """
    per_ticker_pairs: dict[str, tuple[PerContractSnapshotPair, ...]] = {}
    for ticker in ticker_scope:
        contracts = repository.load_options_contracts_for_underlying(underlying=ticker)
        pair_map = repository.load_options_snapshot_pairs_for_underlying(
            underlying=ticker,
            as_of=as_of,
        )
        per_ticker_pairs[ticker] = tuple(
            PerContractSnapshotPair(
                contract=contract,
                today_snapshot=pair_map.get(contract.contract_ticker, (None, None))[0],
                prior_snapshot=pair_map.get(contract.contract_ticker, (None, None))[1],
            )
            for contract in contracts
        )
    return FlowClassificationInputs(per_ticker_pairs=per_ticker_pairs)


def load_put_flow_intent_inputs(
    repository: DistillationRepository,
    *,
    ticker_scope: Sequence[str],
    system_long_positions: Mapping[str, int],
) -> PutFlowIntentInputs:
    """Pre-load per-ticker ADV rows the put-flow-intent compute needs."""
    adv_by_ticker: dict[str, TickerADVRow | None] = {
        ticker: repository.load_ticker_adv(ticker=ticker) for ticker in ticker_scope
    }
    return PutFlowIntentInputs(
        system_long_positions=dict(system_long_positions),
        adv_by_ticker=adv_by_ticker,
    )


__all__ = [
    "load_flow_classification_inputs",
    "load_put_flow_intent_inputs",
]
