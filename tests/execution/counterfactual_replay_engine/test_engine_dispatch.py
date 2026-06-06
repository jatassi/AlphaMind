"""Tests for the per-proposal engine driver ``replay_proposal`` (ALP-564, story 08 §3).

Each routing path (analyst equity / analyst option / strategist position-action /
pending-order) is driven end-to-end against an in-memory DB seeded with the bars
/ IV snapshots / position+order rows the path reads, plus the PM-decision
activity-log entry. The behaviors:

* analyst equity happy path → one EVALUATED record with a non-null realized_pl;
* analyst option happy path → EVALUATED option record;
* analyst option missing IV → UNEVALUABLE / DATA_MISSING (eligibility gate);
* strategist CLOSE → EVALUATED strategist record;
* pending-order CANCEL → EVALUATED record off the reconstructed entry;
* idempotency — a second call for the same (envelope, kind) returns None.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import alphamind.state.invocation_context  # noqa: F401 — break circular import seam
from alphamind._kernel.ids import (
    BracketId,
    EnvelopeId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.execution.counterfactual_replay_engine.engine import replay_proposal
from alphamind.execution.counterfactual_replay_engine.enums import (
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import CounterfactualReplayRecord
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import PMVerdict
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.repository.counterfactual_replays import (
    load_counterfactual_replays_for_envelope,
)
from alphamind.state.tables.brackets_codec import record_to_rows as bracket_record_to_rows
from alphamind.state.tables.orders_codec import record_to_row as order_record_to_row
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row
from tests.execution.counterfactual_replay_engine._fixtures import (
    REPLAY_CONFIG,
    add_pm_decision_row,
    analyst_equity_recommendation_json,
    analyst_option_recommendation_json,
    make_paper_harness,
    pm_decision_entry,
    seed_invocation,
    strategist_close_position_assessment_json,
    strategist_pending_cancel_assessment_json,
)

_INVOCATION = "inv-2026-06-01T14:00:00Z-aaaa"
_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)
# Past the strategist 72h default forward window so every path is "due".
_AS_OF = _PROPOSAL_TS + timedelta(days=4)
_RFR = 0.04
_HARNESS = make_paper_harness()


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


def _bar(
    ticker: str, period_start: datetime, *, open_: float, high: float, low: float
) -> OhlcvBars:
    period_end = datetime.fromtimestamp(period_start.timestamp() + 900, tz=UTC)
    return OhlcvBars(
        ticker=ticker,
        timeframe="15min",
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        session="regular",
        adj_open=open_,
        adj_high=high,
        adj_low=low,
        adj_close=open_,
        adj_volume=1,
        adj_vwap=None,
        unadj_open=open_,
        unadj_high=high,
        unadj_low=low,
        unadj_close=open_,
        unadj_volume=1_000_000,
        unadj_vwap=None,
        trade_count=None,
        source="test",
        ingested_at="2026-06-01T00:00:00Z",
    )


def _seed_bars(session: Session, ticker: str, *, hit_high: float) -> None:
    """Seed a run of 15-min bars from the proposal bar forward.

    The second bar spikes to ``hit_high`` so a long entry fills (bar[1] open) and
    the target (200) is reached, giving a non-null realized P/L.
    """
    _ensure_underlying(session, ticker)
    base = _PROPOSAL_TS
    for i in range(12):
        ts = base + timedelta(minutes=15 * i)
        high = hit_high if i == 1 else 101.0
        session.add(_bar(ticker, ts, open_=100.0, high=high, low=99.0))
    session.flush()


def _seed_option_snapshot(session: Session, occ: str, *, iv: float | None) -> None:
    session.add(
        OptionsContracts(
            contract_ticker=occ,
            underlying_ticker="AAPL",
            expiration_date="2026-07-17",
            strike_price=150.0,
            contract_type="call",
            first_seen_at="2026-05-01T00:00:00Z",
            last_seen_at="2026-06-01T00:00:00Z",
            source="test",
        )
    )
    session.flush()
    # A snapshot at the proposal time and through the window. The collector
    # writes ``datetime.now(UTC).isoformat()`` (``+00:00`` offset); match it so
    # the eligibility / lookup string comparison (``snapshot_ts <= target.isoformat()``)
    # includes the at-proposal snapshot.
    for i in range(12):
        ts = _PROPOSAL_TS + timedelta(minutes=15 * i)
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=ts.isoformat(),
                contract_ticker=occ,
                underlying_ticker="AAPL",
                open_interest=100,
                volume_today=50,
                last_price=5.0,
                bid=4.9,
                ask=5.1,
                implied_volatility=iv,
                underlying_price=100.0,
                source="test",
                ingested_at="2026-06-01T00:00:00Z",
            )
        )
    session.flush()


def _seed_equity_position(session: Session, *, position_id: str, ticker: str) -> None:
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_PROPOSAL_TS,
        details=EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=50.0,
            average_cost_basis_per_share=100.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_PROPOSAL_TS - timedelta(days=1),
                fill_price=price("100.00"),
                fill_quantity=50.0,
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


def _seed_bracket(
    session: Session, *, bracket_id: str, position_id: str, entry_order_id: str
) -> None:
    bracket = BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(entry_order_id),
        protective_legs=(
            BracketLeg(
                leg_id="L-TP",
                leg_type=BracketLegType.TAKE_PROFIT,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("AAPL"), threshold_usd=120.0, direction="GTE"
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
            BracketLeg(
                leg_id="L-SL",
                leg_type=BracketLegType.PRICE_STOP,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("AAPL"), threshold_usd=90.0, direction="LTE"
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
        ),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )
    bracket_row, leg_rows = bracket_record_to_rows(bracket)
    session.add(bracket_row)
    session.flush()  # bracket_legs.bracket_id FK is non-deferrable
    for leg in leg_rows:
        session.add(leg)
    session.flush()


def _seed_entry_order(
    session: Session, *, order_id: str, position_id: str, bracket_id: str
) -> None:
    order = OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(limit_price=price("100.00")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=None,
        alpaca_order_id_chain=(),
        submission_timestamp=_PROPOSAL_TS,
        last_update_timestamp=_PROPOSAL_TS,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=4.0,
    )
    session.add(order_record_to_row(order))
    session.flush()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import alphamind.state.tables  # noqa: F401 — register all tables

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        seed_invocation(sess, _INVOCATION)
        yield sess


def _pm_entry(
    *,
    envelope_id: str,
    verdict: PMVerdict,
    source_provenance_json: dict[str, Any],
    originating_proposal_json: dict[str, Any],
    position_id: str | None = None,
) -> ActivityLogEntry:
    return pm_decision_entry(
        entry_id=f"e-{envelope_id}",
        invocation_id=_INVOCATION,
        timestamp=_PROPOSAL_TS,
        envelope_id=envelope_id,
        verdict=verdict,
        source_provenance_json=source_provenance_json,
        originating_proposal_json=originating_proposal_json,
        position_id=position_id,
    )


def _run(
    session: Session,
    entry: ActivityLogEntry,
    detail: PMDecisionDetail,
    kind: ReplayKind,
) -> CounterfactualReplayRecord | None:
    return replay_proposal(
        session,
        entry,
        detail,
        kind,
        config=REPLAY_CONFIG,
        paper_harness_config=_HARNESS,
        risk_free_rate=_RFR,
        as_of=_AS_OF,
    )


class TestAnalystEquityDispatch:
    def test_happy_path_writes_evaluated_record(self, session: Session) -> None:
        _seed_bars(session, "AAPL", hit_high=201.0)
        entry = _pm_entry(
            envelope_id="env-eq",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_equity_recommendation_json(),
        )
        add_pm_decision_row(session, entry)
        session.flush()

        record = _run(session, entry, entry.detail, ReplayKind.REJECTION)

        assert record is not None
        assert record.replay_status is ReplayStatus.EVALUATED
        assert record.entered is True
        assert record.realized_pl is not None
        # Persisted.
        stored = load_counterfactual_replays_for_envelope(session, EnvelopeId("env-eq"))
        assert len(stored) == 1

    def test_idempotency_hit_returns_none(self, session: Session) -> None:
        _seed_bars(session, "AAPL", hit_high=201.0)
        entry = _pm_entry(
            envelope_id="env-eq",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_equity_recommendation_json(),
        )
        add_pm_decision_row(session, entry)
        session.flush()
        first = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        session.flush()
        assert first is not None
        second = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        assert second is None

    def test_missing_bars_writes_unevaluable(self, session: Session) -> None:
        _ensure_underlying(session, "AAPL")  # no bars
        entry = _pm_entry(
            envelope_id="env-eq",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_equity_recommendation_json(),
        )
        add_pm_decision_row(session, entry)
        session.flush()
        record = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        assert record is not None
        assert record.replay_status is ReplayStatus.UNEVALUABLE
        assert record.unevaluable_reason is UnevaluableReason.DATA_MISSING


class TestAnalystOptionDispatch:
    def test_happy_path_writes_evaluated_record(self, session: Session) -> None:
        _seed_bars(session, "AAPL", hit_high=201.0)
        _seed_option_snapshot(session, "O:AAPL260717C00150000", iv=0.35)
        entry = _pm_entry(
            envelope_id="env-opt",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_option_recommendation_json(),
        )
        add_pm_decision_row(session, entry)
        session.flush()
        record = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        assert record is not None
        assert record.replay_status is ReplayStatus.EVALUATED

    def test_missing_iv_writes_unevaluable(self, session: Session) -> None:
        _seed_bars(session, "AAPL", hit_high=201.0)
        _seed_option_snapshot(session, "O:AAPL260717C00150000", iv=None)
        entry = _pm_entry(
            envelope_id="env-opt",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_option_recommendation_json(),
        )
        add_pm_decision_row(session, entry)
        session.flush()
        record = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        assert record is not None
        assert record.replay_status is ReplayStatus.UNEVALUABLE
        assert record.unevaluable_reason is UnevaluableReason.DATA_MISSING


class TestStrategistDispatch:
    def test_position_close_writes_evaluated_record(self, session: Session) -> None:
        _seed_bars(session, "AAPL", hit_high=110.0)
        _seed_equity_position(session, position_id="POS-1", ticker="AAPL")
        entry = _pm_entry(
            envelope_id="env-sa",
            verdict=PMVerdict.REJECT,
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "position_assessment",
            },
            originating_proposal_json=strategist_close_position_assessment_json(),
            position_id="POS-1",
        )
        add_pm_decision_row(session, entry)
        session.flush()
        record = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        assert record is not None
        assert record.replay_status is ReplayStatus.EVALUATED
        assert record.realized_pl is not None


class TestPendingOrderDispatch:
    def test_cancel_writes_evaluated_record(self, session: Session) -> None:
        _seed_bars(session, "AAPL", hit_high=121.0)
        _seed_equity_position(session, position_id="POS-1", ticker="AAPL")
        _seed_bracket(session, bracket_id="BRK-1", position_id="POS-1", entry_order_id="ORD-1")
        _seed_entry_order(session, order_id="ORD-1", position_id="POS-1", bracket_id="BRK-1")
        entry = _pm_entry(
            envelope_id="env-po",
            verdict=PMVerdict.REJECT,
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "pending_order_assessment",
            },
            originating_proposal_json=strategist_pending_cancel_assessment_json(
                position_id="POS-1", order_id="ORD-1"
            ),
            position_id="POS-1",
        )
        add_pm_decision_row(session, entry)
        session.flush()
        record = _run(session, entry, entry.detail, ReplayKind.REJECTION)
        assert record is not None
        assert record.replay_status is ReplayStatus.EVALUATED
