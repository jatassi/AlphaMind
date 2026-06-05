"""Round-trip codec between ``BracketRecord`` and ``brackets`` /
``bracket_legs`` SQL rows (story 04d / ALP-361).

The typed frozen-dataclass records are the source of truth; this module is the
only place that knows the row shape. Callers operate on the records and let
the codec materialize / hydrate rows.

The discriminated trigger union (``PriceTrigger | TimeTrigger | EventTrigger``)
and the optional ``PLAnchorSpec`` round-trip via hand-written JSON helpers so
the JSON payloads remain validated against the same vocabularies the typed
records enforce.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from alphamind._kernel.ids import BracketId, CommandId, OrderId, PositionId, make_symbol
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegModification,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EnforcementBinding,
    EventTrigger,
    PLAnchorSpec,
    PriceTrigger,
    TimeTrigger,
    TriggerPayload,
    TriggerSignal,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow

log = logging.getLogger(__name__)


def _trigger_to_dict(trigger: TriggerPayload) -> dict[str, Any]:
    if isinstance(trigger, PriceTrigger):
        return {
            "trigger_type": trigger.trigger_type,
            "underlying_ticker": trigger.underlying_ticker,
            "threshold_usd": trigger.threshold_usd,
            "direction": trigger.direction,
        }
    if isinstance(trigger, TimeTrigger):
        return {
            "trigger_type": trigger.trigger_type,
            "deadline": trigger.deadline.isoformat(),
        }
    # EventTrigger
    return {
        "trigger_type": trigger.trigger_type,
        "description": trigger.description,
        "condition_evaluator_id": trigger.condition_evaluator_id,
    }


def _trigger_from_dict(payload: dict[str, Any]) -> TriggerPayload:
    kind = payload["trigger_type"]
    if kind == "price":
        return PriceTrigger(
            underlying_ticker=make_symbol(payload["underlying_ticker"]),
            threshold_usd=payload["threshold_usd"],
            direction=payload["direction"],
        )
    if kind == "time":
        return TimeTrigger(deadline=datetime.fromisoformat(payload["deadline"]))
    if kind == "event":
        return EventTrigger(
            description=payload["description"],
            condition_evaluator_id=payload.get("condition_evaluator_id"),
        )
    msg = f"unknown trigger_type discriminator: {kind!r}"
    raise ValueError(msg)


def _pl_anchor_to_dict(anchor: PLAnchorSpec) -> dict[str, Any]:
    return {
        "spec_type": anchor.spec_type,
        "pct": anchor.pct,
        "planned_entry_price": anchor.planned_entry_price,
        "actual_entry_price": anchor.actual_entry_price,
        "recalculated_at_fill": anchor.recalculated_at_fill,
    }


def _pl_anchor_from_dict(payload: dict[str, Any]) -> PLAnchorSpec:
    return PLAnchorSpec(
        spec_type=payload["spec_type"],
        pct=payload["pct"],
        planned_entry_price=payload["planned_entry_price"],
        actual_entry_price=payload.get("actual_entry_price"),
        recalculated_at_fill=payload.get("recalculated_at_fill", False),
    )


def _modification_to_dict(mod: BracketLegModification) -> dict[str, Any]:
    return {
        "timestamp": mod.timestamp.isoformat(),
        "pm_command_id": mod.pm_command_id,
        "source": mod.source,
        "field_changed": mod.field_changed,
        "old_value": mod.old_value,
        "new_value": mod.new_value,
        "rationale": mod.rationale,
    }


def _modification_from_dict(payload: dict[str, Any]) -> BracketLegModification:
    raw_id = payload.get("pm_command_id")
    return BracketLegModification(
        timestamp=datetime.fromisoformat(payload["timestamp"]),
        pm_command_id=CommandId(raw_id) if raw_id is not None else None,
        source=payload["source"],
        field_changed=payload["field_changed"],
        old_value=payload["old_value"],
        new_value=payload["new_value"],
        rationale=payload["rationale"],
    )


def record_to_rows(record: BracketRecord) -> tuple[BracketRow, tuple[BracketLegRow, ...]]:
    """Decompose a typed ``BracketRecord`` into its parent + leg rows.

    Leg ordering is preserved via ``leg_index`` matching the position
    within ``record.protective_legs``.
    """
    bracket_row = BracketRow(
        bracket_id=record.bracket_id,
        position_id=record.position_id,
        status=record.status.value,
        entry_order_id=record.entry_order_id,
        entry_window_deadline=(
            record.entry_window_deadline.isoformat()
            if record.entry_window_deadline is not None
            else None
        ),
        corporate_action_cancellation_reason=record.corporate_action_cancellation_reason,
        modification_history_json=json.dumps(
            [_modification_to_dict(m) for m in record.modification_history]
        ),
    )
    leg_rows = tuple(
        _leg_to_row(leg, bracket_id=record.bracket_id, leg_index=idx)
        for idx, leg in enumerate(record.protective_legs)
    )
    return bracket_row, leg_rows


def rows_to_record(bracket_row: BracketRow, leg_rows: tuple[BracketLegRow, ...]) -> BracketRecord:
    """Reconstruct a typed ``BracketRecord`` from its parent + leg rows.

    ``leg_rows`` must arrive sorted by ``leg_index`` ascending; the codec
    does not re-sort because callers normally read with an
    ``ORDER BY leg_index`` clause.
    """
    legs = tuple(row_to_leg(row) for row in leg_rows)
    history = tuple(
        _modification_from_dict(m) for m in json.loads(bracket_row.modification_history_json)
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_row.bracket_id),
        position_id=PositionId(bracket_row.position_id),
        status=BracketStatus(bracket_row.status),
        entry_order_id=OrderId(bracket_row.entry_order_id),
        protective_legs=legs,
        modification_history=history,
        corporate_action_cancellation_reason=bracket_row.corporate_action_cancellation_reason,
        entry_window_deadline=_parse_optional_datetime(bracket_row.entry_window_deadline),
    )


def rows_to_records_isolated(
    bracket_rows: Iterable[BracketRow],
    legs_by_bracket: Mapping[str, Iterable[BracketLegRow]],
) -> tuple[BracketRecord, ...]:
    """Reconstruct each bracket independently, skipping any unreadable one (ALP-732).

    The continuous-monitor risk loops (breach loop, bracket-stop watcher) load
    every position's brackets in one batch each tick. Before this guard a single
    unreadable bracket — the ALP-731 corruption, a ``DISSOLVED`` bracket whose
    legs are not all ``CANCELLED`` (``BracketRecord.__post_init__`` raises
    ``ValueError``), or an out-of-vocabulary status / malformed
    ``modification_history`` JSON (also ``ValueError``) — failed the whole batch
    and aborted the entire tick, leaving every position unmonitored.

    Only ``ValueError`` (the data-corruption signature) is isolated: the bad
    bracket is skipped and surfaced at ``ERROR`` (naming bracket + position id,
    with the traceback) so the operator sees *which* row is corrupt, while every
    readable bracket still loads. Any other exception (e.g. a future codec bug
    raising ``AttributeError``) propagates so it fails loudly rather than
    silently dropping brackets and masquerading as a healthy tick.
    """
    readable: list[BracketRecord] = []
    for bracket_row in bracket_rows:
        try:
            readable.append(
                rows_to_record(bracket_row, tuple(legs_by_bracket[bracket_row.bracket_id]))
            )
        except ValueError:
            log.exception(
                "skipping unreadable bracket bracket_id=%s position_id=%s; excluded from "
                "snapshot until the underlying row is repaired",
                bracket_row.bracket_id,
                bracket_row.position_id,
            )
    return tuple(readable)


def _leg_column_values(leg: BracketLeg) -> dict[str, Any]:
    """The non-identity ``bracket_legs`` column values for *leg*.

    Shared by :func:`_leg_to_row` (fresh insert) and :func:`update_leg_row`
    (in-place modification) so the leg-row serialization lives in one place.
    """
    return {
        "leg_type": leg.leg_type.value,
        "order_id": leg.order_id,
        "trigger_kind": leg.trigger.trigger_type.upper(),
        "trigger_payload_json": json.dumps(_trigger_to_dict(leg.trigger)),
        "pl_anchor_json": (
            json.dumps(_pl_anchor_to_dict(leg.pl_anchor)) if leg.pl_anchor is not None else None
        ),
        "enforcement": leg.enforcement.value,
        "enforcement_binding": leg.enforcement_binding.value,
        "leg_status": leg.status.value,
        "trigger_signal": (None if leg.trigger_signal is None else leg.trigger_signal.value),
    }


def _leg_to_row(leg: BracketLeg, *, bracket_id: str, leg_index: int) -> BracketLegRow:
    return BracketLegRow(
        bracket_leg_id=leg.leg_id,
        bracket_id=bracket_id,
        leg_index=leg_index,
        **_leg_column_values(leg),
    )


def row_to_leg(row: BracketLegRow) -> BracketLeg:
    """Hydrate a single persisted leg row to its typed ``BracketLeg`` record."""
    pl_anchor = (
        _pl_anchor_from_dict(json.loads(row.pl_anchor_json))
        if row.pl_anchor_json is not None
        else None
    )
    return BracketLeg(
        leg_id=row.bracket_leg_id,
        leg_type=BracketLegType(row.leg_type),
        order_id=OrderId(row.order_id) if row.order_id is not None else None,
        trigger=_trigger_from_dict(json.loads(row.trigger_payload_json)),
        enforcement=BracketLegEnforcement(row.enforcement),
        enforcement_binding=EnforcementBinding(row.enforcement_binding),
        status=BracketLegStatus(row.leg_status),
        pl_anchor=pl_anchor,
        trigger_signal=(None if row.trigger_signal is None else TriggerSignal(row.trigger_signal)),
    )


def update_leg_row(row: BracketLegRow, leg: BracketLeg) -> None:
    """Overwrite *row*'s non-identity columns in place to reflect *leg*.

    The identity columns — ``bracket_leg_id``, ``bracket_id``, ``leg_index`` —
    are left untouched: the caller is replacing the leg the row already holds,
    not relocating it. Used by the ADJUST / ADD write paths to re-persist a
    modified protective leg so the continuous-monitor watcher evaluates the
    new trigger / ``pl_anchor`` rather than the stale OPEN-time one (ALP-613).
    """
    for column, value in _leg_column_values(leg).items():
        setattr(row, column, value)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
