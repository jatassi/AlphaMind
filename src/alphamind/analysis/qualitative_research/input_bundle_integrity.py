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
- Gap 3: ``prediction_markets`` empty while ``distillation_contract_history``
  carries at least one row at or before ``as_of``.
- Gap 4: a news-digest entry's labeled ticker (within the high-profile
  alias map) is absent from the headline while a *different* high-profile
  ticker IS referenced — ticker-misattribution drift.

Public names
------------
- :data:`SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD`
- :data:`TICKER_ALIASES`
- :func:`log_input_bundle_integrity_warnings`
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.analysis.qualitative_research.loaders import QualitativeInputs
from alphamind.analysis.qualitative_research.news_digest import DigestEntry, NewsDigest
from alphamind.persistence.models import (
    DistillationContractHistory,
    DistillationTickerBaseline,
)

__all__ = [
    "SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD",
    "TICKER_ALIASES",
    "log_input_bundle_integrity_warnings",
]

logger = logging.getLogger(__name__)


SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD: int = 5
"""Distinct calibrated sentiment baselines above which an empty
``sentiment_aggregates`` tuple looks like an assembler bug rather than a
bootstrap window. Five is the smallest pool that ``load_sentiment_aggregates``
can use as a stable fallback distribution without single-row dominance.
"""


TICKER_ALIASES: Mapping[str, frozenset[str]] = {
    "AAPL": frozenset({"AAPL", "Apple"}),
    "AMD": frozenset({"AMD", "Advanced Micro Devices"}),
    "AMZN": frozenset({"AMZN", "Amazon"}),
    "GOOG": frozenset({"GOOG", "GOOGL", "Google", "Alphabet"}),
    "GOOGL": frozenset({"GOOG", "GOOGL", "Google", "Alphabet"}),
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
    _warn_gap1_realized_vol_double_zero(regime_label)
    _warn_gap2_sentiment_block_drop(session, as_of=as_of, inputs=inputs)
    _warn_gap3_prediction_market_block_drop(session, as_of=as_of, inputs=inputs)
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
    session: Session, *, as_of: datetime, inputs: QualitativeInputs
) -> None:
    if inputs.sentiment_aggregates:
        return
    calibrated = _count_calibrated_sentiment_baselines(session, as_of=as_of)
    if calibrated >= SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD:
        logger.warning(
            "input-bundle integrity (ALP-492 Gap 2): sentiment_aggregates empty "
            "but %d calibrated sentiment baselines exist at or before %s "
            "(threshold %d) — the assembler may be dropping the column",
            calibrated,
            _format_iso_utc(as_of),
            SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD,
        )


# ---------------------------------------------------------------------------
# Gap 3 — prediction-market snapshot empty despite contracts with history
# ---------------------------------------------------------------------------


def _warn_gap3_prediction_market_block_drop(
    session: Session, *, as_of: datetime, inputs: QualitativeInputs
) -> None:
    if inputs.prediction_markets:
        return
    contracts_with_history = _count_contracts_with_history(session, as_of=as_of)
    if contracts_with_history > 0:
        logger.warning(
            "input-bundle integrity (ALP-492 Gap 3): prediction_markets empty "
            "but %d contracts have history at or before %s — the assembler may "
            "be dropping the block",
            contracts_with_history,
            _format_iso_utc(as_of),
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
                "%s but headline references %s — ticker-attribution drift",
                entry.reference_id,
                labeled,
                ", ".join(sorted(other_present)),
            )


def _drift_other_tickers(entry: DigestEntry, *, labeled: str) -> tuple[str, ...]:
    """Return the high-profile tickers whose aliases appear in the headline.

    Empty tuple means no drift — either the labeled ticker is outside the
    alias map, its own aliases are in the headline, or no other tracked
    ticker is referenced. The caller logs a single WARN per (entry, label)
    pair when this is non-empty.
    """
    label_aliases = TICKER_ALIASES.get(labeled)
    if label_aliases is None:
        return ()
    if any(_alias_in_headline(alias, entry.headline) for alias in label_aliases):
        return ()
    return tuple(
        sorted(
            other
            for other, aliases in TICKER_ALIASES.items()
            if aliases.isdisjoint(label_aliases)
            and any(_alias_in_headline(a, entry.headline) for a in aliases)
        )
    )


def _alias_in_headline(alias: str, headline: str) -> bool:
    """Return whether ``alias`` appears in ``headline`` as a whole word.

    Word-boundary matching avoids false positives like ``Apple`` matching
    ``pineapple``; case-insensitive matching catches ``Microsoft`` /
    ``MICROSOFT`` regardless of headline casing.
    """
    pattern = rf"\b{re.escape(alias)}\b"
    return re.search(pattern, headline, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _count_calibrated_sentiment_baselines(session: Session, *, as_of: datetime) -> int:
    as_of_str = _format_iso_utc(as_of)
    result = session.execute(
        select(func.count(func.distinct(DistillationTickerBaseline.ticker))).where(
            DistillationTickerBaseline.baseline_kind == "sentiment",
            DistillationTickerBaseline.calibration_state == "calibrated",
            DistillationTickerBaseline.as_of <= as_of_str,
        )
    ).scalar()
    return int(result or 0)


def _count_contracts_with_history(session: Session, *, as_of: datetime) -> int:
    as_of_str = _format_iso_utc(as_of)
    result = session.execute(
        select(func.count(func.distinct(DistillationContractHistory.contract_id))).where(
            DistillationContractHistory.snapshot_ts <= as_of_str,
        )
    ).scalar()
    return int(result or 0)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
