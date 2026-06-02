"""Tests for ``q7_cross_asset.compute_intra_sector_correlation`` — story 08d.

DB-backed tests. One named pin (per ALP-797 preserve-list) plus the
persistence smoke (event written to ``distillation_event_history``), which
has no pure-compute twin.  The fire-path divergence-flag and the perfect-
correlation payload are fully pinned by
``test_compute.py::TestIntraSectorCorrelationCompute``.

Reduced in ALP-797 (q7 pure/DB double-altitude reduction).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

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
CORRELATION_LOCUS_PAIR_COUNT_THRESHOLD = 3


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

    def test_divergence_event_persisted_in_event_history(self, session: Session) -> None:
        # When a divergence fires, an event row of kind ``correlation_divergence``
        # is appended to ``distillation_event_history`` for the lead ticker.
        # This side-effect has no pure-compute twin — it exercises the DB shim.
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
            correlation_locus_pair_count_threshold=CORRELATION_LOCUS_PAIR_COUNT_THRESHOLD,
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
        # PRESERVE PIN (ALP-797): suppression branch has no DB twin in pure
        # tests; this is the only DB-level coverage for the suppression path.
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
            correlation_locus_pair_count_threshold=CORRELATION_LOCUS_PAIR_COUNT_THRESHOLD,
        )

        # When the short-window pair matches the long-window pair, no flag
        # exceeds 1.5sigma.
        block = blocks[0]
        for flag in block.anomaly_flags:
            assert flag.magnitude < NARRATIVE_LAG_CORRELATION_SHIFT_SIGMA, (
                f"unexpected divergence flag {flag}"
            )
