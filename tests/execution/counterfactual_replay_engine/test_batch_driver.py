"""Tests for the batch driver ``replay_pending_proposals`` (ALP-564, story 08 §4).

The batch driver walks the due-proposal queue, replays each, and folds the
outcomes into a ``ReplayBatchResult``. The behaviors:

* a mixed batch (one EVALUATED, one UNEVALUABLE) tallies the right per-reason
  counters and persists one record each;
* a per-proposal typed error (a pending-order assessment naming a missing order)
  is caught, counted in ``error_count`` / ``first_error``, and the batch
  continues;
* an unexpected error (infrastructure-level) propagates out of the batch.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.execution.counterfactual_replay_engine.engine import (
    ReplayBatchResult,
    replay_pending_proposals,
)
from alphamind.execution.counterfactual_replay_engine.enums import UnevaluableReason
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.types import PMVerdict
from tests.execution.counterfactual_replay_engine._fixtures import (
    REPLAY_CONFIG,
    add_pm_decision_row,
    analyst_equity_recommendation_json,
    analyst_option_recommendation_json,
    make_paper_harness,
    pm_decision_entry,
    seed_invocation,
    strategist_pending_cancel_assessment_json,
)

_INVOCATION = "inv-2026-06-01T14:00:00Z-aaaa"
_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)
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


def _bar(ticker: str, period_start: datetime, *, high: float) -> OhlcvBars:
    period_end = datetime.fromtimestamp(period_start.timestamp() + 900, tz=UTC)
    return OhlcvBars(
        ticker=ticker,
        timeframe="15min",
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        session="regular",
        adj_open=100.0,
        adj_high=high,
        adj_low=99.0,
        adj_close=100.0,
        adj_volume=1,
        adj_vwap=None,
        unadj_open=100.0,
        unadj_high=high,
        unadj_low=99.0,
        unadj_close=100.0,
        unadj_volume=1_000_000,
        unadj_vwap=None,
        trade_count=None,
        source="test",
        ingested_at="2026-06-01T00:00:00Z",
    )


def _seed_bars(session: Session, ticker: str, *, count: int = 12) -> None:
    _ensure_underlying(session, ticker)
    for i in range(count):
        ts = _PROPOSAL_TS + timedelta(minutes=15 * i)
        session.add(_bar(ticker, ts, high=201.0 if i == 1 else 101.0))
    session.flush()


def _seed_analyst_reject(session: Session, *, entry_id: str, envelope_id: str) -> None:
    entry = pm_decision_entry(
        entry_id=entry_id,
        invocation_id=_INVOCATION,
        timestamp=_PROPOSAL_TS,
        envelope_id=envelope_id,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=analyst_equity_recommendation_json(),
    )
    add_pm_decision_row(session, entry)


def _seed_unhydratable(session: Session, *, entry_id: str, envelope_id: str) -> None:
    # A REJECT entry whose originating-proposal body is structurally invalid:
    # hydrate_originating_proposal raises ProposalHydrationError. The queue does
    # NOT hydrate, so the error surfaces inside replay_proposal — within the
    # batch's per-proposal try/except — and is counted, not propagated.
    entry = pm_decision_entry(
        entry_id=entry_id,
        invocation_id=_INVOCATION,
        timestamp=_PROPOSAL_TS,
        envelope_id=envelope_id,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json={"not": "a valid recommendation"},
    )
    add_pm_decision_row(session, entry)


def _seed_pending_missing_order(session: Session, *, entry_id: str, envelope_id: str) -> None:
    # A pending-order cancel whose *position* exists (so eligibility resolves the
    # underlying and finds bars) but whose *order_id* is never seeded → the
    # driver's _load_order raises OriginatingProposalLookupError when it routes.
    _seed_equity_position(session, position_id="POS-X", ticker="AAPL")
    entry = pm_decision_entry(
        entry_id=entry_id,
        invocation_id=_INVOCATION,
        timestamp=_PROPOSAL_TS,
        envelope_id=envelope_id,
        verdict=PMVerdict.REJECT,
        source_provenance_json={
            "source_provenance": "pm_strategist",
            "recommendation_type": "pending_order_assessment",
        },
        originating_proposal_json=strategist_pending_cancel_assessment_json(
            position_id="POS-X", order_id="ORD-MISSING"
        ),
        position_id="POS-X",
    )
    add_pm_decision_row(session, entry)


def _seed_equity_position(session: Session, *, position_id: str, ticker: str) -> None:
    from alphamind._kernel.ids import PositionId, Symbol
    from alphamind._kernel.money import money, price
    from alphamind.portfolio_state.records.positions import (
        Direction,
        EquityPositionDetails,
        PositionFill,
        PositionRecord,
        PositionStatus,
    )
    from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row

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
                fill_timestamp=_PROPOSAL_TS - timedelta(days=1),
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
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        seed_invocation(sess, _INVOCATION)
        yield sess


def _run_batch(session: Session) -> ReplayBatchResult:
    return replay_pending_proposals(
        session,
        config=REPLAY_CONFIG,
        paper_harness_config=_HARNESS,
        risk_free_rate=_RFR,
        as_of=_AS_OF,
    )


class TestBatchDriver:
    def test_mixed_batch_tallies_evaluated_and_unevaluable(self, session: Session) -> None:
        # One analyst-equity with bars (EVALUATED) + one with no bars (UNEVALUABLE).
        _seed_bars(session, "AAPL")
        _seed_analyst_reject(session, entry_id="e1", envelope_id="env-eval")
        # No bars for MSFT → DATA_MISSING. Seed the universe row so the
        # ohlcv FK exists but leave the bars table empty.
        _ensure_underlying(session, "MSFT")
        entry = pm_decision_entry(
            entry_id="e2",
            invocation_id=_INVOCATION,
            timestamp=_PROPOSAL_TS,
            envelope_id="env-unev",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_equity_recommendation_json(ticker="MSFT"),
        )
        add_pm_decision_row(session, entry)
        session.flush()

        result = _run_batch(session)

        assert result.evaluated == 1
        assert result.unevaluable_by_reason == {UnevaluableReason.DATA_MISSING.value: 1}
        assert result.error_count == 0
        assert result.skipped_idempotent == 0
        assert result.skipped_not_due == 0

    def test_per_proposal_error_is_counted_not_propagated(self, session: Session) -> None:
        _seed_bars(session, "AAPL")
        _seed_analyst_reject(session, entry_id="e1", envelope_id="env-eval")
        _seed_pending_missing_order(session, entry_id="e2", envelope_id="env-err")
        session.flush()

        result = _run_batch(session)

        # The good proposal still evaluated; the bad one counted, not raised.
        assert result.evaluated == 1
        assert result.error_count == 1
        assert result.first_error is not None
        assert "ORD-MISSING" in result.first_error

    def test_unhydratable_proposal_is_counted_batch_completes(self, session: Session) -> None:
        # The hydration error is raised inside replay_proposal (the queue no
        # longer hydrates), so it is caught by the batch try/except — counted in
        # error_count, with the valid proposal still evaluated.
        _seed_bars(session, "AAPL")
        _seed_unhydratable(session, entry_id="e1", envelope_id="env-bad")
        _seed_analyst_reject(session, entry_id="e2", envelope_id="env-eval")
        session.flush()

        result = _run_batch(session)

        assert result.evaluated == 1
        assert result.error_count == 1
        assert result.first_error is not None

    def test_not_yet_due_proposal_counted_separately_from_idempotent(
        self, session: Session
    ) -> None:
        # as_of before the 24h analyst horizon: the proposal is not yet due. It
        # must land in skipped_not_due, NOT skipped_idempotent.
        _seed_bars(session, "AAPL")
        _seed_analyst_reject(session, entry_id="e1", envelope_id="env-eval")
        session.flush()

        result = replay_pending_proposals(
            session,
            config=REPLAY_CONFIG,
            paper_harness_config=_HARNESS,
            risk_free_rate=_RFR,
            as_of=_PROPOSAL_TS + timedelta(hours=1),
        )

        assert result.skipped_not_due == 1
        assert result.skipped_idempotent == 0
        assert result.evaluated == 0

    def test_unexpected_error_propagates(self, session: Session) -> None:
        # An option proposal that passes eligibility (one bar overlaps the window
        # and the entry IV exists) but has only the proposal bar — the option
        # simulator raises a ValueError, which is NOT a per-proposal typed error,
        # so it propagates out of the batch as an infrastructure-level failure.
        _ensure_underlying(session, "AAPL")
        # A single bar: covers the window start but has no following fill bar.
        session.add(_bar("AAPL", _PROPOSAL_TS, high=101.0))
        session.add(
            OptionsContracts(
                contract_ticker="O:AAPL260717C00150000",
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
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=_PROPOSAL_TS.isoformat(),
                contract_ticker="O:AAPL260717C00150000",
                underlying_ticker="AAPL",
                open_interest=100,
                volume_today=50,
                last_price=5.0,
                bid=4.9,
                ask=5.1,
                implied_volatility=0.35,
                underlying_price=100.0,
                source="test",
                ingested_at="2026-06-01T00:00:00Z",
            )
        )
        entry = pm_decision_entry(
            entry_id="e1",
            invocation_id=_INVOCATION,
            timestamp=_PROPOSAL_TS,
            envelope_id="env-boom",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=analyst_option_recommendation_json(),
        )
        add_pm_decision_row(session, entry)
        session.flush()

        with pytest.raises(ValueError, match="option replay requires"):
            _run_batch(session)
