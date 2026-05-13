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
    """
    per_ticker_pairs: dict[str, tuple[PerContractSnapshotPair, ...]] = {}
    for ticker in ticker_scope:
        contracts = repository.load_options_contracts_for_underlying(underlying=ticker)
        pairs: list[PerContractSnapshotPair] = []
        for contract in contracts:
            today = repository.load_latest_options_snapshot_at(
                contract_ticker=contract.contract_ticker,
                as_of=as_of,
            )
            prior = repository.load_prior_options_snapshot(
                contract_ticker=contract.contract_ticker,
                as_of=as_of,
            )
            pairs.append(
                PerContractSnapshotPair(
                    contract=contract,
                    today_snapshot=today,
                    prior_snapshot=prior,
                )
            )
        per_ticker_pairs[ticker] = tuple(pairs)
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
