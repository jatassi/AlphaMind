"""Q3 options flow classification — thin orchestration shim (story ALP-467).

Public surface preserved from the pre-split layout: existing callers pass a
``Session`` and receive the same return shapes. Internally, the shim wraps
the session in a :class:`SqlDistillationRepository`, calls the new loader
to pre-load the snapshot pairs, and delegates to the pure compute slice in
:mod:`.flow_classification_compute`.

The pure-compute and pure-classification helpers (
:func:`classify_index_vs_sector_flow` was always pure) are re-exported
here unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy.orm import Session

from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.q3.flow_classification_compute import (
    SNAPSHOT_OI_DELTA_ATTRIBUTION,
    PutFlowIntent,
    TickerOptionsFlow,
    compute_options_flow,
    compute_put_flow_intent,
)
from alphamind.distillation.q3.flow_classification_loaders import (
    load_flow_classification_inputs,
    load_put_flow_intent_inputs,
)

if TYPE_CHECKING:
    from alphamind.distillation.q3.pair_trade import FlowZScore


def classify_options_flow(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
) -> dict[str, TickerOptionsFlow]:
    """Aggregate today's options flow via the BTO/STO OI-delta heuristic.

    Session-accepting shim that constructs a repository, pre-loads every
    contract + snapshot pair the compute needs, then delegates to
    :func:`compute_options_flow`.
    """
    repository = SqlDistillationRepository(session)
    inputs = load_flow_classification_inputs(repository, ticker_scope=ticker_scope, as_of=as_of)
    return compute_options_flow(inputs)


def classify_put_flow_intent(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    system_long_positions: Mapping[str, int],
    protective_holding_pct_of_adv: float,
) -> dict[str, PutFlowIntent]:
    """Session-accepting shim that delegates to the pure put-flow-intent compute."""
    repository = SqlDistillationRepository(session)
    inputs = load_put_flow_intent_inputs(
        repository,
        ticker_scope=ticker_scope,
        system_long_positions=system_long_positions,
    )
    return compute_put_flow_intent(
        inputs, protective_holding_pct_of_adv=protective_holding_pct_of_adv
    )


# ---------------------------------------------------------------------------
# Index hedging vs. sector conviction classification — already pure
# ---------------------------------------------------------------------------


IndexVsSectorLabel = Literal[
    "macro_hedging",
    "sector_specific_concern",
    "index_hedging_no_sector_view",
]


@dataclass(frozen=True, slots=True)
class IndexVsSectorClassification:
    """One ``index_vs_sector_classification`` finding."""

    label: IndexVsSectorLabel
    index_max_put_z: float
    sector_max_put_z: float


def classify_index_vs_sector_flow(
    *,
    index_flow_zscores: Mapping[str, FlowZScore],
    sector_etf_flow_zscores: Mapping[str, FlowZScore],
    sigma_threshold: float,
) -> IndexVsSectorClassification | None:
    """Classify the relationship between index and sector-ETF put flow."""
    index_max = max(
        (score.put_bto_z for score in index_flow_zscores.values()),
        default=0.0,
    )
    sector_max = max(
        (score.put_bto_z for score in sector_etf_flow_zscores.values()),
        default=0.0,
    )
    index_spike = index_max >= sigma_threshold
    sector_spike = sector_max >= sigma_threshold
    if not index_spike and not sector_spike:
        return None
    label: IndexVsSectorLabel
    if index_spike and sector_spike:
        label = "macro_hedging"
    elif sector_spike:
        label = "sector_specific_concern"
    else:
        label = "index_hedging_no_sector_view"
    return IndexVsSectorClassification(
        label=label,
        index_max_put_z=index_max,
        sector_max_put_z=sector_max,
    )


__all__ = [
    "SNAPSHOT_OI_DELTA_ATTRIBUTION",
    "IndexVsSectorClassification",
    "IndexVsSectorLabel",
    "PutFlowIntent",
    "TickerOptionsFlow",
    "classify_index_vs_sector_flow",
    "classify_options_flow",
    "classify_put_flow_intent",
]
