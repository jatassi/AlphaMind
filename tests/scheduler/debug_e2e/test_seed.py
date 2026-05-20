"""Tests for the debug-e2e synthetic-portfolio seeder (story 01c / ALP-496).

Covers:

* DB-path guard refuses paths that do not end with ``-debug-e2e.db``.
* Wipe of the 9 enumerated state-persistence tables (FK-safe order).
* Seed of ``positions`` (equity / option / strategy variants),
  ``theses`` (one-to-one with positions), and ``cash_ledger``
  (singleton row).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.persistence.models import Base, Brief
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.scheduler.debug_e2e.seed import (
    _OPTION_CONTRACT_MULTIPLIER,
    _WIPE_ORDER,
    _strategy_leg_premiums,
    wipe_and_seed,
)
from alphamind.state.tables import (
    ActivityLogRow,
    BracketLegRow,
    BracketRow,
    CashLedgerRow,
    InvocationRow,
    PositionRow,
    ProcessLifetimeRow,
    ThesisRow,
)

_NOW = datetime(2026, 5, 17, 14, 30, tzinfo=UTC)


@pytest.fixture()
async def async_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[Path, async_sessionmaker[AsyncSession]]]:
    """Async session factory backed by a -debug-e2e.db file."""
    import alphamind.state.tables  # noqa: F401 — register ORM models on Base.metadata

    db_path = tmp_path / "alphamind-debug-e2e.db"

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield db_path, factory
    finally:
        await async_engine.dispose()


def _make_portfolio(
    *,
    positions: tuple[Any, ...] = (),
    theses: tuple[Any, ...] = (),
    starting_cash_usd: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        positions=positions,
        theses=theses,
        starting_cash_usd=starting_cash_usd,
    )


# ---------------------------------------------------------------------------
# Pre-population helpers — minimal rows for the wipe-list tables.
# ---------------------------------------------------------------------------


def _prepopulate_wipe_list_tables(db_path: Path) -> None:
    """Seed pre-existing rows in the wipe-list tables that admit a clean seed.

    Six of the nine wipe-list tables (``process_lifetimes``, ``invocations``,
    ``positions``, ``theses``, ``cash_ledger``, ``activity_log``) can be seeded
    standalone without depending on tables outside the wipe list. The
    remaining three (``brackets``, ``bracket_legs``, ``fill_records``) require
    an ``orders`` parent, which is NOT in the wipe list — pre-populating those
    would leave orphan rows that the wipe cannot remove (orders.bracket_id is
    a deferrable RESTRICT FK that survives). The test asserts the wipe
    against the rows it CAN seed; the wipe still runs all 9 ``DELETE``
    statements per the design contract, which is verified by the SELECT loop
    in the test asserting every row class is empty.
    """
    ts = "2026-05-07T14:30:00Z"
    sync_engine = make_engine(str(db_path))
    with make_session_factory(sync_engine)() as sess:
        sess.add(
            ProcessLifetimeRow(
                process_lifetime_id="proc-pre",
                process_role="pipeline",
                process_start_at=ts,
                process_pid=1,
                hostname="host",
                git_sha="a" * 40,
                git_branch="main",
                git_dirty=0,
                python_version="3.13.1",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/tmp/snap",
                anthropic_sdk_version="0.40.0",
                claude_agent_sdk_version="0.1.0",
                os_release="Darwin",
            )
        )
        sess.flush()  # process_lifetimes parent must exist before invocations.
        sess.add(
            InvocationRow(
                invocation_id="inv-pre",
                process_lifetime_id="proc-pre",
                start_at=ts,
                phase1_completed_at=None,
                phase2_completed_at=None,
                trigger_type="scheduled",
                trigger_source="cron",
                trigger_reason="9 * * *",
                git_sha_at_invocation="a" * 40,
                active_profile="medium",
                active_regime="normal",
                active_mode="normal",
                active_overlays_json="[]",
                resolved_config_hash="0" * 64,
                resolved_config_snapshot_path="/tmp/cfg",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="/tmp/calib",
                data_source_freshness_json="{}",
                fill_collection_summary_json=None,
                command_execution_summary_json=None,
                staleness_flag=None,
                snapshot_metadata_json=None,
            )
        )
        sess.flush()  # invocations parent must exist before activity_log.
        sess.add(
            PositionRow(
                position_id="pos-pre",
                thesis_id=None,
                bracket_id=None,
                status=PositionStatus.PENDING.value,
                direction=Direction.LONG.value,
                entry_timestamp=None,
                instrument_type=InstrumentType.EQUITY.value,
                details_json=(
                    f'{{"instrument_type":"{InstrumentType.EQUITY.value}",'
                    '"ticker":"OLD","share_count":0,'
                    '"average_cost_basis_per_share":0}'
                ),
                execution_history_json="[]",
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=0,
                parent_position_id=None,
                origin=None,
            )
        )
        sess.add(
            ThesisRow(
                thesis_id="thesis-pre",
                position_id="pos-pre",
                status=ThesisRecordStatus.ACTIVE.value,
                resolution_timestamp=None,
                resolution_category=None,
                summary="old thesis",
                time_expectation_hours=24.0,
                position_size_rationale=None,
                generation_timestamp=ts,
                narrative_json="{}",
            )
        )
        sess.add(
            ActivityLogRow(
                entry_id="entry-pre",
                invocation_id="inv-pre",
                entry_at=ts,
                event_type="POSITION_OPENED",
                event_group="POSITION_LIFECYCLE",
                position_id=None,
                order_id=None,
                thesis_id=None,
                source="COMMAND_EXECUTOR",
                detail_json="{}",
            )
        )
        sess.add(
            CashLedgerRow(
                id="current",
                current_cash_usd=1000,
                settled_cash_usd=1000,
                reserved_capital_usd=0,
                available_buying_power_usd=1000,
                margin_held_usd=0,
                unsettled_proceeds_json="[]",
                last_updated_at=ts,
            )
        )
        sess.add(
            Brief(
                invocation_id="inv-pre",
                brief_kind="correlation_regime",
                reference_index_json="{}",
                text="pre-existing brief",
                created_at=ts,
            )
        )
        sess.commit()
    sync_engine.dispose()


# ---------------------------------------------------------------------------
# (a) DB-path guard
# ---------------------------------------------------------------------------


class TestDbPathGuard:
    @pytest.mark.asyncio
    async def test_refuses_path_without_debug_suffix(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            await wipe_and_seed(
                session=None,  # type: ignore[arg-type]
                now=_NOW,
                db_path="/var/lib/alphamind/alphamind.db",
                portfolio=_make_portfolio(),
            )
        assert "'/var/lib/alphamind/alphamind.db'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# (b) Wipe leaves tables empty against pre-populated fixture
# ---------------------------------------------------------------------------


class TestWipeClearsTables:
    @pytest.mark.asyncio
    async def test_wipe_removes_all_pre_existing_wipe_list_rows(
        self,
        async_factory: tuple[Path, async_sessionmaker[AsyncSession]],
    ) -> None:
        db_path, factory = async_factory
        _prepopulate_wipe_list_tables(db_path)

        async with factory() as session:
            await wipe_and_seed(
                session=session,
                now=_NOW,
                db_path=str(db_path),
                portfolio=_make_portfolio(),
            )

        async with factory() as session:
            for row_type in (
                ActivityLogRow,
                BracketLegRow,
                BracketRow,
                Brief,
                ThesisRow,
                PositionRow,
                InvocationRow,
                ProcessLifetimeRow,
            ):
                rows = (await session.execute(select(row_type))).scalars().all()
                assert rows == [], f"{row_type.__name__} not wiped"
            # cash_ledger is wiped then re-seeded (one row with starting_cash_usd=0).
            cash_rows = (await session.execute(select(CashLedgerRow))).scalars().all()
            assert len(cash_rows) == 1
            assert float(cash_rows[0].current_cash_usd) == 0.0


# ---------------------------------------------------------------------------
# (c) Seed inserts the expected rows from a fixture SyntheticPortfolio.
# ---------------------------------------------------------------------------


def _synthetic_equity(symbol: str, direction: str, qty: float, avg_cost: float) -> Any:
    """Minimal SyntheticEquity-shape stand-in (story 02b ships the real type)."""
    return SimpleNamespace(
        symbol=symbol,
        direction=SimpleNamespace(value=direction),
        qty=qty,
        avg_cost=avg_cost,
        sector="tech",
        borrow_rate_pct=1.5 if direction == "SHORT" else None,
    )


def _synthetic_option(
    underlying: str,
    contract_type: str,
    strike: float,
    expiration_offset_days: int,
    contracts: float,
    premium: float,
) -> Any:
    return SimpleNamespace(
        underlying=underlying,
        contract_type=SimpleNamespace(value=contract_type),
        strike=strike,
        expiration_offset_days=expiration_offset_days,
        contracts=contracts,
        premium_per_contract=premium,
        sector="tech",
    )


def _synthetic_strategy_leg(
    contract_type: str, strike: float, direction: str, contracts: float
) -> Any:
    return SimpleNamespace(
        contract_type=SimpleNamespace(value=contract_type),
        strike=strike,
        direction=SimpleNamespace(value=direction),
        contracts=contracts,
    )


def _synthetic_strategy(
    underlying: str,
    expiration_offset_days: int,
    legs: tuple[Any, ...],
    net_premium: float,
    strategy_label: str,
) -> Any:
    return SimpleNamespace(
        underlying=underlying,
        expiration_offset_days=expiration_offset_days,
        legs=legs,
        net_premium=net_premium,
        strategy_label=strategy_label,
        sector="tech",
    )


def _synthetic_thesis(position_index: int, headline: str, rationale: str) -> Any:
    return SimpleNamespace(
        position_index=position_index,
        headline=headline,
        rationale=rationale,
    )


class TestSeed:
    @pytest.mark.asyncio
    async def test_seed_inserts_positions_theses_cash(
        self,
        async_factory: tuple[Path, async_sessionmaker[AsyncSession]],
    ) -> None:
        db_path, factory = async_factory
        portfolio = _make_portfolio(
            positions=(
                _synthetic_equity("NVDA", "LONG", qty=30, avg_cost=620.0),
                _synthetic_equity("TSLA", "SHORT", qty=30, avg_cost=250.0),
                _synthetic_option(
                    "AAPL",
                    "CALL",
                    strike=230.0,
                    expiration_offset_days=270,
                    contracts=3,
                    premium=14.50,
                ),
                _synthetic_strategy(
                    "MSFT",
                    expiration_offset_days=180,
                    legs=(
                        _synthetic_strategy_leg("CALL", 440.0, "LONG", contracts=2),
                        _synthetic_strategy_leg("CALL", 480.0, "SHORT", contracts=2),
                    ),
                    net_premium=24.60,
                    strategy_label="bull_call_spread",
                ),
            ),
            theses=(
                _synthetic_thesis(0, "NVDA AI tailwind", "data center demand"),
                _synthetic_thesis(1, "TSLA mean revert", "margin compression"),
                _synthetic_thesis(2, "AAPL upside call", "earnings catalyst"),
                _synthetic_thesis(3, "MSFT spread", "moderate bullish stance"),
            ),
            starting_cash_usd=31_940.0,
        )

        async with factory() as session:
            await wipe_and_seed(
                session=session, now=_NOW, db_path=str(db_path), portfolio=portfolio
            )

        async with factory() as session:
            positions = (
                (await session.execute(select(PositionRow).order_by(PositionRow.position_id)))
                .scalars()
                .all()
            )
            theses = (
                (await session.execute(select(ThesisRow).order_by(ThesisRow.thesis_id)))
                .scalars()
                .all()
            )
            cash_rows = (await session.execute(select(CashLedgerRow))).scalars().all()

        # 4 positions, 4 theses, 1 cash_ledger row.
        assert len(positions) == 4
        assert len(theses) == 4
        assert len(cash_rows) == 1

        # Instrument types projected onto the rows match the synthetic
        # discriminators.
        instrument_types = [row.instrument_type for row in positions]
        assert instrument_types == ["EQUITY", "EQUITY", "OPTIONS", "STRATEGY"]

        # Short equity carries the short-side fields.
        short_row = next(row for row in positions if row.direction == "SHORT")
        short_details = json.loads(short_row.details_json)
        assert short_details["borrow_rate_pct"] == 1.5
        assert short_details["locate_status"] == "LOCATED"
        assert short_details["margin_held_usd"] == 0.0

        # Long equity has all three short-only fields cleared.
        long_row = next(
            row for row in positions if row.direction == "LONG" and row.instrument_type == "EQUITY"
        )
        long_details = json.loads(long_row.details_json)
        assert long_details["borrow_rate_pct"] is None
        assert long_details["locate_status"] is None
        assert long_details["margin_held_usd"] is None

        # Option expiration_date is now + offset_days.
        option_row = next(row for row in positions if row.instrument_type == "OPTIONS")
        option_details = json.loads(option_row.details_json)
        expected_expiration = (_NOW + timedelta(days=270)).date().isoformat()
        assert option_details["expiration_date"] == expected_expiration
        assert option_details["contract_type"] == "CALL"
        assert option_details["strike_price"] == 230.0

        # Strategy carries its two legs.
        strategy_row = next(row for row in positions if row.instrument_type == "STRATEGY")
        strategy_details = json.loads(strategy_row.details_json)
        assert strategy_details["strategy_type_label"] == "bull_call_spread"
        assert len(strategy_details["legs"]) == 2
        # A strategy row carries direction = NULL — position-level direction is
        # a category error for a multi-leg strategy (ALP-610).
        assert strategy_row.direction is None

        # Theses are paired by position_index — thesis-XX references pos-XX.
        thesis_position_pairs = [(row.thesis_id, row.position_id) for row in theses]
        assert thesis_position_pairs == [
            ("debug-thesis-00", "debug-pos-00"),
            ("debug-thesis-01", "debug-pos-01"),
            ("debug-thesis-02", "debug-pos-02"),
            ("debug-thesis-03", "debug-pos-03"),
        ]

        # Cash ledger is the singleton with starting_cash_usd populated.
        assert cash_rows[0].id == "current"
        assert float(cash_rows[0].current_cash_usd) == 31_940.0
        assert float(cash_rows[0].settled_cash_usd) == 31_940.0


# ---------------------------------------------------------------------------
# Wipe ordering — children before parents (parent issue § (L))
# ---------------------------------------------------------------------------


class TestWipeOrdering:
    def test_wipe_order_lists_all_state_persistence_tables(self) -> None:
        """The wipe list must cover every state-persistence table.

        The snapshot-from-prod workflow brings real production rows for
        every state-persistence table into the bootstrap DB, so the wipe
        list must cover them all. Tables not covered would leak prod
        state into the verified pipeline.

        ``PRAGMA defer_foreign_keys = ON`` (set inside ``wipe_and_seed``)
        handles the orders↔brackets circular FK; the order below is
        documentation, not load-bearing for correctness.
        """
        names = [row_type.__tablename__ for row_type in _WIPE_ORDER]
        children = {
            "activity_log",
            "bracket_legs",
            "brackets",
            "briefs",
            "corporate_action_integration_ledger",
            "fill_records",
            "orders",
            "thesis_components",
            "theses",
        }
        parents = {
            "positions",
            "cash_ledger",
            "drawdown_state",
            "invocations",
            "process_lifetimes",
            "regime_adaptation_state",
        }

        # Every name in the list is one of the enumerated wipe tables.
        assert set(names) == children | parents

        # Every child appears before every parent it can reference.
        # The explicit FK relations among the 9 wipe-list tables:
        #   activity_log → invocations (RESTRICT, non-deferred)
        #   activity_log → positions / theses (deferred)
        #   bracket_legs → brackets (RESTRICT, non-deferred)
        #   brackets → positions (deferred)
        #   fill_records → invocations (RESTRICT, non-deferred)
        #   theses → positions (deferred)
        #   positions → theses / brackets (deferred)
        #   invocations → process_lifetimes (RESTRICT, non-deferred)
        # The strict child→parent order the design names:
        for child in children:
            for parent in parents:
                assert names.index(child) < names.index(parent), (
                    f"{child} must be wiped before {parent}"
                )


class TestStrategyLegPremiums:
    """``_strategy_leg_premiums`` — synthesizes per-leg premiums for a strategy."""

    def test_direction_aware_leg_sum_equals_net_premium(self) -> None:
        # Asymmetric leg counts: a LONG leg of 2 contracts, a SHORT leg of 1.
        legs = (
            _synthetic_strategy_leg("CALL", 100.0, "LONG", contracts=2),
            _synthetic_strategy_leg("CALL", 120.0, "SHORT", contracts=1),
        )
        net_premium = 50.0
        premiums = _strategy_leg_premiums(legs, net_premium)

        assert all(premium > 0.0 for premium in premiums)
        market_value = sum(
            (1.0 if leg.direction.value == "LONG" else -1.0)
            * leg.contracts
            * _OPTION_CONTRACT_MULTIPLIER
            * premium
            for leg, premium in zip(legs, premiums, strict=True)
        )
        assert market_value == pytest.approx(net_premium)

    def test_raises_when_strategy_has_no_long_leg(self) -> None:
        legs = (_synthetic_strategy_leg("CALL", 120.0, "SHORT", contracts=1),)
        with pytest.raises(ValueError, match="at least one LONG leg"):
            _strategy_leg_premiums(legs, 50.0)

    def test_raises_for_net_credit_strategy(self) -> None:
        # A net credit large enough to drive the long-leg premium non-positive.
        legs = (
            _synthetic_strategy_leg("CALL", 100.0, "LONG", contracts=1),
            _synthetic_strategy_leg("CALL", 120.0, "SHORT", contracts=1),
        )
        with pytest.raises(ValueError, match="net-credit"):
            _strategy_leg_premiums(legs, -150.0)
