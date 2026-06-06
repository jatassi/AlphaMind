"""Codec between ``CounterfactualReplayRecord`` and the ``counterfactual_replays`` row dict.

Mirrors the fill_records_codec / brackets_codec pattern:
* ``encode_counterfactual_replay(record) -> dict[str, Any]`` — projects a
  record to a column-keyed dict for INSERT.
* ``decode_counterfactual_replay(row) -> CounterfactualReplayRecord`` — rehydrates
  a column-keyed dict (or ORM row attribute-access) back into the typed record.

Enum fields encode to their ``.value`` (lowercase tokens matching the table
CHECK constraint vocabularies) and decode back to the enum member.

Datetimes serialize via ``.isoformat()`` and deserialize via
``datetime.fromisoformat()`` — consistent with fill_records_codec and
orders_codec.

Round-trip property: ``decode(encode(r)) == r`` for every valid
``CounterfactualReplayRecord`` permutation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind._kernel.money import Money, Price
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
)


def _dt_to_iso(value: datetime | None) -> str | None:
    """Serialize a tz-aware datetime to ISO 8601; pass through None."""
    if value is None:
        return None
    return value.isoformat()


def _dt_from_iso(value: str | None) -> datetime | None:
    """Deserialize an ISO 8601 string back to datetime; pass through None."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def encode_counterfactual_replay(record: CounterfactualReplayRecord) -> dict[str, Any]:
    """Project a ``CounterfactualReplayRecord`` to a column-keyed dict for INSERT."""
    return {
        "replay_id": str(record.replay_id),
        "pm_decision_envelope_id": str(record.pm_decision_envelope_id),
        "replay_kind": record.replay_kind.value,
        "replay_status": record.replay_status.value,
        "unevaluable_reason": (
            record.unevaluable_reason.value if record.unevaluable_reason is not None else None
        ),
        "entered": record.entered,
        "entry_price": record.entry_price,  # DecimalText handles None + Decimal
        "entry_timestamp": _dt_to_iso(record.entry_timestamp),
        "entry_slippage": record.entry_slippage,
        "entry_fees": record.entry_fees,
        "exit_leg": record.exit_leg.value if record.exit_leg is not None else None,
        "exit_price": record.exit_price,
        "exit_timestamp": _dt_to_iso(record.exit_timestamp),
        "exit_slippage": record.exit_slippage,
        "exit_fees": record.exit_fees,
        "realized_pl": record.realized_pl,
        "confidence": record.confidence.value if record.confidence is not None else None,
        "replay_timestamp": _dt_to_iso(record.replay_timestamp),
        "replay_data_window_start": _dt_to_iso(record.replay_data_window_start),
        "replay_data_window_end": _dt_to_iso(record.replay_data_window_end),
        "replay_engine_version": record.replay_engine_version,
    }


def decode_counterfactual_replay(row: Any) -> CounterfactualReplayRecord:
    """Rehydrate a column-keyed dict or ORM row into a ``CounterfactualReplayRecord``.

    Accepts both a plain ``dict`` (from ``encode_counterfactual_replay``) and
    an ORM row (attribute access) so the same function serves both the round-trip
    tests and the live read path.
    """
    get = row.__getitem__ if isinstance(row, dict) else lambda key: getattr(row, key)

    def _get(key: str) -> Any:
        return get(key)

    # Decimal columns come back as ``Decimal`` from DecimalText; rewrap as
    # the typed NewType aliases (Money / Price) at the record boundary.
    def _price(key: str) -> Price | None:
        val = _get(key)
        return Price(Decimal(val)) if val is not None else None

    def _money(key: str) -> Money | None:
        val = _get(key)
        return Money(Decimal(val)) if val is not None else None

    return CounterfactualReplayRecord(
        replay_id=ReplayId(_get("replay_id")),
        pm_decision_envelope_id=EnvelopeId(_get("pm_decision_envelope_id")),
        replay_kind=ReplayKind(_get("replay_kind")),
        replay_status=ReplayStatus(_get("replay_status")),
        unevaluable_reason=(
            UnevaluableReason(_get("unevaluable_reason"))
            if _get("unevaluable_reason") is not None
            else None
        ),
        entered=_get("entered"),
        entry_price=_price("entry_price"),
        entry_timestamp=_dt_from_iso(_get("entry_timestamp")),
        entry_slippage=_money("entry_slippage"),
        entry_fees=_money("entry_fees"),
        exit_leg=(ExitLeg(_get("exit_leg")) if _get("exit_leg") is not None else None),
        exit_price=_price("exit_price"),
        exit_timestamp=_dt_from_iso(_get("exit_timestamp")),
        exit_slippage=_money("exit_slippage"),
        exit_fees=_money("exit_fees"),
        realized_pl=_money("realized_pl"),
        confidence=(Confidence(_get("confidence")) if _get("confidence") is not None else None),
        replay_timestamp=_dt_from_iso(_get("replay_timestamp")),  # type: ignore[arg-type]
        replay_data_window_start=_dt_from_iso(_get("replay_data_window_start")),
        replay_data_window_end=_dt_from_iso(_get("replay_data_window_end")),
        replay_engine_version=_get("replay_engine_version"),
    )


__all__ = ["decode_counterfactual_replay", "encode_counterfactual_replay"]
