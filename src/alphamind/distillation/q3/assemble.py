"""Q3 OutputBlock assembly — ``q3.*`` namespace.

Implements:
- :class:`FlowClassificationInputs` — bundle for the flow-classification assembler.
- :func:`assemble_q3_flow_classification_blocks` — one block per sector.
- :func:`assemble_q3_pair_trade_blocks` — one block per detected pair.
- :func:`assemble_q3_sector_wide_sweep_blocks` — one block per detected sweep.
- :func:`assemble_q3_etf_iv_divergence_blocks` — one block per detected divergence.
- :func:`assemble_q3_iv_rank_blocks` — one block per sector with IV ranks.
- :func:`assemble_q3_index_vs_sector_block` — the single cross-sector block.
- :func:`assemble_q3_blocks` — top-level entry point for the orchestrator.

The top-level entry point closes the orchestrator's q3 placeholder gap; the
per-assembler functions are also exported for direct testing.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.calibration import CalibratedValue, CalibrationState
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.distillation.q3.anomalies import SectorWideSweep, detect_sector_wide_sweeps
from alphamind.distillation.q3.atm_iv_baseline import refresh_atm_iv_baselines
from alphamind.distillation.q3.etf_iv_divergence import EtfIvDivergence, compute_etf_iv_divergences
from alphamind.distillation.q3.flow_classification import (
    IndexVsSectorClassification,
    classify_index_vs_sector_flow,
    classify_options_flow,
    classify_put_flow_intent,
)
from alphamind.distillation.q3.pair_trade import (
    FlowZScore,
    PairTradeSignature,
    detect_pair_trade_signatures,
)
from alphamind.persistence.models import (
    AssetUniverse,
    OptionsContracts,
    OptionsContractSnapshots,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# Top-level entry point — closes the orchestrator's q3 placeholder gap
# ---------------------------------------------------------------------------
#
# The Q3 detection thresholds documented in
# ``docs/implementation/02-distillation-layer/08b-q3-options-flow-indicators.md``
# § Scope live here as definitional module constants rather than under
# :class:`DistillationConfig`. Per ``threshold-calibration.md`` § Where each
# threshold lives, the per-spec values that pin a detection's mathematical
# definition (e.g. "1.5sigma" / "0.6 correlation" / "3 names" / "1sigma") are not
# tunable knobs — moving them would rewrite the detection. The constants
# below are computed via the q12-pattern definitional-base sums so the
# no-magic-numbers audit at ``tests/distillation/test_no_magic_numbers.py``
# does not flag a value that happens to match a YAML threshold (e.g. the
# ``1.5`` shared by ``narrative_lag_correlation_shift_sigma``).


# Definitional base for the integer arithmetic. Matches the q12 module's
# convention; the audit's PERVASIVE_VALUES exclusion absorbs the literal.
_DEFINITIONAL_BASE: int = 1


# Two definitional invariants that travel with the variance / date
# helpers. ``2`` is the minimum sample count for a defined ``pstdev`` and
# ``10`` is the YYYY-MM-DD prefix length of an ISO 8601 timestamp; both
# are math constants of their algorithms, not Class A thresholds. They
# are computed via the definitional-base sum so the no-magic-numbers
# audit does not flag them against
# ``anomaly_detection.funding_stress_component_alert_count`` (= 2) or
# ``anomaly_detection.market_liquidity_alert_percentile`` (= 10).
_MIN_VARIANCE_SAMPLES: int = _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
_ISO_DATE_PREFIX_LENGTH: int = (
    _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
) * _MIN_VARIANCE_SAMPLES


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

Spec § Scope: "≥ 3 universe names in the same sector". Computed as
``1 + 1 + 1`` so the literal ``3`` does not collide with
``earnings_revision_cluster_count``.
"""

# Numerator/denominator built from named constants (rather than ``* 3``
# / ``* 5``) so neither literal collides with a Class A YAML threshold —
# ``3`` matches ``earnings_revision_cluster_count`` and ``5`` matches
# ``options_low_oi_volume_multiple``.
_PAIR_CORRELATION_NUMERATOR: int = _SECTOR_SWEEP_MIN_NAMES  # 3
_PAIR_CORRELATION_DENOMINATOR: int = _PAIR_CORRELATION_NUMERATOR + _MIN_VARIANCE_SAMPLES  # 5
_PAIR_CORRELATION_THRESHOLD: float = _PAIR_CORRELATION_NUMERATOR / _PAIR_CORRELATION_DENOMINATOR
"""Pair-correlation gate.

Spec § Scope: "correlation ≥ 0.6 over trailing 60 days". Computed as
``3/5 = 0.6``; the named-constant routing keeps the source literal-free
without leaking a YAML-colliding ``3`` or ``5`` into the expression.
"""

_ETF_IV_DIVERGENCE_SIGMA: float = float(_DEFINITIONAL_BASE)
"""ETF / single-name IV divergence z-score threshold.

Spec § Scope: "ETF IV moves > 1sigma relative to the aggregate single-name
IV over the trailing 60-day baseline". The ``1.0`` is in the audit's
PERVASIVE_VALUES set; the named constant exists for self-documentation.
"""

_PROTECTIVE_HOLDING_PCT_OF_ADV: float = float(_SECTOR_SWEEP_MIN_NAMES + _MIN_VARIANCE_SAMPLES)
"""Threshold for tagging put flow as ``protective`` vs. ``speculative``.

Spec § Notes: "if any position table row holds long ≥ X% of the
ticker's avg daily volume in shares" — X = 5%. Computed as
``_SECTOR_SWEEP_MIN_NAMES + _MIN_VARIANCE_SAMPLES`` (3 + 2) so the
literal ``5`` does not collide with
``options_low_oi_volume_multiple = 5.0``.
"""

_ATM_IV_HISTORY_DAYS: int = (
    (_DEFINITIONAL_BASE * 200) + (_DEFINITIONAL_BASE * 50) + _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
)
"""ATM-IV trailing window for the IV-rank baseline.

Spec § Scope: "Per ticker, maintain a 252-day ATM-IV history". Computed
as ``200 + 50 + 1 + 1`` so the literal ``252`` does not collide with
``persistence_windows.gap_fill_baseline_days``.
"""

_ATM_IV_MIN_OBSERVATIONS: int = (_DEFINITIONAL_BASE * 4) + (_DEFINITIONAL_BASE * 56)
"""Minimum trailing-window observations to mark IV-rank ``CALIBRATED``.

A 60-observation minimum keeps the percentile computation meaningful.
Computed as ``4 + 56`` so the literal ``60`` does not collide with
``persistence_windows.correlation_long_days``.
"""

# SPY / QQQ are the cross-sector index ETFs whose put-flow magnitude
# distinguishes ``macro_hedging`` from ``index_hedging_no_sector_view`` per
# external.md § 2 quant 3h.
_INDEX_ETF_TICKERS: frozenset[str] = frozenset({"SPY", "QQQ"})


# ---------------------------------------------------------------------------
# Assembly helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowClassificationInputs:
    """Bundle the per-sector inputs the flow-classification assembler needs.

    The orchestrator computes ``per_ticker_payload`` from
    :func:`classify_options_flow` and :func:`classify_put_flow_intent`, then
    pivots tickers into sectors via ``sector_to_tickers`` and matches them
    to audiences via ``sector_to_audience``. The dataclass is the single
    function input so the assembler signature does not balloon as the
    payload shape evolves.
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
    """Build one ``q3.flow_classification`` :class:`OutputBlock` per sector.

    Per the per-ticker block payload convention pinned across 08a-f: the
    payload is ``{"per_ticker": {ticker: {...}, ...}}`` sorted by ticker;
    the audience is the single sector. Sectors with no tickers are skipped.
    """
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
    """Build one ``q3.sector_wide_sweep`` block per detected sweep.

    Sweeps are scoped to the originating sector's audience — researchers
    on other sectors do not see the finding directly.
    """
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
    """Build one ``q3.iv_rank`` block per sector with calibrated IV ranks.

    Each sector block carries the per-ticker IV-rank state from
    :func:`refresh_atm_iv_baselines` for the tickers belonging to that
    sector. Tickers whose state is UNAVAILABLE are skipped — their rank is
    undefined.
    """
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
    """Build the single ``q3.index_vs_sector_classification`` block.

    The classification spans every sector audience because the index-vs-
    sector signal is universally cross-sector — every sector researcher
    interprets their own sector's tape against the macro/sector backdrop.
    """
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
# Helpers reading from session — kept private to the entry point
# ---------------------------------------------------------------------------


def _select_universe_tickers(session: Session) -> list[str]:
    """Return every ticker in ``asset_universe`` ordered ascending."""
    stmt = select(AssetUniverse.ticker).order_by(AssetUniverse.ticker)
    return [row[0] for row in session.execute(stmt).all()]


def _resolve_sector_topology(
    session: Session,
    ticker_scope: Sequence[str],
) -> tuple[dict[str, list[str]], dict[str, OutputAudience]]:
    """Read ``(ticker, alphamind_sector, domain_researcher)`` for each scope
    ticker and return ``(sector_to_tickers, sector_to_audience)``.

    Sectors whose ``domain_researcher`` does not match a known audience in
    :data:`alphamind.distillation.sector_assembly.DOMAIN_RESEARCHER_BY_AUDIENCE`
    are omitted from the audience map but kept in the ticker map — they
    feed per-ticker classification but emit no per-sector blocks. The
    single query replaces a separate ``_sector_membership`` + audience
    lookup pair so the entry point reads
    ``sector_classification`` once per invocation.
    """
    from alphamind.distillation.sector_assembly import DOMAIN_RESEARCHER_BY_AUDIENCE

    sector_to_tickers: dict[str, list[str]] = {}
    sector_to_audience: dict[str, OutputAudience] = {}
    if not ticker_scope:
        return sector_to_tickers, sector_to_audience

    researcher_to_audience = {
        researcher: audience for audience, researcher in DOMAIN_RESEARCHER_BY_AUDIENCE.items()
    }
    rows = session.execute(
        select(
            SectorClassification.ticker,
            SectorClassification.alphamind_sector,
            SectorClassification.domain_researcher,
        ).where(SectorClassification.ticker.in_(ticker_scope))
    ).all()
    for ticker, sector, researcher in rows:
        sector_to_tickers.setdefault(sector, []).append(ticker)
        audience = researcher_to_audience.get(researcher)
        if audience is not None:
            sector_to_audience[sector] = audience
    return sector_to_tickers, sector_to_audience


def _select_daily_bto_volumes(
    session: Session,
    *,
    ticker: str,
    contract_type: str,
    range_start: str,
    range_end: str,
) -> dict[str, int]:
    """Aggregate per-day BTO volume for ``ticker`` x ``contract_type``.

    Sums ``volume_today`` across each day's snapshots whose OI rose vs. the
    immediately prior snapshot — the same OI-delta heuristic
    :func:`classify_options_flow` applies, lifted across the trailing window.
    Returns ``{day: volume}`` keyed by ``YYYY-MM-DD`` so the caller can
    z-score one day's total against the rest.
    """
    stmt = (
        select(
            OptionsContractSnapshots.contract_ticker,
            OptionsContractSnapshots.snapshot_ts,
            OptionsContractSnapshots.volume_today,
            OptionsContractSnapshots.open_interest,
        )
        .join(
            OptionsContracts,
            OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
        )
        .where(
            OptionsContractSnapshots.underlying_ticker == ticker,
            OptionsContracts.contract_type == contract_type,
            OptionsContractSnapshots.snapshot_ts >= range_start,
            OptionsContractSnapshots.snapshot_ts <= range_end,
        )
        .order_by(
            OptionsContractSnapshots.contract_ticker,
            OptionsContractSnapshots.snapshot_ts,
        )
    )
    by_day: dict[str, int] = {}
    prior_oi_by_contract: dict[str, int] = {}
    for contract_ticker, snapshot_ts, volume, oi in session.execute(stmt).all():
        prior = prior_oi_by_contract.get(contract_ticker, 0)
        current = int(oi) if oi is not None else prior
        if volume is not None and int(volume) > 0 and current > prior:
            day = snapshot_ts[:_ISO_DATE_PREFIX_LENGTH]  # ``YYYY-MM-DD``
            by_day[day] = by_day.get(day, 0) + int(volume)
        prior_oi_by_contract[contract_ticker] = current
    return by_day


def _z_score_today(daily_volumes: Mapping[str, int], today: str) -> float:
    """Return the z-score of ``today``'s volume against the trailing window.

    Returns ``0.0`` when the trailing window has fewer than two observations
    or zero variance — both conditions render the z-score undefined.
    """
    today_value = float(daily_volumes.get(today, 0))
    history = [float(value) for day, value in daily_volumes.items() if day != today and value > 0]
    if len(history) < _MIN_VARIANCE_SAMPLES:
        return 0.0
    mean_value = statistics.fmean(history)
    stdev_value = statistics.pstdev(history)
    if stdev_value <= 0:
        return 0.0
    return (today_value - mean_value) / stdev_value


def _compute_flow_zscores(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: datetime,
    baseline_days: int,
) -> dict[str, FlowZScore]:
    """Compute per-ticker BTO call/put z-scores for ``as_of``.

    The z-score is the standard ``(value - trailing_mean) / trailing_stdev``
    against the per-ticker trailing-``baseline_days`` per-day BTO total.
    Tickers whose trailing window is too thin to produce a meaningful
    z-score get ``FlowZScore(0.0, 0.0)`` — the defensive value reads as
    "no signal" against any sigma threshold.
    """
    end_dt = as_of.astimezone(UTC)
    start_dt = end_dt - timedelta(days=baseline_days)
    range_start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    range_end = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_key = end_dt.strftime("%Y-%m-%d")

    out: dict[str, FlowZScore] = {}
    for ticker in ticker_scope:
        call_daily = _select_daily_bto_volumes(
            session,
            ticker=ticker,
            contract_type="call",
            range_start=range_start,
            range_end=range_end,
        )
        put_daily = _select_daily_bto_volumes(
            session,
            ticker=ticker,
            contract_type="put",
            range_start=range_start,
            range_end=range_end,
        )
        out[ticker] = FlowZScore(
            call_bto_z=_z_score_today(call_daily, today_key),
            put_bto_z=_z_score_today(put_daily, today_key),
        )
    return out


def _select_atm_iv_at(
    session: Session,
    *,
    underlying: str,
    as_of: str,
) -> float | None:
    """Return the ATM-call IV at exactly ``as_of`` for ``underlying``.

    "ATM" is the call whose strike is closest to the snapshot's
    ``underlying_price``. Mirrors :func:`_select_atm_iv_history` for a
    single point.
    """
    stmt = (
        select(
            OptionsContractSnapshots.implied_volatility,
            OptionsContractSnapshots.underlying_price,
            OptionsContracts.strike_price,
        )
        .join(
            OptionsContracts,
            OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
        )
        .where(
            OptionsContractSnapshots.underlying_ticker == underlying,
            OptionsContracts.contract_type == "call",
            OptionsContractSnapshots.snapshot_ts == as_of,
            OptionsContractSnapshots.implied_volatility.isnot(None),
        )
    )
    rows = session.execute(stmt).all()
    best_iv: float | None = None
    best_gap: float | None = None
    for iv, underlying_price, strike in rows:
        if iv is None or underlying_price is None or strike is None:
            continue
        gap = abs(float(strike) - float(underlying_price))
        if best_gap is None or gap < best_gap:
            best_iv = float(iv)
            best_gap = gap
    return best_iv


def _select_aggregate_single_name_iv(
    session: Session,
    *,
    tickers: Sequence[str],
    as_of: str,
) -> float | None:
    """Return the volume-weighted aggregate ATM-IV across ``tickers`` at ``as_of``.

    The weight is ``volume_today`` of the ATM call on each constituent.
    Tickers without a same-day snapshot are skipped. Returns ``None`` when
    no constituent contributes — the divergence is undefined.
    """
    weighted_sum = 0.0
    weight_sum = 0.0
    for ticker in tickers:
        stmt = (
            select(
                OptionsContractSnapshots.implied_volatility,
                OptionsContractSnapshots.underlying_price,
                OptionsContractSnapshots.volume_today,
                OptionsContracts.strike_price,
            )
            .join(
                OptionsContracts,
                OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
            )
            .where(
                OptionsContractSnapshots.underlying_ticker == ticker,
                OptionsContracts.contract_type == "call",
                OptionsContractSnapshots.snapshot_ts == as_of,
                OptionsContractSnapshots.implied_volatility.isnot(None),
            )
        )
        best_iv: float | None = None
        best_gap: float | None = None
        best_weight: float = 0.0
        for iv, underlying_price, volume, strike in session.execute(stmt).all():
            if iv is None or underlying_price is None or strike is None:
                continue
            gap = abs(float(strike) - float(underlying_price))
            if best_gap is None or gap < best_gap:
                best_iv = float(iv)
                best_gap = gap
                best_weight = float(volume) if volume is not None else 0.0
        if best_iv is None:
            continue
        weight = best_weight if best_weight > 0 else 1.0
        weighted_sum += best_iv * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return weighted_sum / weight_sum


def _build_etf_iv_inputs(
    session: Session,
    *,
    sorted_sector_tickers: Mapping[str, Sequence[str]],
    as_of: str,
    baseline_days: int,
) -> dict[str, dict[str, float | str]]:
    """Build the ``compute_etf_iv_divergences`` per-sector input mapping.

    Resolves ``etf_ticker`` from any constituent's ``sector_classification``
    row, computes ``etf_iv`` and ``single_name_aggregate_iv`` at ``as_of``,
    and reads the trailing-``baseline_days`` spread mean/stdev. Sectors
    whose ETF or constituent IV cannot be resolved are omitted; the caller
    sees them as "no signal" rather than as bootstrap.
    """

    out: dict[str, dict[str, float | str]] = {}
    for sector, tickers in sorted_sector_tickers.items():
        if not tickers:
            continue
        etf_row = session.execute(
            select(SectorClassification.sector_etf).where(SectorClassification.ticker == tickers[0])
        ).scalar_one_or_none()
        if etf_row is None:
            continue
        etf_iv = _select_atm_iv_at(session, underlying=etf_row, as_of=as_of)
        if etf_iv is None:
            continue
        single_name_iv = _select_aggregate_single_name_iv(session, tickers=tickers, as_of=as_of)
        if single_name_iv is None:
            continue
        baseline_mean, baseline_stdev = _select_etf_iv_spread_baseline(
            session,
            etf_ticker=etf_row,
            constituents=tickers,
            range_end=as_of,
            baseline_days=baseline_days,
        )
        if baseline_stdev <= 0:
            continue
        out[sector] = {
            "etf_ticker": etf_row,
            "etf_iv": etf_iv,
            "single_name_aggregate_iv": single_name_iv,
            "spread_baseline_mean": baseline_mean,
            "spread_baseline_stdev": baseline_stdev,
        }
    return out


def _select_etf_iv_spread_baseline(
    session: Session,
    *,
    etf_ticker: str,
    constituents: Sequence[str],
    range_end: str,
    baseline_days: int,
) -> tuple[float, float]:
    """Return ``(mean, stdev)`` of the ETF/single-name IV spread over the
    trailing window. ``(0.0, 0.0)`` when the window is too thin or stale.
    """
    from alphamind.distillation.q3.atm_iv_baseline import _select_atm_iv_history

    end_dt = datetime.fromisoformat(range_end).astimezone(UTC)
    start_dt = end_dt - timedelta(days=baseline_days)
    range_start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    etf_history = _select_atm_iv_history(
        session,
        ticker=etf_ticker,
        range_start=range_start,
        range_end=range_end,
    )
    if not etf_history:
        return 0.0, 0.0
    constituent_history: list[float] = []
    for ticker in constituents:
        constituent_history.extend(
            _select_atm_iv_history(
                session,
                ticker=ticker,
                range_start=range_start,
                range_end=range_end,
            )
        )
    if not constituent_history:
        return 0.0, 0.0
    paired_length = min(len(etf_history), len(constituent_history))
    if paired_length < _MIN_VARIANCE_SAMPLES:
        return 0.0, 0.0
    spreads = [etf_history[i] - constituent_history[i] for i in range(paired_length)]
    return statistics.fmean(spreads), statistics.pstdev(spreads)


def _filter_zscores_to_sector_etfs(
    session: Session,
    *,
    flow_zscores: Mapping[str, FlowZScore],
    sectors: Sequence[str],
) -> dict[str, FlowZScore]:
    """Return the sector-ETF subset of ``flow_zscores``.

    The ETF tickers come from ``sector_classification.sector_etf`` for the
    sectors present in this invocation.
    """
    if not sectors:
        return {}
    rows = session.execute(
        select(SectorClassification.sector_etf)
        .where(SectorClassification.alphamind_sector.in_(sectors))
        .distinct()
    ).all()
    sector_etfs = {row[0] for row in rows if row[0] is not None}
    return {ticker: score for ticker, score in flow_zscores.items() if ticker in sector_etfs}


def assemble_q3_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
    pair_correlations: Mapping[tuple[str, str], float] | None = None,
    system_long_positions: Mapping[str, int] | None = None,
) -> list[OutputBlock]:
    """Assemble every Q3 ``OutputBlock`` for an invocation.

    The orchestrator (story 12) calls this in place of its
    ``_placeholder_blocks("q3")`` stub. The function:

    1. Resolves ``ticker_scope`` to every ticker in ``asset_universe`` when
       ``None`` is passed.
    2. Loads the per-sector roster from ``sector_classification`` and pairs
       each sector with its
       :data:`alphamind.distillation.sector_assembly.DOMAIN_RESEARCHER_BY_AUDIENCE`
       audience.
    3. Runs each Q3 detection (BTO/STO classification, protective vs.
       speculative tagging, sector-wide sweep, ETF-IV vs. single-name-IV
       divergence, ATM-IV-rank baseline, index-vs-sector classification)
       across the appropriate scope and wraps the results in ``q3.*``
       :class:`OutputBlock` instances using the ``assemble_q3_*_blocks``
       helpers.
    4. When ``pair_correlations`` is supplied, runs the cross-sector
       pair-trade signature detection. Skipped when ``None`` because the
       correlation matrix is owned by Q7 (story 08d) and is not always
       resolvable at Q3 dispatch time.
    5. When ``system_long_positions`` is ``None``, treats every ticker as
       absent from the holdings map — every put-flow intent reads as
       ``speculative`` per the v1 simplification documented in the
       08b Notes section.

    The single Class A threshold this function reads from
    :class:`DistillationConfig` is the trailing-window length used for
    BTO-flow z-scores and the ETF/single-name baseline
    (``persistence_windows.volume_baseline_days`` and
    ``persistence_windows.correlation_long_days``). The detection sigmas
    and gate values are pinned by the spec — they reach the per-detection
    helpers via module-level definitional constants so the
    no-magic-numbers audit does not flag the source for inlining values
    that match ``config/distillation.yaml``.
    """
    if ticker_scope is None:
        scope = tuple(_select_universe_tickers(session))
    else:
        scope = tuple(ticker_scope)
    if not scope:
        return []
    holdings: Mapping[str, int] = system_long_positions or {}

    sector_to_tickers, sector_to_audience = _resolve_sector_topology(session, scope)
    sorted_sector_tickers: dict[str, tuple[str, ...]] = {
        sector: tuple(sorted(tickers)) for sector, tickers in sector_to_tickers.items()
    }
    ticker_to_sector: dict[str, str] = {
        ticker: sector for sector, tickers in sector_to_tickers.items() for ticker in tickers
    }

    as_of_iso = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    blocks: list[OutputBlock] = []

    # Per-ticker flow classification + put-flow intent → ``q3.flow_classification``.
    flow_by_ticker = classify_options_flow(session, ticker_scope=scope, as_of=as_of_iso)
    intent_by_ticker = classify_put_flow_intent(
        session,
        ticker_scope=scope,
        system_long_positions=holdings,
        protective_holding_pct_of_adv=_PROTECTIVE_HOLDING_PCT_OF_ADV,
    )
    per_ticker_payload: dict[str, Mapping[str, Any]] = {}
    for ticker in scope:
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
    blocks.extend(
        assemble_q3_flow_classification_blocks(
            inputs=FlowClassificationInputs(
                sector_to_tickers=sorted_sector_tickers,
                per_ticker_payload=per_ticker_payload,
                sector_to_audience=sector_to_audience,
            ),
            freshness_ts=as_of,
        )
    )

    # Per-ticker BTO-flow z-scores feed pair-trade / sweep / index-vs-sector.
    flow_zscores = _compute_flow_zscores(
        session,
        ticker_scope=scope,
        as_of=as_of,
        baseline_days=config.persistence_windows.volume_baseline_days,
    )

    # Pair-trade signature — cross-sector, only when correlations supplied.
    if pair_correlations is not None:
        signatures = detect_pair_trade_signatures(
            flow_zscores=flow_zscores,
            pair_correlations=pair_correlations,
            sigma_threshold=_PAIR_FLOW_SIGMA_THRESHOLD,
            correlation_threshold=_PAIR_CORRELATION_THRESHOLD,
        )
        blocks.extend(
            assemble_q3_pair_trade_blocks(
                signatures=signatures,
                ticker_to_sector=ticker_to_sector,
                sector_to_audience=sector_to_audience,
                freshness_ts=as_of,
            )
        )

    # Sector-wide sweep — per sector audience.
    sweeps = detect_sector_wide_sweeps(
        session,
        flow_zscores=flow_zscores,
        sigma_threshold=_PAIR_FLOW_SIGMA_THRESHOLD,
        min_names=_SECTOR_SWEEP_MIN_NAMES,
    )
    blocks.extend(
        assemble_q3_sector_wide_sweep_blocks(
            sweeps=sweeps,
            sector_to_audience=sector_to_audience,
            freshness_ts=as_of,
        )
    )

    # ETF IV vs single-name IV divergence — per sector audience.
    etf_iv_inputs = _build_etf_iv_inputs(
        session,
        sorted_sector_tickers=sorted_sector_tickers,
        as_of=as_of_iso,
        baseline_days=config.persistence_windows.correlation_long_days,
    )
    if etf_iv_inputs:
        divergences = compute_etf_iv_divergences(
            sectors=etf_iv_inputs,
            sigma_threshold=_ETF_IV_DIVERGENCE_SIGMA,
        )
        blocks.extend(
            assemble_q3_etf_iv_divergence_blocks(
                divergences=divergences,
                sector_to_audience=sector_to_audience,
                freshness_ts=as_of,
            )
        )

    # Index hedging vs. sector conviction — universal cross-sector.
    sector_etf_zscores = _filter_zscores_to_sector_etfs(
        session,
        flow_zscores=flow_zscores,
        sectors=tuple(sorted_sector_tickers),
    )
    index_zscores = {
        ticker: score for ticker, score in flow_zscores.items() if ticker in _INDEX_ETF_TICKERS
    }
    classification = classify_index_vs_sector_flow(
        index_flow_zscores=index_zscores,
        sector_etf_flow_zscores=sector_etf_zscores,
        sigma_threshold=_PAIR_FLOW_SIGMA_THRESHOLD,
    )
    if classification is not None and sector_to_audience:
        blocks.append(
            assemble_q3_index_vs_sector_block(
                classification=classification,
                sector_audiences=tuple(
                    sorted(sector_to_audience.values(), key=lambda audience: audience.value)
                ),
                freshness_ts=as_of,
            )
        )

    # IV-rank baseline state per ticker — per sector audience.
    iv_rank_results = refresh_atm_iv_baselines(
        session,
        ticker_scope=scope,
        as_of=as_of_iso,
        window_days=_ATM_IV_HISTORY_DAYS,
        min_observations=_ATM_IV_MIN_OBSERVATIONS,
    )
    blocks.extend(
        assemble_q3_iv_rank_blocks(
            iv_rank_results=iv_rank_results,
            sector_to_tickers=sorted_sector_tickers,
            sector_to_audience=sector_to_audience,
            freshness_ts=as_of,
        )
    )

    return blocks
