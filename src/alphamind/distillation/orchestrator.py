"""Distillation orchestrator entry point.

The single ``async`` entry point the pipeline process calls during its
distillation phase. The orchestrator sequences seven steps per
``docs/design/02-distillation-layer/external.md`` § Output format and the
spec at ``docs/implementation/02-distillation-layer/12-distillation-orchestrator.md``:

1. Class B refresh — refresh every rolling-state primitive before any
   computation reads from state. Refresh failure prevents any downstream
   work per ``docs/design/mid-pipeline-failure-handling.md``.
2. Per-category indicator computations — Q1's, Q3's, Q6's, and
   qualitative's pure-compute paths run in parallel with each other and
   with the remaining legacy session-bound categories (q7, q12, which
   remain internally serialized under one Session) via
   :class:`asyncio.TaskGroup`. Each category is a thin module-level
   helper that wraps the synchronous DB-bound primitives in
   :func:`asyncio.to_thread` so the event loop does not block.
3. Regime classification — universal-broadcast volatility regime label
   per story 09.
4. Aggregation — partition blocks by audience, collect anomaly summaries
   per story 10.
5. Per-consumer assembly — three sector outputs via story 11a and one
   correlation/regime brief via story 11b.
6. Invocation-archive write — deterministic filenames under
   ``%USERPROFILE%/AlphaMind/archive/<date>/<invocation>/distillation/``
   so the archive diffs cleanly across runs.
7. Brief-store population — INSERT one row per produced brief into the
   ``briefs`` table per ``docs/architecture/data-and-state.md`` § Brief
   store, so cross-process consumers (replay harness, command-center
   diagnostic) can hydrate the brief by ``(invocation_id, brief_kind)``.
   The :class:`CorrelationRegimeBrief` instance keeps riding in
   :class:`DistillationOutputs` for hot-path consumers in the same
   process.

Per the LLM-agents-uniformly-Critical policy, any failure in any step
aborts the invocation. The orchestrator does not catch and continue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind.analysis.qualitative_research.loaders import (
    compute_sentiment_inflow_metrics,
)
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation._severity_cap import cap_blocks_for_calibration
from alphamind.distillation.aggregation import (
    AnomalySummary,
    anomaly_summary_to_activity_log_entry,
    collect_anomalies,
    group_anomalies_by_audience,
    partition_blocks,
)
from alphamind.distillation.baselines import (
    _refresh_transaction,
    refresh_contract_history,
    refresh_event_history,
    refresh_pair_lag,
    refresh_ticker_baselines,
)
from alphamind.distillation.calibration import (
    CalibrationState,
    combine_calibration_states,
    pair_max_lag_days,
)
from alphamind.distillation.calibration_snapshot import (
    write_calibration_state_snapshot,
    write_operator_data_health_summary,
)
from alphamind.distillation.contract_scope import resolve_prediction_market_scope
from alphamind.distillation.correlation_brief import (
    CorrelationRegimeBrief,
    assemble_correlation_brief,
)
from alphamind.distillation.normalization import percentile_rank
from alphamind.distillation.output import (
    OutputAudience,
    OutputBlock,
    format_block,
)
from alphamind.distillation.q1 import assemble_q1_blocks
from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs
from alphamind.distillation.q1.assemble import assemble_q1_blocks_from_inputs
from alphamind.distillation.q3._loaders import Q3Inputs, load_q3_inputs
from alphamind.distillation.q3.assemble import assemble_q3_blocks_from_inputs
from alphamind.distillation.q6._loaders import Q6Inputs, load_q6_inputs
from alphamind.distillation.q6.assemble import assemble_q6_blocks_from_inputs
from alphamind.distillation.q7 import compute_pair_correlations
from alphamind.distillation.q7._loaders import Q7Inputs, load_q7_inputs
from alphamind.distillation.q7.assemble import assemble_q7_blocks_from_inputs
from alphamind.distillation.q12_corporate_actions import detect_q12_signals
from alphamind.distillation.qualitative._loaders import (
    QualitativeInputs,
    load_qualitative_inputs,
)
from alphamind.distillation.qualitative.assemble import (
    assemble_qualitative_blocks_from_inputs,
)
from alphamind.distillation.realized_vol import persist_per_ticker_realized_vol
from alphamind.distillation.regime import (
    RegimeClassificationThresholds,
    RegimeRefreshResult,
    RegimeSnapshot,
    RegimeTransitionThresholds,
    assemble_regime_block,
    refresh_regime_state,
)
from alphamind.distillation.sector_assembly import (
    DOMAIN_RESEARCHER_BY_AUDIENCE,
    SectorOutput,
    assemble_sector_output,
    load_sector_roster,
)
from alphamind.persistence.models import (
    CORRELATION_REGIME_BRIEF_KIND,
    Brief,
    MacroObservations,
    OhlcvBars,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass — return value of run_external_distillation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DistillationOutputs:
    """Return value of :func:`run_external_distillation`.

    Carries the three sector documents (one per :data:`DOMAIN_RESEARCHER_BY_AUDIENCE`
    key), the synthesizer's correlation/regime brief, and the universal
    regime label payload (a convenience accessor for analysis-layer agents
    that need the regime as universal context but not the full structured
    text).

    ``all_blocks`` carries every :class:`OutputBlock` the orchestrator
    emitted in this invocation, in deterministic order. The per-consumer
    assemblers (:class:`SectorOutput`, :class:`CorrelationRegimeBrief`)
    discard the underlying blocks during rendering, so this list is the
    single source the calibration-state snapshot writer (story 17) reads
    to produce the per-invocation reduction.
    """

    sector_outputs: dict[OutputAudience, SectorOutput]
    correlation_regime_brief: CorrelationRegimeBrief
    universal_regime_label: dict[str, Any]
    invocation_id: str
    as_of: datetime
    total_blocks: int
    total_anomalies: int
    non_calibrated_block_count: int
    all_blocks: tuple[OutputBlock, ...]


# ---------------------------------------------------------------------------
# Cross-platform archive root resolution
# ---------------------------------------------------------------------------


def _default_archive_root() -> Path:
    """Resolve the platform-default archive root.

    Mirrors the convention used elsewhere in the codebase (see
    :mod:`alphamind.persistence.session`,
    :mod:`alphamind.data_sources.finnhub.news`,
    :mod:`alphamind.data_sources.marketaux.news`,
    :mod:`alphamind.data_sources.sec_edgar.rss`):
    ``%USERPROFILE%/AlphaMind/archive`` on Windows and
    ``~/AlphaMind/archive`` elsewhere.
    """
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(userprofile) / "AlphaMind" / "archive"


def _default_provenance_root() -> Path:
    """Resolve the platform-default provenance-snapshot root.

    Mirrors the layout pinned by
    ``docs/design/05-execution-layer/state-persistence.md`` § Tier 2:
    ``data/provenance/invocations/{invocation_id}/...``. The orchestrator
    resolves the ``data/provenance`` portion under
    ``%USERPROFILE%/AlphaMind/`` on Windows and ``~/AlphaMind/`` elsewhere
    so the snapshot lives alongside the rest of AlphaMind's per-invocation
    state.
    """
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(userprofile) / "AlphaMind" / "data" / "provenance"


def _invocation_archive_dir(*, archive_root: Path, as_of: datetime, invocation_id: str) -> Path:
    """Return the per-invocation distillation archive directory.

    Layout: ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/distillation/``.
    The date partition mirrors the per-invocation file tree convention
    pinned by ``docs/architecture/infrastructure.md`` § Layer 2 invocation
    archive. Delegates to :func:`alphamind._kernel.archive_layout.invocation_archive_dir`
    and appends the ``distillation/`` subdir.
    """
    return (
        invocation_archive_dir(archive_root=archive_root, as_of=as_of, invocation_id=invocation_id)
        / "distillation"
    )


# ---------------------------------------------------------------------------
# Class B refresh
# ---------------------------------------------------------------------------


def _format_as_of(as_of: datetime) -> str:
    """Format a tz-aware datetime as ISO 8601 ``Z``-suffixed UTC."""
    return as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refresh_class_b_state(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    contract_scope: Sequence[str],
    as_of: datetime,
) -> int:
    """Run the five Class B refresh primitives in sequence.

    Returns the total number of refresh-output rows produced (sum across
    primitives). The orchestrator logs the per-step count.
    """
    as_of_iso = _format_as_of(as_of)
    pw = config.persistence_windows

    rows = 0
    # Per-ticker baselines (volume / ATR / spread / sentiment).
    for kind, window_days, min_observations in (
        ("volume", pw.volume_baseline_days, pw.volume_baseline_days),
        ("atr", pw.atr_baseline_days, pw.atr_baseline_days),
        ("spread", pw.spread_baseline_days, pw.spread_baseline_days),
        ("sentiment", pw.sentiment_baseline_days, pw.sentiment_min_observations),
    ):
        rows += len(
            refresh_ticker_baselines(
                session,
                kind=kind,
                ticker_scope=ticker_scope,
                as_of=as_of_iso,
                window_days=window_days,
                min_observations=min_observations,
            )
        )

    # Lead-lag pairs — each pair searches against its own configured
    # ``_max_days`` ceiling rather than a shared scalar (ALP-628).
    rows += len(
        refresh_pair_lag(
            session,
            pair_scope=tuple(
                (
                    p.lead,
                    p.lag,
                    pair_max_lag_days(pair_key=p.key, lead_lag_config=config.lead_lag),
                )
                for p in config.lead_lag.pairs
            ),
            as_of=as_of_iso,
            window_days=pw.correlation_long_days,
            min_events=config.anomaly_detection.earnings_revision_cluster_count,
        )
    )

    # Prediction-market contract history runs over the orchestrator-resolved
    # scope; an empty scope is a no-op (e.g. when ``tracked_categories`` is
    # empty in config).
    rows += len(
        refresh_contract_history(
            session,
            contract_scope=contract_scope,
            as_of=as_of_iso,
            min_observations=pw.sentiment_min_observations,
        )
    )

    # Event history — gap and extended_hours.
    for event_kind, resolution_days, min_events in (
        ("gap", pw.gap_fill_baseline_days, pw.gap_fill_min_events),
        ("extended_hours", pw.extended_hours_confirmation_days, pw.extended_hours_min_events),
    ):
        rows += len(
            refresh_event_history(
                session,
                event_kind=event_kind,
                ticker_scope=ticker_scope,
                as_of=as_of_iso,
                min_events=min_events,
                detection_atr_multiple=config.anomaly_detection.price_move_atr_multiple,
                outcome_resolution_days=resolution_days,
                detection_window_days=pw.atr_baseline_days,
            )
        )

    return rows


# ---------------------------------------------------------------------------
# Per-category indicator compute
# ---------------------------------------------------------------------------
#
# Each dispatcher is a thin wrapper over its category's public surface.
# The orchestrator runs Q1's, Q3's, Q6's, and qualitative's pure-compute
# paths concurrently with the remaining legacy session-bound categories
# (q7, q12, which stay sequential under one Session) via
# asyncio.TaskGroup; the synchronous DB-bound work runs through
# asyncio.to_thread so the event loop never blocks waiting for SQLite.
#
# All six categories (q1, q3, q6, q7, q12, qualitative) wire up directly
# to their module-level entry points. The :data:`_CATEGORY_COMPUTE_PLACEHOLDER_GAPS`
# table is preserved as an empty tuple for the verification script in
# story 13 (`scripts/verify_distillation.py`) so its summary section's
# shape stays stable; it now reports "all categories integrated."

_CATEGORY_COMPUTE_PLACEHOLDER_GAPS: tuple[tuple[str, str], ...] = ()


def _compute_q1_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
) -> list[OutputBlock]:
    """Q1 price/volume indicator blocks (story 08a).

    Delegates to :func:`assemble_q1_blocks`, the package's top-level
    entry point that fans out per ``(sector_audience, indicator_group)``
    and returns the six per-sector indicator blocks plus any anomaly
    blocks that fired this invocation.
    """
    return assemble_q1_blocks(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
    )


def _load_q1_inputs_via_session(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
) -> Q1Inputs:
    """ALP-467 — pre-load Q1 inputs under the shared Session.

    The shell half of the q1 compute/load split. The per-category indicator
    compute step calls this sequentially under one Session before launching
    the TaskGroup; the returned :class:`Q1Inputs` is then handed to
    :func:`_compute_q1_blocks_from_inputs` (pure compute, thread-safe).
    """
    repository = SqlDistillationRepository(session)
    return load_q1_inputs(repository, config=config, as_of=as_of, ticker_scope=ticker_scope)


def _compute_q1_blocks_from_inputs(
    q1_inputs: Q1Inputs,
    *,
    config: DistillationDomainConfig,
) -> list[OutputBlock]:
    """ALP-467 — pure-compute Q1 dispatch from pre-loaded inputs.

    Wraps :func:`assemble_q1_blocks_from_inputs` so the per-category
    indicator compute TaskGroup has a single ``to_thread`` callable that
    takes only serializable / immutable arguments — no Session, no ORM.
    """
    return assemble_q1_blocks_from_inputs(q1_inputs, config=config)


def _persist_realized_vol_from_q1_inputs(
    session: Session,
    *,
    q1_inputs: Q1Inputs,
    invocation_id: str,
    as_of: datetime,
) -> int:
    """ALP-530 — write per-ticker realized-vol rows from already-loaded Q1 bars.

    Q1's IO shell already loads ~300 trailing daily bars per universe
    ticker via ``repository.load_daily_bars`` (see
    ``q1/_loaders._BAR_LOAD_LOOKBACK_DAYS``). The realized-vol persister
    consumes the last 30 trading days from those same bar tuples, so the
    additional read pressure is zero — the per-ticker close-price load
    that Q1 already performs covers this story's input requirement.
    """
    closes_by_ticker: dict[str, tuple[float, ...]] = {
        ticker: tuple(bar.adj_close for bar in bars)
        for ticker, bars in q1_inputs.bars_by_ticker.items()
    }
    return persist_per_ticker_realized_vol(
        session,
        invocation_id=invocation_id,
        tickers=q1_inputs.ticker_scope,
        closes_by_ticker=closes_by_ticker,
        as_of_date=as_of.date(),
        computed_at=as_of,
    )


def _compute_legacy_session_bound_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> tuple[list[OutputBlock], ...]:
    """ALP-486 — sequential session-bound categories (q12 only).

    Q12 still shares the Session and remains serialized inside one
    TaskGroup task to avoid "Session is already flushing"
    InvalidRequestError. Q1 / Q3 / Q6 / Q7 / qualitative each lift to
    their own TaskGroup tasks under the pure-compute path.
    """
    return (_compute_q12_blocks(session, config=config, as_of=as_of),)


def _load_q3_inputs_via_session(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
    pair_correlations: dict[tuple[str, str], float],
) -> Q3Inputs:
    """ALP-484 — pre-load Q3 inputs under the shared Session.

    The shell half of the q3 compute/load split. The per-category indicator
    compute step calls this sequentially under one Session before launching
    the TaskGroup; the returned :class:`Q3Inputs` is then handed to
    :func:`_compute_q3_blocks_from_inputs` (pure compute, thread-safe).

    The IV-rank baseline UPSERT happens inside this loader so the
    parallel pure compute consumes already-calibrated values without any
    further session touch.
    """
    return load_q3_inputs(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
        pair_correlations=pair_correlations,
    )


def _compute_q3_blocks_from_inputs(q3_inputs: Q3Inputs) -> list[OutputBlock]:
    """ALP-484 — pure-compute Q3 dispatch from pre-loaded inputs.

    Wraps :func:`assemble_q3_blocks_from_inputs` so the per-category
    indicator compute TaskGroup has a single ``to_thread`` callable that
    takes only serializable / immutable arguments — no Session, no ORM.
    """
    return assemble_q3_blocks_from_inputs(q3_inputs)


def _load_q6_inputs_via_session(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> Q6Inputs:
    """ALP-485 — pre-load Q6 inputs under the shared Session.

    The shell half of the q6 compute/load split. The per-category indicator
    compute step calls this sequentially under one Session before launching
    the TaskGroup; the returned :class:`Q6Inputs` is then handed to
    :func:`_compute_q6_blocks_from_inputs` (pure compute, thread-safe).

    The funding-stress and market-liquidity composite-refresh writes
    (``session.flush()``) happen inside this loader so the parallel pure
    compute consumes already-persisted composite results without any
    further session touch.
    """
    return load_q6_inputs(session, config=config, as_of=as_of)


def _compute_q6_blocks_from_inputs(q6_inputs: Q6Inputs) -> list[OutputBlock]:
    """ALP-485 — pure-compute Q6 dispatch from pre-loaded inputs.

    Wraps :func:`assemble_q6_blocks_from_inputs` so the per-category
    indicator compute TaskGroup has a single ``to_thread`` callable that
    takes only serializable / immutable arguments — no Session, no ORM.
    """
    return assemble_q6_blocks_from_inputs(q6_inputs)


def _load_q7_inputs_via_session(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
    pair_correlations: dict[tuple[str, str], float],
) -> Q7Inputs:
    """ALP-486 — pre-load Q7 inputs under the shared Session.

    The shell half of the q7 compute/load split. The per-category indicator
    compute step calls this sequentially under one Session before launching
    the TaskGroup; the returned :class:`Q7Inputs` is then handed to
    :func:`_compute_q7_blocks_from_inputs` (pure compute, thread-safe).

    The intra-sector ``correlation_divergence`` event writes happen
    inside this loader so the parallel pure compute consumes
    already-persisted state without any further session touch.
    """
    return load_q7_inputs(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
        pair_correlations=pair_correlations,
    )


def _compute_q7_blocks_from_inputs(q7_inputs: Q7Inputs) -> list[OutputBlock]:
    """ALP-486 — pure-compute Q7 dispatch from pre-loaded inputs.

    Wraps :func:`assemble_q7_blocks_from_inputs` so the per-category
    indicator compute TaskGroup has a single ``to_thread`` callable that
    takes only serializable / immutable arguments — no Session, no ORM.
    """
    return assemble_q7_blocks_from_inputs(q7_inputs)


def _compute_q12_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> list[OutputBlock]:
    """Q12 corporate-actions blocks.

    Q12 (story 08e) exposes a clean :func:`detect_q12_signals` entry
    point that reads its ticker scope from
    ``sector_classification`` directly. The orchestrator dispatches it
    with the configured ``volume_baseline_days``.
    """
    return detect_q12_signals(
        session,
        as_of=_format_as_of(as_of),
        volume_baseline_days=config.persistence_windows.volume_baseline_days,
    )


def _load_qualitative_inputs_via_session(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    contract_scope: Sequence[str],
    as_of: datetime,
) -> QualitativeInputs:
    """ALP-487 — pre-load qualitative inputs under the shared Session.

    The shell half of the qualitative compute/load split. The per-category
    indicator compute step calls this sequentially under one Session before
    launching the TaskGroup; the returned :class:`QualitativeInputs` is
    then handed to :func:`_compute_qualitative_blocks_from_inputs` (pure
    compute, thread-safe).
    """
    repository = SqlDistillationRepository(session)
    return load_qualitative_inputs(
        repository,
        config=config,
        ticker_scope=ticker_scope,
        contract_scope=contract_scope,
        as_of=as_of,
    )


def _compute_qualitative_blocks_from_inputs(
    qualitative_inputs: QualitativeInputs,
    *,
    config: DistillationDomainConfig,
) -> list[OutputBlock]:
    """ALP-487 — pure-compute qualitative dispatch from pre-loaded inputs.

    Wraps :func:`assemble_qualitative_blocks_from_inputs` so the
    per-category indicator compute TaskGroup has a single ``to_thread``
    callable that takes only serializable / immutable arguments — no
    Session, no ORM.
    """
    return assemble_qualitative_blocks_from_inputs(qualitative_inputs, config=config)


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------


def _build_regime_thresholds(
    config: DistillationDomainConfig,
) -> tuple[RegimeClassificationThresholds, RegimeTransitionThresholds]:
    """Translate the YAML regime groups into the regime module's threshold dataclasses."""
    rc = config.regime_classification
    rt = config.regime_transition
    classification = RegimeClassificationThresholds(
        low_vol_vix_max=rc.regime_low_vol_vix_max,
        normal_vix_min=rc.regime_normal_vix_min,
        normal_vix_max=rc.regime_normal_vix_max,
        elevated_vix_min=rc.regime_elevated_vix_min,
        elevated_vix_max=rc.regime_elevated_vix_max,
        crisis_vix_min=rc.regime_crisis_vix_min,
        term_structure_backwardation_threshold=rc.regime_term_structure_backwardation_threshold,
        vvix_high_percentile=float(rc.regime_vvix_high_percentile),
        vvix_low_percentile=float(rc.regime_vvix_low_percentile),
    )
    transition = RegimeTransitionThresholds(
        confirmed_invocations=rt.regime_transition_confirmed_invocations,
        indicator_agreement_min=rt.regime_transition_indicator_agreement_min,
    )
    return classification, transition


def _latest_macro_value(session: Session, series_id: str) -> float | None:
    """Return the most recent value for a macro series, or ``None`` if missing."""
    stmt = (
        select(MacroObservations.value)
        .where(MacroObservations.series_id == series_id)
        .order_by(MacroObservations.observation_date.desc())
        .limit(1)
    )
    value = session.execute(stmt).scalar_one_or_none()
    return float(value) if value is not None else None


# VVIX percentile rank reads from the VVIX time-series stored under this
# ``macro_observations.series_id``. CBOE publishes VVIX directly; the
# collector that will ingest it has not yet been built (no VVIX entries
# in the FRED series list, no vendor adapter in ``data_sources/``). The
# percentile calculator therefore returns ``UNAVAILABLE`` today, but the
# function is shaped to start returning ``CALIBRATED`` values as soon as
# observations land — no further plumbing change required.
_VVIX_SERIES_ID: str = "VVIX"

# Calendar-day lookback for the trailing-history reference window the
# percentile ranks against. 365 calendar days ≈ 252 trading days — the
# "trailing one-year history" the ``RegimeSnapshot.vvix_percentile``
# docstring promises.
_VVIX_PERCENTILE_LOOKBACK_DAYS: int = 365

# Minimum number of VVIX observations required to publish a percentile
# rank. Below this the trailing distribution is too sparse to rank
# against reliably; the calculator returns ``ACCUMULATING``. Chosen as
# ~3 trading months — large enough to span a typical vol cycle without
# requiring the full year before any signal is emitted.
_VVIX_PERCENTILE_MIN_OBSERVATIONS: int = 60


# VIX term-structure basis reads the front-month VIX future from this
# ``macro_observations.series_id``. CBOE publishes the VX1 settlement
# directly; the collector that will ingest it has not yet been built
# (no VX1 entries in the FRED series list, no vendor adapter in
# ``data_sources/``). The basis calculator therefore returns
# ``UNAVAILABLE`` today, but the function is shaped to start returning
# ``CALIBRATED`` values as soon as observations land — no further
# plumbing change required.
_VX1_SERIES_ID: str = "VX1"


# Realized-vol computation pinned to SPY daily log returns annualized via
# the standard √252 trading-days factor. The lookback fetches calendar days
# rather than trading days; 35 calendar days yields ≥21 trading days
# (enough for the 20-day window) under the worst-case U.S. holiday density.
_REALIZED_VOL_BENCHMARK_TICKER: str = "SPY"
_TIMEFRAME_DAILY: str = "1d"
_TRADING_DAYS_PER_YEAR: int = 252
_REALIZED_VOL_SHORT_WINDOW: int = 5
_REALIZED_VOL_LONG_WINDOW: int = 20
_REALIZED_VOL_LOOKBACK_DAYS: int = 35


def _annualized_window_stdev(returns: Sequence[float], window: int) -> float:
    """Sample-stdev of the last ``window`` returns, annualized via √252.

    Returns ``0.0`` when fewer than ``window`` returns are available so the
    caller can fall back to the placeholder snapshot without fabricating a
    vol reading from too little data.
    """
    if len(returns) < window:
        return 0.0
    return float(statistics.stdev(returns[-window:]) * math.sqrt(_TRADING_DAYS_PER_YEAR))


def _realized_vols_from_log_returns(returns: Sequence[float]) -> tuple[float, float]:
    """Return ``(realized_vol_5d, realized_vol_20d)`` from daily log returns."""
    return (
        _annualized_window_stdev(returns, _REALIZED_VOL_SHORT_WINDOW),
        _annualized_window_stdev(returns, _REALIZED_VOL_LONG_WINDOW),
    )


def _load_spy_closes(session: Session, *, as_of: datetime) -> list[float]:
    """Ascending SPY daily ``adj_close`` over the realized-vol lookback window."""
    range_end = _format_as_of(as_of)
    range_start = _format_as_of(as_of - timedelta(days=_REALIZED_VOL_LOOKBACK_DAYS))
    stmt = (
        select(OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == _REALIZED_VOL_BENCHMARK_TICKER,
            OhlcvBars.timeframe == _TIMEFRAME_DAILY,
            OhlcvBars.period_start >= range_start,
            OhlcvBars.period_start <= range_end,
        )
        .order_by(OhlcvBars.period_start)
    )
    return [float(v) for v in session.execute(stmt).scalars().all()]


def _spy_log_returns(closes: Sequence[float]) -> list[float]:
    """Day-over-day log returns from ascending SPY closes.

    Non-positive closes are a data-corruption signal: SPY adjusted closes are
    bounded strictly above zero. The function logs a WARN per occurrence and
    substitutes ``0.0`` so a single bad bar doesn't take the whole window out;
    the WARN makes the bad bar visible in operator logs rather than silently
    deflating realized vol.
    """
    out: list[float] = []
    for prior, latest in pairwise(closes):
        if prior <= 0.0 or latest <= 0.0:
            logger.warning(
                "realized-vol: non-positive SPY close in pair (prior=%r, latest=%r) — "
                "substituting 0.0 return; investigate upstream OHLCV ingest",
                prior,
                latest,
            )
            out.append(0.0)
        else:
            out.append(math.log(latest / prior))
    return out


def _compute_realized_vols(session: Session, *, as_of: datetime) -> tuple[float, float]:
    """Annualized SPY realized vol over 5 and 20 trading days at or before ``as_of``.

    Returns ``(0.0, 0.0)`` when fewer than two SPY closes are available — the
    placeholder caller (``_build_regime_snapshot``) propagates that state so
    the downstream emitter can mark the regime block as data-degraded.
    """
    closes = _load_spy_closes(session, as_of=as_of)
    if len(closes) < 2:
        return 0.0, 0.0
    return _realized_vols_from_log_returns(_spy_log_returns(closes))


def _load_vvix_history(session: Session, *, as_of: datetime) -> list[float]:
    """Return VVIX observations within the trailing-history window.

    Reads :data:`_VVIX_SERIES_ID` from ``macro_observations`` ordered by
    ``observation_date`` ascending. The window is :data:`_VVIX_PERCENTILE_LOOKBACK_DAYS`
    calendar days ending at ``as_of`` — the percentile rank ranks the
    most-recent observation against the prior window.
    """
    # macro_observations.observation_date is stored as ``YYYY-MM-DD``, so
    # the range bounds must be date-only — comparing against an ISO
    # datetime would lex-sort the day-of-as_of row outside the window
    # (``"2026-05-19" < "2026-05-19T17:00:00Z"``).
    range_end = as_of.strftime("%Y-%m-%d")
    range_start = (as_of - timedelta(days=_VVIX_PERCENTILE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.series_id == _VVIX_SERIES_ID,
            MacroObservations.observation_date >= range_start,
            MacroObservations.observation_date <= range_end,
        )
        .order_by(MacroObservations.observation_date)
    )
    # ``MacroObservations.value`` is nullable — vendor gaps land as NULL
    # rather than as missing rows. Skip them so they don't sink the
    # percentile computation.
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _compute_vvix_percentile(
    session: Session, *, as_of: datetime
) -> tuple[float | None, CalibrationState, str | None]:
    """Return ``(percentile, calibration_state, reason)`` for VVIX.

    Three outcomes per the ALP-540 vocabulary:

    - ``(percentile, CALIBRATED, None)`` when the trailing window holds
      ≥ :data:`_VVIX_PERCENTILE_MIN_OBSERVATIONS`; the most-recent
      observation is ranked against the full window.
    - ``(None, ACCUMULATING, reason)`` when the window holds at least one
      observation but fewer than the minimum, or every observation is
      identical (zero-variance distribution — percentile is undefined
      per :func:`~alphamind.distillation.normalization.percentile_rank`).
    - ``(None, UNAVAILABLE, reason)`` when the window holds zero
      observations — collector failure or series-not-yet-implemented;
      operator action required.

    Per ALP-571: never returns ``50.0`` (or any other fabricated value)
    as a default. Downstream rendering treats ``None`` as explicit
    missing-data signal.
    """
    history = _load_vvix_history(session, as_of=as_of)
    observations = len(history)
    if observations == 0:
        return None, CalibrationState.UNAVAILABLE, "regime: VVIX series unavailable"
    if observations < _VVIX_PERCENTILE_MIN_OBSERVATIONS:
        reason = (
            f"regime: VVIX percentile accumulating "
            f"({observations} obs < {_VVIX_PERCENTILE_MIN_OBSERVATIONS} required)"
        )
        return None, CalibrationState.ACCUMULATING, reason
    rank = percentile_rank(history, history[-1])
    if rank is None:
        return (
            None,
            CalibrationState.ACCUMULATING,
            "regime: VVIX percentile undefined (zero-variance trailing window)",
        )
    return rank, CalibrationState.CALIBRATED, None


def _compute_term_structure_basis(
    session: Session, *, vix: float | None
) -> tuple[float | None, CalibrationState, str | None]:
    """Return ``(basis, calibration_state, reason)`` for the VIX term structure.

    Two outcomes per the ALP-540 vocabulary:

    - ``(basis, CALIBRATED, None)`` when VX1 (front-month VIX future) is
      available and ``vix`` is non-null; ``basis = VX1 - VIX``.
    - ``(None, UNAVAILABLE, reason)`` when VX1 is absent from
      ``macro_observations`` — collector failure or series-not-yet-
      implemented; operator action required. Also returns ``UNAVAILABLE``
      when ``vix`` itself is null (no anchor for the subtraction); the
      caller already surfaces the VIX outage separately so the duplicate
      reason is omitted.

    Per ALP-572: never returns ``0.0`` as a default. Downstream rendering
    treats ``None`` as an explicit missing-data signal.
    """
    vx1 = _latest_macro_value(session, _VX1_SERIES_ID)
    if vx1 is None:
        return None, CalibrationState.UNAVAILABLE, "regime: VX1 series unavailable"
    if vix is None:
        # No anchor for the subtraction. The VIX outage is surfaced by
        # the caller; emit UNAVAILABLE without a duplicate reason so the
        # combined message stays scannable.
        return None, CalibrationState.UNAVAILABLE, None
    return vx1 - vix, CalibrationState.CALIBRATED, None


def _build_regime_snapshot(
    session: Session, *, as_of: datetime
) -> tuple[RegimeSnapshot, CalibrationState, str | None]:
    """Build a :class:`RegimeSnapshot` plus the calibration state + reason.

    Reads VIX (``VIXCLS``), VVIX, VX1, and SPY daily closes directly.
    Realized vol is computed from SPY log returns when closes are
    available; absent SPY data, both windows fall back to ``0.0`` and the
    downstream input-bundle integrity check surfaces the gap.

    Calibration-state precedence (worst wins via
    :func:`combine_calibration_states`): each of the three supporting
    series (VIX, VVIX, VX1) emits its own ``(state, reason)``; the
    combiner picks the most-severe state and concatenates the reasons
    that match it. When the regime block is degraded the operator sees
    every contributing gap named in one combined string rather than the
    first-discovered gap winner-takes-all. The block is still emitted
    with partial inputs; the calibration tag surfaces the degradation
    through the operator-facing data-health summary (ALP-540 / ALP-571 /
    ALP-572).

    Carrying the state out alongside the snapshot avoids encoding the
    signal as a magic numeric sentinel — a real VIX print of exactly
    zero would otherwise mis-tag.
    """
    vix = _latest_macro_value(session, "VIXCLS")
    realized_vol_5d, realized_vol_20d = _compute_realized_vols(session, as_of=as_of)
    vvix_percentile, vvix_state, vvix_reason = _compute_vvix_percentile(session, as_of=as_of)
    vx1_minus_vix, basis_state, basis_reason = _compute_term_structure_basis(session, vix=vix)

    vix_state, vix_reason = (
        (CalibrationState.CALIBRATED, None)
        if vix is not None
        else (CalibrationState.UNAVAILABLE, "regime: VIXCLS observation missing")
    )

    snapshot = RegimeSnapshot(
        vix_level=vix if vix is not None else 0.0,
        vx1_minus_vix=vx1_minus_vix,
        vvix_percentile=vvix_percentile,
        realized_vol_5d=realized_vol_5d,
        realized_vol_20d=realized_vol_20d,
        vix_trailing_20d_mean=None,
        prior_term_structure_backwardation=False,
    )
    combined_state, combined_reason = combine_calibration_states(
        (vix_state, vix_reason),
        (vvix_state, vvix_reason),
        (basis_state, basis_reason),
    )
    return snapshot, combined_state, combined_reason


def _refresh_regime(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> tuple[RegimeRefreshResult, OutputBlock]:
    """Regime classification — refresh the regime row and assemble the universal block."""
    classification, transition = _build_regime_thresholds(config)
    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=as_of)
    result = refresh_regime_state(
        session,
        as_of=_format_as_of(as_of),
        snapshot=snapshot,
        classification_thresholds=classification,
        transition_thresholds=transition,
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
    )
    block = assemble_regime_block(result=result, freshness_ts=as_of)
    return result, block


# ---------------------------------------------------------------------------
# Invocation-archive write
# ---------------------------------------------------------------------------


def _write_invocation_archive(
    *,
    archive_dir: Path,
    sector_outputs: dict[OutputAudience, SectorOutput],
    correlation_regime_brief: CorrelationRegimeBrief,
    regime_block: OutputBlock,
) -> None:
    """Write the three sector documents, the CR brief, and the regime label.

    Files are written deterministically (UTF-8, ``\\n`` line endings preserved
    from the assemblers) so the archive diffs cleanly across runs per
    ``docs/architecture/infrastructure.md`` § Layer 2 invocation archive.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    for audience, sector_output in sector_outputs.items():
        # sector audience values are ``sector_<name>``; the documented
        # filename convention drops the prefix so the path is human-readable.
        sector_slug = audience.value.removeprefix("sector_")
        archive_path = archive_dir / f"{sector_slug}_sector.md"
        archive_path.write_text(sector_output.text, encoding="utf-8")
    (archive_dir / "correlation_regime_brief.md").write_text(
        correlation_regime_brief.text, encoding="utf-8"
    )
    (archive_dir / "regime.md").write_text(format_block(regime_block), encoding="utf-8")


# ---------------------------------------------------------------------------
# Brief-store population
# ---------------------------------------------------------------------------


def _populate_brief_store(
    session: Session,
    *,
    correlation_regime_brief: CorrelationRegimeBrief,
    invocation_id: str,
) -> int:
    """Brief-store population — INSERT the correlation-regime brief row into the ``briefs`` table.

    The hot-path passthrough in :class:`DistillationOutputs` is preserved
    so in-process consumers (the synthesizer, the sector analysts) keep
    reading the brief without a round trip; the persistent row is the
    cross-process backup loaded by :func:`alphamind.persistence.brief_store.load_brief`.
    Returns the number of rows inserted so the caller can report it; the
    multi-brief extension (sector / qualitative / adaptive) accumulates
    here when those producers land.
    """
    with _refresh_transaction(session):
        session.add(
            Brief(
                invocation_id=invocation_id,
                brief_kind=CORRELATION_REGIME_BRIEF_KIND,
                reference_index_json=json.dumps(
                    correlation_regime_brief.reference_index, sort_keys=True
                ),
                text=correlation_regime_brief.text,
                created_at=_format_as_of(datetime.now(UTC)),
            )
        )
    return 1


# ---------------------------------------------------------------------------
# Anomaly activity-log emission (ALP-881 / story 04b)
# ---------------------------------------------------------------------------


def _emit_anomaly_activity_log(
    session: Session,
    *,
    summaries: Sequence[AnomalySummary],
    invocation_id: str,
    as_of: datetime,
) -> int:
    """Persist one ``DISTILLATION_ANOMALY_FLAG`` row per produced anomaly flag.

    Mirrors :func:`_populate_brief_store`: each summary is mapped to a typed
    :class:`~alphamind.portfolio_state.events.ActivityLogEntry` by
    :func:`~alphamind.distillation.aggregation.anomaly_summary_to_activity_log_entry`
    (which resolves the threshold taxonomy via story 02g) and added to the sync
    session inside the fail-closed :func:`_refresh_transaction` boundary. Returns
    the number of rows written. A zero-flag run writes nothing (and skips the
    transaction entirely — a clean no-op).

    The distillation orchestrator runs on a synchronous ``Session``, so the
    async OMS helper ``append_activity_log_entry`` (which requires an
    ``AsyncSession``-backed ``InvocationHandle``) is not applicable here; the
    row is added directly on the sync session via the shared codec.

    Summaries are de-duplicated by their ``(source_block_id, flag.name)``
    entry_id key before the single ``executemany``: two summaries sharing that
    key mint the same ``activity_log.entry_id`` PK, which would otherwise abort
    the whole distillation transaction with an ``IntegrityError`` (the q12
    corporate-action collision, ALP-934). The first occurrence in iteration
    order is kept; each dropped duplicate emits one ``WARNING`` naming its
    ``block_id`` + ``flag.name`` so the lost signal is observable rather than
    fatal. This is the emission-boundary safety net for *any* producer;
    per-producer subject embedding (suffixing ``flag.name`` with the subject) is
    what keeps distinct logical anomalies from colliding in the first place.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[AnomalySummary] = []
    for summary in summaries:
        key = (summary.source_block_id, summary.flag.name)
        if key in seen:
            logger.warning(
                "dropping duplicate distillation anomaly flag (entry_id collision): "
                "block_id=%s flag_name=%s — first occurrence kept",
                summary.source_block_id,
                summary.flag.name,
            )
            continue
        seen.add(key)
        deduped.append(summary)
    if not deduped:
        return 0
    with _refresh_transaction(session):
        for summary in deduped:
            entry = anomaly_summary_to_activity_log_entry(
                summary, invocation_id=invocation_id, timestamp=as_of
            )
            session.add(activity_log_entry_to_row(entry))
    return len(deduped)


# ---------------------------------------------------------------------------
# Main entry point — run_external_distillation
# ---------------------------------------------------------------------------


def _count_non_calibrated_blocks(blocks: Iterable[OutputBlock]) -> int:
    return sum(1 for block in blocks if block.calibration_state is not CalibrationState.CALIBRATED)


async def _compute_category_indicators(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    contract_scope: Sequence[str],
    as_of: datetime,
    invocation_id: str,
) -> list[OutputBlock]:
    """Per-category indicator compute — run all six category dispatchers.

    ALP-467 piloted the compute/load boundary split on q1; ALP-484
    propagated the split to q3; ALP-487 propagated it to qualitative;
    ALP-485 propagated it to q6; ALP-486 propagated it to q7. Q1 / Q3 /
    Q6 / Q7 / qualitative inputs are pre-loaded under the shared Session
    (the shell) and then the pure computes run in parallel with each
    other and with the legacy session-bound categories via an
    ``asyncio.TaskGroup``. The remaining legacy category (q12) still
    shares the Session so it remains serialized inside one task — the
    multi-quarter migration to per-category pure compute is tracked as
    ALP-467 follow-ups.

    Pair correlations are computed once before the dispatch so Q3's
    pair-trade-signature detection sees the same matrix Q7 reports (Q7
    threads the matrix through Q7Inputs for API symmetry; its own block
    emissions compute intra-sector matrices independently).
    """
    phase_start = time.monotonic()
    pair_correlations = await asyncio.to_thread(
        compute_pair_correlations,
        session,
        ticker_scope=ticker_scope,
        as_of=as_of,
        window_days=config.persistence_windows.correlation_short_days,
    )
    # Shell: pre-load Q1 + Q3 + Q6 + Q7 + qualitative inputs under the
    # shared Session before the core. The Q3 loader UPSERTs the ATM-IV
    # baseline rows; the Q6 loader performs the funding-stress +
    # market-liquidity composite-refresh writes; the Q7 loader persists
    # intra-sector correlation_divergence events — all synchronously so
    # the parallel compute consumes already-persisted state.
    q1_inputs = await asyncio.to_thread(
        _load_q1_inputs_via_session,
        session,
        config=config,
        ticker_scope=ticker_scope,
        as_of=as_of,
    )
    # ALP-530 — populate the per-ticker realized-vol substrate from the
    # per-ticker close-price series Q1 already loaded. Synchronous under
    # the shared Session so downstream consumers (fill_collection attribution,
    # continuous-monitor refresh) see the rows from this invocation
    # onward.
    rows_persisted = await asyncio.to_thread(
        _persist_realized_vol_from_q1_inputs,
        session,
        q1_inputs=q1_inputs,
        invocation_id=invocation_id,
        as_of=as_of,
    )
    logger.info(
        "per-category indicator compute (per-ticker realized vol) complete: rows=%d",
        rows_persisted,
    )
    q3_inputs = await asyncio.to_thread(
        _load_q3_inputs_via_session,
        session,
        config=config,
        ticker_scope=ticker_scope,
        as_of=as_of,
        pair_correlations=pair_correlations,
    )
    q6_inputs = await asyncio.to_thread(
        _load_q6_inputs_via_session,
        session,
        config=config,
        as_of=as_of,
    )
    q7_inputs = await asyncio.to_thread(
        _load_q7_inputs_via_session,
        session,
        config=config,
        ticker_scope=ticker_scope,
        as_of=as_of,
        pair_correlations=pair_correlations,
    )
    qualitative_inputs = await asyncio.to_thread(
        _load_qualitative_inputs_via_session,
        session,
        config=config,
        ticker_scope=ticker_scope,
        contract_scope=contract_scope,
        as_of=as_of,
    )

    # Core: TaskGroup runs Q1 + Q3 + Q6 + Q7 + qualitative pure computes
    # concurrently with the legacy session-bound categories (which remain
    # internally serialized).
    # ExceptionGroup unwrapping: TaskGroup wraps any sub-exception in a
    # BaseExceptionGroup. Per the LLM-agents-uniformly-Critical policy,
    # propagate the original exception so callers (and tests) see the
    # underlying failure type, not the wrapper.
    try:
        async with asyncio.TaskGroup() as tg:
            q1_task = tg.create_task(
                asyncio.to_thread(
                    _compute_q1_blocks_from_inputs,
                    q1_inputs,
                    config=config,
                )
            )
            q3_task = tg.create_task(
                asyncio.to_thread(
                    _compute_q3_blocks_from_inputs,
                    q3_inputs,
                )
            )
            q6_task = tg.create_task(
                asyncio.to_thread(
                    _compute_q6_blocks_from_inputs,
                    q6_inputs,
                )
            )
            q7_task = tg.create_task(
                asyncio.to_thread(
                    _compute_q7_blocks_from_inputs,
                    q7_inputs,
                )
            )
            qualitative_task = tg.create_task(
                asyncio.to_thread(
                    _compute_qualitative_blocks_from_inputs,
                    qualitative_inputs,
                    config=config,
                )
            )
            legacy_task = tg.create_task(
                asyncio.to_thread(
                    _compute_legacy_session_bound_blocks,
                    session,
                    config=config,
                    as_of=as_of,
                )
            )
    except BaseExceptionGroup as eg:
        raise eg.exceptions[0] from eg
    per_category_blocks = (
        q1_task.result(),
        q3_task.result(),
        q6_task.result(),
        q7_task.result(),
        qualitative_task.result(),
        *legacy_task.result(),
    )
    indicator_blocks: list[OutputBlock] = []
    for category_blocks in per_category_blocks:
        indicator_blocks.extend(category_blocks)
    logger.info(
        "per-category indicator compute complete: blocks=%d elapsed=%.3fs",
        len(indicator_blocks),
        time.monotonic() - phase_start,
    )
    return indicator_blocks


async def run_external_distillation(
    session: Session,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
    invocation_id: str,
    *,
    archive_root: Path | None = None,
    provenance_root: Path | None = None,
    emit_anomaly_flags: bool = False,
) -> DistillationOutputs:
    """The single ``async`` entry point the pipeline process calls.

    Runs the seven steps per the story spec; any failure aborts the
    invocation per ``docs/design/mid-pipeline-failure-handling.md``. The
    orchestrator does not catch and continue.

    ``archive_root`` defaults to the platform-default location
    (``%USERPROFILE%/AlphaMind/archive`` on Windows;
    ``~/AlphaMind/archive`` elsewhere). ``provenance_root`` defaults to
    ``%USERPROFILE%/AlphaMind/data/provenance`` (or its POSIX equivalent),
    where the per-invocation calibration-state snapshot lands per
    ``docs/design/05-execution-layer/state-persistence.md`` § Tier 2.
    Tests pass explicit paths.

    ``emit_anomaly_flags`` (story 04b / ALP-881) opts into persisting each
    produced :class:`~alphamind.distillation.output.AnomalyFlag` as a
    ``DISTILLATION_ANOMALY_FLAG`` activity-log row on ``session``. It defaults
    to ``False`` so callers without an invocation context — notably the replay
    harness, which runs against a throwaway DB — degrade to a clean no-op
    (mirroring the ``diag_dir=None`` pattern in the analysis harness). The
    production pipeline (``alphamind.pipeline.analysis``) passes ``True``.
    """
    if archive_root is None:
        archive_root = _default_archive_root()
    if provenance_root is None:
        provenance_root = _default_provenance_root()

    overall_start = time.monotonic()

    # Resolve prediction-market contract scope ONCE so every consumer
    # (the class-B-refresh step's refresh_contract_history and the
    # per-category-indicator-compute step's load_qualitative_inputs) sees
    # the identical tuple. Splitting would let the writer ingest one set
    # while the reader reports on another.
    contract_scope: tuple[str, ...] = await asyncio.to_thread(
        resolve_prediction_market_scope,
        session,
        config=config,
        as_of=as_of,
    )

    # Class B refresh.
    phase_start = time.monotonic()
    baseline_rows = await asyncio.to_thread(
        _refresh_class_b_state,
        session,
        config=config,
        ticker_scope=ticker_scope,
        contract_scope=contract_scope,
        as_of=as_of,
    )
    logger.info(
        "class B refresh complete: rows=%d elapsed=%.3fs",
        baseline_rows,
        time.monotonic() - phase_start,
    )

    # Per-category indicator compute.
    indicator_blocks = await _compute_category_indicators(
        session,
        config=config,
        ticker_scope=ticker_scope,
        contract_scope=contract_scope,
        as_of=as_of,
        invocation_id=invocation_id,
    )

    # Regime classification.
    phase_start = time.monotonic()
    regime_result, regime_block = await asyncio.to_thread(
        _refresh_regime,
        session,
        config=config,
        as_of=as_of,
    )
    all_blocks = cap_blocks_for_calibration(
        [*indicator_blocks, regime_block],
        exempt_flag_names=config.severity_caps.exempt_flag_names,
    )
    logger.info(
        "regime classification complete: label=%s elapsed=%.3fs",
        regime_result.regime_label.value,
        time.monotonic() - phase_start,
    )

    # Aggregation. Partition / collect / group per story 10
    # to populate the diagnostic counts the orchestrator returns; the
    # assembly step re-derives the same partitioning from the
    # block list, so the dicts here are not threaded forward.
    phase_start = time.monotonic()
    partitioned = partition_blocks(all_blocks)
    anomaly_summaries: list[AnomalySummary] = collect_anomalies(all_blocks)
    grouped_anomalies = group_anomalies_by_audience(anomaly_summaries)
    # Anomaly activity-log emission (story 04b / ALP-881). ``anomaly_summaries``
    # holds one entry per produced flag (``collect_anomalies`` does not
    # broadcast), so this writes exactly N rows for N flags. Gated on
    # ``emit_anomaly_flags`` so the replay harness (no invocation context)
    # degrades to a no-op; runs the sync write off the event loop like the
    # brief-store population below.
    if emit_anomaly_flags:
        await asyncio.to_thread(
            _emit_anomaly_activity_log,
            session,
            summaries=anomaly_summaries,
            invocation_id=invocation_id,
            as_of=as_of,
        )
    logger.info(
        "aggregation complete: audiences=%d total_anomalies=%d elapsed=%.3fs",
        len(partitioned),
        len(anomaly_summaries),
        time.monotonic() - phase_start,
    )

    # Assembly. The three sector assemblies and the CR brief
    # are independent (each reads its own audience slice of the same
    # block list), so they fan out via asyncio.gather instead of
    # serializing through a per-audience loop.
    #
    # The sector roster lookup is the only DB read inside the per-audience
    # work, and SQLAlchemy's ``Session`` is not thread-safe under
    # concurrent reads (sporadic ``IndexError`` inside the result-row
    # constructor). Pre-compute every roster in the main thread before the
    # gather so the gathered tasks touch only pure-Python state.
    phase_start = time.monotonic()
    sector_audiences = tuple(DOMAIN_RESEARCHER_BY_AUDIENCE)
    sector_rosters: dict[OutputAudience, tuple[str, ...]] = {
        audience: load_sector_roster(session, DOMAIN_RESEARCHER_BY_AUDIENCE[audience])
        for audience in sector_audiences
    }
    sector_assembly_results: list[SectorOutput] = await asyncio.gather(
        *(
            asyncio.to_thread(
                assemble_sector_output,
                audience=audience,
                blocks=all_blocks,
                roster=sector_rosters[audience],
                invocation_id=invocation_id,
            )
            for audience in sector_audiences
        )
    )
    sector_outputs: dict[OutputAudience, SectorOutput] = dict(
        zip(sector_audiences, sector_assembly_results, strict=True)
    )
    correlation_regime_brief = await asyncio.to_thread(
        assemble_correlation_brief,
        all_blocks,
        invocation_id,
    )
    logger.info(
        "assembly complete: elapsed=%.3fs",
        time.monotonic() - phase_start,
    )

    # Build the universal regime label payload from the regime block.
    universal_regime_label: dict[str, Any] = dict(regime_block.payload)

    outputs = DistillationOutputs(
        sector_outputs=sector_outputs,
        correlation_regime_brief=correlation_regime_brief,
        universal_regime_label=universal_regime_label,
        invocation_id=invocation_id,
        as_of=as_of,
        total_blocks=len(all_blocks),
        total_anomalies=sum(len(items) for items in grouped_anomalies.values()),
        non_calibrated_block_count=_count_non_calibrated_blocks(all_blocks),
        all_blocks=tuple(all_blocks),
    )

    # Invocation-archive write plus calibration-state snapshot.
    # The archive write emits the markdown documents; the snapshot write
    # emits the deterministic JSON the feedback loop and command center
    # consume per story 17. Both writes share the invocation-archive step's
    # fail-closed semantics — any failure aborts the invocation.
    phase_start = time.monotonic()
    archive_dir = _invocation_archive_dir(
        archive_root=archive_root, as_of=as_of, invocation_id=invocation_id
    )
    await asyncio.to_thread(
        _write_invocation_archive,
        archive_dir=archive_dir,
        sector_outputs=sector_outputs,
        correlation_regime_brief=correlation_regime_brief,
        regime_block=regime_block,
    )
    await asyncio.to_thread(
        write_calibration_state_snapshot,
        outputs,
        invocation_id,
        provenance_root,
    )
    # ALP-540: replace the bootstrap-seed scaffold at the archive root with
    # the operator-facing data-health summary. The seed (written by
    # ``_persist_data_calibration_snapshot`` at invocation start) is the
    # ``{}`` placeholder the operator sees in failed e2e runs — the
    # invocation-archive write step overwrites it with the structured
    # per-state summary.
    #
    # ALP-709: compute inter-baseline sentiment inflow telemetry against
    # the same session + ticker_scope the qualitative loader will see
    # downstream. The class-B refresh step has already refreshed the
    # sentiment baselines, so the (prior, latest) windows the helper
    # reports against match the windows ``load_sentiment_aggregates``
    # would compute against.
    sentiment_inflow = await asyncio.to_thread(
        compute_sentiment_inflow_metrics,
        session,
        as_of=as_of,
        ticker_scope=ticker_scope,
    )
    await asyncio.to_thread(
        write_operator_data_health_summary,
        outputs,
        invocation_id,
        archive_root,
        sentiment_inflow=sentiment_inflow,
    )
    logger.info(
        "invocation-archive write complete: dir=%s elapsed=%.3fs",
        archive_dir,
        time.monotonic() - phase_start,
    )

    # Brief-store population.
    phase_start = time.monotonic()
    inserted_brief_rows = await asyncio.to_thread(
        _populate_brief_store,
        session,
        correlation_regime_brief=correlation_regime_brief,
        invocation_id=invocation_id,
    )
    logger.info(
        "brief-store population complete: rows=%d elapsed=%.3fs",
        inserted_brief_rows,
        time.monotonic() - phase_start,
    )
    logger.info(
        "run_external_distillation complete: total_blocks=%d total_anomalies=%d "
        "non_calibrated_blocks=%d elapsed=%.3fs",
        outputs.total_blocks,
        outputs.total_anomalies,
        outputs.non_calibrated_block_count,
        time.monotonic() - overall_start,
    )
    return outputs


__all__ = [
    "DistillationOutputs",
    "run_external_distillation",
]
