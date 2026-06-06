"""Tests for the SQL-backed replay-engine repositories (ALP-564, story 08 §1).

Covers ``SqlBarRepository`` and ``SqlCorporateActionRepository`` against an
in-memory SQLite database with the alembic-migrated schema (here built from
``Base.metadata`` — the migration is exercised separately by the migration
tests). Three behaviors carry the contract:

* ``load_bars`` returns the proposal-*containing* bar as ``bars[0]`` (the
  market-order fill contract from story 05a hangs on this — see the docstring
  on ``SqlBarRepository.load_bars``), mapping the ``unadj_*`` columns onto the
  ``OhlcvBar`` OHLC fields and ordering ascending by ``period_start``;
* ``has_bars_over_window`` / ``has_action_in_window`` resolve a bare underlying
  ticker directly, and resolve a ``position_id`` (the ``PendingOrderAssessment``
  repo key) to its underlying ticker before querying;
* ``has_action_in_window`` gates the corporate-action window by ``ex_date``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import money, price
from alphamind.execution.counterfactual_replay_engine.sql_repos import (
    SqlBarRepository,
    SqlCorporateActionRepository,
)
from alphamind.persistence.models import (
    AssetUniverse,
    CorporateActions,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row

_PROPOSAL_TS = datetime(2026, 6, 1, 14, 7, 0, tzinfo=UTC)  # mid-bar (14:00 bar)
_WINDOW_START = _PROPOSAL_TS
_WINDOW_END = datetime(2026, 6, 1, 18, 0, 0, tzinfo=UTC)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _ensure_underlying(session: Session, ticker: str) -> None:
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
            last_updated="2026-04-01T00:00:00Z",
        )
    )
    session.flush()


def _bar(ticker: str, period_start: datetime, *, open_: float) -> OhlcvBars:
    period_end = datetime.fromtimestamp(period_start.timestamp() + 900, tz=UTC)
    return OhlcvBars(
        ticker=ticker,
        timeframe="15min",
        period_start=_iso(period_start),
        period_end=_iso(period_end),
        session="regular",
        adj_open=open_ + 1000.0,  # deliberately wrong so a read of adj_* fails the assert
        adj_high=open_ + 1000.0,
        adj_low=open_ + 1000.0,
        adj_close=open_ + 1000.0,
        adj_volume=1,
        adj_vwap=None,
        unadj_open=open_,
        unadj_high=open_ + 1.0,
        unadj_low=open_ - 1.0,
        unadj_close=open_ + 0.5,
        unadj_volume=10_000,
        unadj_vwap=None,
        trade_count=None,
        source="test",
        ingested_at="2026-06-01T00:00:00Z",
    )


def _prod_bar(ticker: str, period_start: datetime, *, open_: float) -> OhlcvBars:
    """A bar shaped like the production polygon collector writes: ``period_end == period_start``.

    The collector (``data_sources.polygon.equity``) writes ``period_end =
    period_start`` for every 15-minute bar, so a ``period_end``-overlap predicate
    degenerates and drops the proposal-containing bar. Seeding with this shape
    exercises ``load_bars`` against the real on-disk format.
    """
    bar = _bar(ticker, period_start, open_=open_)
    bar.period_end = bar.period_start
    return bar


def _equity_position(session: Session, *, position_id: str, ticker: str) -> None:
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_PROPOSAL_TS,
        details=EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=10.0,
            average_cost_basis_per_share=100.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_PROPOSAL_TS,
                fill_price=price("100.00"),
                fill_quantity=10.0,
                slippage=money("0"),
                fees=money("0"),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    session.add(position_record_to_row(record))
    session.flush()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import alphamind.state.tables  # noqa: F401 — register all tables

    eng = make_engine(":memory:")
    from alphamind.persistence.models import Base

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


class TestSqlBarRepository:
    def test_load_bars_includes_proposal_containing_bar_as_first(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        # The 14:00 bar contains the 14:07 proposal timestamp; 13:45 precedes it.
        session.add(_bar("AAPL", datetime(2026, 6, 1, 13, 45, tzinfo=UTC), open_=99.0))
        session.add(_bar("AAPL", datetime(2026, 6, 1, 14, 0, tzinfo=UTC), open_=100.0))
        session.add(_bar("AAPL", datetime(2026, 6, 1, 14, 15, tzinfo=UTC), open_=101.0))
        session.flush()

        bars = SqlBarRepository(session).load_bars(
            ticker="AAPL", start=_WINDOW_START, end=_WINDOW_END
        )

        # bars[0] is the proposal-containing 14:00 bar (period covers 14:07),
        # NOT the 13:45 bar and NOT the strictly-after 14:15 bar.
        assert bars[0].period_start == datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
        assert bars[1].period_start == datetime(2026, 6, 1, 14, 15, tzinfo=UTC)
        # unadj_* columns map onto the OHLC fields (adj_* are decoys).
        assert bars[0].open == 100.0
        assert bars[0].adj_volume == 10_000

    def test_load_bars_containing_bar_first_with_prod_period_end(self, session: Session) -> None:
        # Production shape: period_end == period_start. A period_end-overlap
        # predicate degenerates to period_start > start and DROPS the
        # proposal-containing 14:00 bar, putting every market entry one bar late.
        _ensure_underlying(session, "AAPL")
        session.add(_prod_bar("AAPL", datetime(2026, 6, 1, 13, 45, tzinfo=UTC), open_=99.0))
        session.add(_prod_bar("AAPL", datetime(2026, 6, 1, 14, 0, tzinfo=UTC), open_=100.0))
        session.add(_prod_bar("AAPL", datetime(2026, 6, 1, 14, 15, tzinfo=UTC), open_=101.0))
        session.flush()

        # _WINDOW_START is 14:07 (mid the 14:00 bar).
        bars = SqlBarRepository(session).load_bars(
            ticker="AAPL", start=_WINDOW_START, end=_WINDOW_END
        )

        # bars[0] is the proposal-containing 14:00 bar, NOT the 13:45 bar and
        # NOT the strictly-after 14:15 bar — even though period_end == period_start.
        assert bars[0].period_start == datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
        assert bars[0].open == 100.0
        assert bars[1].period_start == datetime(2026, 6, 1, 14, 15, tzinfo=UTC)

    def test_has_bars_over_window_true_for_underlying(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        session.add(_bar("AAPL", datetime(2026, 6, 1, 14, 0, tzinfo=UTC), open_=100.0))
        session.flush()
        assert (
            SqlBarRepository(session).has_bars_over_window("AAPL", _WINDOW_START, _WINDOW_END)
            is True
        )

    def test_has_bars_over_window_false_when_no_bars(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        assert (
            SqlBarRepository(session).has_bars_over_window("AAPL", _WINDOW_START, _WINDOW_END)
            is False
        )

    def test_has_bars_resolves_position_id_to_underlying(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        _equity_position(session, position_id="POS-1", ticker="AAPL")
        session.add(_bar("AAPL", datetime(2026, 6, 1, 14, 0, tzinfo=UTC), open_=100.0))
        session.flush()
        # A PendingOrderAssessment passes its position_id, not the ticker.
        assert (
            SqlBarRepository(session).has_bars_over_window("POS-1", _WINDOW_START, _WINDOW_END)
            is True
        )

    def test_load_bars_resolves_position_id_to_underlying(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        _equity_position(session, position_id="POS-1", ticker="AAPL")
        session.add(_bar("AAPL", datetime(2026, 6, 1, 14, 0, tzinfo=UTC), open_=100.0))
        session.flush()
        bars = SqlBarRepository(session).load_bars(
            ticker="POS-1", start=_WINDOW_START, end=_WINDOW_END
        )
        assert len(bars) == 1
        assert bars[0].open == 100.0


class TestSqlCorporateActionRepository:
    def test_action_in_window_true_for_underlying(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        session.add(
            CorporateActions(
                action_id="CA-1",
                ticker="AAPL",
                action_type="SPLIT",
                ex_date="2026-06-01",
                ratio=2.0,
                source="test",
                ingested_at="2026-05-01T00:00:00Z",
            )
        )
        session.flush()
        assert (
            SqlCorporateActionRepository(session).has_action_in_window(
                "AAPL", _WINDOW_START, _WINDOW_END
            )
            is True
        )

    def test_action_in_window_false_when_outside(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        session.add(
            CorporateActions(
                action_id="CA-1",
                ticker="AAPL",
                action_type="SPLIT",
                ex_date="2026-07-01",  # after the window
                ratio=2.0,
                source="test",
                ingested_at="2026-05-01T00:00:00Z",
            )
        )
        session.flush()
        assert (
            SqlCorporateActionRepository(session).has_action_in_window(
                "AAPL", _WINDOW_START, _WINDOW_END
            )
            is False
        )

    def test_action_resolves_position_id_to_underlying(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")
        _equity_position(session, position_id="POS-1", ticker="AAPL")
        session.add(
            CorporateActions(
                action_id="CA-1",
                ticker="AAPL",
                action_type="SPLIT",
                ex_date="2026-06-01",
                ratio=2.0,
                source="test",
                ingested_at="2026-05-01T00:00:00Z",
            )
        )
        session.flush()
        assert (
            SqlCorporateActionRepository(session).has_action_in_window(
                "POS-1", _WINDOW_START, _WINDOW_END
            )
            is True
        )
