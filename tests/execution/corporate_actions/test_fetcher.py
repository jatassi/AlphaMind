"""Tests for the v1beta1 corporate-actions fetcher (ALP-410).

Exercises ``fetch_unprocessed_ca_activities`` end-to-end against a fake
``CorporateActionsQueries`` that returns synthetic typed v1beta1 events plus
a real ``InvocationHandle`` over an on-disk SQLite database for ledger dedup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alpaca.data.enums import CorporateActionsType
from alpaca.data.models.corporate_actions import (
    CashDividend,
    CashMerger,
    CorporateAction,
    ForwardSplit,
    NameChange,
    Redemption,
    ReverseSplit,
    RightsDistribution,
    SpinOff,
    StockAndCashMerger,
    StockDividend,
    StockMerger,
    UnitSplit,
    WorthlessRemoval,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId
from alphamind.execution.corporate_actions.config import CorporateActionsConfig
from alphamind.execution.corporate_actions.types import (
    CorporateActionActivity,
    PositionLookup,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.events.activity_log import CorporateActionType
from alphamind.portfolio_state.records.positions import Direction
from alphamind.state.invocation_context.context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    CorporateActionLedgerStatus,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_PROCESS_ID = "proc-fetcher-1"
_INV_ID = "inv-fetcher-2026-05-08T12:00:00Z"


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield ``(async_engine, session_factory)`` over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind_fetcher.db"

    import alphamind.state.tables  # noqa: F401 — side-effect import

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


def _make_process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-fetcher-1.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0",
    )


def _make_invocation_record(invocation_id: str = _INV_ID) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/provenance/inv/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/provenance/calibration.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _seed_invocation_substrate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_make_invocation_record()))
        await sess.commit()


async def _seed_ledger_row(
    factory: async_sessionmaker[AsyncSession],
    *,
    alpaca_activity_id: str,
    processing_timestamp: datetime,
) -> None:
    async with factory() as sess:
        sess.add(
            CorporateActionIntegrationLedgerRow(
                alpaca_activity_id=alpaca_activity_id,
                processing_invocation_id=_INV_ID,
                processing_timestamp=processing_timestamp.isoformat(),
                processing_status=CorporateActionLedgerStatus.PROCESSED.value,
            )
        )
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=_INV_ID + "-direct"),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Fake queries + position lookup
# ---------------------------------------------------------------------------


class _FakeQueries:
    """Stand-in for ``CorporateActionsQueries``; records the request kwargs."""

    def __init__(self, events: tuple[CorporateAction, ...]) -> None:
        self._events = events
        self.calls: list[dict[str, object]] = []

    async def get_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...] | None = None,
        start: date,
        end: date,
        types: tuple[CorporateActionsType, ...] | None = None,
    ) -> tuple[CorporateAction, ...]:
        self.calls.append(
            {"symbols": symbols, "start": start, "end": end, "types": types},
        )
        return self._events


def _lookup_for(
    positions: dict[str, PositionLookup],
) -> Callable[[str], PositionLookup | None]:
    """Build the callable the fetcher consumes from a dict."""

    def _lookup(symbol: str) -> PositionLookup | None:
        return positions.get(symbol)

    return _lookup


async def _run_fetch(
    factory: async_sessionmaker[AsyncSession],
    queries: _FakeQueries,
    positions: dict[str, PositionLookup],
    *,
    config: CorporateActionsConfig | None = None,
    known_symbols: tuple[str, ...] | None = None,
) -> tuple[CorporateActionActivity, ...]:
    """Open a handle, run the fetcher, close — used by every parametric test."""
    from alphamind.execution.corporate_actions.fetcher import (
        fetch_unprocessed_ca_activities,
    )

    ctx, handle = await _open_handle(factory)
    try:
        return await fetch_unprocessed_ca_activities(
            handle,
            queries,
            config=config or CorporateActionsConfig(),
            position_lookup_for_symbol=_lookup_for(positions),
            known_symbols=(tuple(positions.keys()) if known_symbols is None else known_symbols),
        )
    finally:
        await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _uuid(seed: int) -> UUID:
    return UUID(int=seed)


def _forward_split(symbol: str, ex_date: date, *, seed: int = 1) -> ForwardSplit:
    return ForwardSplit(
        id=_uuid(seed),
        corporate_action_type="forward_split",
        symbol=symbol,
        cusip="037833100",
        new_rate=4.0,
        old_rate=1.0,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _reverse_split(symbol: str, ex_date: date, *, seed: int = 2) -> ReverseSplit:
    return ReverseSplit(
        id=_uuid(seed),
        corporate_action_type="reverse_split",
        symbol=symbol,
        old_cusip="037833100",
        new_cusip="037833109",
        new_rate=1.0,
        old_rate=10.0,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _stock_dividend(symbol: str, ex_date: date, *, seed: int = 3) -> StockDividend:
    return StockDividend(
        id=_uuid(seed),
        corporate_action_type="stock_dividend",
        symbol=symbol,
        cusip="037833100",
        rate=0.10,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _cash_dividend(symbol: str, ex_date: date, *, seed: int = 4) -> CashDividend:
    return CashDividend(
        id=_uuid(seed),
        corporate_action_type="cash_dividend",
        symbol=symbol,
        cusip="037833100",
        rate=0.50,
        special=False,
        foreign=False,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _spin_off(parent: str, child: str, ex_date: date, *, seed: int = 5) -> SpinOff:
    return SpinOff(
        id=_uuid(seed),
        corporate_action_type="spin_off",
        source_symbol=parent,
        source_cusip="037833100",
        source_rate=1.0,
        new_symbol=child,
        new_cusip="037833200",
        new_rate=0.25,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _cash_merger(target: str, effective: date, *, seed: int = 6) -> CashMerger:
    return CashMerger(
        id=_uuid(seed),
        corporate_action_type="cash_merger",
        acquirer_symbol="ACQR",
        acquirer_cusip="111111111",
        acquiree_symbol=target,
        acquiree_cusip="037833100",
        rate=25.0,
        process_date=effective,
        effective_date=effective,
    )


def _stock_merger(target: str, effective: date, *, seed: int = 7) -> StockMerger:
    return StockMerger(
        id=_uuid(seed),
        corporate_action_type="stock_merger",
        acquirer_symbol="ACQR",
        acquirer_cusip="111111111",
        acquirer_rate=0.5,
        acquiree_symbol=target,
        acquiree_cusip="037833100",
        acquiree_rate=1.0,
        process_date=effective,
        effective_date=effective,
    )


def _stock_and_cash_merger(
    target: str,
    effective: date,
    *,
    seed: int = 8,
) -> StockAndCashMerger:
    return StockAndCashMerger(
        id=_uuid(seed),
        corporate_action_type="stock_and_cash_merger",
        acquirer_symbol="ACQR",
        acquirer_cusip="111111111",
        acquirer_rate=0.5,
        acquiree_symbol=target,
        acquiree_cusip="037833100",
        acquiree_rate=1.0,
        cash_rate=5.0,
        process_date=effective,
        effective_date=effective,
    )


def _name_change(old: str, new: str, process_date: date, *, seed: int = 9) -> NameChange:
    return NameChange(
        id=_uuid(seed),
        corporate_action_type="name_change",
        old_symbol=old,
        old_cusip="037833100",
        new_symbol=new,
        new_cusip="037833200",
        process_date=process_date,
    )


# ---------------------------------------------------------------------------
# Tests — cursor derivation
# ---------------------------------------------------------------------------


async def test_cold_start_cursor_is_today_minus_lookback(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Empty ledger → ``start = today - lookback_days``, ``end = today``."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=())

    await _run_fetch(
        factory,
        queries,
        {},
        config=CorporateActionsConfig(fetcher_lookback_days=7),
        known_symbols=("AAPL",),
    )

    assert len(queries.calls) == 1
    call = queries.calls[0]
    today = datetime.now(UTC).date()
    assert call["end"] == today
    assert call["start"] == today - timedelta(days=7)


async def test_warm_start_cursor_uses_max_processing_timestamp(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Warm start → ``start = max(processing_timestamp).date() - lookback_days``."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    seed_ts = datetime(2026, 5, 1, 14, 0, 0, tzinfo=UTC)
    await _seed_ledger_row(factory, alpaca_activity_id="seed-1", processing_timestamp=seed_ts)
    # Older row should not displace the max.
    await _seed_ledger_row(
        factory,
        alpaca_activity_id="seed-older",
        processing_timestamp=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
    )
    queries = _FakeQueries(events=())

    await _run_fetch(
        factory,
        queries,
        {},
        config=CorporateActionsConfig(fetcher_lookback_days=7),
        known_symbols=("AAPL",),
    )

    assert queries.calls[0]["start"] == seed_ts.date() - timedelta(days=7)


# ---------------------------------------------------------------------------
# Tests — discriminator parametric
# ---------------------------------------------------------------------------


_POSITIONS_AAPL_LONG: dict[str, PositionLookup] = {
    "AAPL": PositionLookup(
        position_id=PositionId("pos-aapl"), direction=Direction.LONG, quantity=10.0
    ),
}
_POSITIONS_AAPL_SHORT: dict[str, PositionLookup] = {
    "AAPL": PositionLookup(
        position_id=PositionId("pos-aapl"), direction=Direction.SHORT, quantity=10.0
    ),
}
_POSITIONS_PARENT: dict[str, PositionLookup] = {
    "PRNT": PositionLookup(
        position_id=PositionId("pos-prnt"), direction=Direction.LONG, quantity=8.0
    ),
}
_POSITIONS_ACQUIREE: dict[str, PositionLookup] = {
    "TGT": PositionLookup(
        position_id=PositionId("pos-tgt"), direction=Direction.LONG, quantity=20.0
    ),
}
_POSITIONS_OLDSYM: dict[str, PositionLookup] = {
    "OLD": PositionLookup(
        position_id=PositionId("pos-old"), direction=Direction.LONG, quantity=5.0
    ),
}


async def test_dispatch_forward_split(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``ForwardSplit`` → ``SPLIT`` with ``new_rate / old_rate`` ratio."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    event = _forward_split("AAPL", date(2026, 5, 5))
    queries = _FakeQueries(events=(event,))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.SPLIT
    assert activity.alpaca_activity_id == str(event.id)
    assert activity.ticker == "AAPL"
    assert activity.position_id == "pos-aapl"
    assert activity.ratio_or_amount == pytest.approx(4.0)
    assert activity.signed_cash_impact_usd == pytest.approx(0.0)
    assert activity.new_ticker is None
    assert activity.transaction_time == datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC)


async def test_dispatch_reverse_split(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``ReverseSplit`` → ``REVERSE_SPLIT`` with ratio < 1."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_reverse_split("AAPL", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.REVERSE_SPLIT
    assert activity.ratio_or_amount == pytest.approx(0.1)
    assert activity.signed_cash_impact_usd == pytest.approx(0.0)


async def test_dispatch_stock_dividend(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``StockDividend`` → ``STOCK_DIVIDEND``; ``ratio_or_amount`` is the raw rate."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_stock_dividend("AAPL", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.STOCK_DIVIDEND
    assert activity.ratio_or_amount == pytest.approx(0.10)
    assert activity.signed_cash_impact_usd == pytest.approx(0.0)


async def test_dispatch_cash_dividend_long(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``CashDividend`` against a LONG position → ``CASH_DIVIDEND_LONG`` (positive)."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_cash_dividend("AAPL", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.CASH_DIVIDEND_LONG
    # rate (0.50) * quantity (10) = +5.0
    assert activity.signed_cash_impact_usd == pytest.approx(5.0)
    assert activity.ratio_or_amount == pytest.approx(0.0)


async def test_dispatch_cash_dividend_short(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``CashDividend`` against a SHORT position → ``CASH_DIVIDEND_SHORT`` (negative)."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_cash_dividend("AAPL", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_SHORT)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.CASH_DIVIDEND_SHORT
    # -rate (0.50) * quantity (10) = -5.0
    assert activity.signed_cash_impact_usd == pytest.approx(-5.0)


async def test_dispatch_spin_off(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``SpinOff`` → ``SPIN_OFF`` keyed on the parent ``source_symbol``."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_spin_off("PRNT", "CHLD", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_PARENT)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.SPIN_OFF
    assert activity.ticker == "PRNT"
    assert activity.new_ticker == "CHLD"
    # new_rate / source_rate = 0.25 / 1.0 = 0.25 child per parent share
    assert activity.ratio_or_amount == pytest.approx(0.25)
    assert activity.signed_cash_impact_usd == pytest.approx(0.0)
    assert activity.position_id == "pos-prnt"


async def test_dispatch_cash_merger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``CashMerger`` → ``CASH_MERGER`` keyed on the ``acquiree_symbol``."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_cash_merger("TGT", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_ACQUIREE)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.CASH_MERGER
    assert activity.ticker == "TGT"
    assert activity.new_ticker is None
    # rate (25.0) * quantity (20.0) = +500.0
    assert activity.signed_cash_impact_usd == pytest.approx(500.0)
    # Effective date anchored at midnight UTC
    assert activity.transaction_time == datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC)


async def test_dispatch_stock_merger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``StockMerger`` → ``STOCK_MERGER`` with the new acquirer ticker."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_stock_merger("TGT", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_ACQUIREE)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.STOCK_MERGER
    assert activity.ticker == "TGT"
    assert activity.new_ticker == "ACQR"
    assert activity.signed_cash_impact_usd == pytest.approx(0.0)


async def test_dispatch_stock_and_cash_merger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``StockAndCashMerger`` → ``STOCK_MERGER`` with the ``cash_rate`` leg credited."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_stock_and_cash_merger("TGT", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_ACQUIREE)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.STOCK_MERGER
    assert activity.ticker == "TGT"
    assert activity.new_ticker == "ACQR"
    # cash_rate (5.0) * quantity (20.0) = +100.0
    assert activity.signed_cash_impact_usd == pytest.approx(100.0)


async def test_dispatch_name_change(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``NameChange`` → ``SYMBOL_CHANGE`` keyed on the ``old_symbol``."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_name_change("OLD", "NEW", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_OLDSYM)

    assert len(result) == 1
    activity = result[0]
    assert activity.action_type == CorporateActionType.SYMBOL_CHANGE
    assert activity.ticker == "OLD"
    assert activity.new_ticker == "NEW"
    assert activity.signed_cash_impact_usd == pytest.approx(0.0)
    # process_date used when no ex_date
    assert activity.transaction_time == datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests — filtering and dedup
# ---------------------------------------------------------------------------


async def test_event_for_unknown_symbol_is_filtered(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An event whose symbol returns ``None`` from the lookup is dropped."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_forward_split("UNKNOWN", date(2026, 5, 5)),))

    # Caller has at least one local position so ``known_symbols`` is non-empty
    # — the broker returns an UNKNOWN event for a symbol the local side has no
    # exposure to and the lookup filter drops it.
    result = await _run_fetch(factory, queries, {}, known_symbols=("AAPL",))

    assert result == ()


async def test_event_already_in_ledger_is_filtered(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An event whose UUID already appears in the ledger is dropped."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    event = _forward_split("AAPL", date(2026, 5, 5))
    await _seed_ledger_row(factory, alpaca_activity_id=str(event.id), processing_timestamp=_NOW)
    queries = _FakeQueries(events=(event,))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    assert result == ()


# ---------------------------------------------------------------------------
# Tests — sort order
# ---------------------------------------------------------------------------


async def test_output_is_sorted_ascending_by_transaction_time(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Events return in ascending ``transaction_time`` order regardless of input order."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(
        events=(
            _forward_split("AAPL", date(2026, 5, 8), seed=100),
            _forward_split("AAPL", date(2026, 5, 3), seed=101),
            _forward_split("AAPL", date(2026, 5, 5), seed=102),
        )
    )

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    transaction_times = [activity.transaction_time for activity in result]
    assert transaction_times == sorted(transaction_times)
    assert transaction_times[0] == datetime(2026, 5, 3, 0, 0, 0, tzinfo=UTC)
    assert transaction_times[-1] == datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests — capture-only v1beta1 types (ALP-849 / W1c)
# ---------------------------------------------------------------------------


def _unit_split(seed: int = 200) -> UnitSplit:
    return UnitSplit(
        id=_uuid(seed),
        corporate_action_type="unit_split",
        old_symbol="AAPL",
        old_cusip="037833100",
        old_rate=1.0,
        new_symbol="AAPL",
        new_cusip="037833100",
        new_rate=2.0,
        alternate_symbol="ALT",
        alternate_cusip="037833999",
        alternate_rate=0.5,
        process_date=date(2026, 5, 5),
        effective_date=date(2026, 5, 5),
    )


def _redemption(seed: int = 201) -> Redemption:
    return Redemption(
        id=_uuid(seed),
        corporate_action_type="redemption",
        symbol="AAPL",
        cusip="037833100",
        rate=100.0,
        process_date=date(2026, 5, 5),
    )


def _worthless(seed: int = 202) -> WorthlessRemoval:
    return WorthlessRemoval(
        id=_uuid(seed),
        corporate_action_type="worthless_removal",
        symbol="AAPL",
        cusip="037833100",
        process_date=date(2026, 5, 5),
    )


async def test_capture_only_types_are_surfaced_for_event_log_capture(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``WorthlessRemoval`` / ``UnitSplit`` / ``Redemption`` — the v1beta1 types
    the fetcher used to drop — are now surfaced as capture-only activities (no
    position-mutation math, ``signed_cash_impact_usd == 0``) so fill collection captures
    them onto the append-only event log (W1c)."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_unit_split(), _redemption(), _worthless()))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    by_type = {a.action_type for a in result}
    assert by_type == {
        CorporateActionType.UNIT_SPLIT,
        CorporateActionType.REDEMPTION,
        CorporateActionType.WORTHLESS_REMOVAL,
    }
    for activity in result:
        assert activity.position_id == "pos-aapl"
        assert activity.signed_cash_impact_usd == 0.0
        assert activity.ratio_or_amount == 0.0


async def test_rights_distribution_is_dropped(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``RightsDistribution`` is not in the event-log CA vocabulary and is
    dropped silently (defense in depth)."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    rights = RightsDistribution(
        id=_uuid(203),
        corporate_action_type="rights_distribution",
        source_symbol="AAPL",
        source_cusip="037833100",
        new_symbol="AAPLR",
        new_cusip="037833900",
        rate=0.1,
        ex_date=date(2026, 5, 5),
        process_date=date(2026, 5, 5),
    )
    queries = _FakeQueries(events=(rights,))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG)

    assert result == ()


# ---------------------------------------------------------------------------
# Tests — return-type stability
# ---------------------------------------------------------------------------


async def test_returns_tuple_of_corporate_action_activity(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The return type is ``tuple[CorporateActionActivity, ...]`` even when empty."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=())

    result = await _run_fetch(factory, queries, {}, known_symbols=("AAPL",))

    assert isinstance(result, tuple)
    for activity in result:
        assert isinstance(activity, CorporateActionActivity)


# ---------------------------------------------------------------------------
# Tests — server-side symbol narrowing
# ---------------------------------------------------------------------------


async def test_known_symbols_are_passed_to_broker(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``known_symbols`` is forwarded verbatim to ``get_corporate_actions(symbols=...)``."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=())

    symbols = ("AAPL", "MSFT", "TGT")
    await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG, known_symbols=symbols)

    assert len(queries.calls) == 1
    assert queries.calls[0]["symbols"] == symbols


async def test_empty_known_symbols_short_circuits_without_api_call(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When ``known_symbols`` is empty the fetcher returns ``()`` without hitting the API."""
    _, factory = db
    await _seed_invocation_substrate(factory)
    queries = _FakeQueries(events=(_forward_split("AAPL", date(2026, 5, 5)),))

    result = await _run_fetch(factory, queries, _POSITIONS_AAPL_LONG, known_symbols=())

    assert result == ()
    assert queries.calls == []
