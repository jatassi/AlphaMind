"""Genesis-verify — the §8 first-run genesis assertions made executable (ALP-858 / W5a).

Story 06 of the broker-boundary redesign. The genesis-cutover runbook §8 lists
the assertions that must hold on a **fresh, flat account** after the very first
pipeline invocation and a single canary trade. This module makes that checklist
*runnable* so the cutover (story 08) is gated by code, not by a human reading a
list — it is broker-boundary-redesign **invariant 6** ("genesis is clean") in
executable form.

What it asserts (genesis-cutover runbook §8):

* **Zero reconciliation alerts** — the projection rebuild deleted the
  reconcile-adjudication path (ALP-854 / ADR-0001), so a flat genesis has nothing
  to reconcile and emits no ``RECONCILIATION_ALERT`` / ``RECONCILIATION_CORRECTION``
  activity-log row.
* **Canary self-attribution** — a canary entry fill resolves to its thesis /
  invocation / position via the broker-carried link in ``client_order_id``
  (ALP-844 / 02a) and appends to the append-only ``broker_event_log`` with **no
  local ``orders`` row required** and **no ``unattributed_fills`` strand**.
* **Projection + Intent both reflect the canary** — the projection ``positions``
  row carries the canary quantity; the Intent ``thesis_pnl_ledger`` carries the
  per-thesis cost basis and the ``capital_reservations`` row the reservation
  (03c / 04b).
* **Greeks in the side table** — the canary's greeks land in ``position_greeks``
  (the single-writer side table, ADR-0005 / 04b), never on the ``positions`` row.
* **Options capital floor resting** — the options canary has a durable
  ``STOP_LIMIT`` floor ``OrderRow`` (04c / ADR-0003) keyed by its own
  ``client_order_id``, resting at the broker.
* **No synthetic broker id** — a fired monitor stop submits a fresh
  self-attributing close (engine-originated link) and **no ``alp-…`` placeholder
  exists anywhere** (invariant 5).

The DB is the sole sanctioned mock boundary here (no live account): the canary is
seeded by driving the **production** primitives — :func:`bootstrap_singletons_from_alpaca`
(the real fresh-start), :func:`persist_fill_report` (the real self-attributing
fill consumer), and the real state codecs / ``command_ids`` derivation — so the
assertions exercise the post-wave behaviors rather than re-implementations.

Operator usage is documented in ``scripts/RUNBOOK_genesis_verify.md``.

Exit codes:
- 0 — every genesis assertion holds
- 1 — at least one assertion failed (the failing check is printed)
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from alpaca.trading.enums import (
    AssetClass,
    OrderSide,
    TimeInForce,
)
from alpaca.trading.enums import (
    OrderClass as AlpacaOrderClass,
)
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import (
    OrderType as AlpacaOrderType,
)
from alpaca.trading.models import Order, TradeUpdate
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.money import money
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.broker_adapter.order_options import (
    derive_capital_floor_client_order_id,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.oms.command_ids import (
    derive_engine_command_id,
    derive_open_thesis_id,
    derive_pm_command_id,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.portfolio_state.records.orders import (
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRole,
    OrderStatus,
    OrderType,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.scheduler.fresh_start import bootstrap_singletons_from_alpaca
from alphamind.scripts._common import AssertionFailure
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.records_intent import (
    CapitalReservationRecord,
    ThesisPnlLedgerRecord,
)
from alphamind.state.records_position_greeks import PositionGreeksRecord
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.capital_reservations import CapitalReservationRow
from alphamind.state.tables.capital_reservations_codec import (
    record_to_row as reservation_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.position_greeks_codec import (
    record_to_row as greeks_record_to_row,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from alphamind.state.tables.thesis_pnl_ledger_codec import (
    record_to_row as ledger_record_to_row,
)

# Side-effect import: registers every state-persistence table on
# ``Base.metadata`` so an in-memory engine sees the full genesis schema.
import alphamind.state.tables  # noqa: F401  isort: skip

_GENESIS_CASH = Decimal("100000.00")
_TS = "2026-06-05T14:30:00Z"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Genesis-state seeding — the flat fresh account.
# ---------------------------------------------------------------------------


async def seed_flat_genesis(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cash: Decimal | None = None,
) -> None:
    """Bootstrap the cash/drawdown singletons from a synthetic *flat* account.

    Drives the real :func:`bootstrap_singletons_from_alpaca` (the fresh-start
    path story 06 extends) against a zero-position, zero-open-order snapshot —
    the genuinely-flat genesis state. Nothing else is written, so the activity
    log stays empty (the zero-reconciliation-alert assertion's by-construction
    truth).
    """
    from alphamind.execution.broker_adapter.queries import TradeAccountSnapshot

    cash_value = cash if cash is not None else _GENESIS_CASH
    account = TradeAccountSnapshot(
        account_id="genesis-fresh-account",
        cash=money(cash_value),
        equity=money(cash_value),
        buying_power=money(cash_value),
        regt_buying_power=money(cash_value),
        daytrading_buying_power=money(cash_value),
        maintenance_margin=money(Decimal(0)),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )
    async with session_factory() as session:
        await bootstrap_singletons_from_alpaca(
            session=session,
            account=account,
            positions=(),
            open_orders=(),
            now=_now(),
        )
        await session.commit()


# ---------------------------------------------------------------------------
# The canary — one options position opened at genesis. Its identity is derived
# through the canonical broker-carried-link helpers so the fixture never
# hand-writes the id grammar (mirrors the 02a self-attribution fixtures).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GenesisCanary:
    """The single options position the genesis canary trade opens.

    Identity is derived through :func:`derive_pm_command_id` /
    :func:`derive_open_thesis_id` so the broker-carried link the fill echoes is
    self-attributing by construction. ``quantity`` / ``cost_basis`` are the
    projection + Intent figures the §8 checklist asserts both reflect.
    """

    underlying: str = "AAPL"
    invocation_bare: str = "20260605T143000Z-canary1"
    envelope_id: str = "ENV-SA-1"
    monitor_session_id: str = "mon-20260605T143000Z-canary01"
    quantity: float = 2.0
    strike: float = 200.0
    expiration: date = date(2026, 6, 20)
    premium_per_contract: float = 5.00
    floor_price_per_contract: float = 3.00
    contract_multiplier: float = 100.0

    @property
    def invocation_id(self) -> str:
        return f"inv-{self.invocation_bare}"

    @property
    def base_command_id(self) -> str:
        return f"inv-{self.invocation_bare}.{self.envelope_id}.0.0"

    @property
    def thesis_id(self) -> str:
        return str(derive_open_thesis_id(self.underlying, self.base_command_id))

    @property
    def entry_client_order_id(self) -> str:
        """The entry order's broker-carried ``client_order_id`` (thesis + invocation)."""
        return derive_pm_command_id(
            invocation_id=self.invocation_bare,
            envelope_id=self.envelope_id,
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=derive_open_thesis_id(self.underlying, self.base_command_id),
        )

    @property
    def floor_client_order_id(self) -> str:
        """The capital-floor order's ``client_order_id`` — same link, shifted ordinal."""
        return derive_capital_floor_client_order_id(self.entry_client_order_id)

    @property
    def close_client_order_id(self) -> str:
        """An engine-originated monitor-stop close's ``client_order_id`` (no ``alp-``)."""
        return derive_engine_command_id(
            monitor_session_id=self.monitor_session_id,
            trigger_id=1,
            thesis_id=derive_open_thesis_id(self.underlying, self.base_command_id),
            invocation_id=self.invocation_bare,
        )

    position_id: str = "pos-genesis-canary"
    bracket_id: str = "bracket-genesis-canary"
    entry_order_id: str = "ORD-genesis-canary-entry"
    floor_order_id: str = "ORD-genesis-canary-floor"
    cost_basis: Decimal = Decimal("1000.00")  # 2 contracts * $5 * 100
    reserved_capital: Decimal = Decimal("1000.00")


GENESIS_CANARY = _GenesisCanary()


def _occ_symbol(canary: _GenesisCanary) -> str:
    """Bare OCC symbol Alpaca keys the options position by (no ``O:`` prefix)."""
    expiry = canary.expiration.strftime("%y%m%d")
    strike_milli = round(canary.strike * 1000)
    return f"{canary.underlying}{expiry}C{strike_milli:08d}"


def _canary_position_details_json(canary: _GenesisCanary) -> str:

    return json.dumps(
        {
            "instrument_type": InstrumentType.OPTIONS.value,
            "underlying_ticker": canary.underlying,
            "strike_price": canary.strike,
            "expiration_date": canary.expiration.isoformat(),
            "contract_type": "CALL",
            "contract_count": canary.quantity,
            "contract_multiplier": canary.contract_multiplier,
            "premium_paid_per_contract": canary.premium_per_contract,
            "greeks": {"delta": 0.55, "gamma": 0.02, "theta": -0.10, "vega": 0.15, "iv": 0.32},
        }
    )


def _seed_canary_cluster(session: AsyncSession, canary: _GenesisCanary) -> None:
    """Stage the canary's projection + Intent rows (single deferred-FK transaction).

    The cluster is the projection ``positions`` row (carrying the canary
    quantity), the Intent ``theses`` row, the OPEN ``brackets``, the entry
    ``orders`` row (link as ``client_order_id``, NO synthetic ``alpaca_order_id``),
    and the resting options-floor ``STOP_LIMIT`` ``orders`` row. All reference
    each other cyclically, so they commit together under deferred FKs.
    """
    session.add(
        PositionRow(
            position_id=canary.position_id,
            thesis_id=canary.thesis_id,
            bracket_id=canary.bracket_id,
            status=PositionStatus.OPEN.value,
            direction=Direction.LONG.value,
            entry_timestamp=_TS,
            instrument_type=InstrumentType.OPTIONS.value,
            details_json=_canary_position_details_json(canary),
            execution_history_json="[]",
            realized_pnl_to_date_usd=None,
            corporate_action_adjustment_needed=0,
            parent_position_id=None,
            origin=None,
        )
    )
    session.add(
        ThesisRow(
            thesis_id=canary.thesis_id,
            position_id=canary.position_id,
            status=ThesisRecordStatus.ACTIVE.value,
            resolution_timestamp=None,
            resolution_category=None,
            summary="genesis canary",
            time_expectation_hours=24.0,
            position_size_rationale=None,
            generation_timestamp=_TS,
            narrative_json="{}",
        )
    )
    session.add(
        BracketRow(
            bracket_id=canary.bracket_id,
            position_id=canary.position_id,
            status="ACTIVE",
            entry_order_id=canary.entry_order_id,
            entry_window_deadline=None,
            corporate_action_cancellation_reason=None,
            modification_history_json="[]",
        )
    )
    session.add(_canary_entry_order_row(canary))
    session.add(_canary_floor_order_row(canary))


def _canary_entry_order_row(canary: _GenesisCanary) -> OrderRow:
    """The entry order's projection-cache row — link as ``client_order_id``, NO ``alp-`` id.

    ``alpaca_order_id`` is NULL (the broker-carried link, not a synthetic
    placeholder, is the durable resolution key — invariant 5 / ALP-847).
    """

    return OrderRow(
        order_id=canary.entry_order_id,
        position_id=canary.position_id,
        bracket_id=canary.bracket_id,
        order_role=OrderRole.ENTRY.value,
        order_class=OrderClass.SIMPLE.value,
        instrument_spec_json=json.dumps(
            {"instrument_type": InstrumentType.OPTIONS.value, "underlying": canary.underlying}
        ),
        direction=OrderDirection.BUY.value,
        order_type=OrderType.MARKET.value,
        quantity=canary.quantity,
        price_parameters_json="{}",
        duration=OrderDuration.DAY.value,
        status=OrderStatus.FILLED.value,
        alpaca_order_id=None,
        alpaca_order_id_chain_json="[]",
        submission_timestamp=_TS,
        last_update_timestamp=_TS,
        filled_quantity=canary.quantity,
        average_fill_price=canary.premium_per_contract,
        remaining_quantity=0.0,
        modification_count=0,
        metadata_json="{}",
        client_order_id=canary.entry_client_order_id,
    )


def _canary_floor_order_row(canary: _GenesisCanary) -> OrderRow:
    """The options capital-protection floor's durable resting ``STOP_LIMIT`` OrderRow.

    A long floor SELLs on a decline (a CLOSE), resting GTC at the
    PnL-denominated floor price (04c / ADR-0003). Keyed by its OWN
    ``client_order_id`` (the link with the floor's shifted ordinal); NO synthetic
    ``alp-`` id.
    """

    floor_price_params = json.dumps(
        {
            "limit_price": canary.floor_price_per_contract,
            "stop_trigger_price": canary.floor_price_per_contract,
        }
    )
    return OrderRow(
        order_id=canary.floor_order_id,
        position_id=canary.position_id,
        bracket_id=canary.bracket_id,
        order_role=OrderRole.PRICE_STOP.value,
        order_class=OrderClass.SIMPLE.value,
        instrument_spec_json=json.dumps(
            {"instrument_type": InstrumentType.OPTIONS.value, "underlying": canary.underlying}
        ),
        direction=OrderDirection.SELL.value,
        order_type=OrderType.STOP_LIMIT.value,
        quantity=canary.quantity,
        price_parameters_json=floor_price_params,
        duration=OrderDuration.GTC.value,
        status=OrderStatus.PENDING.value,
        alpaca_order_id=str(uuid4()),
        alpaca_order_id_chain_json="[]",
        submission_timestamp=_TS,
        last_update_timestamp=_TS,
        filled_quantity=0.0,
        average_fill_price=None,
        remaining_quantity=canary.quantity,
        modification_count=0,
        metadata_json="{}",
        client_order_id=canary.floor_client_order_id,
    )


def _canary_entry_fill(canary: _GenesisCanary) -> FillReport:
    """Translate a canary entry trade-update into the FillReport the monitor sees.

    Built through :func:`translate_trade_update` so the fixture exercises the
    real fill-stream translation rather than hand-constructing a FillReport.
    """
    occ = _occ_symbol(canary)
    update = TradeUpdate(
        event="fill",
        order=Order(
            id=uuid4(),
            client_order_id=canary.entry_client_order_id,
            created_at=_now(),
            updated_at=_now(),
            submitted_at=_now(),
            symbol=occ,
            asset_class=AssetClass.US_OPTION,
            order_class=AlpacaOrderClass.SIMPLE,
            order_type=AlpacaOrderType.MARKET,
            type=AlpacaOrderType.MARKET,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            status=AlpacaOrderStatus.FILLED,
            extended_hours=False,
            qty=str(canary.quantity),
            filled_qty=str(canary.quantity),
        ),
        timestamp=_now(),
        price=canary.premium_per_contract,
        qty=canary.quantity,
    )
    (report,) = translate_trade_update(update)
    return report


async def seed_canary(
    session_factory: async_sessionmaker[AsyncSession],
    canary: _GenesisCanary = GENESIS_CANARY,
) -> None:
    """Open the genesis canary end-to-end through the production primitives.

    Seeds the projection + Intent cluster (the FK parents the broker-carried link
    points at), then drives the entry fill through the **real**
    :func:`persist_fill_report` so it self-attributes to the canary's
    thesis / invocation / position with no order-row dependency (02a). Finally
    writes the Intent PnL ledger + reservation and the greeks side-table row via
    the real codecs — the rows a complete canary OPEN produces — so every §8
    assertion has real state to read.
    """
    async with session_factory() as session:
        session.add(_canary_process_lifetime_row())
        await session.flush()
        session.add(_canary_invocation_row(canary))
        _seed_canary_cluster(session, canary)
        # Intent: per-thesis PnL ledger (cost basis) + capital reservation.
        session.add(
            ledger_record_to_row(
                ThesisPnlLedgerRecord(
                    thesis_id=canary.thesis_id,  # type: ignore[arg-type]
                    realized_pnl_usd=money(Decimal(0)),
                    cost_basis_usd=money(canary.cost_basis),
                    provenance_json="{}",
                    derived_from_invocation_id=canary.invocation_id,  # type: ignore[arg-type]
                    updated_at=_now(),
                )
            )
        )
        session.add(
            reservation_record_to_row(
                CapitalReservationRecord(
                    reservation_id=f"res-{canary.position_id}",
                    thesis_id=canary.thesis_id,  # type: ignore[arg-type]
                    reserved_capital_usd=money(canary.reserved_capital),
                    reserved_by_invocation_id=canary.invocation_id,  # type: ignore[arg-type]
                    reserved_at=_now(),
                    released_at=None,
                )
            )
        )
        # Greeks land in the side table, never on the positions row.
        session.add(
            greeks_record_to_row(
                PositionGreeksRecord(
                    position_id=canary.position_id,  # type: ignore[arg-type]
                    delta=0.55,
                    gamma=0.02,
                    theta=-0.10,
                    vega=0.15,
                    iv=0.32,
                    updated_at=_now(),
                )
            )
        )
        await session.commit()

    # Drive the entry fill through the real self-attributing consumer (no order
    # row is consulted — the broker-carried link resolves attribution).
    await persist_fill_report(
        _canary_entry_fill(canary),
        session_factory=session_factory,
        enrichment_callable=None,
    )


def _canary_process_lifetime_row() -> ProcessLifetimeRow:
    return ProcessLifetimeRow(
        process_lifetime_id="plt-genesis-canary",
        process_role="pipeline",
        process_start_at=_TS,
        process_pid=1,
        hostname="genesis-host",
        git_sha="0" * 40,
        git_branch="main",
        git_dirty=0,
        python_version="3.13.0",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="snapshot/path",
        anthropic_sdk_version="0.0.0",
        claude_agent_sdk_version="0.0.0",
        os_release="genesis-os",
    )


def _canary_invocation_row(canary: _GenesisCanary) -> InvocationRow:
    return InvocationRow(
        invocation_id=canary.invocation_id,
        process_lifetime_id="plt-genesis-canary",
        start_at=_TS,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="continuous_monitor",
        trigger_reason="genesis-canary",
        git_sha_at_invocation="0" * 40,
        active_profile="default",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="snapshot/path",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="snapshot/path",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=0,
        snapshot_metadata_json=None,
    )


# ---------------------------------------------------------------------------
# Assertion functions — one per §8 checklist item. Each reads the real tables
# and returns ``None`` on success or an ``AssertionFailure`` on a violation.
# ---------------------------------------------------------------------------


_RECONCILIATION_EVENT_TYPES = (
    EventType.RECONCILIATION_ALERT.value,
    EventType.RECONCILIATION_CORRECTION.value,
)


async def assert_zero_reconciliation_alerts(session: AsyncSession) -> AssertionFailure | None:
    """§8 — a clean genesis emits zero reconciliation alerts.

    The projection rebuild (ALP-854) deleted the reconcile-adjudication path, so
    ``RECONCILIATION_ALERT`` / ``RECONCILIATION_CORRECTION`` activity-log rows are
    unreachable on a flat first run. Any such row means the by-construction
    cleanliness invariant 6 was violated.
    """
    count = (
        await session.execute(
            select(func.count())
            .select_from(ActivityLogRow)
            .where(ActivityLogRow.event_type.in_(_RECONCILIATION_EVENT_TYPES))
        )
    ).scalar_one()
    if count:
        return AssertionFailure(
            code="genesis-reconciliation-alerts-present",
            message=(
                f"expected zero reconciliation alerts on a flat genesis; found {count} "
                "RECONCILIATION_ALERT/RECONCILIATION_CORRECTION activity-log row(s) "
                "— the reconcile-adjudication path should be deleted (ADR-0001)"
            ),
        )
    return None


async def assert_canary_self_attributes(
    session: AsyncSession, canary: _GenesisCanary = GENESIS_CANARY
) -> AssertionFailure | None:
    """§8 — the canary entry fill self-attributes via the broker-carried link.

    The fill resolves to its thesis / invocation / position through the link in
    ``client_order_id`` (02a / invariant 2): exactly one attributed ``FILL`` row
    on the append-only ``broker_event_log`` and **no** ``unattributed_fills``
    strand — proving attribution rode the link with no local order-row hop.
    """
    from alphamind.execution.write_paths.unattributed_fill_persistence import (
        list_unattributed_fills,
    )
    from alphamind.state.records_broker_event_log import BrokerEventType
    from alphamind.state.tables.broker_event_log import BrokerEventLogRow

    fills = (
        (
            await session.execute(
                select(BrokerEventLogRow).where(
                    BrokerEventLogRow.event_type == BrokerEventType.FILL.value
                )
            )
        )
        .scalars()
        .all()
    )
    if not fills:
        return AssertionFailure(
            code="genesis-canary-not-attributed",
            message="the canary entry fill did not append an attributed FILL event-log row",
        )
    entry = next(
        (f for f in fills if f.thesis_id == canary.thesis_id),
        None,
    )
    if entry is None:
        return AssertionFailure(
            code="genesis-canary-wrong-thesis",
            message=(
                f"no FILL event-log row attributed to the canary thesis {canary.thesis_id!r}; "
                f"saw thesis_ids {[f.thesis_id for f in fills]!r}"
            ),
        )
    if entry.invocation_id != canary.invocation_id or entry.position_id != canary.position_id:
        return AssertionFailure(
            code="genesis-canary-link-mismatch",
            message=(
                "the canary FILL row's broker-carried link did not decode to the canary "
                f"invocation/position: got invocation={entry.invocation_id!r} "
                f"position={entry.position_id!r}"
            ),
        )
    strands = await list_unattributed_fills(session)
    if strands:
        return AssertionFailure(
            code="genesis-canary-stranded",
            message=(
                f"a self-attributing canary fill must not strand; found {len(strands)} "
                "unattributed_fills row(s) — attribution did not ride the broker-carried link"
            ),
        )
    return None


async def assert_projection_and_intent_reflect_canary(
    session: AsyncSession, canary: _GenesisCanary = GENESIS_CANARY
) -> AssertionFailure | None:
    """§8 — projection (quantity) AND Intent (thesis, reservation, cost basis) reflect it.

    The projection ``positions`` row carries the canary quantity; the Intent
    ``thesis_pnl_ledger`` carries the per-thesis cost basis and the
    ``capital_reservations`` row the live reservation (03c / 04b). Intent and
    projection are separate stores (ADR-0001) — both must reflect the canary.
    """
    position = await session.get(PositionRow, canary.position_id)
    if position is None or position.thesis_id != canary.thesis_id:
        return AssertionFailure(
            code="genesis-projection-missing",
            message=(
                "the projection positions row for the canary is missing or not linked to "
                f"its thesis {canary.thesis_id!r}"
            ),
        )
    details = json.loads(position.details_json)
    projected_qty = float(details.get("contract_count", 0.0))
    if projected_qty != canary.quantity:
        return AssertionFailure(
            code="genesis-projection-quantity-mismatch",
            message=(
                f"projection quantity {projected_qty} does not reflect the canary "
                f"quantity {canary.quantity}"
            ),
        )

    ledger = await session.get(ThesisPnlLedgerRow, canary.thesis_id)
    if ledger is None:
        return AssertionFailure(
            code="genesis-intent-ledger-missing",
            message=f"no Intent thesis_pnl_ledger row for the canary thesis {canary.thesis_id!r}",
        )
    if Decimal(str(ledger.cost_basis_usd)) != canary.cost_basis:
        return AssertionFailure(
            code="genesis-intent-cost-basis-mismatch",
            message=(
                f"Intent cost basis {ledger.cost_basis_usd} does not reflect the canary "
                f"cost basis {canary.cost_basis}"
            ),
        )

    reservation = (
        await session.execute(
            select(CapitalReservationRow).where(
                CapitalReservationRow.thesis_id == canary.thesis_id,
                CapitalReservationRow.released_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if reservation is None:
        return AssertionFailure(
            code="genesis-intent-reservation-missing",
            message=(
                f"no live Intent capital reservation for the canary thesis {canary.thesis_id!r}"
            ),
        )
    return None


async def assert_greeks_in_side_table(
    session: AsyncSession, canary: _GenesisCanary = GENESIS_CANARY
) -> AssertionFailure | None:
    """§8 — the canary's greeks land in the ``position_greeks`` side table.

    Greeks live in their own single-writer side table keyed by ``position_id``
    (ADR-0005 / 04b), never as columns on the pipeline-owned ``positions`` row.
    """
    greeks = await session.get(PositionGreeksRow, canary.position_id)
    if greeks is None:
        return AssertionFailure(
            code="genesis-greeks-missing",
            message=(
                f"no position_greeks side-table row for the canary position "
                f"{canary.position_id!r} — greeks must land in the side table (ADR-0005)"
            ),
        )
    return None


async def assert_options_floor_resting(
    session: AsyncSession, canary: _GenesisCanary = GENESIS_CANARY
) -> AssertionFailure | None:
    """§8 — the options capital-protection floor ``stop_limit`` rests at the broker.

    The floor is a durable ``STOP_LIMIT`` ``OrderRow`` (04c / ADR-0003): a tracked
    broker order resting GTC against the canary position, keyed by its own
    ``client_order_id`` (the link with the floor's shifted ordinal), carrying a
    real broker id once dispatched — never a synthetic ``alp-`` placeholder.
    """
    floor = (
        await session.execute(
            select(OrderRow).where(
                OrderRow.client_order_id == canary.floor_client_order_id,
            )
        )
    ).scalar_one_or_none()
    if floor is None:
        return AssertionFailure(
            code="genesis-floor-missing",
            message=(
                f"no options capital-floor order resting under client_order_id "
                f"{canary.floor_client_order_id!r}"
            ),
        )
    if floor.order_type != OrderType.STOP_LIMIT.value:
        return AssertionFailure(
            code="genesis-floor-not-stop-limit",
            message=(f"the capital floor must be a STOP_LIMIT; got {floor.order_type!r}"),
        )
    if floor.status not in (OrderStatus.PENDING.value, OrderStatus.PENDING_SUBMIT.value):
        return AssertionFailure(
            code="genesis-floor-not-resting",
            message=(
                f"the capital floor must be resting (PENDING/PENDING_SUBMIT); got "
                f"status {floor.status!r}"
            ),
        )
    return None


async def assert_no_synthetic_broker_ids(
    session: AsyncSession,
) -> AssertionFailure | None:
    """§8 / invariant 5 — no ``alp-…`` synthetic broker id exists on any order.

    The redesign deleted the ``alp-{order_id}`` placeholder: a leg with no broker
    order carries a NULL ``alpaca_order_id``, never a synthetic id. Any order row
    whose ``alpaca_order_id`` is an ``alp-`` placeholder is a regression of
    invariant 5.
    """
    synthetic = (
        (
            await session.execute(
                select(OrderRow.order_id).where(OrderRow.alpaca_order_id.like("alp-%"))
            )
        )
        .scalars()
        .all()
    )
    if synthetic:
        return AssertionFailure(
            code="genesis-synthetic-broker-id-present",
            message=(
                f"found {len(synthetic)} order(s) carrying a synthetic alp- broker id "
                f"({list(synthetic)!r}) — invariant 5 forbids any alp- placeholder"
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Report shell (story-13 verify-script convention).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenesisCheck:
    """One named genesis assertion paired with its outcome."""

    name: str
    failure: AssertionFailure | None

    @property
    def passed(self) -> bool:
        return self.failure is None


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Assert the genesis-cutover runbook §8 checklist on a fresh account."
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="SQLite DB path; omit to run the assertions against an ephemeral in-memory genesis.",
    )
    args = parser.parse_args(argv)
    import asyncio

    passed = asyncio.run(run_genesis_verification(args.db_path))
    return 0 if passed else 1


async def run_genesis_verification(db_path: str | None = None) -> bool:
    """Seed (when ephemeral) and assert the genesis checklist; print a report.

    With ``db_path`` omitted the verifier provisions an ephemeral genesis DB
    (flat bootstrap + the synthetic canary, both driven through the production
    primitives) and asserts against it — the self-contained smoke the operator
    runs to confirm the redesign's machinery is wired before cutover. With
    ``db_path`` supplied it asserts against that DB as-is (no seeding).
    """
    if db_path is None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            ephemeral_db = str(Path(tmp) / "genesis-verify.db")
            sync_engine = make_engine(ephemeral_db)
            Base.metadata.create_all(sync_engine)
            sync_engine.dispose()
            async_engine = make_async_engine(ephemeral_db)
            factory = make_async_session_factory(async_engine)
            try:
                await seed_flat_genesis(factory)
                await seed_canary(factory)
                checks = await _run_checks(factory)
            finally:
                await async_engine.dispose()
    else:
        async_engine = make_async_engine(db_path)
        factory = make_async_session_factory(async_engine)
        try:
            checks = await _run_checks(factory)
        finally:
            await async_engine.dispose()

    return _print_report(checks)


async def _run_checks(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[GenesisCheck]:
    async with session_factory() as session:
        return [
            GenesisCheck(
                "zero reconciliation alerts",
                await assert_zero_reconciliation_alerts(session),
            ),
            GenesisCheck(
                "canary self-attributes via the link",
                await assert_canary_self_attributes(session),
            ),
            GenesisCheck(
                "projection + Intent reflect the canary",
                await assert_projection_and_intent_reflect_canary(session),
            ),
            GenesisCheck(
                "greeks in the side table",
                await assert_greeks_in_side_table(session),
            ),
            GenesisCheck(
                "options capital floor resting",
                await assert_options_floor_resting(session),
            ),
            GenesisCheck(
                "no alp- synthetic broker id",
                await assert_no_synthetic_broker_ids(session),
            ),
        ]


def _print_report(checks: list[GenesisCheck]) -> bool:
    print("=" * 70)
    print("AlphaMind Genesis Verification (genesis-cutover runbook §8)")
    print("=" * 70)
    all_pass = True
    for check in checks:
        status = "OK" if check.passed else "FAIL"
        print(f"  {check.name:<45} {status}")
        if check.failure is not None:
            all_pass = False
            print(f"      {check.failure.code}: {check.failure.message}")
    print("=" * 70)
    print("RESULT: PASS" if all_pass else "RESULT: FAIL")
    print("=" * 70)
    return all_pass


__all__ = [
    "GENESIS_CANARY",
    "GenesisCheck",
    "assert_canary_self_attributes",
    "assert_greeks_in_side_table",
    "assert_no_synthetic_broker_ids",
    "assert_options_floor_resting",
    "assert_projection_and_intent_reflect_canary",
    "assert_zero_reconciliation_alerts",
    "main",
    "run_genesis_verification",
    "seed_canary",
    "seed_flat_genesis",
]
