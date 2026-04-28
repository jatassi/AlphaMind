"""Tests for ``refresh_event_history`` — story 02-distillation-layer/07.

Two distinct refresh modes inside a single entry point:

- **Event detection** — append new rows for gaps / extended-hours moves
  observed since the last refresh, with ``outcome = "pending"`` until the
  outcome window passes.
- **Outcome resolution** — update prior pending rows once the underlying
  signal can be evaluated (gap filled at next session open; extended-hours
  direction confirmed in the first 30 min of regular trading).

Per story 07 the two paths must remain in separate branches so the
invariants are obvious.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.baselines import refresh_event_history
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationEventHistory,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory

# Per threshold-calibration.md § Bootstrap policy, gap-fill events are
# treated as calibrated once 30 events have been observed.
GAP_FILL_MIN_EVENTS = 30
GAP_DETECT_MIN_ATR_MULTIPLE = 1.5
EVENT_OUTCOME_RESOLUTION_DAYS = 1
EVENT_DETECTION_WINDOW_DAYS = 2


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker}",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_bar(
    session: Session,
    *,
    ticker: str,
    period_start: str,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=open_,
            adj_high=high,
            adj_low=low,
            adj_close=close,
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=open_,
            unadj_high=high,
            unadj_low=low,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


# ---------------------------------------------------------------------------
# Event detection — happy path: a gap is detected and appended with pending outcome
# ---------------------------------------------------------------------------


class TestRefreshEventHistoryDetection:
    def test_detects_overnight_gap_and_appends_pending_row(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # Day 1: typical bar, close at 100.
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-24T00:00:00Z",
            open_=99.0,
            high=101.0,
            low=98.0,
            close=100.0,
        )
        # Day 2: gap up — open at 105, well above prior close of 100.
        # ATR-magnitude relative to a 1-pt typical range → ~5x ATR, way above
        # the 1.5x detection threshold.
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-25T00:00:00Z",
            open_=105.0,
            high=106.0,
            low=104.5,
            close=105.5,
        )
        session.commit()

        result = refresh_event_history(
            session,
            event_kind="gap",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
            detection_atr_multiple=GAP_DETECT_MIN_ATR_MULTIPLE,
            outcome_resolution_days=EVENT_OUTCOME_RESOLUTION_DAYS,
            detection_window_days=EVENT_DETECTION_WINDOW_DAYS,
        )

        cv = result["AAPL"]
        # Bootstrap: only 1 event observed so far, far below 30-event minimum.
        assert cv.state is CalibrationState.BOOTSTRAP
        assert cv.value["n_events"] == 1
        assert cv.value["n_resolved"] == 0

        rows = session.execute(select(DistillationEventHistory)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_kind == "gap"
        assert row.outcome == "pending"
        assert row.outcome_observed_at is None
        assert row.direction == "up"
        assert row.magnitude_atr_multiple > GAP_DETECT_MIN_ATR_MULTIPLE


# ---------------------------------------------------------------------------
# Outcome resolution — pending row from a prior refresh resolves on rerun
# ---------------------------------------------------------------------------


class TestRefreshEventHistoryOutcomeResolution:
    def test_pending_gap_resolves_to_filled_after_window(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # Day 0: typical bar.
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-23T00:00:00Z",
            open_=99.0,
            high=101.0,
            low=98.0,
            close=100.0,
        )
        # Day 1: gap up — opens at 105, but range covers prior close (filled).
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-24T00:00:00Z",
            open_=105.0,
            high=106.0,
            low=99.5,
            close=104.0,
        )
        # Day 2: bar fully inside post-gap range — outcome window closed.
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-25T00:00:00Z",
            open_=104.0,
            high=106.0,
            low=103.0,
            close=105.0,
        )
        session.commit()

        # First refresh detects the gap and writes a pending row.
        first = refresh_event_history(
            session,
            event_kind="gap",
            ticker_scope=("AAPL",),
            as_of="2026-04-24T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
            detection_atr_multiple=GAP_DETECT_MIN_ATR_MULTIPLE,
            outcome_resolution_days=EVENT_OUTCOME_RESOLUTION_DAYS,
            detection_window_days=EVENT_DETECTION_WINDOW_DAYS,
        )
        assert first["AAPL"].value["n_events"] == 1
        assert first["AAPL"].value["n_resolved"] == 0

        # Second refresh: outcome window has now passed; the prior gap's
        # outcome resolves. The intra-bar low on day 1 (99.5) is below the
        # gap origin (100.0), so the gap was filled.
        second = refresh_event_history(
            session,
            event_kind="gap",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
            detection_atr_multiple=GAP_DETECT_MIN_ATR_MULTIPLE,
            outcome_resolution_days=EVENT_OUTCOME_RESOLUTION_DAYS,
            detection_window_days=EVENT_DETECTION_WINDOW_DAYS,
        )
        # No new gap event detected on day 2 — only the resolution.
        cv = second["AAPL"]
        assert cv.value["n_events"] == 1
        assert cv.value["n_resolved"] == 1

        rows = session.execute(select(DistillationEventHistory)).scalars().all()
        assert len(rows) == 1, "outcome resolution must NOT duplicate the row"
        row = rows[0]
        assert row.outcome == "filled"
        assert row.outcome_observed_at is not None


# ---------------------------------------------------------------------------
# Idempotent re-run (event detection)
# ---------------------------------------------------------------------------


class TestRefreshEventHistoryIdempotent:
    def test_rerun_on_same_as_of_does_not_duplicate_events(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-24T00:00:00Z",
            open_=99.0,
            high=101.0,
            low=98.0,
            close=100.0,
        )
        _add_bar(
            session,
            ticker="AAPL",
            period_start="2026-04-25T00:00:00Z",
            open_=105.0,
            high=106.0,
            low=104.5,
            close=105.5,
        )
        session.commit()
        kwargs: dict[str, Any] = dict(
            event_kind="gap",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            min_events=GAP_FILL_MIN_EVENTS,
            detection_atr_multiple=GAP_DETECT_MIN_ATR_MULTIPLE,
            outcome_resolution_days=EVENT_OUTCOME_RESOLUTION_DAYS,
            detection_window_days=EVENT_DETECTION_WINDOW_DAYS,
        )
        refresh_event_history(session, **kwargs)
        first_count = session.scalar(select(func.count()).select_from(DistillationEventHistory))
        refresh_event_history(session, **kwargs)
        second_count = session.scalar(select(func.count()).select_from(DistillationEventHistory))
        assert first_count == 1
        assert second_count == 1


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


class TestRefreshEventHistoryFaultInjection:
    def test_query_failure_rolls_back_and_propagates(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        for ticker in ("AAPL", "MSFT"):
            _add_bar(
                session,
                ticker=ticker,
                period_start="2026-04-24T00:00:00Z",
                open_=99.0,
                high=101.0,
                low=98.0,
                close=100.0,
            )
            _add_bar(
                session,
                ticker=ticker,
                period_start="2026-04-25T00:00:00Z",
                open_=105.0,
                high=106.0,
                low=104.5,
                close=105.5,
            )
        session.commit()

        original_execute = session.execute
        call_state = {"ohlcv_calls": 0}

        def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            stmt_text = str(statement).lower()
            if "from ohlcv_bars" in stmt_text:
                call_state["ohlcv_calls"] += 1
                # Fail mid-loop — after AAPL but before MSFT.
                if call_state["ohlcv_calls"] >= 2:
                    raise RuntimeError("simulated mid-refresh DB failure")
            return original_execute(statement, *args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated mid-refresh"):
                refresh_event_history(
                    session,
                    event_kind="gap",
                    ticker_scope=("AAPL", "MSFT"),
                    as_of="2026-04-25T00:00:00Z",
                    min_events=GAP_FILL_MIN_EVENTS,
                    detection_atr_multiple=GAP_DETECT_MIN_ATR_MULTIPLE,
                    outcome_resolution_days=EVENT_OUTCOME_RESOLUTION_DAYS,
                    detection_window_days=EVENT_DETECTION_WINDOW_DAYS,
                )
        finally:
            session.execute = original_execute  # type: ignore[method-assign]

        count = session.scalar(select(func.count()).select_from(DistillationEventHistory))
        assert count == 0
