"""Input-bundle integrity diagnostics for the qualitative researcher — ALP-492.

Emit WARN-level log records when the assembled bundle exhibits one of four
data-pipeline integrity gaps surfaced by the 2026-05-16 e2e run. None of
these abort the invocation — they are operator-grep signals that surface
silent upstream failures the qualitative researcher would otherwise only
flag in ``signal_quality_reason`` prose.

The four gaps:

- Gap 1: ``realized_vol_5d`` and ``realized_vol_20d`` both exactly ``0.0``
  (the empty-bar-window signature; legitimate near-zero is unaffected).
- Gap 2: ``sentiment_aggregates`` empty while a non-trivial number of
  calibrated sentiment baselines exist at or before ``as_of``.
- Gap 3: ``prediction_markets`` empty while at least one unresolved contract
  has ``distillation_contract_history`` at or before ``as_of``.
- Gap 4: a news-digest entry's labeled ticker (within the high-profile
  alias map) is absent from the headline while a *different* high-profile
  ticker IS referenced — ticker-misattribution drift.

Wiring scope
------------
The check fires from the qualitative-researcher runner only. The adaptive
researcher consumes ``universal_regime_label`` (Gap 1) from the same
distillation payload that feeds the qualitative path, so the WARN is
guaranteed to fire on Gap 1 regressions regardless of which researcher
runs first. Gaps 2/3/4 are qualitative-bundle-specific by construction:
sentiment aggregates, prediction-market snapshots, and news digests are
not part of the adaptive bundle's contract.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from alphamind.analysis.qualitative_research.loaders import QualitativeInputs
from alphamind.analysis.qualitative_research.news_digest import DigestEntry, NewsDigest
from alphamind.analysis.tools._envelope import format_iso
from alphamind.persistence.models import (
    DistillationContractHistory,
    DistillationTickerBaseline,
    PredictionMarketContracts,
)

__all__ = [
    "SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD",
    "log_input_bundle_integrity_warnings",
]

logger = logging.getLogger(__name__)


SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD: int = 5
"""Distinct calibrated sentiment baselines above which an empty
``sentiment_aggregates`` tuple looks like an assembler bug rather than a
bootstrap window. Five is the smallest pool that ``load_sentiment_aggregates``
can use as a stable fallback distribution without single-row dominance.
"""


_GOOGLE_ALIASES: frozenset[str] = frozenset({"GOOG", "GOOGL", "Google", "Alphabet"})

_TICKER_ALIASES: Mapping[str, frozenset[str]] = {
    "AAPL": frozenset({"AAPL", "Apple"}),
    "AMD": frozenset({"AMD", "Advanced Micro Devices"}),
    "AMZN": frozenset({"AMZN", "Amazon"}),
    "GOOG": _GOOGLE_ALIASES,
    "GOOGL": _GOOGLE_ALIASES,
    "META": frozenset({"META", "Meta Platforms", "Facebook"}),
    "MSFT": frozenset({"MSFT", "Microsoft"}),
    "NFLX": frozenset({"NFLX", "Netflix"}),
    "NVDA": frozenset({"NVDA", "Nvidia", "NVIDIA"}),
    "TSLA": frozenset({"TSLA", "Tesla"}),
}
"""Case-insensitive aliases for the high-profile universe tickers most
likely to surface in news digests. The drift detector only fires for
labeled tickers in this map — small-cap and ETF labels are skipped to keep
false positives out of the WARN stream.
"""


_ALIAS_PATTERNS: Mapping[str, re.Pattern[str]] = {
    alias: re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)
    for aliases in _TICKER_ALIASES.values()
    for alias in aliases
}
"""Precompiled word-boundary patterns for every alias in
:data:`_TICKER_ALIASES`. Built once at import so the per-digest drift
detector does not recompile on each entry-alias combination.
"""


_HEADLINE_LOG_MAX_CHARS: int = 80
"""Maximum headline characters surfaced in the Gap 4 WARN message — enough
context for the operator to identify the story without overflowing log
lines. The full headline remains accessible via the digest archive.
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def log_input_bundle_integrity_warnings(
    session: Session,
    *,
    as_of: datetime,
    regime_label: Mapping[str, Any],
    inputs: QualitativeInputs,
    news_digest: NewsDigest,
) -> None:
    """Emit WARN log records for the four input-bundle integrity gaps.

    Side-effect only; the function never raises. Callers should pass the
    same ``inputs`` / ``regime_label`` / ``news_digest`` that will be sent
    to the harness so the WARN annotates the invocation those values
    produced.
    """
    as_of_iso = format_iso(as_of)
    _warn_gap1_realized_vol_double_zero(regime_label)
    _warn_gap2_sentiment_block_drop(session, as_of_iso=as_of_iso, inputs=inputs)
    _warn_gap3_prediction_market_block_drop(session, as_of_iso=as_of_iso, inputs=inputs)
    _warn_gap4_news_ticker_drift(news_digest)


# ---------------------------------------------------------------------------
# Gap 1 — realized-vol double zero
# ---------------------------------------------------------------------------


def _warn_gap1_realized_vol_double_zero(regime_label: Mapping[str, Any]) -> None:
    if regime_label.get("realized_vol_5d") == 0.0 and regime_label.get("realized_vol_20d") == 0.0:
        logger.warning(
            "input-bundle integrity (ALP-492 Gap 1): realized_vol_5d and "
            "realized_vol_20d both report 0.0 — likely an empty upstream bar "
            "window rather than genuine near-zero realized vol"
        )


# ---------------------------------------------------------------------------
# Gap 2 — sentiment aggregates empty despite calibrated baselines
# ---------------------------------------------------------------------------


def _warn_gap2_sentiment_block_drop(
    session: Session, *, as_of_iso: str, inputs: QualitativeInputs
) -> None:
    if inputs.sentiment_aggregates:
        return
    calibrated = _count_calibrated_sentiment_baselines(session, as_of_iso=as_of_iso)
    if calibrated >= SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD:
        logger.warning(
            "input-bundle integrity (ALP-492 Gap 2): sentiment_aggregates empty "
            "but %d calibrated sentiment baselines exist at or before %s "
            "(threshold %d) — the assembler may be dropping the column",
            calibrated,
            as_of_iso,
            SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD,
        )


# ---------------------------------------------------------------------------
# Gap 3 — prediction-market snapshot empty despite unresolved contracts with history
# ---------------------------------------------------------------------------


def _warn_gap3_prediction_market_block_drop(
    session: Session, *, as_of_iso: str, inputs: QualitativeInputs
) -> None:
    if inputs.prediction_markets:
        return
    contracts_with_history = _count_unresolved_contracts_with_history(session, as_of_iso=as_of_iso)
    if contracts_with_history > 0:
        logger.warning(
            "input-bundle integrity (ALP-492 Gap 3): prediction_markets empty "
            "but %d unresolved contracts have history at or before %s — the "
            "assembler may be dropping the block",
            contracts_with_history,
            as_of_iso,
        )


# ---------------------------------------------------------------------------
# Gap 4 — news-ticker labeling drift
# ---------------------------------------------------------------------------


def _warn_gap4_news_ticker_drift(news_digest: NewsDigest) -> None:
    for entry in news_digest.entries:
        for labeled in entry.tickers:
            other_present = _drift_other_tickers(entry, labeled=labeled)
            if not other_present:
                continue
            logger.warning(
                "input-bundle integrity (ALP-492 Gap 4): news entry %s labeled "
                "%s but headline references %s — ticker-attribution drift "
                "(headline: %r)",
                entry.reference_id,
                labeled,
                ", ".join(sorted(other_present)),
                _truncate_headline(entry.headline),
            )


def _drift_other_tickers(entry: DigestEntry, *, labeled: str) -> tuple[str, ...]:
    """Return the high-profile tickers whose aliases appear in the headline.

    Empty tuple means no drift — either the labeled ticker is outside the
    alias map, its own aliases are in the headline, or no other tracked
    ticker is referenced. The caller logs a single WARN per (entry, label)
    pair when this is non-empty.
    """
    label_aliases = _TICKER_ALIASES.get(labeled)
    if label_aliases is None:
        return ()
    if any(_alias_in_headline(alias, entry.headline) for alias in label_aliases):
        return ()
    return tuple(
        sorted(
            other
            # ``isdisjoint`` excludes tickers that share aliases with the labeled
            # ticker (e.g., GOOG ↔ GOOGL) so class-share pairs don't false-flag.
            for other, aliases in _TICKER_ALIASES.items()
            if aliases.isdisjoint(label_aliases)
            and any(_alias_in_headline(a, entry.headline) for a in aliases)
        )
    )


def _alias_in_headline(alias: str, headline: str) -> bool:
    """Return whether ``alias`` appears in ``headline`` as a whole word.

    Word-boundary matching avoids false positives like ``Apple`` matching
    ``pineapple``; case-insensitive matching catches ``Microsoft`` /
    ``MICROSOFT`` regardless of headline casing. Patterns are precompiled
    in :data:`_ALIAS_PATTERNS`.
    """
    pattern = _ALIAS_PATTERNS.get(alias)
    if pattern is None:
        return False
    return pattern.search(headline) is not None


def _truncate_headline(headline: str) -> str:
    if len(headline) <= _HEADLINE_LOG_MAX_CHARS:
        return headline
    return headline[: _HEADLINE_LOG_MAX_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _count_calibrated_sentiment_baselines(session: Session, *, as_of_iso: str) -> int:
    result = session.execute(
        select(func.count(func.distinct(DistillationTickerBaseline.ticker))).where(
            DistillationTickerBaseline.baseline_kind == "sentiment",
            DistillationTickerBaseline.calibration_state == "calibrated",
            DistillationTickerBaseline.as_of <= as_of_iso,
        )
    ).scalar()
    return int(result or 0)


def _count_unresolved_contracts_with_history(session: Session, *, as_of_iso: str) -> int:
    """Distinct contracts whose ``resolution_date`` has not passed and which
    have ``distillation_contract_history`` at or before ``as_of_iso``.

    Mirrors the ``resolution_date IS NULL OR resolution_date > as_of`` filter
    used by :func:`alphamind.distillation.contract_scope.resolve_prediction_market_scope`
    so the WARN aligns with the loader's notion of "contract scope is non-empty".
    """
    result = session.execute(
        select(func.count(func.distinct(DistillationContractHistory.contract_id)))
        .join(
            PredictionMarketContracts,
            PredictionMarketContracts.contract_id == DistillationContractHistory.contract_id,
        )
        .where(
            DistillationContractHistory.snapshot_ts <= as_of_iso,
            or_(
                PredictionMarketContracts.resolution_date.is_(None),
                PredictionMarketContracts.resolution_date > as_of_iso,
            ),
        )
    ).scalar()
    return int(result or 0)
