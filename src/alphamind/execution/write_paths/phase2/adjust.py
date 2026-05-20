"""ADJUST command writeback (bracket adjustment, stop/target modification).

Also owns the shared replacement-order construction (``_new_*_to_order_shape``
helpers + :func:`_build_replacement_order_for_change_fields`) — ADJUST is the
primary user; :mod:`.add` reuses these for its optional ``bracket_adjustment``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select

from alphamind._kernel.ids import OrderId
from alphamind.commands.command_models import (
    AdjustCommand,
    NewStopLevel,
    NewTargetLevel,
)
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.write_paths.phase2._shared import (
    _OMS_COMPONENT_TYPE_TO_PERSISTED,
    _append_bracket_modification,
    _build_pending_order,
    _cancel_pending_protective_orders,
    _emit,
    _emit_order_cancelled,
    _emit_order_submitted,
    _id_suffix,
    _order_direction_for_close,
    _order_position_direction,
    _position_quantity,
    _position_ticker,
    _price_to_float,
    _protective_roles_for_change_fields,
)
from alphamind.portfolio_state.events.activity_log import (
    BracketModificationSource,
    BracketModifiedDetail,
    EventType,
    ThesisComponentUpdatedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegType,
    OrderClass,
    OrderRecord,
    OrderRole,
    OrderType,
    PLAnchorSpec,
    PriceParameters,
    PriceTrigger,
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import (
    PositionRecord,
    StrategyPositionDetails,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets_codec import (
    row_to_leg,
    update_leg_row,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

_ADJUST_FIELD_LABELS: dict[str, str] = {
    "new_stop_level": "stop_level",
    "new_target_level": "target_level",
    "new_time_expiration": "time_expiration",
    "new_event_invalidation": "event_invalidation",
    "thesis_component_updates": "thesis_components",
}


def _adjust_field_set(command: AdjustCommand) -> str:
    """Return a deterministic label for whichever change-field(s) are set.

    Multi-field adjustments concatenate the labels separated by ``+`` so a
    BracketModifiedDetail.field_changed value still pinpoints what moved.
    """
    set_labels = [
        label for attr, label in _ADJUST_FIELD_LABELS.items() if getattr(command, attr) is not None
    ]
    return "+".join(set_labels) if set_labels else "no_op"


async def _writeback_adjust(
    handle: InvocationHandle,
    *,
    command: AdjustCommand,
    result: SubmissionResult,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """ADJUST: insert new protective leg order(s) and/or thesis component
    updates per the change-field(s) set on the canonical command.

    Dispatches on whichever of ``new_stop_level``, ``new_target_level``,
    ``new_time_expiration``, ``new_event_invalidation``,
    ``thesis_component_updates`` is set. For each price/time/target leg
    change, the existing protective order is CANCELLED and a new PENDING
    replacement is inserted. For thesis_component_updates, no order
    mutations occur — only the activity-log emit captures the change.
    ``BracketModifiedDetail.rationale`` is populated from
    ``command.adjustment_rationale``.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"ADJUST references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    if position.bracket_id is None:
        msg = f"ADJUST references position with no bracket: {command.position_id!r}"
        raise ValueError(msg)

    cancelled_orders = await _cancel_pending_protective_orders(
        handle,
        bracket_id=position.bracket_id,
        timestamp=timestamp,
        target_roles=_protective_roles_for_change_fields(
            new_stop_level=command.new_stop_level,
            new_target_level=command.new_target_level,
            new_time_expiration_present=command.new_time_expiration is not None,
        ),
    )
    for cancelled in cancelled_orders:
        _emit_order_cancelled(
            handle,
            order=cancelled,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            cancel_reason="adjust_command",
            timestamp=timestamp,
        )

    new_protective = _build_replacement_protective_order(
        command=command,
        position=position,
        result=result,
        timestamp=timestamp,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    new_order_id: str
    if new_protective is not None:
        new_order_id = new_protective.order_id
        handle.session.add(order_record_to_row(new_protective))
        _emit_order_submitted(
            handle,
            order=new_protective,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            pm_command_id=result.command_id,
        )
        await _apply_protective_leg_modification(
            handle,
            bracket_id=position.bracket_id,
            new_stop_level=command.new_stop_level,
            new_target_level=command.new_target_level,
            new_time_expiration=command.new_time_expiration,
            position=position,
            replacement_order_id=new_protective.order_id,
        )
    else:
        # Thesis-only adjustments produce no protective replacement; the
        # bracket modification history still records the adjustment intent.
        new_order_id = "<no_order>"

    await _append_bracket_modification(
        handle,
        bracket_id=position.bracket_id,
        old_order_ids=tuple(o.order_id for o in cancelled_orders),
        new_order_id=new_order_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
        rationale=command.adjustment_rationale,
    )
    _emit(
        handle,
        event_type=EventType.BRACKET_MODIFIED,
        order_id=None,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        detail=BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed=_adjust_field_set(command),
            old_value=",".join(o.order_id for o in cancelled_orders) or "<none>",
            new_value=new_order_id,
            rationale=command.adjustment_rationale,
        ),
    )

    if command.thesis_component_updates is not None and position.thesis_id is not None:
        for wc in command.thesis_component_updates:
            component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wc.component_type]
            _emit(
                handle,
                event_type=EventType.THESIS_COMPONENT_UPDATED,
                order_id=None,
                position_id=command.position_id,
                thesis_id=position.thesis_id,
                timestamp=timestamp,
                detail=ThesisComponentUpdatedDetail(
                    component_id=f"{position.thesis_id}-{component_type.value.lower()}",
                    field_changed="narrative",
                    old_value="",
                    new_value=wc.narrative,
                ),
            )


# ---------------------------------------------------------------------------
# Replacement-order construction (shared with .add's bracket_adjustment)
# ---------------------------------------------------------------------------


def _adjust_replacement_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADJUST-{position_id}-{_id_suffix(command_id)}"


def _build_replacement_protective_order(
    *,
    command: AdjustCommand,
    position: PositionRecord,
    result: SubmissionResult,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord | None:
    """Build the replacement protective order for an ADJUST.

    Returns ``None`` for thesis-only or event-only adjustments (no broker
    order changes — event invalidation is advisory). Stop / target / time
    changes each map to a single replacement order. When multiple
    change-fields are set on one command, the precedence is
    stop -> target -> time so the resulting order is deterministic.
    """
    new_order_id = _adjust_replacement_order_id(command.position_id, result.command_id)
    return _build_replacement_order_for_change_fields(
        new_order_id=new_order_id,
        new_stop_level=command.new_stop_level,
        new_target_level=command.new_target_level,
        new_time_expiration_present=command.new_time_expiration is not None,
        position=position,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _new_stop_level_to_order_shape(
    stop: NewStopLevel,
) -> tuple[OrderRole, OrderType, PriceParameters]:
    if stop.order_type == "limit":
        return (
            OrderRole.PRICE_STOP,
            OrderType.LIMIT,
            PriceParameters(limit_price=_price_to_float(stop.limit_price)),
        )
    if stop.order_type == "stop_limit":
        return (
            OrderRole.PRICE_STOP,
            OrderType.STOP_LIMIT,
            PriceParameters(
                limit_price=_price_to_float(stop.limit_price),
                stop_trigger_price=_price_to_float(stop.trigger_price),
            ),
        )
    return (
        OrderRole.PRICE_STOP,
        OrderType.STOP,
        PriceParameters(stop_trigger_price=_price_to_float(stop.trigger_price)),
    )


def _new_target_level_to_order_shape(
    tgt: NewTargetLevel,
) -> tuple[OrderRole, OrderType, PriceParameters]:
    if tgt.order_type == "market":
        return OrderRole.TAKE_PROFIT, OrderType.MARKET, PriceParameters()
    return (
        OrderRole.TAKE_PROFIT,
        OrderType.LIMIT,
        PriceParameters(limit_price=_price_to_float(tgt.price)),
    )


_ChangeKind = Literal["stop", "target", "time"]

_CHANGE_KIND_TO_LEG_TYPE: dict[_ChangeKind, BracketLegType] = {
    "stop": BracketLegType.PRICE_STOP,
    "target": BracketLegType.TAKE_PROFIT,
    "time": BracketLegType.TIME_EXPIRATION,
}


def _effective_change_kind(
    *,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
) -> _ChangeKind | None:
    """The single protective change-field a replacement order + leg derive from.

    Precedence is stop -> target -> time: a multi-field ADJUST yields exactly
    one replacement order and one rebuilt leg, so the two always describe the
    same protective leg. ``None`` means no protective change-field is set
    (event-only or thesis-only paths produce no broker order or leg change).
    """
    if new_stop_level is not None:
        return "stop"
    if new_target_level is not None:
        return "target"
    if new_time_expiration_present:
        return "time"
    return None


def _validate_strategy_target_level(
    position: PositionRecord, new_target_level: NewTargetLevel
) -> None:
    """Reject a non-``pl_percentage`` replacement take-profit on a strategy position.

    A multi-leg strategy take-profit references the strategy's net P/L as a
    fraction of max profit (parent ALP-588 decision F; ALP-601), so
    ``pl_percentage`` is the only coherent target — and the strategy leg's
    ``PLAnchorSpec`` needs both ``pl_percentage`` and ``price``. ``OpenCommand``
    rejects the other target types with a model validator (ALP-611), but
    ``AdjustCommand`` / ``AddCommand`` carry no instrument — they reference a
    position by id — so the strategy-ness is read from the persisted
    ``PositionRecord`` here (ALP-613).
    """
    if not isinstance(position.details, StrategyPositionDetails):
        return
    if (
        new_target_level.target_type != "pl_percentage"
        or new_target_level.pl_percentage is None
        or new_target_level.price is None
    ):
        msg = (
            "ADJUST/ADD replacement take-profit for a strategy position requires "
            "target_type='pl_percentage' with pl_percentage and price set "
            "(a strategy take-profit references net P/L as a fraction of max "
            f"profit); got target_type={new_target_level.target_type!r}"
        )
        raise ValueError(msg)


def _build_replacement_order_for_change_fields(
    *,
    new_order_id: str,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
    position: PositionRecord,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord | None:
    """Shared replacement-order construction used by ADJUST and ADD's
    optional ``bracket_adjustment``.

    Returns ``None`` when no protective change-field is set (event-only or
    thesis-only paths produce no broker order). A strategy-instrument target
    is validated to ``pl_percentage`` here so the rejection covers both the
    ADJUST and the ADD path (ALP-613).
    """
    if new_target_level is not None:
        _validate_strategy_target_level(position, new_target_level)
    kind = _effective_change_kind(
        new_stop_level=new_stop_level,
        new_target_level=new_target_level,
        new_time_expiration_present=new_time_expiration_present,
    )
    if kind is None:
        return None
    bracket_id = position.bracket_id or ""
    common: dict[str, Any] = {
        "order_id": new_order_id,
        "position_id": position.position_id,
        "bracket_id": bracket_id,
        "order_class": OrderClass.OTO,
        "direction": _order_direction_for_close(_order_position_direction(position)),
        "quantity": _position_quantity(position),
        "ticker": _position_ticker(position),
        "pm_command_id": pm_command_id,
        "thesis_id": position.thesis_id,
        "timestamp": timestamp,
        "alpaca_order_id_override": alpaca_order_id_override,
    }
    if kind == "stop":
        assert new_stop_level is not None
        role, order_type, price_parameters = _new_stop_level_to_order_shape(new_stop_level)
    elif kind == "target":
        assert new_target_level is not None
        role, order_type, price_parameters = _new_target_level_to_order_shape(new_target_level)
    else:
        # new_time_expiration set: time-stop becomes a market order at deadline.
        role, order_type, price_parameters = (
            OrderRole.TIME_STOP,
            OrderType.MARKET,
            PriceParameters(),
        )
    return _build_pending_order(
        role=role,
        order_type=order_type,
        price_parameters=price_parameters,
        **common,
    )


# ---------------------------------------------------------------------------
# Protective-leg re-persistence (shared with .add's bracket_adjustment)
# ---------------------------------------------------------------------------


async def _apply_protective_leg_modification(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration: datetime | None,
    position: PositionRecord,
    replacement_order_id: str,
) -> None:
    """Re-persist the bracket leg an ADJUST / ADD change-field modifies.

    The replacement-order path cancels the old protective order and inserts a
    new one, but a bracket's ``protective_legs`` are written once at OPEN and
    were never updated afterwards — so the continuous-monitor watcher kept
    firing on the stale OPEN-time trigger / ``pl_anchor`` regardless of how
    many times the protective order moved (ALP-613). This rebuilds the single
    leg the replacement order targets — by the same stop -> target -> time
    precedence as :func:`_build_replacement_order_for_change_fields` — and
    overwrites its persisted row in place.
    """
    kind = _effective_change_kind(
        new_stop_level=new_stop_level,
        new_target_level=new_target_level,
        new_time_expiration_present=new_time_expiration is not None,
    )
    if kind is None:
        return
    leg_type = _CHANGE_KIND_TO_LEG_TYPE[kind]
    rows = list(
        (
            await handle.session.execute(
                select(BracketLegRow)
                .where(BracketLegRow.bracket_id == bracket_id)
                .where(BracketLegRow.leg_type == leg_type.value)
            )
        ).scalars()
    )
    if len(rows) != 1:
        msg = (
            f"ADJUST/ADD {kind} change requires exactly one {leg_type.value} leg on "
            f"bracket {bracket_id!r} to re-persist; found {len(rows)}"
        )
        raise ValueError(msg)
    row = rows[0]
    new_leg = _rebuild_protective_leg(
        kind=kind,
        old_leg=row_to_leg(row),
        new_stop_level=new_stop_level,
        new_target_level=new_target_level,
        new_time_expiration=new_time_expiration,
        position=position,
        replacement_order_id=replacement_order_id,
    )
    update_leg_row(row, new_leg)


def _rebuild_protective_leg(
    *,
    kind: _ChangeKind,
    old_leg: BracketLeg,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration: datetime | None,
    position: PositionRecord,
    replacement_order_id: str,
) -> BracketLeg:
    """Return *old_leg* with its trigger / ``pl_anchor`` / ``order_id`` updated.

    The modification counterpart of the OPEN-path leg builders
    (``_target_to_bracket_leg`` / ``_strategy_target_to_bracket_leg`` /
    ``_wire_leg_to_bracket_leg``). Identity (``leg_id``, ``leg_type``),
    enforcement and status carry through unchanged — an ADJUST moves a leg's
    level, not its role. The ``PriceTrigger`` LTE/GTE side is preserved from
    *old_leg*: a stop's side and a take-profit's (inert-for-strategy) GTE are
    fixed at OPEN; an ADJUST only moves the threshold.

    A strategy take-profit is rebuilt P/L-anchored — a ``PLAnchorSpec`` whose
    ``pct`` is the strategy profit fraction the watcher scores via
    ``evaluate_strategy_pl_target_trigger``. Equity / single-option
    take-profits and every stop / time leg keep a plain underlying-price or
    time trigger, mirroring the OPEN-path dispatch (ALP-613).
    """
    trigger: PriceTrigger | TimeTrigger
    pl_anchor: PLAnchorSpec | None = None
    if kind == "stop":
        assert new_stop_level is not None
        assert isinstance(old_leg.trigger, PriceTrigger)
        trigger = PriceTrigger(
            underlying_ticker=old_leg.trigger.underlying_ticker,
            threshold_usd=float(new_stop_level.trigger_price),
            direction=old_leg.trigger.direction,
        )
    elif kind == "target":
        assert new_target_level is not None
        assert isinstance(old_leg.trigger, PriceTrigger)
        trigger, pl_anchor = _rebuild_target_trigger(
            old_trigger=old_leg.trigger,
            new_target_level=new_target_level,
            position=position,
        )
    else:
        assert new_time_expiration is not None
        trigger = TimeTrigger(deadline=new_time_expiration)
    return BracketLeg(
        leg_id=old_leg.leg_id,
        leg_type=old_leg.leg_type,
        order_id=OrderId(replacement_order_id),
        trigger=trigger,
        enforcement=old_leg.enforcement,
        status=old_leg.status,
        pl_anchor=pl_anchor,
    )


def _rebuild_target_trigger(
    *,
    old_trigger: PriceTrigger,
    new_target_level: NewTargetLevel,
    position: PositionRecord,
) -> tuple[PriceTrigger, PLAnchorSpec | None]:
    """Build the replacement TAKE_PROFIT trigger (+ optional ``pl_anchor``).

    A strategy position's take-profit is P/L-anchored — the counterpart of
    ``_strategy_target_to_bracket_leg``: the leg carries a ``PLAnchorSpec``
    (``spec_type='target'``) whose ``pct`` is the whole-number
    ``pl_percentage`` divided to a fraction, and the structurally-required
    ``PriceTrigger`` keeps its inert OPEN-time direction. Equity / single-leg
    options keep a plain underlying-price trigger and no anchor, matching the
    OPEN-path ``_target_to_bracket_leg`` (which ignores ``target_type``).
    ``_validate_strategy_target_level`` guarantees a strategy target carries
    ``pl_percentage`` and ``price``.
    """
    if isinstance(position.details, StrategyPositionDetails):
        assert new_target_level.pl_percentage is not None
        assert new_target_level.price is not None
        planned_price = float(new_target_level.price)
        trigger = PriceTrigger(
            underlying_ticker=old_trigger.underlying_ticker,
            threshold_usd=planned_price,
            direction=old_trigger.direction,
        )
        anchor = PLAnchorSpec(
            spec_type="target",
            pct=new_target_level.pl_percentage / 100.0,
            planned_entry_price=planned_price,
        )
        return trigger, anchor
    # Equity / single-option take-profit: a market replacement target carries
    # no price, so fall back to the same 0.01 floor as _target_to_bracket_leg
    # to satisfy the PriceTrigger threshold_usd > 0 invariant.
    threshold = float(new_target_level.price) if new_target_level.price is not None else 0.01
    trigger = PriceTrigger(
        underlying_ticker=old_trigger.underlying_ticker,
        threshold_usd=threshold,
        direction=old_trigger.direction,
    )
    return trigger, None
