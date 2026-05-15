"""Tests for ``refresh_pair_lag`` — story 02-distillation-layer/07.

Cover the per-pair lead-lag refresh entry point.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.baselines import refresh_pair_lag
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationPairLag,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory

# Pair-lag scan window — story 07 leaves the per-pair window unspecified;
# tests use the volume-baseline window and the spec's "10 observed pair
# events" boundary from threshold-calibration.md § Bootstrap policy.
PAIR_LAG_WINDOW_DAYS = 20
PAIR_LAG_MIN_EVENTS = 10
PAIR_LAG_MAX_DAYS = 7


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


def _add_close(session: Session, *, ticker: str, period_start: str, close: float) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=close,
            adj_high=close,
            adj_low=close,
            adj_close=close,
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


# ---------------------------------------------------------------------------
# Happy path — calibrated lead-lag estimate against fixture pair series
# ---------------------------------------------------------------------------


class TestRefreshPairLagHappyPath:
    def test_writes_calibrated_row_for_pair(self, session: Session) -> None:
        _add_ticker(session, "SMH")
        _add_ticker(session, "QQQ")
        # Build a series where QQQ's daily returns lag SMH's by 1 day.
        # SMH return on day i drives QQQ on day i+1.
        smh_returns = [
            0.01, -0.02, 0.03, 0.00, 0.02, -0.01, 0.04, -0.03, 0.01, 0.00,
            0.02, -0.01, 0.03, 0.00, 0.02, -0.04, 0.01, 0.00, 0.02, -0.01,
            0.03, 0.00, 0.02, -0.01, 0.01,
        ]  # fmt: skip
        smh_close = 100.0
        qqq_close = 200.0
        for day in range(1, 26):
            smh_close *= 1 + smh_returns[day - 1]
            ts = f"2026-04-{day:02d}T00:00:00Z"
            _add_close(session, ticker=Symbol("SMH"), period_start=ts, close=smh_close)
            # QQQ lags SMH by one day: today's QQQ return = yesterday's SMH return.
            if day == 1:
                _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=qqq_close)
            else:
                qqq_close *= 1 + smh_returns[day - 2]
                _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=qqq_close)
        session.commit()

        result = refresh_pair_lag(
            session,
            pair_scope=(("SMH", "QQQ"),),
            as_of="2026-04-25T00:00:00Z",
            window_days=PAIR_LAG_WINDOW_DAYS,
            min_events=PAIR_LAG_MIN_EVENTS,
            max_lag_days=PAIR_LAG_MAX_DAYS,
        )

        cv = result[("SMH", "QQQ")]
        assert cv.state is CalibrationState.CALIBRATED
        # The seeded data has QQQ lag SMH by 1 day; the estimate should be 1.
        assert cv.value["lead_lag_days_estimate"] == pytest.approx(1.0, abs=1e-9)
        # 25 daily bars → 24 day-over-day returns → at lag=1 alignment we have
        # 24 - 1 = 23 paired observations; well above min_events=10.
        assert cv.value["n_pair_events"] >= PAIR_LAG_MIN_EVENTS

        row = session.execute(
            select(DistillationPairLag).where(
                DistillationPairLag.lead_ticker == "SMH",
                DistillationPairLag.lag_ticker == "QQQ",
            )
        ).scalar_one()
        assert row.calibration_state == "calibrated"
        assert row.lead_lag_days_estimate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bootstrap path — too few aligned pair events
# ---------------------------------------------------------------------------


class TestRefreshPairLagBootstrapPath:
    def test_writes_bootstrap_row_when_aligned_events_below_min(self, session: Session) -> None:
        _add_ticker(session, "SMH")
        _add_ticker(session, "QQQ")
        # Only five bars per ticker → 4 returns, 3 aligned at lag=1 — well
        # below the 10-event minimum.
        for day in range(1, 6):
            ts = f"2026-04-{day:02d}T00:00:00Z"
            _add_close(session, ticker=Symbol("SMH"), period_start=ts, close=100.0 + day)
            _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=200.0 + day)
        session.commit()

        result = refresh_pair_lag(
            session,
            pair_scope=(("SMH", "QQQ"),),
            as_of="2026-04-25T00:00:00Z",
            window_days=PAIR_LAG_WINDOW_DAYS,
            min_events=PAIR_LAG_MIN_EVENTS,
            max_lag_days=PAIR_LAG_MAX_DAYS,
        )

        cv = result[("SMH", "QQQ")]
        assert cv.state is CalibrationState.BOOTSTRAP
        assert cv.bootstrap_reason is not None
        assert "pair_lag_min_events" in cv.bootstrap_reason

        row = session.execute(
            select(DistillationPairLag).where(
                DistillationPairLag.lead_ticker == "SMH",
            )
        ).scalar_one()
        assert row.calibration_state == "bootstrap"


# ---------------------------------------------------------------------------
# Idempotent re-run
# ---------------------------------------------------------------------------


class TestRefreshPairLagIdempotent:
    def test_rerun_on_same_scope_does_not_duplicate(self, session: Session) -> None:
        _add_ticker(session, "SMH")
        _add_ticker(session, "QQQ")
        for day in range(1, 26):
            ts = f"2026-04-{day:02d}T00:00:00Z"
            _add_close(session, ticker=Symbol("SMH"), period_start=ts, close=100.0 + day)
            _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=200.0 + day)
        session.commit()
        kwargs: dict[str, Any] = dict(
            pair_scope=(("SMH", "QQQ"),),
            as_of="2026-04-25T00:00:00Z",
            window_days=PAIR_LAG_WINDOW_DAYS,
            min_events=PAIR_LAG_MIN_EVENTS,
            max_lag_days=PAIR_LAG_MAX_DAYS,
        )
        refresh_pair_lag(session, **kwargs)
        first = session.scalar(select(func.count()).select_from(DistillationPairLag))
        refresh_pair_lag(session, **kwargs)
        second = session.scalar(select(func.count()).select_from(DistillationPairLag))
        assert first == 1
        assert second == 1


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


class TestRefreshPairLagFaultInjection:
    def test_query_failure_rolls_back_and_propagates(self, session: Session) -> None:
        _add_ticker(session, "SMH")
        _add_ticker(session, "QQQ")
        _add_ticker(session, "XLF")
        _add_ticker(session, "SPY")
        for day in range(1, 26):
            ts = f"2026-04-{day:02d}T00:00:00Z"
            _add_close(session, ticker=Symbol("SMH"), period_start=ts, close=100.0 + day)
            _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=200.0 + day)
            _add_close(session, ticker=Symbol("XLF"), period_start=ts, close=50.0 + day)
            _add_close(session, ticker=Symbol("SPY"), period_start=ts, close=300.0 + day)
        session.commit()

        original_execute = session.execute
        call_state = {"ohlcv_calls": 0}

        def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            stmt_text = str(statement).lower()
            if "from ohlcv_bars" in stmt_text:
                call_state["ohlcv_calls"] += 1
                # Fail after the first pair has finished its two queries.
                if call_state["ohlcv_calls"] >= 3:
                    raise RuntimeError("simulated mid-refresh DB failure")
            return original_execute(statement, *args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated mid-refresh"):
                refresh_pair_lag(
                    session,
                    pair_scope=(("SMH", "QQQ"), ("XLF", "SPY")),
                    as_of="2026-04-25T00:00:00Z",
                    window_days=PAIR_LAG_WINDOW_DAYS,
                    min_events=PAIR_LAG_MIN_EVENTS,
                    max_lag_days=PAIR_LAG_MAX_DAYS,
                )
        finally:
            session.execute = original_execute  # type: ignore[method-assign]

        # No pair-lag rows are persisted — the partial first-pair write
        # rolled back when the second pair's query failed.
        count = session.scalar(select(func.count()).select_from(DistillationPairLag))
        assert count == 0
