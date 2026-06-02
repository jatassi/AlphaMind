"""Tests for the gap analysis computation — story 08a.

Covers:

- Gap classification (full / partial, with-trend / counter-trend).
- Gap-fill probability lookup with per-ticker (calibrated) vs sector-pooled
  (bootstrap) fallback.
- Gap event detection that writes a new ``distillation_event_history`` row
  with ``outcome = "pending"`` (the schema-level sentinel for "unresolved")
  and does not duplicate on rerun.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.q1.gap import (
    GAP_KIND_FULL,
    GAP_KIND_PARTIAL,
    GAP_TREND_COUNTER,
    GAP_TREND_WITH,
    DetectedGap,
    GapAnalysisResult,
    analyze_gap,
    detect_session_gap,
    record_pending_gap_event,
    resolve_gap_fill_probability,
)
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationEventHistory,
    SectorClassification,
)

# Mirrors config/distillation.yaml: gap_fill events are calibrated once 30
# events have been observed.
GAP_FILL_MIN_EVENTS = 30


def _add_ticker(session: Session, ticker: str, *, sector: str = "tech") -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="manual",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_gap_event(
    session: Session,
    *,
    ticker: str,
    event_ts: str,
    outcome: str,
    direction: str = "up",
    magnitude: float = 2.0,
) -> None:
    session.add(
        DistillationEventHistory(
            ticker=ticker,
            event_kind="gap",
            event_ts=event_ts,
            direction=direction,
            magnitude_atr_multiple=magnitude,
            outcome=outcome,
            outcome_observed_at=event_ts if outcome != "pending" else None,
            ingested_at=event_ts,
        )
    )


# ---------------------------------------------------------------------------
# Gap analysis (classification + ATR ratio)
# ---------------------------------------------------------------------------


class TestAnalyzeGap:
    def test_full_with_trend_gap_up(self) -> None:
        """Open above prior high with prior bar bullish = full, with-trend."""
        result = analyze_gap(
            today_open=110.0,
            prior_close=100.0,
            prior_high=104.0,
            prior_low=99.0,
            atr_14d=2.0,
            trend_direction="up",
        )
        assert isinstance(result, GapAnalysisResult)
        assert result.gap_absolute == pytest.approx(10.0)
        assert result.gap_atr_ratio == pytest.approx(5.0)
        assert result.kind == GAP_KIND_FULL
        assert result.trend_classification == GAP_TREND_WITH

    def test_partial_counter_trend_gap_down(self) -> None:
        """Down gap inside prior range with prior trend up = partial, counter-trend."""
        result = analyze_gap(
            today_open=98.0,
            prior_close=100.0,
            prior_high=104.0,
            prior_low=97.0,
            atr_14d=2.0,
            trend_direction="up",
        )
        assert result.gap_absolute == pytest.approx(-2.0)
        assert result.kind == GAP_KIND_PARTIAL
        assert result.trend_classification == GAP_TREND_COUNTER


# ---------------------------------------------------------------------------
# Gap-fill probability lookup
# ---------------------------------------------------------------------------


class TestResolveGapFillProbability:
    def test_per_ticker_rate_used_when_calibrated(self, session: Session) -> None:
        """When per-ticker history meets min_events, use the per-ticker rate."""
        _add_ticker(session, "AAPL", sector="tech")
        # Add enough events to clear the calibration threshold; alternate
        # outcomes so the per-ticker rate is exactly 0.5. Use unique
        # timestamps via hour offsets so the (ticker, kind, ts) PK never
        # collides.
        for i in range(30):
            outcome = "filled" if i % 2 == 0 else "unfilled"
            _add_gap_event(
                session,
                ticker=Symbol("AAPL"),
                event_ts=f"2026-03-01T00:{i:02d}:00Z",
                outcome=outcome,
            )
        session.commit()

        result = resolve_gap_fill_probability(
            session,
            ticker=Symbol("AAPL"),
            sector="tech",
            as_of="2026-04-25T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
        )
        assert result.state is CalibrationState.CALIBRATED
        assert result.value == pytest.approx(0.5)

    def test_sector_pooled_fallback_when_bootstrap(self, session: Session) -> None:
        """Below min_events for the ticker, fall back to the sector-pooled rate."""
        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        # AAPL: only 3 events — well below the 30-event threshold.
        for i in range(3):
            _add_gap_event(
                session,
                ticker=Symbol("AAPL"),
                event_ts=f"2026-03-{i + 1:02d}T00:00:00Z",
                outcome="unfilled",
            )
        # MSFT: 30 events, all filled — sector pool ends up dominated by MSFT.
        for i in range(30):
            _add_gap_event(
                session,
                ticker=Symbol("MSFT"),
                event_ts=f"2026-02-01T00:{i:02d}:00Z",
                outcome="filled",
            )
        session.commit()

        result = resolve_gap_fill_probability(
            session,
            ticker=Symbol("AAPL"),
            sector="tech",
            as_of="2026-04-25T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
        )
        assert result.state is CalibrationState.ACCUMULATING
        # Sector pool: MSFT 30 filled + AAPL 3 unfilled = 30/33 fill rate.
        assert result.value == pytest.approx(30.0 / 33.0)

    def test_returns_unavailable_when_neither_per_ticker_nor_sector_pool(
        self, session: Session
    ) -> None:
        """Empty sector pool collapses to UNAVAILABLE per the calibration framework."""
        _add_ticker(session, "AAPL", sector="tech")
        session.commit()
        result = resolve_gap_fill_probability(
            session,
            ticker=Symbol("AAPL"),
            sector="tech",
            as_of="2026-04-25T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
        )
        assert result.state is CalibrationState.UNAVAILABLE
        assert result.value is None


# ---------------------------------------------------------------------------
# Pending event recording
# ---------------------------------------------------------------------------


class TestRecordPendingGapEvent:
    def test_writes_pending_row_on_first_sight(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        session.commit()
        detected = DetectedGap(
            ticker=Symbol("AAPL"),
            event_ts="2026-04-25T00:00:00Z",
            direction="up",
            magnitude_atr_multiple=3.0,
        )
        record_pending_gap_event(session, detected, ingested_at="2026-04-25T00:00:00Z")
        session.commit()

        rows = session.execute(select(DistillationEventHistory)).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "pending"
        assert rows[0].outcome_observed_at is None
        assert rows[0].direction == "up"
        assert rows[0].magnitude_atr_multiple == pytest.approx(3.0)

    def test_does_not_duplicate_on_rerun(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        session.commit()
        detected = DetectedGap(
            ticker=Symbol("AAPL"),
            event_ts="2026-04-25T00:00:00Z",
            direction="up",
            magnitude_atr_multiple=3.0,
        )
        record_pending_gap_event(session, detected, ingested_at="2026-04-25T00:00:00Z")
        session.commit()
        record_pending_gap_event(session, detected, ingested_at="2026-04-25T00:00:00Z")
        session.commit()
        count = session.scalar(select(func.count()).select_from(DistillationEventHistory))
        assert count == 1

    def test_does_not_overwrite_resolved_outcome(self, session: Session) -> None:
        """A second-sight detection on a resolved row leaves the outcome alone."""
        _add_ticker(session, "AAPL")
        # Pre-populate a resolved row.
        _add_gap_event(
            session,
            ticker=Symbol("AAPL"),
            event_ts="2026-04-25T00:00:00Z",
            outcome="filled",
        )
        session.commit()
        detected = DetectedGap(
            ticker=Symbol("AAPL"),
            event_ts="2026-04-25T00:00:00Z",
            direction="up",
            magnitude_atr_multiple=3.0,
        )
        record_pending_gap_event(session, detected, ingested_at="2026-04-25T00:00:00Z")
        session.commit()
        rows = session.execute(select(DistillationEventHistory)).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "filled", "must not regress filled → pending"


# ---------------------------------------------------------------------------
# Detection — the integrated entry point that ties the pieces together
# ---------------------------------------------------------------------------


class TestDetectSessionGap:
    def test_returns_none_when_open_inside_prior_range(self) -> None:
        """A small-overlap open is not a gap — no event row to write."""
        gap = detect_session_gap(
            ticker=Symbol("AAPL"),
            today_open=100.5,
            prior_close=100.0,
            atr_14d=2.0,
            event_ts="2026-04-25T00:00:00Z",
            min_atr_multiple=1.5,
        )
        assert gap is None

    def test_returns_detected_gap_when_open_clears_threshold(self) -> None:
        gap = detect_session_gap(
            ticker=Symbol("AAPL"),
            today_open=110.0,
            prior_close=100.0,
            atr_14d=2.0,
            event_ts="2026-04-25T00:00:00Z",
            min_atr_multiple=1.5,
        )
        assert isinstance(gap, DetectedGap)
        assert gap.direction == "up"
        # 10-pt gap / 2.0 ATR = 5.0 * ATR.
        assert gap.magnitude_atr_multiple == pytest.approx(5.0)
