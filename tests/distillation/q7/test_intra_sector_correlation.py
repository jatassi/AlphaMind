"""Tests for ``q7_cross_asset.compute_intra_sector_correlation`` — story 08d.

Cover intra-sector pairwise correlation matrices at the short and long
windows, plus the divergence detection that fires when the short-window
correlation deviates from the long-window baseline by the configured sigma
multiple.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import compute_intra_sector_correlation
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationEventHistory,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory

# Window sizes mirror the values in ``config/distillation.yaml``.
CORRELATION_SHORT_DAYS = 20
CORRELATION_LONG_DAYS = 60
NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA = 1.5


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


def _seed_synthetic_log_returns(
    session: Session,
    *,
    ticker: str,
    log_returns: list[float],
    start_close: float = 100.0,
    start_day: int = 1,
) -> None:
    """Walk a price series from ``start_close`` driven by ``log_returns``.

    Inserts ``len(log_returns) + 1`` daily bars starting at day ``start_day``
    in April 2026.
    """
    close = start_close
    _add_close(
        session,
        ticker=ticker,
        period_start=f"2026-04-{start_day:02d}T00:00:00Z",
        close=close,
    )
    for i, lr in enumerate(log_returns, start=1):
        close = close * math.exp(lr)
        _add_close(
            session,
            ticker=ticker,
            period_start=f"2026-04-{start_day + i:02d}T00:00:00Z",
            close=close,
        )


# ---------------------------------------------------------------------------
# Happy path — perfectly correlated tickers produce ~1.0 correlations
# ---------------------------------------------------------------------------


class TestIntraSectorCorrelationHappyPath:
    def test_two_tickers_with_identical_returns_yield_full_correlation(
        self, session: Session
    ) -> None:
        # Two tickers with identical day-by-day log returns → perfect
        # correlation. Use 4 days = 3 returns, well below the 20-day short
        # window — the helper should still compute correlation over the
        # available observations and tag bootstrap.
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        log_returns = [0.01, -0.02, 0.015]
        _seed_synthetic_log_returns(session, ticker="AAPL", log_returns=log_returns)
        _seed_synthetic_log_returns(session, ticker="MSFT", log_returns=log_returns)
        session.commit()

        as_of = datetime(2026, 4, 4, tzinfo=UTC)
        blocks = compute_intra_sector_correlation(
            session,
            sector="tech",
            sector_tickers=("AAPL", "MSFT"),
            as_of=as_of,
            short_window_days=CORRELATION_SHORT_DAYS,
            long_window_days=CORRELATION_LONG_DAYS,
            divergence_sigma=NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA,
        )

        # One correlation block per sector.
        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q7.intra_sector_correlation.tech"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience

        # Short-window correlation matrix between AAPL and MSFT should be ~1.
        short_matrix = block.payload["short_window"]["correlation_matrix"]
        assert short_matrix["AAPL"]["MSFT"] == pytest.approx(1.0, abs=1e-9)
        assert short_matrix["MSFT"]["AAPL"] == pytest.approx(1.0, abs=1e-9)
        # Diagonal is exactly 1.
        assert short_matrix["AAPL"]["AAPL"] == pytest.approx(1.0, abs=1e-9)
        # Calibration: only 3 returns observed → bootstrap.
        assert block.calibration_state is CalibrationState.BOOTSTRAP


# ---------------------------------------------------------------------------
# Divergence detection — fires at 1.5sigma, suppressed below
# ---------------------------------------------------------------------------


def _build_close_series(returns: list[float], start_close: float = 100.0) -> list[float]:
    """Cumulative-product close series from a list of log returns."""
    closes = [start_close]
    for r in returns:
        closes.append(closes[-1] * math.exp(r))
    return closes


def _insert_close_series(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    """Insert daily bars at consecutive ISO dates starting at ``start_day``."""
    for i, close in enumerate(closes):
        ts = start_day + timedelta(days=i)
        _add_close(
            session,
            ticker=ticker,
            period_start=_iso(ts),
            close=close,
        )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestIntraSectorCorrelationDivergence:
    def _seed_three_tickers(
        self,
        session: Session,
        *,
        a_returns: list[float],
        b_returns: list[float],
        c_returns: list[float],
        start_day: datetime,
    ) -> None:
        for ticker, rets in (("A", a_returns), ("B", b_returns), ("C", c_returns)):
            _add_ticker(session, ticker)
            closes = _build_close_series(rets)
            _insert_close_series(session, ticker=ticker, closes=closes, start_day=start_day)

    def test_divergence_flag_fires_when_short_breaks_long_baseline(self, session: Session) -> None:
        # Construct a 60-day return history where:
        #   - In the LONG window (60 days back), all three tickers are highly
        #     correlated (small noise around the same path) → long-window
        #     pairwise correlations ~ 1.0 with a tiny stdev across pairs.
        #   - In the SHORT window (last 20 days), pair (A, B) suddenly
        #     decorrelates (B flips signs) while (A, C) and (B, C) stay
        #     correlated → AB short correlation ~ -1, deviation ~ 2.0,
        #     well above 1.5 * (long-window distribution stdev).
        long_returns = [0.01, -0.005, 0.008, -0.012, 0.006] * 8  # 40 days
        # base_a serves as the baseline path for long window.
        a_long = list(long_returns)
        b_long = [r + 0.0001 * (i % 3) for i, r in enumerate(long_returns)]
        c_long = [r + 0.00005 * (i % 5) for i, r in enumerate(long_returns)]

        # Short window: a continues with similar pattern; b flips sign; c
        # tracks a. 20 short returns.
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b = [-x for x in short_a]
        short_c = list(short_a)

        a_returns = a_long + short_a
        b_returns = b_long + short_b
        c_returns = c_long + short_c
        # Total: 60 returns → 61 daily bars.

        # Anchor the series so the most recent bar is at as_of.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=len(a_returns))
        self._seed_three_tickers(
            session,
            a_returns=a_returns,
            b_returns=b_returns,
            c_returns=c_returns,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intra_sector_correlation(
            session,
            sector="tech",
            sector_tickers=("A", "B", "C"),
            as_of=as_of,
            short_window_days=20,
            long_window_days=60,
            divergence_sigma=NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA,
        )

        block = blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        # The (A, B) pair correlation diverged sharply; expect a flag.
        assert any("A:B" in name for name in names), f"expected an A:B divergence flag; got {names}"

    def test_divergence_event_persisted_in_event_history(self, session: Session) -> None:
        # When a divergence fires, an event row of kind ``correlation_divergence``
        # is appended to ``distillation_event_history`` for the lead ticker.
        long_returns = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        a_long = list(long_returns)
        b_long = [r + 0.0001 * (i % 3) for i, r in enumerate(long_returns)]
        c_long = [r + 0.00005 * (i % 5) for i, r in enumerate(long_returns)]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b = [-x for x in short_a]
        short_c = list(short_a)
        a_returns = a_long + short_a
        b_returns = b_long + short_b
        c_returns = c_long + short_c

        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=len(a_returns))
        self._seed_three_tickers(
            session,
            a_returns=a_returns,
            b_returns=b_returns,
            c_returns=c_returns,
            start_day=start_day,
        )
        session.commit()

        compute_intra_sector_correlation(
            session,
            sector="tech",
            sector_tickers=("A", "B", "C"),
            as_of=as_of,
            short_window_days=20,
            long_window_days=60,
            divergence_sigma=NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA,
        )

        # An event was persisted for the (A, B) pair.
        rows = (
            session.execute(
                select(DistillationEventHistory).where(
                    DistillationEventHistory.event_kind == "correlation_divergence",
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) >= 1
        # The divergence event names the pair; the lead ticker is the row's
        # ticker and the lag ticker is encoded in the direction string.
        tickers_present = {row.ticker for row in rows}
        assert "A" in tickers_present or "B" in tickers_present

    def test_divergence_flag_suppressed_when_short_matches_long(self, session: Session) -> None:
        # All three tickers track the same return path across both windows;
        # short and long correlations are both ~ 1 → zero deviation → no
        # flag fires.
        long_returns = [0.01, -0.005, 0.008, -0.012, 0.006] * 8  # 40
        short_returns = [0.01, -0.02, 0.015, 0.005, -0.01,
                         0.012, -0.018, 0.02, -0.005, 0.008,
                         -0.015, 0.01, -0.005, 0.012, -0.008,
                         0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        a_returns = long_returns + short_returns
        # All three series are *exactly* the same path so every pairwise
        # correlation is 1.0 across both windows; the deviation distribution
        # collapses to zero and no flag can fire.
        b_returns = list(a_returns)
        c_returns = list(a_returns)

        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=len(a_returns))
        self._seed_three_tickers(
            session,
            a_returns=a_returns,
            b_returns=b_returns,
            c_returns=c_returns,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intra_sector_correlation(
            session,
            sector="tech",
            sector_tickers=("A", "B", "C"),
            as_of=as_of,
            short_window_days=20,
            long_window_days=60,
            divergence_sigma=NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA,
        )

        # When the short-window pair matches the long-window pair, no flag
        # exceeds 1.5sigma.
        block = blocks[0]
        for flag in block.anomaly_flags:
            assert flag.magnitude < NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA, (
                f"unexpected divergence flag {flag}"
            )
