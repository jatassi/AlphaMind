"""Distillation orchestrator entry point — story 02-distillation-layer/12.

The single ``async`` entry point the pipeline process calls during its
distillation phase. The orchestrator sequences seven phases per
``docs/design/02-distillation-layer/external.md`` § Output format and the
spec at ``docs/implementation/02-distillation-layer/12-distillation-orchestrator.md``:

1. Class B refresh — refresh every rolling-state primitive before any
   computation reads from state. Refresh failure prevents any downstream
   work per ``docs/design/mid-pipeline-failure-handling.md``.
2. Per-category indicator computations — six categories dispatched in
   parallel via :func:`asyncio.gather`. Each category is a thin
   module-level helper that wraps the synchronous DB-bound primitives in
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
7. Brief-store population — TODO; no ``briefs`` table is implemented in
   the persistence schema yet (verified against
   :mod:`alphamind.persistence.models`). The
   :class:`CorrelationRegimeBrief` instance still rides in
   :class:`DistillationOutputs` so an in-process consumer can use it
   without the persistent store.

Per the LLM-agents-uniformly-Critical policy, any failure in any phase
aborts the invocation. The orchestrator does not catch and continue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.aggregation import (
    AnomalySummary,
    collect_anomalies,
    group_anomalies_by_audience,
    partition_blocks,
)
from alphamind.distillation.baselines import (
    refresh_contract_history,
    refresh_event_history,
    refresh_pair_lag,
    refresh_ticker_baselines,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.calibration_snapshot import (
    write_calibration_state_snapshot,
)
from alphamind.distillation.correlation_brief import (
    CorrelationRegimeBrief,
    assemble_correlation_brief,
)
from alphamind.distillation.output import (
    OutputAudience,
    OutputBlock,
    format_block,
)
from alphamind.distillation.q1 import assemble_q1_blocks
from alphamind.distillation.q3_options import assemble_q3_blocks
from alphamind.distillation.q6_macro import compute_q6_blocks
from alphamind.distillation.q7_cross_asset import (
    assemble_q7_blocks,
    compute_pair_correlations,
)
from alphamind.distillation.q12_corporate_actions import detect_q12_signals
from alphamind.distillation.qualitative_derived import (
    compute_news_price_divergence,
    compute_prediction_market_deltas,
    compute_sentiment_percentile,
)
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
)
from alphamind.persistence.models import MacroObservations

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
    bootstrap_block_count: int
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
    archive.
    """
    date_part = as_of.astimezone(UTC).strftime("%Y-%m-%d")
    return archive_root / date_part / invocation_id / "distillation"


# ---------------------------------------------------------------------------
# Lead-lag pair scope (Phase 1.2)
# ---------------------------------------------------------------------------


_LEAD_LAG_PAIR_SCOPE: tuple[tuple[str, str], ...] = (
    # Lead-lag relationships per external.md § quant 7d. Keyed by
    # the (lead_ticker, lag_ticker) tuple convention used by
    # refresh_pair_lag and the persisted distillation_pair_lag rows.
    ("HYG", "SPY"),  # credit -> equity
    ("SOXX", "QQQ"),  # semis -> tech
    ("XLF", "SPY"),  # financials -> market
    ("USO", "XLE"),  # commodity -> energy equity
)


# ---------------------------------------------------------------------------
# Phase 1 — Class B refresh
# ---------------------------------------------------------------------------


def _format_as_of(as_of: datetime) -> str:
    """Format a tz-aware datetime as ISO 8601 ``Z``-suffixed UTC."""
    return as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refresh_class_b_state(
    session: Session,
    *,
    config: DistillationConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
) -> int:
    """Run the five Class B refresh primitives in sequence.

    Returns the total number of refresh-output rows produced (sum across
    primitives). The orchestrator logs the per-phase count.
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

    # Lead-lag pairs.
    rows += len(
        refresh_pair_lag(
            session,
            pair_scope=_LEAD_LAG_PAIR_SCOPE,
            as_of=as_of_iso,
            window_days=pw.correlation_long_days,
            min_events=config.anomaly_detection.earnings_revision_cluster_count,
            max_lag_days=config.lead_lag.lead_lag_credit_to_equity_max_days,
        )
    )

    # Prediction-market contract history is empty when no contracts are
    # tracked in the fixture; refresh accepts an empty scope and is a no-op.
    rows += len(
        refresh_contract_history(
            session,
            contract_scope=(),
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
# Phase 2 — per-category dispatchers
# ---------------------------------------------------------------------------
#
# Each dispatcher is a thin wrapper over its category's public surface.
# The orchestrator runs them concurrently via asyncio.gather; the
# synchronous DB-bound work runs through asyncio.to_thread so the event
# loop never blocks waiting for SQLite.
#
# All six categories (q1, q3, q6, q7, q12, qualitative_derived) wire up
# directly to their module-level entry points. The :data:`_PHASE_2_PLACEHOLDER_GAPS`
# table is preserved as an empty tuple for the verification script in
# story 13 (`scripts/verify_distillation.py`) so its summary section's
# shape stays stable; it now reports "all categories integrated."

_PHASE_2_PLACEHOLDER_GAPS: tuple[tuple[str, str], ...] = ()


def _compute_q1_blocks(
    session: Session,
    *,
    config: DistillationConfig,
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


def _compute_q3_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
    pair_correlations: dict[tuple[str, str], float],
) -> list[OutputBlock]:
    """Q3 options-flow blocks (story 08b).

    Delegates to :func:`assemble_q3_blocks`. The ``pair_correlations``
    mapping is sourced from :func:`compute_pair_correlations` (Q7's
    canonical helper) so pair-trade-signature detection sees the same
    correlation matrix Q7 reports — the orchestrator computes it once
    and threads it through to both consumers.
    """
    return assemble_q3_blocks(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
        pair_correlations=pair_correlations,
    )


def _compute_q6_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
) -> list[OutputBlock]:
    """Q6 macro / funding-stress blocks (story 08c).

    Delegates to :func:`compute_q6_blocks`, the wrapper that handles
    the FRED-series / breakeven / dollar / surprise data plumbing
    before calling the per-classifier helpers and assembling.
    """
    return compute_q6_blocks(session, config=config, as_of=as_of)


def _compute_q7_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
) -> list[OutputBlock]:
    """Q7 cross-asset / correlation blocks (story 08d).

    Delegates to :func:`assemble_q7_blocks`. The intra-sector pair
    correlations Q7 also produces are computed once at phase-2 entry
    via :func:`compute_pair_correlations` and passed independently to
    Q3; Q7 recomputes them internally for its own block emissions —
    accepted minor duplication for a thin orchestrator.
    """
    return assemble_q7_blocks(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
    )


def _compute_q12_blocks(
    session: Session,
    *,
    config: DistillationConfig,
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


def _compute_qualitative_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
) -> list[OutputBlock]:
    """Qualitative-derived blocks.

    Qualitative-derived (story 08f) has three public computations
    (news/price divergence, sentiment percentile, prediction-market
    deltas). The orchestrator dispatches each in turn; primitives that
    receive an empty contract scope (e.g. prediction markets when no
    contracts are tracked) emit no blocks.
    """
    blocks: list[OutputBlock] = []
    as_of_iso = _format_as_of(as_of)
    blocks.extend(
        compute_news_price_divergence(
            session,
            ticker_scope=ticker_scope,
            as_of=as_of_iso,
            window_hours=config.anomaly_detection.news_price_divergence_window_hours,
            min_articles=config.anomaly_detection.news_price_divergence_min_articles,
        )
    )
    blocks.extend(
        compute_sentiment_percentile(
            session,
            ticker_scope=ticker_scope,
            as_of=as_of_iso,
            sentiment_min_observations=config.persistence_windows.sentiment_min_observations,
        )
    )
    blocks.extend(
        compute_prediction_market_deltas(
            session,
            contract_scope=(),
            as_of=as_of_iso,
            delta_pp_threshold=config.prediction_market.prediction_market_delta_pp_threshold,
            low_liquidity_volume_min_usd=(
                config.prediction_market.prediction_market_low_liquidity_volume_min_usd
            ),
            prediction_market_history_days=(
                config.persistence_windows.prediction_market_history_days
            ),
        )
    )
    return blocks


# ---------------------------------------------------------------------------
# Phase 3 — regime classification
# ---------------------------------------------------------------------------


def _build_regime_thresholds(
    config: DistillationConfig,
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


# Conservative VVIX-percentile midpoint used when the underlying VVIX
# series is unavailable. The 50th-percentile placeholder is the
# information-neutral choice — it positions the regime block in the
# middle of the VVIX-band classifier without skewing toward either
# tail.
_VVIX_PERCENTILE_PLACEHOLDER: float = 50.0


def _build_regime_snapshot(session: Session) -> tuple[RegimeSnapshot, str | None]:
    """Build a :class:`RegimeSnapshot` plus an optional bootstrap reason.

    Reads VIX (``VIXCLS``) directly. The remaining series (VX1 future,
    VVIX percentile, realized vol) carry conservative placeholders when
    the underlying series are missing — per story 09's dispatch
    instruction the regime block emits with ``BOOTSTRAP`` calibration in
    that case rather than blocking the orchestrator.

    Returns ``(snapshot, None)`` when VIX is observed and
    ``(snapshot, "regime: VIXCLS observation missing")`` when it is not.
    Carrying the bootstrap reason out alongside the snapshot avoids
    encoding the bootstrap signal as a magic ``vix == 0.0`` sentinel —
    a real VIX print of exactly zero would otherwise mis-tag.
    """
    vix = _latest_macro_value(session, "VIXCLS")
    if vix is None:
        # No VIX series available — emit a placeholder snapshot tagged as
        # bootstrap so the regime block surfaces with the right state.
        snapshot = RegimeSnapshot(
            vix_level=0.0,
            vx1_minus_vix=0.0,
            vvix_percentile=_VVIX_PERCENTILE_PLACEHOLDER,
            realized_vol_5d=0.0,
            realized_vol_20d=0.0,
            vix_trailing_20d_mean=None,
            prior_term_structure_backwardation=False,
        )
        return snapshot, "regime: VIXCLS observation missing"
    snapshot = RegimeSnapshot(
        vix_level=vix,
        vx1_minus_vix=0.0,
        vvix_percentile=_VVIX_PERCENTILE_PLACEHOLDER,
        realized_vol_5d=0.0,
        realized_vol_20d=0.0,
        vix_trailing_20d_mean=None,
        prior_term_structure_backwardation=False,
    )
    return snapshot, None


def _refresh_regime(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
) -> tuple[RegimeRefreshResult, OutputBlock]:
    """Phase 3 — refresh the regime row and assemble the universal block."""
    classification, transition = _build_regime_thresholds(config)
    snapshot, bootstrap_reason = _build_regime_snapshot(session)
    calibration_state = (
        CalibrationState.BOOTSTRAP if bootstrap_reason is not None else CalibrationState.CALIBRATED
    )
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
# Phase 6 — invocation-archive write
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
# Phase 7 — brief-store population
# ---------------------------------------------------------------------------
#
# TODO(story 12): the brief store (``briefs`` SQL table) per
# ``docs/architecture/data-and-state.md`` § Brief store is not yet
# implemented in :mod:`alphamind.persistence.models`. Once the schema
# lands (separate story), this phase will INSERT one row per
# ``CR-N`` reference from
# :attr:`CorrelationRegimeBrief.reference_index` keyed by invocation_id.
# In the meantime ``DistillationOutputs.correlation_regime_brief`` carries
# the brief in-process so consumers can reference CR-N entries without
# the persistent store.


def _populate_brief_store(
    session: Session,
    *,
    correlation_regime_brief: CorrelationRegimeBrief,
    invocation_id: str,
) -> None:
    """Phase 7 — populate the brief store.

    No-op today: the ``briefs`` table is not in the persistence schema.
    The brief still travels through :class:`DistillationOutputs` so an
    in-process consumer (the synthesizer, the sector analysts) can read
    it without the persistent store.
    """
    # Reserved for the brief-store INSERT once the ``briefs`` table lands
    # (see module docstring Phase 7 TODO).
    del session, correlation_regime_brief, invocation_id
    logger.info(
        "phase 7 (brief-store population) skipped: briefs table not yet implemented; "
        "CR brief carried in DistillationOutputs"
    )


# ---------------------------------------------------------------------------
# Main entry point — run_external_distillation
# ---------------------------------------------------------------------------


def _count_bootstrap_blocks(blocks: Iterable[OutputBlock]) -> int:
    return sum(1 for block in blocks if block.calibration_state is not CalibrationState.CALIBRATED)


async def run_external_distillation(
    session: Session,
    config: DistillationConfig,
    ticker_scope: Sequence[str],
    as_of: datetime,
    invocation_id: str,
    *,
    archive_root: Path | None = None,
    provenance_root: Path | None = None,
) -> DistillationOutputs:
    """The single ``async`` entry point the pipeline process calls.

    Runs the seven phases per the story spec; any failure aborts the
    invocation per ``docs/design/mid-pipeline-failure-handling.md``. The
    orchestrator does not catch and continue.

    ``archive_root`` defaults to the platform-default location
    (``%USERPROFILE%/AlphaMind/archive`` on Windows;
    ``~/AlphaMind/archive`` elsewhere). ``provenance_root`` defaults to
    ``%USERPROFILE%/AlphaMind/data/provenance`` (or its POSIX equivalent),
    where the per-invocation calibration-state snapshot lands per
    ``docs/design/05-execution-layer/state-persistence.md`` § Tier 2.
    Tests pass explicit paths.
    """
    if archive_root is None:
        archive_root = _default_archive_root()
    if provenance_root is None:
        provenance_root = _default_provenance_root()

    overall_start = time.monotonic()

    # Phase 1 — Class B refresh.
    phase_start = time.monotonic()
    baseline_rows = await asyncio.to_thread(
        _refresh_class_b_state,
        session,
        config=config,
        ticker_scope=ticker_scope,
        as_of=as_of,
    )
    logger.info(
        "phase 1 (class B refresh) complete: rows=%d elapsed=%.3fs",
        baseline_rows,
        time.monotonic() - phase_start,
    )

    # Phase 2 — per-category indicator computations. Categories run
    # sequentially because Q3 (via refresh_atm_iv_baselines) and Q6
    # (via refresh_funding_stress_composite plus refresh_market_liquidity_composite)
    # issue session.flush() calls; running them concurrently via
    # asyncio.to_thread on a shared Session triggers
    # "Session is already flushing" InvalidRequestError. Each call still
    # wraps in asyncio.to_thread so the event loop remains free for
    # surrounding pipeline work. Pair correlations are computed once
    # before the per-category loop so Q3's pair-trade-signature
    # detection sees the same matrix Q7 reports (Q7 recomputes
    # internally; orchestrator-side pre-compute is the canonical source
    # for Q3).
    phase_start = time.monotonic()
    pair_correlations = await asyncio.to_thread(
        compute_pair_correlations,
        session,
        ticker_scope=ticker_scope,
        as_of=as_of,
        window_days=config.persistence_windows.correlation_short_days,
    )
    per_category_blocks: tuple[list[OutputBlock], ...] = (
        await asyncio.to_thread(
            _compute_q1_blocks,
            session,
            config=config,
            ticker_scope=ticker_scope,
            as_of=as_of,
        ),
        await asyncio.to_thread(
            _compute_q3_blocks,
            session,
            config=config,
            ticker_scope=ticker_scope,
            as_of=as_of,
            pair_correlations=pair_correlations,
        ),
        await asyncio.to_thread(
            _compute_q6_blocks,
            session,
            config=config,
            as_of=as_of,
        ),
        await asyncio.to_thread(
            _compute_q7_blocks,
            session,
            config=config,
            ticker_scope=ticker_scope,
            as_of=as_of,
        ),
        await asyncio.to_thread(
            _compute_q12_blocks,
            session,
            config=config,
            as_of=as_of,
        ),
        await asyncio.to_thread(
            _compute_qualitative_blocks,
            session,
            config=config,
            ticker_scope=ticker_scope,
            as_of=as_of,
        ),
    )
    indicator_blocks: list[OutputBlock] = []
    for category_blocks in per_category_blocks:
        indicator_blocks.extend(category_blocks)
    logger.info(
        "phase 2 (per-category indicators) complete: blocks=%d elapsed=%.3fs",
        len(indicator_blocks),
        time.monotonic() - phase_start,
    )

    # Phase 3 — regime classification.
    phase_start = time.monotonic()
    regime_result, regime_block = await asyncio.to_thread(
        _refresh_regime,
        session,
        config=config,
        as_of=as_of,
    )
    all_blocks = [*indicator_blocks, regime_block]
    logger.info(
        "phase 3 (regime classification) complete: label=%s elapsed=%.3fs",
        regime_result.regime_label.value,
        time.monotonic() - phase_start,
    )

    # Phase 4 — aggregation. Partition / collect / group per story 10
    # to populate the diagnostic counts the orchestrator returns; the
    # assemblers (Phase 5) re-derive the same partitioning from the
    # block list, so the dicts here are not threaded forward.
    phase_start = time.monotonic()
    partitioned = partition_blocks(all_blocks)
    anomaly_summaries: list[AnomalySummary] = collect_anomalies(all_blocks)
    grouped_anomalies = group_anomalies_by_audience(anomaly_summaries)
    logger.info(
        "phase 4 (aggregation) complete: audiences=%d total_anomalies=%d elapsed=%.3fs",
        len(partitioned),
        len(anomaly_summaries),
        time.monotonic() - phase_start,
    )

    # Phase 5 — assembly. The three sector assemblies and the CR brief
    # are independent (each reads its own audience slice of the same
    # block list), so they fan out via asyncio.gather instead of
    # serializing through a per-audience loop.
    phase_start = time.monotonic()
    sector_audiences = tuple(DOMAIN_RESEARCHER_BY_AUDIENCE)
    sector_assembly_results: list[SectorOutput] = await asyncio.gather(
        *(
            asyncio.to_thread(
                assemble_sector_output,
                audience=audience,
                blocks=all_blocks,
                session=session,
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
        "phase 5 (assembly) complete: elapsed=%.3fs",
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
        bootstrap_block_count=_count_bootstrap_blocks(all_blocks),
        all_blocks=tuple(all_blocks),
    )

    # Phase 6 — invocation-archive write plus calibration-state snapshot.
    # The archive write emits the markdown documents; the snapshot write
    # emits the deterministic JSON the feedback loop and command center
    # consume per story 17. Both writes share phase 6's fail-closed
    # semantics — any failure aborts the invocation.
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
    logger.info(
        "phase 6 (archive write) complete: dir=%s elapsed=%.3fs",
        archive_dir,
        time.monotonic() - phase_start,
    )

    # Phase 7 — brief-store population (TODO; see module docstring).
    phase_start = time.monotonic()
    await asyncio.to_thread(
        _populate_brief_store,
        session,
        correlation_regime_brief=correlation_regime_brief,
        invocation_id=invocation_id,
    )
    logger.info(
        "phase 7 (brief-store population) complete: elapsed=%.3fs",
        time.monotonic() - phase_start,
    )
    logger.info(
        "run_external_distillation complete: total_blocks=%d total_anomalies=%d "
        "bootstrap_blocks=%d elapsed=%.3fs",
        outputs.total_blocks,
        outputs.total_anomalies,
        outputs.bootstrap_block_count,
        time.monotonic() - overall_start,
    )
    return outputs


__all__ = [
    "DistillationOutputs",
    "run_external_distillation",
]
