"""Idempotent distillation writes — residual backstop for ALP-745.

The primary ALP-745 fix is the Tier B schedule (no two triggers share a
minute, so no two passes share an ``as_of``). This file pins the *backstop*:
the two ``baselines.py`` writers issue ``INSERT … ON CONFLICT DO UPDATE`` so a
duplicate / concurrent same-``as_of`` pass — a manual ``cli`` run bar-aligned
with a scheduled one — folds into the existing row instead of crashing the
loser on the ``UNIQUE(ticker, baseline_kind, as_of)`` / ``UNIQUE(lead, lag,
as_of)`` constraint (the prod ``sqlite3.IntegrityError``).

Two complementary kinds of check per writer:

1. **Behavior pins** — a "second full refresh pass on the same ``as_of`` does
   not raise and leaves exactly one row" test, and a helper-level test that
   pre-seeds a committed row (the race winner) and confirms the writer folds
   into it (one row, payload overwritten). These pin the *required behavior*
   but, because they exercise sequential writes against an already-committed
   row, the old check-then-insert code would satisfy them too (it took its
   ``UPDATE`` branch) — so they do not by themselves detect a revert.
2. **Structural guard** (:class:`TestWriterEmitsAtomicUpsert`) — captures the
   SQL each writer emits and asserts it is a single ``INSERT … ON CONFLICT …
   DO UPDATE`` with no preceding existence ``SELECT`` on the target table.
   *This* is the regression guard: reverting to check-then-insert (which
   reintroduces the ALP-745 race) emits a ``SELECT`` then a plain ``INSERT``
   and trips it.

Wall-clock concurrency between two OS processes is non-deterministic and not
reproducible in-process, so the race itself is guarded structurally (the
atomic upsert is correct regardless of interleaving) rather than by timing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.baselines import (
    _TickerBaselineRow,
    _upsert_pair_lag,
    _upsert_ticker_baseline,
    refresh_pair_lag,
    refresh_ticker_baselines,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationPairLag,
    DistillationTickerBaseline,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory

VOLUME_WINDOW_DAYS = 20
VOLUME_MIN_OBSERVATIONS = 20
PAIR_LAG_WINDOW_DAYS = 20
PAIR_LAG_MIN_EVENTS = 10
PAIR_LAG_MAX_DAYS = 7
AS_OF = "2026-04-25T00:00:00Z"


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


def _add_ohlcv(session: Session, *, ticker: str, period_start: str, volume: int) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=100.0,
            adj_high=105.0,
            adj_low=95.0,
            adj_close=100.0,
            adj_volume=volume,
            adj_vwap=100.0,
            unadj_open=100.0,
            unadj_high=105.0,
            unadj_low=95.0,
            unadj_close=100.0,
            unadj_volume=volume,
            unadj_vwap=100.0,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
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


def _seed_volume_bars(session: Session, ticker: str) -> None:
    _add_ticker(session, ticker)
    for day in range(1, 26):
        _add_ohlcv(
            session,
            ticker=Symbol(ticker),
            period_start=f"2026-04-{day:02d}T00:00:00Z",
            volume=day * 100_000,
        )
    session.commit()


# ---------------------------------------------------------------------------
# Ticker baseline writer
# ---------------------------------------------------------------------------


class TestTickerBaselineConcurrentWriter:
    def test_second_pass_same_as_of_does_not_raise(self, engine: Engine, session: Session) -> None:
        """Two full refresh passes on the same ``as_of`` → one row, no raise.

        The second pass runs on an independent session (a second
        distillation invocation), mirroring the prod double-fire that
        crashed the loser on the ``UNIQUE`` constraint.
        """
        _seed_volume_bars(session, "AAPL")
        refresh_ticker_baselines(
            session,
            kind="volume",
            ticker_scope=("AAPL",),
            as_of=AS_OF,
            window_days=VOLUME_WINDOW_DAYS,
            min_observations=VOLUME_MIN_OBSERVATIONS,
        )

        factory = make_session_factory(engine)
        with factory() as second_pass:
            # No IntegrityError — the atomic upsert folds into the row the
            # first pass already committed under the same as_of.
            refresh_ticker_baselines(
                second_pass,
                kind="volume",
                ticker_scope=("AAPL",),
                as_of=AS_OF,
                window_days=VOLUME_WINDOW_DAYS,
                min_observations=VOLUME_MIN_OBSERVATIONS,
            )

        count = session.scalar(select(func.count()).select_from(DistillationTickerBaseline))
        assert count == 1

    def test_upsert_folds_into_committed_row_in_place(self, session: Session) -> None:
        """A pre-existing committed row (race winner) is updated, not re-inserted.

        Directly exercises the ``ON CONFLICT(ticker, baseline_kind, as_of)
        DO UPDATE`` path: the helper must overwrite the sentinel row's
        payload without raising and without appending a duplicate.
        """
        _add_ticker(session, "AAPL")
        session.add(
            DistillationTickerBaseline(
                ticker="AAPL",
                baseline_kind="volume",
                as_of=AS_OF,
                mean=1.0,
                stdev=1.0,
                n_observations=999,  # sentinel — must be overwritten
                window_days=VOLUME_WINDOW_DAYS,
                calibration_state="accumulating",
                ingested_at=AS_OF,
            )
        )
        session.commit()

        _upsert_ticker_baseline(
            session,
            _TickerBaselineRow(
                ticker="AAPL",
                kind="volume",
                as_of=AS_OF,
                mean=42.0,
                stdev=3.0,
                n_observations=20,
                window_days=VOLUME_WINDOW_DAYS,
                state=CalibrationState.CALIBRATED,
            ),
        )
        session.commit()

        rows = (
            session.execute(
                select(DistillationTickerBaseline).where(
                    DistillationTickerBaseline.ticker == "AAPL",
                    DistillationTickerBaseline.baseline_kind == "volume",
                    DistillationTickerBaseline.as_of == AS_OF,
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].n_observations == 20
        assert rows[0].mean == pytest.approx(42.0)
        assert rows[0].calibration_state == "calibrated"


# ---------------------------------------------------------------------------
# Pair-lag writer
# ---------------------------------------------------------------------------


def _seed_pair_series(session: Session) -> None:
    _add_ticker(session, "SMH")
    _add_ticker(session, "QQQ")
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
        if day == 1:
            _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=qqq_close)
        else:
            qqq_close *= 1 + smh_returns[day - 2]
            _add_close(session, ticker=Symbol("QQQ"), period_start=ts, close=qqq_close)
    session.commit()


class TestPairLagConcurrentWriter:
    def test_second_pass_same_as_of_does_not_raise(self, engine: Engine, session: Session) -> None:
        _seed_pair_series(session)
        refresh_pair_lag(
            session,
            pair_scope=(("SMH", "QQQ", PAIR_LAG_MAX_DAYS),),
            as_of=AS_OF,
            window_days=PAIR_LAG_WINDOW_DAYS,
            min_events=PAIR_LAG_MIN_EVENTS,
        )

        factory = make_session_factory(engine)
        with factory() as second_pass:
            refresh_pair_lag(
                second_pass,
                pair_scope=(("SMH", "QQQ", PAIR_LAG_MAX_DAYS),),
                as_of=AS_OF,
                window_days=PAIR_LAG_WINDOW_DAYS,
                min_events=PAIR_LAG_MIN_EVENTS,
            )

        count = session.scalar(select(func.count()).select_from(DistillationPairLag))
        assert count == 1

    def test_upsert_folds_into_committed_row_and_preserves_overdue_flag(
        self, session: Session
    ) -> None:
        """Update path overwrites the estimate but leaves ``last_overdue_flag``.

        The old read-modify-write never reassigned ``last_overdue_flag`` on
        update (it is set to ``0`` only on insert; the overdue signal itself
        is derived at read time, not written back here), so the upsert's
        ``DO UPDATE`` set must omit it too to stay faithful.
        """
        _add_ticker(session, "SMH")
        _add_ticker(session, "QQQ")
        session.add(
            DistillationPairLag(
                lead_ticker="SMH",
                lag_ticker="QQQ",
                as_of=AS_OF,
                lead_lag_days_estimate=9.0,  # sentinel — must be overwritten
                n_pair_events=1,
                last_overdue_flag=1,  # must be preserved across the update
                calibration_state="accumulating",
                ingested_at=AS_OF,
            )
        )
        session.commit()

        _upsert_pair_lag(
            session,
            lead="SMH",
            lag="QQQ",
            as_of=AS_OF,
            estimate=2.0,
            n_events=23,
            state=CalibrationState.CALIBRATED,
        )
        session.commit()

        rows = (
            session.execute(
                select(DistillationPairLag).where(
                    DistillationPairLag.lead_ticker == "SMH",
                    DistillationPairLag.lag_ticker == "QQQ",
                    DistillationPairLag.as_of == AS_OF,
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].lead_lag_days_estimate == pytest.approx(2.0)
        assert rows[0].n_pair_events == 23
        assert rows[0].calibration_state == "calibrated"
        assert rows[0].last_overdue_flag == 1


# ---------------------------------------------------------------------------
# Sanity: the UNIQUE constraint the upsert protects against is real.
# ---------------------------------------------------------------------------


class TestUniqueConstraintIsReal:
    def test_naive_duplicate_insert_still_raises(self, session: Session) -> None:
        """A plain duplicate INSERT (not via the upsert helper) still raises.

        Guards against the table losing its composite PK: if this stops
        raising, the upsert protects nothing.
        """
        _add_ticker(session, "AAPL")
        session.add(
            DistillationTickerBaseline(
                ticker="AAPL",
                baseline_kind="volume",
                as_of=AS_OF,
                mean=1.0,
                stdev=1.0,
                n_observations=20,
                window_days=VOLUME_WINDOW_DAYS,
                calibration_state="calibrated",
                ingested_at=AS_OF,
            )
        )
        session.commit()
        session.add(
            DistillationTickerBaseline(
                ticker="AAPL",
                baseline_kind="volume",
                as_of=AS_OF,
                mean=2.0,
                stdev=2.0,
                n_observations=21,
                window_days=VOLUME_WINDOW_DAYS,
                calibration_state="calibrated",
                ingested_at=AS_OF,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# ---------------------------------------------------------------------------
# Structural guard — the writers must emit an atomic ON CONFLICT upsert.
# ---------------------------------------------------------------------------


@contextmanager
def _capture_sql(engine: Engine) -> Iterator[list[str]]:
    """Collect the lowercased SQL text of every statement run on *engine*."""
    statements: list[str] = []

    def _on_execute(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)


class TestWriterEmitsAtomicUpsert:
    """The writers must emit a single ``INSERT … ON CONFLICT DO UPDATE``.

    This is the real regression guard for ALP-745. The behavior-pin tests
    above pass even against the old check-then-insert code (they exercise
    sequential writes that the old ``UPDATE`` branch handled), so they cannot
    catch a revert. Asserting the *shape* of the emitted SQL can: a return to
    check-then-insert emits a ``SELECT`` followed by a plain ``INSERT`` (no
    ``ON CONFLICT``) and trips these tests.
    """

    def test_ticker_baseline_writer_emits_on_conflict_upsert(
        self, engine: Engine, session: Session
    ) -> None:
        _add_ticker(session, "AAPL")
        session.commit()
        with _capture_sql(engine) as sql:
            _upsert_ticker_baseline(
                session,
                _TickerBaselineRow(
                    ticker="AAPL",
                    kind="volume",
                    as_of=AS_OF,
                    mean=42.0,
                    stdev=3.0,
                    n_observations=20,
                    window_days=VOLUME_WINDOW_DAYS,
                    state=CalibrationState.CALIBRATED,
                ),
            )
            session.commit()
        assert any(
            "insert into distillation_ticker_baseline" in s and "on conflict" in s for s in sql
        ), sql
        # No read-modify-write: the writer must not pre-read the target row.
        assert not any(
            s.lstrip().startswith("select") and "distillation_ticker_baseline" in s for s in sql
        ), sql

    def test_pair_lag_writer_emits_on_conflict_upsert(
        self, engine: Engine, session: Session
    ) -> None:
        _add_ticker(session, "SMH")
        _add_ticker(session, "QQQ")
        session.commit()
        with _capture_sql(engine) as sql:
            _upsert_pair_lag(
                session,
                lead="SMH",
                lag="QQQ",
                as_of=AS_OF,
                estimate=1.0,
                n_events=10,
                state=CalibrationState.CALIBRATED,
            )
            session.commit()
        assert any("insert into distillation_pair_lag" in s and "on conflict" in s for s in sql), (
            sql
        )
        assert not any(
            s.lstrip().startswith("select") and "distillation_pair_lag" in s for s in sql
        ), sql
