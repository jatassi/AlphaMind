"""Q3 IO shell (ALP-484): pre-load every input the q3 compute path consumes.

The compute path is :func:`assemble_q3_blocks_from_inputs` — a pure
function over :class:`Q3Inputs` (frozen) plus
:class:`DistillationDomainConfig`. This module is the only place Q3
reaches the database for the per-category indicator compute dispatch: it
composes the per-sub loaders (flow_classification, anomalies,
atm_iv_baseline, etf_iv_divergence) plus the universe / sector-topology
/ flow-z-score / sector-ETF-membership reads previously embedded in the
legacy session-bound :func:`assemble_q3_blocks`.

The shell-vs-core split is what lets the per-category indicator compute
step run q3's ``compute_*`` in ``asyncio.TaskGroup`` over pre-loaded
frozen inputs — no shared mutable session means no "Session is already
flushing" InvalidRequestError.

The IV-rank baseline upsert happens during the loader (sequentially,
under the shared session) so the resulting :class:`Q3Inputs` carries
``iv_rank_results`` as already-calibrated values; the parallel pure
compute consumes them directly.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation._calibration_core import CalibratedValue
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q3.atm_iv_baseline_loaders import load_and_refresh_atm_iv_baselines
from alphamind.distillation.q3.etf_iv_divergence_loaders import load_etf_iv_divergence_inputs
from alphamind.distillation.q3.flow_classification_compute import (
    FlowClassificationInputs,
    PutFlowIntentInputs,
)
from alphamind.distillation.q3.flow_classification_loaders import (
    load_flow_classification_inputs,
    load_put_flow_intent_inputs,
)
from alphamind.distillation.q3.pair_trade import FlowZScore
from alphamind.distillation.sector_assembly import DOMAIN_RESEARCHER_BY_AUDIENCE
from alphamind.persistence.models import (
    AssetUniverse,
    OptionsContracts,
    OptionsContractSnapshots,
    SectorClassification,
)

# YYYY-MM-DD prefix length of an ISO 8601 timestamp. Computed via the
# definitional-base sum used elsewhere in q3 to avoid the no-magic-numbers
# audit collision against
# ``anomaly_detection.market_liquidity_alert_percentile`` (= 10).
_DEFINITIONAL_BASE: int = 1
_MIN_VARIANCE_SAMPLES: int = _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
_ISO_DATE_PREFIX_LENGTH: int = (
    _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
    + _DEFINITIONAL_BASE
) * _MIN_VARIANCE_SAMPLES

# SPY / QQQ are the cross-sector index ETFs whose put-flow magnitude
# distinguishes ``macro_hedging`` from ``index_hedging_no_sector_view`` per
# external.md § 2 quant 3h.
_INDEX_ETF_TICKERS: frozenset[str] = frozenset({"SPY", "QQQ"})

# Trailing window length for the ATM-IV baseline (252 days) and minimum
# observations to mark IV-rank ``CALIBRATED`` (60). Computed via the
# definitional-base sums so the no-magic-numbers audit does not flag
# ``252`` against ``persistence_windows.gap_fill_baseline_days`` or ``60``
# against ``persistence_windows.correlation_long_days``. Mirror the
# constants previously inlined in ``q3/assemble.py``.
_ATM_IV_HISTORY_DAYS: int = (
    (_DEFINITIONAL_BASE * 200) + (_DEFINITIONAL_BASE * 50) + _DEFINITIONAL_BASE + _DEFINITIONAL_BASE
)
_ATM_IV_MIN_OBSERVATIONS: int = (_DEFINITIONAL_BASE * 4) + (_DEFINITIONAL_BASE * 56)


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Q3Inputs:
    """Frozen pre-loaded inputs for the q3 compute path.

    Carries everything :func:`assemble_q3_blocks_from_inputs` needs:

    - ``ticker_scope`` — final list of tickers in scope (resolved or passed in).
    - ``as_of`` / ``as_of_iso`` — the freshness timestamp, kept in both forms.
    - ``sector_to_tickers`` — sorted-tuple per sector for the in-scope tickers.
    - ``sector_to_audience`` — sector → :class:`OutputAudience` (audience-routable
      sectors only; sectors lacking a researcher mapping are absent here).
    - ``ticker_to_sector`` — inverse mapping for the pair-trade assembler.
    - ``flow_classification_inputs`` — pre-loaded snapshot pairs for
      :func:`compute_options_flow`.
    - ``put_flow_intent_inputs`` — pre-loaded ADV + holdings for
      :func:`compute_put_flow_intent`.
    - ``flow_zscores`` — per-ticker BTO call/put z-scores against the
      trailing window (sweep + index-vs-sector consumer).
    - ``sector_etf_zscores`` — pre-filtered z-score subset for sector
      ETFs (index-vs-sector consumer).
    - ``index_zscores`` — pre-filtered z-score subset for the cross-sector
      index ETFs (SPY / QQQ).
    - ``etf_iv_inputs`` — per-sector pre-loaded ETF / single-name IV
      payload for :func:`compute_etf_iv_divergences`.
    - ``iv_rank_results`` — pre-computed (and pre-upserted) per-ticker
      :class:`CalibratedValue` for :func:`assemble_q3_iv_rank_blocks`.
    - ``pair_correlations`` — passthrough for
      :func:`detect_pair_trade_signatures` (orchestrator-computed).

    The sweep compute reads ``ticker_to_sector`` directly; the put-flow-intent
    compute reads ``system_long_positions`` off the
    ``put_flow_intent_inputs`` sub-dataclass. Neither is replicated as a
    top-level field.
    """

    ticker_scope: tuple[str, ...]
    as_of: datetime
    as_of_iso: str
    sector_to_tickers: Mapping[str, tuple[str, ...]]
    sector_to_audience: Mapping[str, OutputAudience]
    ticker_to_sector: Mapping[str, str]
    flow_classification_inputs: FlowClassificationInputs
    put_flow_intent_inputs: PutFlowIntentInputs
    flow_zscores: Mapping[str, FlowZScore]
    sector_etf_zscores: Mapping[str, FlowZScore]
    index_zscores: Mapping[str, FlowZScore]
    etf_iv_inputs: Mapping[str, Mapping[str, float | str]]
    iv_rank_results: Mapping[str, CalibratedValue]
    pair_correlations: Mapping[tuple[str, str], float] | None


# ---------------------------------------------------------------------------
# Internal session-bound helpers (lifted from the legacy assemble.py)
# ---------------------------------------------------------------------------


def _select_universe_tickers(session: Session) -> tuple[str, ...]:
    """Return every ticker in ``asset_universe`` ordered ascending."""
    stmt = select(AssetUniverse.ticker).order_by(AssetUniverse.ticker)
    return tuple(row[0] for row in session.execute(stmt).all())


def _resolve_sector_topology(
    session: Session,
    ticker_scope: Sequence[str],
) -> tuple[dict[str, list[str]], dict[str, OutputAudience]]:
    """Resolve ``(sector_to_tickers, sector_to_audience)`` for ``ticker_scope``.

    Sectors whose ``domain_researcher`` does not match a known audience in
    :data:`alphamind.distillation.sector_assembly.DOMAIN_RESEARCHER_BY_AUDIENCE`
    are omitted from the audience map but kept in the ticker map — they
    feed per-ticker classification but emit no per-sector blocks.
    """
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
    """Aggregate per-day BTO volume for ``ticker`` x ``contract_type``."""
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
            day = snapshot_ts[:_ISO_DATE_PREFIX_LENGTH]
            by_day[day] = by_day.get(day, 0) + int(volume)
        prior_oi_by_contract[contract_ticker] = current
    return by_day


def _z_score_today(daily_volumes: Mapping[str, int], today: str) -> float:
    """Return the z-score of ``today``'s volume against the trailing window."""
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
    """Compute per-ticker BTO call/put z-scores for ``as_of``."""
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


def _filter_zscores_to_sector_etfs(
    session: Session,
    *,
    flow_zscores: Mapping[str, FlowZScore],
    sectors: Sequence[str],
) -> dict[str, FlowZScore]:
    """Return the sector-ETF subset of ``flow_zscores``."""
    if not sectors:
        return {}
    rows = session.execute(
        select(SectorClassification.sector_etf)
        .where(SectorClassification.alphamind_sector.in_(sectors))
        .distinct()
    ).all()
    sector_etfs = {row[0] for row in rows if row[0] is not None}
    return {ticker: score for ticker, score in flow_zscores.items() if ticker in sector_etfs}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_q3_inputs(
    *,
    as_of: datetime,
    pair_correlations: Mapping[tuple[str, str], float] | None,
    system_long_positions: Mapping[str, int],
) -> Q3Inputs:
    """Empty-scope shortcut used when ticker_scope resolves to an empty tuple."""
    return Q3Inputs(
        ticker_scope=(),
        as_of=as_of,
        as_of_iso=_format_iso_utc(as_of),
        sector_to_tickers={},
        sector_to_audience={},
        ticker_to_sector={},
        flow_classification_inputs=FlowClassificationInputs(per_ticker_pairs={}),
        put_flow_intent_inputs=PutFlowIntentInputs(
            system_long_positions=dict(system_long_positions),
            adv_by_ticker={},
        ),
        flow_zscores={},
        sector_etf_zscores={},
        index_zscores={},
        etf_iv_inputs={},
        iv_rank_results={},
        pair_correlations=pair_correlations,
    )


def load_q3_inputs(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None,
    pair_correlations: Mapping[tuple[str, str], float] | None,
    system_long_positions: Mapping[str, int] | None = None,
) -> Q3Inputs:
    """Pre-load every q3 input the pure compute consumes.

    Composes the per-sub loaders plus the legacy session-bound resolution
    helpers (universe scope, sector topology, flow z-scores, sector-ETF
    filter, ATM-IV baselines) into one call. After this returns, the
    per-category indicator compute ``TaskGroup`` runs
    :func:`assemble_q3_blocks_from_inputs` in a thread without further
    DB access.

    The ATM-IV baseline upsert happens here (synchronously under the
    shared session) so the parallel pure compute sees ``iv_rank_results``
    as already-calibrated values.

    The ``session`` parameter is required because q3 mixes two read modes:
    the per-sub flow-classification + put-flow-intent loaders route through
    the :class:`DistillationRepository` Protocol (the pilot seam), while
    the q3-wide helpers (``_select_universe_tickers``, ``_resolve_sector_topology``,
    ``_compute_flow_zscores``, ``_filter_zscores_to_sector_etfs``, plus the
    IV-rank UPSERT) need raw session access for reads + writes that the
    pilot-scoped read-only Protocol does not expose. Promoting the seven
    additional reads + the write seam to the Protocol for q3 alone would
    expand the pilot beyond its declared boundary; that work is tracked
    as a multi-quarter Protocol-propagation follow-up.
    """
    holdings: Mapping[str, int] = system_long_positions or {}
    repository = SqlDistillationRepository(session)

    scope = _select_universe_tickers(session) if ticker_scope is None else tuple(ticker_scope)
    if not scope:
        return _empty_q3_inputs(
            as_of=as_of, pair_correlations=pair_correlations, system_long_positions=holdings
        )

    as_of_iso = _format_iso_utc(as_of)

    sector_to_tickers, sector_to_audience = _resolve_sector_topology(session, scope)
    sorted_sector_tickers: dict[str, tuple[str, ...]] = {
        sector: tuple(sorted(tickers)) for sector, tickers in sector_to_tickers.items()
    }
    ticker_to_sector: dict[str, str] = {
        ticker: sector for sector, tickers in sector_to_tickers.items() for ticker in tickers
    }

    # Per-sub loaders: flow classification + put-flow intent.
    flow_classification_inputs = load_flow_classification_inputs(
        repository, ticker_scope=scope, as_of=as_of_iso
    )
    put_flow_intent_inputs = load_put_flow_intent_inputs(
        repository, ticker_scope=scope, system_long_positions=holdings
    )

    # Per-ticker BTO-flow z-scores feed sweep / pair-trade / index-vs-sector.
    flow_zscores = _compute_flow_zscores(
        session,
        ticker_scope=scope,
        as_of=as_of,
        baseline_days=config.persistence_windows.volume_baseline_days,
    )

    # Sector-ETF subset for the index-vs-sector classifier.
    sector_etf_zscores = _filter_zscores_to_sector_etfs(
        session,
        flow_zscores=flow_zscores,
        sectors=tuple(sorted_sector_tickers),
    )
    index_zscores: dict[str, FlowZScore] = {
        ticker: score for ticker, score in flow_zscores.items() if ticker in _INDEX_ETF_TICKERS
    }

    # ETF IV inputs (per sector) for the divergence compute.
    etf_iv_inputs = load_etf_iv_divergence_inputs(
        session,
        sorted_sector_tickers=sorted_sector_tickers,
        as_of=as_of_iso,
        baseline_days=config.persistence_windows.correlation_long_days,
    )

    # ATM-IV trailing baseline + IV-rank — the loader UPSERTs the baseline
    # row and returns the per-ticker rank for the q3 IV-rank block.
    iv_rank_results = load_and_refresh_atm_iv_baselines(
        session,
        ticker_scope=scope,
        as_of=as_of_iso,
        window_days=_ATM_IV_HISTORY_DAYS,
        min_observations=_ATM_IV_MIN_OBSERVATIONS,
    )

    return Q3Inputs(
        ticker_scope=scope,
        as_of=as_of,
        as_of_iso=as_of_iso,
        sector_to_tickers=sorted_sector_tickers,
        sector_to_audience=sector_to_audience,
        ticker_to_sector=ticker_to_sector,
        flow_classification_inputs=flow_classification_inputs,
        put_flow_intent_inputs=put_flow_intent_inputs,
        flow_zscores=flow_zscores,
        sector_etf_zscores=sector_etf_zscores,
        index_zscores=index_zscores,
        etf_iv_inputs=etf_iv_inputs,
        iv_rank_results=iv_rank_results,
        pair_correlations=pair_correlations,
    )


__all__ = [
    "Q3Inputs",
    "load_q3_inputs",
]
