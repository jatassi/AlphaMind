"""Tests for ``q7_cross_asset.compute_breadth_internals`` — story 08d.

Cover the percentage of universe names above EMA, advance/decline within
sectors, and equal-weight vs. cap-weight performance comparison.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import compute_breadth_internals
from alphamind.distillation.q7.breadth_internals_compute import (
    compute_breadth_internals_pure,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OhlcvBars,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory


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


def _add_ticker(session: Session, ticker: str, *, sector: str = "tech") -> None:
    asset_id = f"asset-{ticker.lower()}"
    session.add(
        AssetUniverse(
            asset_id=asset_id,
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
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=asset_id,
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="test",
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


def _seed_path(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


# ---------------------------------------------------------------------------
# Percentage above EMA
# ---------------------------------------------------------------------------


class TestBreadthPctAboveEMA:
    def test_pct_above_20day_ema_aggregates_correctly(self, session: Session) -> None:
        # Three tickers in the universe.
        # AAPL: rising path → finishes above 20-day EMA.
        # MSFT: also rising → finishes above 20-day EMA.
        # GOOG: declining path → finishes below 20-day EMA.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=20)

        rising = [100.0 * (1.0 + 0.01 * i) for i in range(21)]
        falling = [120.0 * (1.0 - 0.01 * i) for i in range(21)]

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "GOOG", sector="tech")
        _seed_path(session, ticker=Symbol("AAPL"), closes=rising, start_day=start_day)
        _seed_path(session, ticker=Symbol("MSFT"), closes=rising, start_day=start_day)
        _seed_path(session, ticker=Symbol("GOOG"), closes=falling, start_day=start_day)
        # Add SPY for cap-weight reference.
        _add_ticker(session, "SPY", sector="tech")
        _seed_path(session, ticker=Symbol("SPY"), closes=rising, start_day=start_day)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT", "GOOG"),
            sector_members={"tech": ("AAPL", "MSFT", "GOOG")},
            as_of=as_of,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q7.breadth_internals"
        # Breadth and intermarket carry UNIVERSAL_BROADCAST per the story.
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

        # 2 of 3 tickers are above 20-day EMA → ~66.7%.
        pct_above = block.payload["pct_above_20d_ema"]
        assert 0.6 < pct_above < 0.7

    def test_advance_decline_per_sector_counts_correctly(self, session: Session) -> None:
        # Two sectors: tech (3 tickers) and financials (2 tickers).
        # Today: AAPL up, MSFT up, GOOG down → tech 2 up, 1 down.
        #        JPM up, BAC down → financials 1 up, 1 down.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        prev_day = as_of - timedelta(days=1)

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "GOOG", sector="tech")
        _add_ticker(session, "JPM", sector="financials")
        _add_ticker(session, "BAC", sector="financials")
        _add_ticker(session, "SPY", sector="tech")

        prev_iso = prev_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        as_of_iso = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Previous closes.
        for ticker in ("AAPL", "MSFT", "GOOG", "JPM", "BAC", "SPY"):
            _add_close(session, ticker=ticker, period_start=prev_iso, close=100.0)
        # Today's closes — direction varies.
        _add_close(session, ticker=Symbol("AAPL"), period_start=as_of_iso, close=101.0)
        _add_close(session, ticker=Symbol("MSFT"), period_start=as_of_iso, close=102.0)
        _add_close(session, ticker=Symbol("GOOG"), period_start=as_of_iso, close=99.0)
        _add_close(session, ticker=Symbol("JPM"), period_start=as_of_iso, close=101.0)
        _add_close(session, ticker=Symbol("BAC"), period_start=as_of_iso, close=98.0)
        _add_close(session, ticker=Symbol("SPY"), period_start=as_of_iso, close=101.0)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT", "GOOG", "JPM", "BAC"),
            sector_members={
                "tech": ("AAPL", "MSFT", "GOOG"),
                "financials": ("JPM", "BAC"),
            },
            as_of=as_of,
        )

        block = blocks[0]
        ad = block.payload["advance_decline_per_sector"]
        # Sorted by sector key for deterministic rendering.
        assert ad["financials"] == {"advances": 1, "declines": 1}
        assert ad["tech"] == {"advances": 2, "declines": 1}

    def test_equal_vs_cap_weight_compares_today(self, session: Session) -> None:
        # Equal-weight: simple mean of universe daily returns.
        # Cap-weight: SPY daily return.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        prev_day = as_of - timedelta(days=1)

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "SPY", sector="tech")

        prev_iso = prev_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        as_of_iso = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for ticker in ("AAPL", "MSFT", "SPY"):
            _add_close(session, ticker=ticker, period_start=prev_iso, close=100.0)
        # AAPL +2%, MSFT +4% → equal-weight return = (0.02 + 0.04) / 2 = 0.03.
        _add_close(session, ticker=Symbol("AAPL"), period_start=as_of_iso, close=102.0)
        _add_close(session, ticker=Symbol("MSFT"), period_start=as_of_iso, close=104.0)
        # SPY +1% → cap-weight proxy = 0.01.
        _add_close(session, ticker=Symbol("SPY"), period_start=as_of_iso, close=101.0)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT"),
            sector_members={"tech": ("AAPL", "MSFT")},
            as_of=as_of,
        )

        block = blocks[0]
        eq_vs_cap = block.payload["equal_vs_cap_weight"]
        assert eq_vs_cap["equal_weight_return"] == pytest.approx(0.03, abs=1e-9)
        assert eq_vs_cap["cap_weight_return"] == pytest.approx(0.01, abs=1e-9)
        assert eq_vs_cap["spread"] == pytest.approx(0.02, abs=1e-9)


# ---------------------------------------------------------------------------
# Per-window calibration of pct_above_EMA breadth metrics (ALP-576).
#
# The block tracks each EMA window's observation threshold separately so the
# 200d window's simple-mean fallback never silently degenerates onto the 50d
# real-EMA value. Pure-compute tests below construct close paths directly,
# bypassing the session-bound loader, since the bootstrap-window mechanics are
# independent of the DB shape.
# ---------------------------------------------------------------------------


def _as_of() -> datetime:
    return datetime(2026, 5, 19, tzinfo=UTC)


class TestPerWindowCalibration:
    def test_distinct_50d_and_200d_breadth_when_underlying_emas_diverge(self) -> None:
        # 250 closes per ticker so every EMA window is calibrated. The
        # universe is shaped asymmetrically: two tickers sit above the 200d
        # EMA but below the 50d EMA (mild recent dip after a long uptrend),
        # one ticker above the 50d EMA but below the 200d EMA (mild recent
        # rally after a long downtrend). The 50d-above count and the
        # 200d-above count then diverge, exercising the case the production
        # bug (bit-identical 0.6515) hid behind a simple-mean fallback.
        n = 250
        knee = n - 30
        closes_by_ticker: dict[str, tuple[float, ...]] = {
            "trend_up": tuple(100.0 + 0.5 * i for i in range(n)),
            "trend_down": tuple(200.0 - 0.4 * i for i in range(n)),
            "recent_dip_a": tuple(
                100.0 + 0.5 * i if i < knee else 210.0 - 1.0 * (i - knee) for i in range(n)
            ),
            "recent_dip_b": tuple(
                100.0 + 0.5 * i if i < knee else 210.0 - 1.0 * (i - knee) for i in range(n)
            ),
            "recent_rally": tuple(
                200.0 - 0.4 * i if i < knee else 200.0 - 0.4 * knee + 1.0 * (i - knee)
                for i in range(n)
            ),
            "flat": tuple(150.0 for _ in range(n)),
        }

        block = compute_breadth_internals_pure(
            closes_by_ticker=closes_by_ticker,
            sector_members={"sector": tuple(closes_by_ticker)},
            universe_tickers=tuple(closes_by_ticker),
            broad_market_closes=closes_by_ticker["trend_up"],
            as_of=_as_of(),
        )

        payload = block.payload
        assert block.calibration_state is CalibrationState.CALIBRATED
        assert block.bootstrap_reason is None
        # All three windows return a fraction; the 50d and 200d fractions are
        # not bit-identical when their underlying EMAs differ.
        pct_20 = payload["pct_above_20d_ema"]
        pct_50 = payload["pct_above_50d_ema"]
        pct_200 = payload["pct_above_200d_ema"]
        assert isinstance(pct_20, float)
        assert isinstance(pct_50, float)
        assert isinstance(pct_200, float)
        assert pct_50 != pct_200

    def test_undercalibrated_200d_window_emits_null_sentinel(self) -> None:
        # 100 closes per ticker — above the 20d and 50d thresholds, below the
        # 200d threshold. The 200d window must emit null rather than the
        # simple-mean fallback that masquerades as a real EMA reading.
        n = 100
        closes_by_ticker = {
            ticker: tuple(100.0 + 0.5 * i for i in range(n)) for ticker in ("A", "B", "C", "D", "E")
        }

        block = compute_breadth_internals_pure(
            closes_by_ticker=closes_by_ticker,
            sector_members={"sector": tuple(closes_by_ticker)},
            universe_tickers=tuple(closes_by_ticker),
            broad_market_closes=closes_by_ticker["A"],
            as_of=_as_of(),
        )

        payload = block.payload
        assert isinstance(payload["pct_above_20d_ema"], float)
        assert isinstance(payload["pct_above_50d_ema"], float)
        assert payload["pct_above_200d_ema"] is None
        assert block.calibration_state is CalibrationState.ACCUMULATING
        reason = block.bootstrap_reason
        assert reason is not None
        # Reason names the specific window in fallback.
        assert "breadth_ema_200d_observations" in reason
        assert "100 < 200" in reason
        # Calibrated windows do not appear in the reason string.
        assert "breadth_ema_20d_observations" not in reason
        assert "breadth_ema_50d_observations" not in reason

    def test_all_windows_under_threshold_emit_null_with_per_window_reasons(self) -> None:
        # 10 closes per ticker — below every EMA window threshold. Every
        # pct_above_Xd_ema value is null and the reason string names each
        # window so the operator sees the full degeneracy.
        n = 10
        closes_by_ticker = {
            ticker: tuple(100.0 + 0.1 * i for i in range(n)) for ticker in ("A", "B")
        }

        block = compute_breadth_internals_pure(
            closes_by_ticker=closes_by_ticker,
            sector_members={"sector": tuple(closes_by_ticker)},
            universe_tickers=tuple(closes_by_ticker),
            broad_market_closes=closes_by_ticker["A"],
            as_of=_as_of(),
        )

        payload = block.payload
        assert payload["pct_above_20d_ema"] is None
        assert payload["pct_above_50d_ema"] is None
        assert payload["pct_above_200d_ema"] is None
        assert block.calibration_state is CalibrationState.ACCUMULATING
        reason = block.bootstrap_reason
        assert reason is not None
        assert "breadth_ema_20d_observations" in reason
        assert "breadth_ema_50d_observations" in reason
        assert "breadth_ema_200d_observations" in reason

    def test_no_observations_emits_unavailable(self) -> None:
        closes_by_ticker: dict[str, tuple[float, ...]] = {"A": (), "B": ()}

        block = compute_breadth_internals_pure(
            closes_by_ticker=closes_by_ticker,
            sector_members={"sector": ("A", "B")},
            universe_tickers=("A", "B"),
            broad_market_closes=(),
            as_of=_as_of(),
        )

        payload = block.payload
        assert payload["pct_above_20d_ema"] is None
        assert payload["pct_above_50d_ema"] is None
        assert payload["pct_above_200d_ema"] is None
        assert block.calibration_state is CalibrationState.UNAVAILABLE
