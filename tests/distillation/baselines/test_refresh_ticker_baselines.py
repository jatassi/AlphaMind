"""Tests for ``refresh_ticker_baselines`` — story 02-distillation-layer/07.

Cover the per-ticker volume / ATR / spread / sentiment refresh entry point:
happy-path against fixture OHLCV data, bootstrap when observations are too
few, idempotent re-run on the same ``(scope, as_of)``, fault injection that
rolls back the transaction, and Welford's-algorithm O(1) incremental cost.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.baselines import refresh_ticker_baselines
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationTickerBaseline,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory

# Mirror the values that ``config/distillation.yaml`` carries for the
# volume baseline. Per the no-magic-numbers audit, distillation source
# code may not embed these literals; tests scope to scenarios where the
# orchestrator (story 12) has already loaded and resolved them, so the
# test file is the right place to anchor the fixtures.
VOLUME_WINDOW_DAYS = 20
VOLUME_MIN_OBSERVATIONS = 20
ATR_WINDOW_DAYS = 14
ATR_MIN_OBSERVATIONS = 14
SPREAD_WINDOW_DAYS = 20
SPREAD_MIN_OBSERVATIONS = 20
SENTIMENT_WINDOW_DAYS = 60
SENTIMENT_MIN_OBSERVATIONS = 30


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
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_ohlcv(
    session: Session,
    *,
    ticker: str,
    period_start: str,
    volume: int = 1_000_000,
    high: float = 105.0,
    low: float = 95.0,
    close: float = 100.0,
    open_: float = 100.0,
) -> None:
    """Insert one daily-timeframe OHLCV row.

    ``timeframe`` is fixed to ``"1d"`` so refresh tests work off the
    daily-bar series. ``adj_*`` and ``unadj_*`` are populated identically;
    refresh logic reads adjusted columns.
    """
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
            adj_volume=volume,
            adj_vwap=close,
            unadj_open=open_,
            unadj_high=high,
            unadj_low=low,
            unadj_close=close,
            unadj_volume=volume,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


# ---------------------------------------------------------------------------
# Happy path — calibrated state for volume baseline
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesVolumeCalibrated:
    def test_writes_calibrated_volume_baseline_with_mean_and_stdev(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # 25 daily bars with strictly increasing volumes 1..25 inside a
        # 20-day window so the trailing-window slice has enough observations
        # to be calibrated against the default ``volume_baseline_days = 20``.
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()
        as_of = "2026-04-25T00:00:00Z"

        result = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=as_of,
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        assert "AAPL" in result
        cv = result["AAPL"]
        assert cv.state is CalibrationState.CALIBRATED
        assert cv.bootstrap_reason is None
        # Window covers period_start in [as_of - 20d, as_of] inclusive,
        # which is days 5..25 → 21 observations.
        assert cv.value["n_observations"] >= 20
        assert cv.value["mean"] > 0
        assert cv.value["stdev"] > 0

        # Row was upserted into the state table.
        row = session.execute(
            select(DistillationTickerBaseline).where(
                DistillationTickerBaseline.ticker == "AAPL",
                DistillationTickerBaseline.baseline_kind == "volume",
                DistillationTickerBaseline.as_of == as_of,
            )
        ).scalar_one()
        assert row.calibration_state == "calibrated"
        assert row.window_days == 20
        assert row.n_observations >= 20


# ---------------------------------------------------------------------------
# Bootstrap path — fewer observations than the min threshold
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesBootstrapPath:
    def test_writes_bootstrap_row_when_observations_below_min(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # Only three bars — well below the 20-observation minimum for volume.
        for day in (1, 2, 3):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-2{day}T00:00:00Z",
                volume=day * 1_000_000,
            )
        session.commit()
        as_of = "2026-04-25T00:00:00Z"

        result = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=as_of,
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        cv = result["AAPL"]
        assert cv.state is CalibrationState.BOOTSTRAP
        assert cv.bootstrap_reason is not None
        assert "volume_min_observations" in cv.bootstrap_reason
        assert "3 < 20" in cv.bootstrap_reason

        # Row is still written so the orchestrator can read its own write.
        row = session.execute(
            select(DistillationTickerBaseline).where(
                DistillationTickerBaseline.ticker == "AAPL",
            )
        ).scalar_one()
        assert row.calibration_state == "bootstrap"
        assert row.n_observations == 3


# ---------------------------------------------------------------------------
# Idempotent re-run — UPSERT semantics on (ticker, kind, as_of)
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesIdempotent:
    def test_rerun_on_same_scope_and_as_of_does_not_duplicate(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()
        as_of = "2026-04-25T00:00:00Z"

        refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=as_of,
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )
        # Capture the row count after the first refresh — must equal 1.
        first_count = session.scalar(select(func.count()).select_from(DistillationTickerBaseline))
        assert first_count == 1

        # Re-run with identical scope and as_of.
        refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=as_of,
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        # Row count is unchanged — UPSERT, not append.
        second_count = session.scalar(select(func.count()).select_from(DistillationTickerBaseline))
        assert second_count == 1


# ---------------------------------------------------------------------------
# Fault injection — exception mid-refresh rolls back, propagates
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesFaultInjection:
    def test_query_failure_mid_refresh_rolls_back_and_propagates(self, session: Session) -> None:
        # Two tickers — AAPL has data, ZZZZ exists in the universe but its
        # OHLCV query is rigged to fail by dropping the ohlcv_bars table mid
        # transaction. The first ticker's row must NOT be persisted because
        # the second ticker's failure forces a rollback of the whole batch.
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
            _add_ohlcv(
                session,
                ticker=Symbol("MSFT"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 200_000,
            )
        session.commit()

        # Sabotage the second-ticker query: drop the OHLCV table after the
        # first ticker's loop iteration has run. The most reliable way is to
        # replace the session's ``execute`` with a wrapper that fails on the
        # second OHLCV select. Subclass-free: monkey-patch the bound method.
        original_execute = session.execute
        call_state = {"ohlcv_calls": 0}

        def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            stmt_text = str(statement).lower()
            if "ohlcv_bars" in stmt_text and "select" in stmt_text:
                call_state["ohlcv_calls"] += 1
                if call_state["ohlcv_calls"] >= 2:
                    raise RuntimeError("simulated mid-refresh DB failure")
            return original_execute(statement, *args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]

        try:
            with pytest.raises(RuntimeError, match="simulated mid-refresh"):
                refresh_ticker_baselines(
                    session,
                    kind="volume",
                    ticker_scope=("AAPL", "MSFT"),
                    as_of="2026-04-25T00:00:00Z",
                    window_days=VOLUME_WINDOW_DAYS,
                    min_observations=VOLUME_MIN_OBSERVATIONS,
                )
        finally:
            session.execute = original_execute  # type: ignore[method-assign]

        # No baseline rows are persisted — the partial AAPL write rolled back.
        count = session.scalar(select(func.count()).select_from(DistillationTickerBaseline))
        assert count == 0


# ---------------------------------------------------------------------------
# Welford incremental cost — only new bars are scanned when prior row exists
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesWelfordIncremental:
    def test_first_time_deployment_scans_full_window(self, session: Session) -> None:
        """No prior baseline row → full-window recompute fallback.

        Asserts that the underlying source scan is at least the window size
        the first time refresh runs for a ticker.
        """
        _add_ticker(session, "AAPL")
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()

        result = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        # Full-recompute path: the n_observations equals the rows in the
        # window (days 5..25 inclusive = 21 obs).
        assert result["AAPL"].value["n_observations"] == 21

    def test_incremental_update_scans_only_window_delta(self, session: Session) -> None:
        """Prior row exists → only the inflow + outflow window-delta is scanned.

        Counts source-table SELECTs against ``ohlcv_bars`` during the
        second refresh. The Welford incremental path scans only the bars
        added to the window (inflow) and the bars falling out of it
        (outflow); the rest of the window is carried forward via the
        ``(n, mean, M2)`` triple. Each individual scan is bounded by the
        per-day refresh delta, regardless of window size — that is the
        spec's "O(1) per ticker per kind regardless of window" contract.
        """
        _add_ticker(session, "AAPL")
        # 25 bars, days 1..25.
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()

        # First refresh anchors the running statistics at as_of=day 25.
        first_as_of = "2026-04-25T00:00:00Z"
        first = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=first_as_of,
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )
        prior_n = first["AAPL"].value["n_observations"]

        # Add two more daily bars (days 26 & 27); refresh against day 27.
        for day in (26, 27):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()

        # Spy on OHLCV scan size via SQLAlchemy core events. ``after_cursor_execute``
        # fires after the cursor has run; ``cursor.rowcount`` is unreliable on
        # SQLite SELECTs, so we count rows fetched by the resulting statement
        # by re-running it once during the spy. Counting via parameter binding
        # keeps the test purely behavioral — no patching of refresh internals.
        from sqlalchemy import event as sa_event

        bind = session.get_bind()
        scan_counts: list[int] = []

        @sa_event.listens_for(bind, "after_cursor_execute")
        def _after_cursor_execute(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            text = statement.lower()
            if "from ohlcv_bars" in text:
                # Re-execute the same SQL with the same params to count.
                cur = conn.connection.cursor()
                try:
                    if isinstance(parameters, dict):
                        cur.execute(statement, parameters)
                    else:
                        cur.execute(statement, parameters or ())
                    rows = cur.fetchall()
                    scan_counts.append(len(rows))
                finally:
                    cur.close()

        try:
            second = refresh_ticker_baselines(
                session,
                kind="volume",
                ticker_scope=("AAPL",),
                as_of="2026-04-27T00:00:00Z",
                window_days=VOLUME_WINDOW_DAYS,
                min_observations=VOLUME_MIN_OBSERVATIONS,
            )
        finally:
            sa_event.remove(bind, "after_cursor_execute", _after_cursor_execute)

        # Each OHLCV scan returns at most the two-bar window delta — the
        # 19 unchanged middle bars are carried forward via the Welford
        # ``(n, mean, M2)`` state. Two scans run on the incremental path:
        # one for inflow (new bars) and one for outflow (bars falling out
        # of the window).
        assert scan_counts, "expected an OHLCV select during the second refresh"
        assert max(scan_counts) == 2

        # Two days advanced, two new bars added, two oldest bars evicted —
        # the rolling window is still the same size, so ``n_observations``
        # is unchanged. This is the "drop the oldest" half of the
        # threshold-calibration.md contract.
        assert second["AAPL"].value["n_observations"] == prior_n

    def test_eviction_math_matches_full_recompute(self, session: Session) -> None:
        """Incremental add+evict produces the same mean/stdev as a fresh recompute.

        Math-correctness check: the Welford-evict reverse update must be
        the exact inverse of Welford-extend, so the running statistics
        after slide-the-window arithmetic equal what a from-scratch
        recompute over the new window would produce.
        """
        _add_ticker(session, "AAPL")
        # 25 distinct daily volumes, days 1..25.
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()

        # Incremental path: anchor at day 25, advance to day 27.
        refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )
        for day in (26, 27):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()
        incremental = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of="2026-04-27T00:00:00Z",
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        # Full-recompute baseline: same data, second ticker, refresh once
        # at day 27 with no prior row.
        _add_ticker(session, "MSFT")
        for day in range(1, 28):
            _add_ohlcv(
                session,
                ticker=Symbol("MSFT"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()
        full = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("MSFT",),
            as_of="2026-04-27T00:00:00Z",
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        assert incremental["AAPL"].value["n_observations"] == full["MSFT"].value["n_observations"]
        assert incremental["AAPL"].value["mean"] == pytest.approx(full["MSFT"].value["mean"])
        assert incremental["AAPL"].value["stdev"] == pytest.approx(full["MSFT"].value["stdev"])

    def test_window_days_change_triggers_full_recompute(self, session: Session) -> None:
        """Changing the configured window forces a full recompute over the new window.

        The persisted ``n``/``mean``/``M2`` triple is anchored to a specific
        window length; reusing it under a different window would corrupt
        the running statistics. The refresh detects the mismatch and
        rescans the new window from cold.
        """
        _add_ticker(session, "AAPL")
        for day in range(1, 26):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                volume=day * 100_000,
            )
        session.commit()

        # Anchor at day 25 with a 20-day window — n = 21 (days 5..25).
        refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            window_days=20,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        # Reload the baseline with a 10-day window — n must drop to 11
        # (days 15..25), which is only achievable via full recompute.
        result = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            window_days=10,
            min_observations=10,
        )
        assert result["AAPL"].value["n_observations"] == 11
        assert result["AAPL"].value["window_days"] == 10

    def test_refresh_gap_exceeding_window_triggers_full_recompute(self, session: Session) -> None:
        """A refresh gap ≥ window_days drops the entire prior state.

        When the time between refreshes meets or exceeds the window, every
        observation in the prior window has fallen out. Scanning the
        eviction range would cost as much as a fresh recompute, and the
        carried Welford state has nothing useful left.
        """
        from datetime import UTC, datetime, timedelta

        _add_ticker(session, "AAPL")
        # Seed 50 consecutive daily bars starting 2026-03-15.
        first_day = datetime(2026, 3, 15, tzinfo=UTC)
        for day_offset in range(50):
            current_dt = first_day + timedelta(days=day_offset)
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=current_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                volume=(day_offset + 1) * 100_000,
            )
        session.commit()

        # Anchor at offset 9 (10th bar) with W=20.
        first_as_of = (first_day + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        first = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=first_as_of,
            window_days=20,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        # Refresh 25 days later — gap exceeds W=20, so the prior window
        # has rolled entirely off. The new window covers 21 daily bars
        # ending at offset 34.
        second_as_of = (first_day + timedelta(days=34)).strftime("%Y-%m-%dT%H:%M:%SZ")
        second = refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=second_as_of,
            window_days=20,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        # The carried mean from offset 9 is nowhere near the offset-14..34
        # mean; the divergence confirms a full recompute landed.
        assert second["AAPL"].value["mean"] != first["AAPL"].value["mean"]
        # New window covers exactly 21 daily bars (offsets 14..34 inclusive).
        assert second["AAPL"].value["n_observations"] == 21


# ---------------------------------------------------------------------------
# ATR kind — average true range from OHLCV high-low
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesAtrKind:
    def test_writes_calibrated_atr_baseline_against_ohlcv(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # 20 bars with constant 10-pt range so the ATR mean = 10 and stdev = 0.
        for day in range(1, 21):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                high=110.0,
                low=100.0,
                close=105.0,
                open_=105.0,
            )
        session.commit()

        result = refresh_ticker_baselines(
            session,
            kind="atr",
            ticker_scope=("AAPL",),
            as_of="2026-04-20T00:00:00Z",
            window_days=ATR_WINDOW_DAYS,
            min_observations=ATR_MIN_OBSERVATIONS,
        )
        cv = result["AAPL"]
        assert cv.state is CalibrationState.CALIBRATED
        assert cv.value["mean"] == pytest.approx(10.0)
        assert cv.value["stdev"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Spread kind — relative intraday range as a liquidity proxy
# ---------------------------------------------------------------------------


class TestRefreshTickerBaselinesSpreadKind:
    def test_writes_calibrated_spread_baseline(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        for day in range(1, 21):
            _add_ohlcv(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                high=102.0,
                low=98.0,
                close=100.0,
                open_=100.0,
            )
        session.commit()
        result = refresh_ticker_baselines(
            session,
            kind="spread",
            ticker_scope=("AAPL",),
            as_of="2026-04-20T00:00:00Z",
            window_days=SPREAD_WINDOW_DAYS,
            min_observations=SPREAD_MIN_OBSERVATIONS,
        )
        cv = result["AAPL"]
        assert cv.state is CalibrationState.CALIBRATED
        # Relative range = (102 - 98) / 100 = 0.04.
        assert cv.value["mean"] == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# Sentiment kind — pooled per-ticker article sentiment from news_article_tickers
# ---------------------------------------------------------------------------


def _add_news_article(
    session: Session,
    *,
    article_id: str,
    ticker: str,
    published_at: str,
    sentiment_score: float,
) -> None:
    from alphamind.persistence.models import (
        NewsArticles,
        NewsArticleTickers,
    )

    session.add(
        NewsArticles(
            article_id=article_id,
            source="test",
            url=None,
            language="en",
            headline_text=f"headline for {ticker}",
            body_path=None,
            published_at=published_at,
            ingested_at=published_at,
        )
    )
    # Flush so the FK target exists before the join row is added.
    session.flush()
    session.add(
        NewsArticleTickers(
            article_id=article_id,
            ticker=ticker,
            is_primary=1,
            vendor_sentiment_score=sentiment_score,
            vendor_sentiment_label=None,
        )
    )


class TestRefreshTickerBaselinesSentimentKind:
    def test_writes_calibrated_sentiment_baseline_from_articles(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # 30 articles with sentiment scores spread across the window so the
        # per-ticker baseline can hit the 30-observation calibrated minimum.
        for i in range(30):
            _add_news_article(
                session,
                article_id=f"art-{i:03d}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-03-{(i % 28) + 1:02d}T0{i % 10}:00:00Z",
                sentiment_score=0.1 + 0.01 * (i % 5),
            )
        session.commit()
        result = refresh_ticker_baselines(
            session,
            kind="sentiment",
            ticker_scope=("AAPL",),
            as_of="2026-04-25T00:00:00Z",
            window_days=SENTIMENT_WINDOW_DAYS,
            min_observations=SENTIMENT_MIN_OBSERVATIONS,
        )
        cv = result["AAPL"]
        assert cv.state is CalibrationState.CALIBRATED
        assert cv.value["n_observations"] == 30
        # Mean should be 0.1 + 0.01 * mean(0..4) = 0.1 + 0.02 = 0.12.
        assert cv.value["mean"] == pytest.approx(0.12, abs=1e-9)
