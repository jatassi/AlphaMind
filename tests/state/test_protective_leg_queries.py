"""Unit tests for the shared protective-leg resolver (ALP-939).

``equity_broker_enforced_protective_leg_ids`` returns only the PENDING,
broker-enforced protective legs that carry a real broker id; ``leg_enforcement_binding``
reads a leg's typed ``enforcement_binding`` (defaulting to ``BROKER_ENFORCED`` for
an order with no ``bracket_legs`` row). Both are behavior-identical to the
decision-side privates ALP-937 extracted them from.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    EnforcementBinding,
    OrderRole,
    OrderStatus,
)
from alphamind.state.protective_leg_queries import (
    equity_broker_enforced_protective_leg_ids,
    leg_enforcement_binding,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from tests.state._fk_substrate import (
    stub_bracket_row,
    stub_order_row,
    stub_position_row,
)

_BRACKET_ID = "BRK-MRVL-1"
_POSITION_ID = "POS-MRVL-001"
_ENTRY_ORDER_ID = "BRK-MRVL-1-ord-entry"


class _LegSpec(NamedTuple):
    """One protective ``orders`` row + (optionally) its ``bracket_legs`` row."""

    order_id: str
    role: str
    alpaca_order_id: str | None
    binding: EnforcementBinding | None  # None → no bracket_legs row for this order
    status: str = OrderStatus.PENDING.value


def _leg_row(leg_index: int, order_id: str, binding: EnforcementBinding) -> BracketLegRow:
    return BracketLegRow(
        bracket_leg_id=f"{_BRACKET_ID}-leg-{leg_index}",
        bracket_id=_BRACKET_ID,
        leg_index=leg_index,
        leg_type=BracketLegType.PRICE_STOP.value,
        order_id=order_id,
        trigger_kind="PRICE",
        trigger_payload_json="{}",
        pl_anchor_json=None,
        enforcement=BracketLegEnforcement.MECHANICAL.value,
        enforcement_binding=binding.value,
        leg_status=BracketLegStatus.ACTIVE.value,
        trigger_signal=None,
    )


def _seed(db_path: Path, specs: list[_LegSpec]) -> None:
    """Seed a position + bracket + entry order + the given protective legs.

    All FKs are deferred, so the position / bracket / order cycle lands in a
    single transaction. ``alpaca_order_id`` is set verbatim (including ``None``)
    so the no-broker-id exclusion can be exercised.
    """
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_position_row(_POSITION_ID, bracket_id=_BRACKET_ID))
        sess.add(stub_order_row(_ENTRY_ORDER_ID, _BRACKET_ID, position_id=_POSITION_ID))
        sess.add(stub_bracket_row(_BRACKET_ID, _POSITION_ID, _ENTRY_ORDER_ID))
        for spec in specs:
            row = stub_order_row(
                spec.order_id,
                _BRACKET_ID,
                position_id=_POSITION_ID,
                role=spec.role,
                status=spec.status,
            )
            row.alpaca_order_id = spec.alpaca_order_id
            row.alpaca_order_id_chain_json = (
                "[]" if spec.alpaca_order_id is None else f'["{spec.alpaca_order_id}"]'
            )
            sess.add(row)
        sess.flush()
        for leg_index, spec in enumerate(specs):
            if spec.binding is not None:
                sess.add(_leg_row(leg_index, spec.order_id, spec.binding))
        sess.commit()
    sync_engine.dispose()


async def _resolve_leg_ids(
    db_path: Path, specs: list[_LegSpec], *, bracket_id: str | None = _BRACKET_ID
) -> tuple[str, ...]:
    _seed(db_path, specs)
    async_engine = make_async_engine(str(db_path))
    try:
        factory = make_async_session_factory(async_engine)
        async with factory() as session:
            return await equity_broker_enforced_protective_leg_ids(session, bracket_id=bracket_id)
    finally:
        await async_engine.dispose()


async def _resolve_binding(
    db_path: Path, specs: list[_LegSpec], *, order_id: str
) -> EnforcementBinding:
    _seed(db_path, specs)
    async_engine = make_async_engine(str(db_path))
    try:
        factory = make_async_session_factory(async_engine)
        async with factory() as session:
            return await leg_enforcement_binding(session, order_id=order_id)
    finally:
        await async_engine.dispose()


async def test_returns_only_pending_broker_enforced_legs_with_ids(tmp_path: Path) -> None:
    """The resolver returns exactly the broker ids of PENDING, BROKER_ENFORCED
    protective legs carrying a real broker id — excluding, in one matrix: a
    monitor-enforced leg (binding), a broker-enforced leg with no id (id-nullity),
    a non-protective ENTRY order (role), and a FILLED protective leg (status)."""
    specs = [
        _LegSpec(
            "ord-tp", OrderRole.TAKE_PROFIT.value, "alp-tp-1", EnforcementBinding.BROKER_ENFORCED
        ),
        _LegSpec(
            "ord-stop", OrderRole.PRICE_STOP.value, "alp-stop-1", EnforcementBinding.BROKER_ENFORCED
        ),
        # monitor-enforced — excluded by binding even though it carries a real id.
        _LegSpec(
            "ord-mon", OrderRole.TIME_STOP.value, "alp-mon-1", EnforcementBinding.MONITOR_ENFORCED
        ),
        # broker-enforced but no broker id (un-acked) — excluded by id-nullity.
        _LegSpec("ord-noid", OrderRole.PRICE_STOP.value, None, EnforcementBinding.BROKER_ENFORCED),
        # non-protective ENTRY order — excluded by role.
        _LegSpec("ord-entry2", OrderRole.ENTRY.value, "alp-entry-1", None),
        # broker-enforced protective leg already FILLED — excluded by status.
        _LegSpec(
            "ord-filled",
            OrderRole.PRICE_STOP.value,
            "alp-filled-1",
            EnforcementBinding.BROKER_ENFORCED,
            status=OrderStatus.FILLED.value,
        ),
    ]
    ids = await _resolve_leg_ids(tmp_path / "matrix.db", specs)
    assert set(ids) == {"alp-tp-1", "alp-stop-1"}


async def test_absent_bracket_returns_empty_tuple(tmp_path: Path) -> None:
    """A position with no bracket (``bracket_id`` None) yields an empty tuple."""
    ids = await _resolve_leg_ids(tmp_path / "no_bracket.db", [], bracket_id=None)
    assert ids == ()


async def test_leg_enforcement_binding_reads_present_row(tmp_path: Path) -> None:
    """``leg_enforcement_binding`` returns the leg's persisted binding."""
    specs = [
        _LegSpec(
            "ord-mon", OrderRole.PRICE_STOP.value, "alp-mon-1", EnforcementBinding.MONITOR_ENFORCED
        )
    ]
    binding = await _resolve_binding(tmp_path / "binding_present.db", specs, order_id="ord-mon")
    assert binding is EnforcementBinding.MONITOR_ENFORCED


async def test_leg_enforcement_binding_absent_row_defaults_broker(tmp_path: Path) -> None:
    """An order with no ``bracket_legs`` row defaults to ``BROKER_ENFORCED``."""
    binding = await _resolve_binding(tmp_path / "binding_absent.db", [], order_id="ord-absent")
    assert binding is EnforcementBinding.BROKER_ENFORCED
