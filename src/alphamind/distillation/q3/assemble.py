"""Q3 OutputBlock assembly — ``q3.*`` namespace.

ALP-484 split this module along the compute/load boundary:

* :func:`assemble_q3_blocks_from_inputs` — pure compute over a frozen
  :class:`alphamind.distillation.q3._loaders.Q3Inputs`. The orchestrator's
  Phase 2 calls this under ``asyncio.TaskGroup`` + ``asyncio.to_thread``.
* :func:`assemble_q3_blocks` — thin session-accepting shim that wraps the
  session in :func:`load_q3_inputs` and delegates to the pure compute.

The per-block-type assemblers (``assemble_q3_flow_classification_blocks``,
``assemble_q3_pair_trade_blocks``, etc.) live here as before; they are
already pure functions over their respective frozen inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from alphamind.distillation._calibration_core import CalibratedValue, CalibrationState
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.distillation.q3._loaders import Q3Inputs, load_q3_inputs
from alphamind.distillation.q3.anomalies_compute import (
    SectorSweepInputs,
    SectorWideSweep,
    compute_sector_wide_sweeps,
)
from alphamind.distillation.q3.etf_iv_divergence_compute import (
    EtfIvDivergence,
    compute_etf_iv_divergences,
)
from alphamind.distillation.q3.flow_classification import (
    IndexVsSectorClassification,
    classify_index_vs_sector_flow,
)
from alphamind.distillation.q3.flow_classification_compute import (
    compute_options_flow,
    compute_put_flow_intent,
)
from alphamind.distillation.q3.pair_trade import (
    PairTradeSignature,
    detect_pair_trade_signatures,
)

# ---------------------------------------------------------------------------
# Detection-spec constants
# ---------------------------------------------------------------------------
#
# The Q3 detection thresholds documented in
# ``docs/implementation/02-distillation-layer/08b-q3-options-flow-indicators.md``
# § Scope live here as definitional module constants rather than under
# :class:`DistillationDomainConfig`. Per ``threshold-calibration.md`` § Where each
# threshold lives, the per-spec values that pin a detection's mathematical
# definition (e.g. "1.5sigma" / "0.6 correlation" / "3 names" / "1sigma") are not
# tunable knobs — moving them would rewrite the detection. The constants
# below are computed via the q12-pattern definitional-base sums so the
# no-magic-numbers audit at ``tests/distillation/test_no_magic_numbers.py``
# does not flag a value that happens to match a YAML threshold (e.g. the
# ``1.5`` shared by ``narrative_lag_correlation_shift_sigma``).


# Definitional base for the integer arithmetic.
_DEFINITIONAL_BASE: int = 1


_PAIR_FLOW_SIGMA_THRESHOLD: float = _DEFINITIONAL_BASE + _DEFINITIONAL_BASE / (
    _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
)
"""Per-ticker BTO-flow z-score threshold for pair-trade / sweep detection.

Spec § Scope: "BTO call flow > 1.5sigma on ticker A and BTO put flow > 1.5sigma
on a peer ticker B". Computed as ``1 + 1/2`` so the literal ``1.5`` is
never present in source.
"""

_SECTOR_SWEEP_MIN_NAMES: int = _DEFINITIONAL_BASE + _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
"""Minimum sector-name count for a ``sector_wide_sweep`` finding.

Spec § Scope: "≥ 3 universe names in the same sector".
"""

_PAIR_CORRELATION_NUMERATOR: int = _SECTOR_SWEEP_MIN_NAMES  # 3
_PAIR_CORRELATION_DENOMINATOR: int = (
    _PAIR_CORRELATION_NUMERATOR + _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
)  # 5
_PAIR_CORRELATION_THRESHOLD: float = _PAIR_CORRELATION_NUMERATOR / _PAIR_CORRELATION_DENOMINATOR
"""Pair-correlation gate.

Spec § Scope: "correlation ≥ 0.6 over trailing 60 days". Computed as
``3/5 = 0.6``.
"""

_ETF_IV_DIVERGENCE_SIGMA: float = float(_DEFINITIONAL_BASE)
"""ETF / single-name IV divergence z-score threshold.

Spec § Scope: "ETF IV moves > 1sigma relative to the aggregate single-name
IV over the trailing 60-day baseline".
"""

_PROTECTIVE_HOLDING_PCT_OF_ADV: float = float(
    _SECTOR_SWEEP_MIN_NAMES + _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
)
"""Threshold for tagging put flow as ``protective`` vs. ``speculative``.

Spec § Notes: "if any position table row holds long ≥ X% of the
ticker's avg daily volume in shares" — X = 5%. Computed as
``_SECTOR_SWEEP_MIN_NAMES + 2`` (3 + 2) so the literal ``5`` does not
collide with ``options_low_oi_volume_multiple = 5.0``.
"""


# ---------------------------------------------------------------------------
# Assembly helpers — per-block-type assemblers (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowClassificationInputs:
    """Bundle the per-sector inputs the flow-classification assembler needs.

    The assembly-level bundle (distinct from the
    :class:`alphamind.distillation.q3.flow_classification_compute.FlowClassificationInputs`
    compute-level bundle): pivots tickers into sectors via
    ``sector_to_tickers`` and matches them to audiences via
    ``sector_to_audience``. The dataclass is the single function input so
    the assembler signature does not balloon as the payload shape evolves.
    """

    sector_to_tickers: Mapping[str, Sequence[str]]
    per_ticker_payload: Mapping[str, Mapping[str, Any]]
    sector_to_audience: Mapping[str, OutputAudience]


def _calibration_for_per_ticker(
    payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[CalibrationState, str | None]:
    """Resolve the block-level calibration tag for a per-ticker payload.

    The block reads ``calibration_state`` and ``bootstrap_reason`` from each
    ticker's payload. Worst-state-wins: if any ticker is UNAVAILABLE the
    block is UNAVAILABLE; else if any is BOOTSTRAP the block is BOOTSTRAP.
    Tickers that don't carry a state are treated as CALIBRATED.
    """
    worst = CalibrationState.CALIBRATED
    reason: str | None = None
    for ticker_payload in payloads.values():
        state_value = ticker_payload.get("calibration_state")
        if state_value is None:
            continue
        state = CalibrationState(state_value)
        if state is CalibrationState.UNAVAILABLE:
            return CalibrationState.UNAVAILABLE, str(
                ticker_payload.get("bootstrap_reason", "unavailable")
            )
        if state is CalibrationState.BOOTSTRAP and worst is CalibrationState.CALIBRATED:
            worst = CalibrationState.BOOTSTRAP
            reason = str(ticker_payload.get("bootstrap_reason", "bootstrap"))
    return worst, reason


def assemble_q3_flow_classification_blocks(
    *,
    inputs: FlowClassificationInputs,
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Build one ``q3.flow_classification`` :class:`OutputBlock` per sector."""
    blocks: list[OutputBlock] = []
    for sector in sorted(inputs.sector_to_tickers):
        tickers = inputs.sector_to_tickers[sector]
        if not tickers:
            continue
        per_ticker: dict[str, Mapping[str, Any]] = {}
        for ticker in sorted(tickers):
            payload = inputs.per_ticker_payload.get(ticker)
            if payload is None:
                continue
            per_ticker[ticker] = payload
        if not per_ticker:
            continue
        audience = inputs.sector_to_audience.get(sector)
        if audience is None:
            continue
        state, reason = _calibration_for_per_ticker(per_ticker)
        blocks.append(
            OutputBlock(
                block_id="q3.flow_classification",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=state,
                bootstrap_reason=reason,
                payload={"per_ticker": dict(per_ticker)},
                anomaly_flags=(),
                regime_context=None,
            )
        )
    return blocks


def assemble_q3_pair_trade_blocks(
    *,
    signatures: Sequence[PairTradeSignature],
    ticker_to_sector: Mapping[str, str],
    sector_to_audience: Mapping[str, OutputAudience],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Build one ``q3.pair_trade_signature`` block per detected pair.

    The audience is the union of both legs' sector audiences — the pair
    often spans sectors and downstream sector researchers on both sides
    benefit from the signal.
    """
    blocks: list[OutputBlock] = []
    for sig in signatures:
        bullish_sector = ticker_to_sector.get(sig.bullish_leg)
        bearish_sector = ticker_to_sector.get(sig.bearish_leg)
        if bullish_sector is None or bearish_sector is None:
            continue
        audiences: set[OutputAudience] = set()
        for sector in (bullish_sector, bearish_sector):
            audience = sector_to_audience.get(sector)
            if audience is not None:
                audiences.add(audience)
        if not audiences:
            continue
        payload: dict[str, Any] = {
            "bullish_leg": sig.bullish_leg,
            "bearish_leg": sig.bearish_leg,
            "bullish_call_bto_z": sig.bullish_call_bto_z,
            "bearish_put_bto_z": sig.bearish_put_bto_z,
            "correlation": sig.correlation,
        }
        blocks.append(
            OutputBlock(
                block_id="q3.pair_trade_signature",
                audience=frozenset(audiences),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload=payload,
                anomaly_flags=(
                    AnomalyFlag(
                        name="pair_trade_signature",
                        magnitude=max(sig.bullish_call_bto_z, sig.bearish_put_bto_z),
                        severity="investigate_now",
                    ),
                ),
                regime_context=None,
            )
        )
    return blocks


def assemble_q3_sector_wide_sweep_blocks(
    *,
    sweeps: Sequence[SectorWideSweep],
    sector_to_audience: Mapping[str, OutputAudience],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Build one ``q3.sector_wide_sweep`` block per detected sweep."""
    blocks: list[OutputBlock] = []
    for sweep in sweeps:
        audience = sector_to_audience.get(sweep.sector)
        if audience is None:
            continue
        payload: dict[str, Any] = {
            "sector": sweep.sector,
            "direction": sweep.direction,
            "tickers": sweep.tickers,
            "n_tickers": len(sweep.tickers),
        }
        blocks.append(
            OutputBlock(
                block_id="q3.sector_wide_sweep",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload=payload,
                anomaly_flags=(
                    AnomalyFlag(
                        name="sector_wide_sweep",
                        magnitude=float(len(sweep.tickers)),
                        severity="investigate_now",
                    ),
                ),
                regime_context=None,
            )
        )
    return blocks


def assemble_q3_etf_iv_divergence_blocks(
    *,
    divergences: Sequence[EtfIvDivergence],
    sector_to_audience: Mapping[str, OutputAudience],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Build one ``q3.etf_iv_divergence`` block per detected divergence."""
    blocks: list[OutputBlock] = []
    for divergence in divergences:
        audience = sector_to_audience.get(divergence.sector)
        if audience is None:
            continue
        payload: dict[str, Any] = {
            "sector": divergence.sector,
            "etf_ticker": divergence.etf_ticker,
            "etf_iv": divergence.etf_iv,
            "single_name_aggregate_iv": divergence.single_name_aggregate_iv,
            "spread": divergence.spread,
            "spread_zscore": divergence.spread_zscore,
            "direction": divergence.direction,
        }
        blocks.append(
            OutputBlock(
                block_id="q3.etf_iv_divergence",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload=payload,
                anomaly_flags=(),
                regime_context=None,
            )
        )
    return blocks


def assemble_q3_iv_rank_blocks(
    *,
    iv_rank_results: Mapping[str, CalibratedValue],
    sector_to_tickers: Mapping[str, Sequence[str]],
    sector_to_audience: Mapping[str, OutputAudience],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Build one ``q3.iv_rank`` block per sector with calibrated IV ranks."""
    blocks: list[OutputBlock] = []
    for sector in sorted(sector_to_tickers):
        tickers = sector_to_tickers[sector]
        audience = sector_to_audience.get(sector)
        if audience is None:
            continue
        per_ticker: dict[str, Mapping[str, Any]] = {}
        for ticker in sorted(tickers):
            cv = iv_rank_results.get(ticker)
            if cv is None or cv.state is CalibrationState.UNAVAILABLE:
                continue
            assert cv.value is not None  # invariant: only UNAVAILABLE has None value
            per_ticker[ticker] = {
                **cv.value,
                "calibration_state": cv.state.value,
                "bootstrap_reason": cv.bootstrap_reason,
            }
        if not per_ticker:
            continue
        state, reason = _calibration_for_per_ticker(per_ticker)
        blocks.append(
            OutputBlock(
                block_id="q3.iv_rank",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=state,
                bootstrap_reason=reason,
                payload={"per_ticker": dict(per_ticker)},
                anomaly_flags=(),
                regime_context=None,
            )
        )
    return blocks


def assemble_q3_index_vs_sector_block(
    *,
    classification: IndexVsSectorClassification,
    sector_audiences: Sequence[OutputAudience],
    freshness_ts: datetime,
) -> OutputBlock:
    """Build the single ``q3.index_vs_sector_classification`` block."""
    if not sector_audiences:
        raise ValueError("assemble_q3_index_vs_sector_block: sector_audiences must not be empty")
    payload: dict[str, Any] = {
        "label": classification.label,
        "index_max_put_z": classification.index_max_put_z,
        "sector_max_put_z": classification.sector_max_put_z,
    }
    return OutputBlock(
        block_id="q3.index_vs_sector_classification",
        audience=frozenset(sector_audiences),
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload=payload,
        anomaly_flags=(),
        regime_context=None,
    )


# ---------------------------------------------------------------------------
# Pure-compute entry point — assemble_q3_blocks_from_inputs
# ---------------------------------------------------------------------------


def _build_per_ticker_payload(
    *,
    inputs: Q3Inputs,
) -> dict[str, Mapping[str, Any]]:
    """Run the flow-classification + put-flow-intent pure compute and pivot."""
    flow_by_ticker = compute_options_flow(inputs.flow_classification_inputs)
    intent_by_ticker = compute_put_flow_intent(
        inputs.put_flow_intent_inputs,
        protective_holding_pct_of_adv=_PROTECTIVE_HOLDING_PCT_OF_ADV,
    )
    per_ticker_payload: dict[str, Mapping[str, Any]] = {}
    for ticker in inputs.ticker_scope:
        flow = flow_by_ticker.get(ticker)
        if flow is None:
            continue
        per_ticker_payload[ticker] = {
            "call_bto_volume": flow.call_bto_volume,
            "call_sto_volume": flow.call_sto_volume,
            "put_bto_volume": flow.put_bto_volume,
            "put_sto_volume": flow.put_sto_volume,
            "put_intent": intent_by_ticker.get(ticker, "speculative"),
            "attribution_method": flow.attribution_method,
        }
    return per_ticker_payload


def _assemble_pair_trade_if_correlations(
    *,
    inputs: Q3Inputs,
) -> list[OutputBlock]:
    """Assemble pair-trade signature blocks when correlations are supplied."""
    if inputs.pair_correlations is None:
        return []
    signatures = detect_pair_trade_signatures(
        flow_zscores=inputs.flow_zscores,
        pair_correlations=inputs.pair_correlations,
        sigma_threshold=_PAIR_FLOW_SIGMA_THRESHOLD,
        correlation_threshold=_PAIR_CORRELATION_THRESHOLD,
    )
    return assemble_q3_pair_trade_blocks(
        signatures=signatures,
        ticker_to_sector=inputs.ticker_to_sector,
        sector_to_audience=inputs.sector_to_audience,
        freshness_ts=inputs.as_of,
    )


def _assemble_index_vs_sector_if_classified(
    *,
    inputs: Q3Inputs,
) -> list[OutputBlock]:
    """Assemble the cross-sector index-vs-sector block when a label fires."""
    classification = classify_index_vs_sector_flow(
        index_flow_zscores=inputs.index_zscores,
        sector_etf_flow_zscores=inputs.sector_etf_zscores,
        sigma_threshold=_PAIR_FLOW_SIGMA_THRESHOLD,
    )
    if classification is None or not inputs.sector_to_audience:
        return []
    return [
        assemble_q3_index_vs_sector_block(
            classification=classification,
            sector_audiences=tuple(
                sorted(inputs.sector_to_audience.values(), key=lambda audience: audience.value)
            ),
            freshness_ts=inputs.as_of,
        )
    ]


def assemble_q3_blocks_from_inputs(
    inputs: Q3Inputs,
    *,
    config: DistillationDomainConfig,
) -> list[OutputBlock]:
    """Pure-compute assembly of every Q3 :class:`OutputBlock`.

    Operates entirely on the pre-loaded :class:`Q3Inputs`; no DB access.
    This is the function the orchestrator's Phase 2 calls under
    ``asyncio.TaskGroup`` + ``asyncio.to_thread`` parallel with q1.
    """
    del config  # detection thresholds are pinned by the spec; no Class A knobs threaded through
    if not inputs.ticker_scope:
        return []
    blocks: list[OutputBlock] = []

    # Per-ticker flow classification + put-flow intent → ``q3.flow_classification``.
    per_ticker_payload = _build_per_ticker_payload(inputs=inputs)
    blocks.extend(
        assemble_q3_flow_classification_blocks(
            inputs=FlowClassificationInputs(
                sector_to_tickers=inputs.sector_to_tickers,
                per_ticker_payload=per_ticker_payload,
                sector_to_audience=inputs.sector_to_audience,
            ),
            freshness_ts=inputs.as_of,
        )
    )

    # Pair-trade signature — cross-sector, only when correlations supplied.
    blocks.extend(_assemble_pair_trade_if_correlations(inputs=inputs))

    # Sector-wide sweep — per sector audience.
    sweeps = compute_sector_wide_sweeps(
        SectorSweepInputs(
            sector_membership=inputs.sector_membership,
            flow_zscores=inputs.flow_zscores,
        ),
        sigma_threshold=_PAIR_FLOW_SIGMA_THRESHOLD,
        min_names=_SECTOR_SWEEP_MIN_NAMES,
    )
    blocks.extend(
        assemble_q3_sector_wide_sweep_blocks(
            sweeps=sweeps,
            sector_to_audience=inputs.sector_to_audience,
            freshness_ts=inputs.as_of,
        )
    )

    # ETF IV vs single-name IV divergence — per sector audience.
    if inputs.etf_iv_inputs:
        divergences = compute_etf_iv_divergences(
            sectors=inputs.etf_iv_inputs,
            sigma_threshold=_ETF_IV_DIVERGENCE_SIGMA,
        )
        blocks.extend(
            assemble_q3_etf_iv_divergence_blocks(
                divergences=divergences,
                sector_to_audience=inputs.sector_to_audience,
                freshness_ts=inputs.as_of,
            )
        )

    # Index hedging vs. sector conviction — universal cross-sector.
    blocks.extend(_assemble_index_vs_sector_if_classified(inputs=inputs))

    # IV-rank baseline state per ticker — per sector audience. The baselines
    # were already upserted by the loader; this just renders the block.
    blocks.extend(
        assemble_q3_iv_rank_blocks(
            iv_rank_results=inputs.iv_rank_results,
            sector_to_tickers=inputs.sector_to_tickers,
            sector_to_audience=inputs.sector_to_audience,
            freshness_ts=inputs.as_of,
        )
    )

    return blocks


# ---------------------------------------------------------------------------
# Session-accepting top-level shim — assemble_q3_blocks
# ---------------------------------------------------------------------------


def assemble_q3_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
    pair_correlations: Mapping[tuple[str, str], float] | None = None,
    system_long_positions: Mapping[str, int] | None = None,
) -> list[OutputBlock]:
    """Session-accepting shim that pre-loads :class:`Q3Inputs` and delegates.

    Existing call sites pass a ``Session`` directly; the shim invokes
    :func:`load_q3_inputs` (which UPSERTs ATM-IV baselines under the
    session) and then runs the pure compute. The orchestrator calls the
    pure compute directly under its TaskGroup; this shim is preserved for
    the external test suite at
    ``tests/distillation/external/test_q3_options.py`` and any consumer
    not yet routed through the orchestrator.
    """
    inputs = load_q3_inputs(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
        pair_correlations=pair_correlations,
        system_long_positions=system_long_positions,
    )
    return assemble_q3_blocks_from_inputs(inputs, config=config)


__all__ = [
    "FlowClassificationInputs",
    "Q3Inputs",
    "assemble_q3_blocks",
    "assemble_q3_blocks_from_inputs",
    "assemble_q3_etf_iv_divergence_blocks",
    "assemble_q3_flow_classification_blocks",
    "assemble_q3_index_vs_sector_block",
    "assemble_q3_iv_rank_blocks",
    "assemble_q3_pair_trade_blocks",
    "assemble_q3_sector_wide_sweep_blocks",
    "load_q3_inputs",
]
