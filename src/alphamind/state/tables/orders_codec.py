"""Round-trip codec between ``OrderRecord`` and ``OrderRow`` (story 04c / ALP-360).

The typed frozen-dataclass ``OrderRecord`` is the authoritative shape; the SQL
row mirrors its non-null commitments. ``record_to_row`` projects a record to
its row form for INSERT/UPDATE; ``row_to_record`` rehydrates a row back into
the typed model for the read path.

The discriminated ``InstrumentSpec`` (equity / options / strategy) and the
Alpaca order-ID chain serialise via hand-written JSON helpers so every
variant / ordering round-trips without information loss.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    CommandId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import decimal_json_default, price
from alphamind.portfolio_state.records.orders import (
    EquityInstrumentSpec,
    InstrumentSpec,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    StrategyInstrumentSpec,
)
from alphamind.portfolio_state.records.positions import InstrumentType, OptionContractType
from alphamind.state.tables.orders import OrderRow


def _options_spec_to_dict(spec: OptionsInstrumentSpec) -> dict[str, Any]:
    return {
        "instrument_type": spec.instrument_type.value,
        "underlying": spec.underlying,
        "strike": spec.strike,
        "expiration": spec.expiration.isoformat(),
        "contract_type": spec.contract_type.value,
        "contract_multiplier": spec.contract_multiplier,
    }


def _options_spec_from_dict(payload: dict[str, Any]) -> OptionsInstrumentSpec:
    return OptionsInstrumentSpec(
        underlying=Symbol(payload["underlying"]),
        strike=payload["strike"],
        expiration=date.fromisoformat(payload["expiration"]),
        contract_type=OptionContractType(payload["contract_type"]),
        contract_multiplier=payload["contract_multiplier"],
    )


def _instrument_spec_to_dict(spec: InstrumentSpec) -> dict[str, Any]:
    if isinstance(spec, EquityInstrumentSpec):
        return {"instrument_type": spec.instrument_type.value, "ticker": spec.ticker}
    if isinstance(spec, OptionsInstrumentSpec):
        return _options_spec_to_dict(spec)
    # StrategyInstrumentSpec
    return {
        "instrument_type": spec.instrument_type.value,
        "legs": [_options_spec_to_dict(leg) for leg in spec.legs],
    }


def _instrument_spec_from_dict(payload: dict[str, Any]) -> InstrumentSpec:
    kind = payload["instrument_type"]
    if kind == InstrumentType.EQUITY.value:
        return EquityInstrumentSpec(ticker=Symbol(payload["ticker"]))
    if kind == InstrumentType.OPTIONS.value:
        return _options_spec_from_dict(payload)
    if kind == InstrumentType.STRATEGY.value:
        return StrategyInstrumentSpec(
            legs=tuple(_options_spec_from_dict(leg) for leg in payload["legs"])
        )
    msg = f"unknown instrument_type discriminator: {kind!r}"
    raise ValueError(msg)


def _price_parameters_to_json(pp: PriceParameters) -> str:
    return json.dumps(
        {"limit_price": pp.limit_price, "stop_trigger_price": pp.stop_trigger_price},
        default=decimal_json_default,
    )


def _price_parameters_from_json(payload: str) -> PriceParameters:
    raw = json.loads(payload)
    limit_raw = raw.get("limit_price")
    stop_raw = raw.get("stop_trigger_price")
    return PriceParameters(
        limit_price=price(limit_raw) if limit_raw is not None else None,
        stop_trigger_price=price(stop_raw) if stop_raw is not None else None,
    )


def _metadata_to_json(
    *, originating_thesis_id: str | None, originating_pm_command_id: str | None, age_hours: float
) -> str:
    """Encode the order's sidecar metadata payload as JSON."""
    return json.dumps(
        {
            "originating_thesis_id": originating_thesis_id,
            "originating_pm_command_id": originating_pm_command_id,
            "age_hours": age_hours,
        }
    )


def _metadata_from_json(payload: str) -> tuple[str | None, str | None, float]:
    raw = json.loads(payload)
    return (
        raw.get("originating_thesis_id"),
        raw.get("originating_pm_command_id"),
        raw["age_hours"],
    )


def record_to_row(record: OrderRecord) -> OrderRow:
    """Project an ``OrderRecord`` to its ``OrderRow`` form."""
    return OrderRow(
        order_id=record.order_id,
        position_id=record.position_id,
        bracket_id=record.bracket_id,
        order_role=record.role.value,
        order_class=record.order_class.value,
        instrument_spec_json=json.dumps(_instrument_spec_to_dict(record.instrument_spec)),
        direction=record.direction.value,
        order_type=record.order_type.value,
        quantity=record.quantity,
        price_parameters_json=_price_parameters_to_json(record.price_parameters),
        duration=record.duration.value,
        status=record.status.value,
        alpaca_order_id=record.alpaca_order_id,
        alpaca_order_id_chain_json=json.dumps(list(record.alpaca_order_id_chain)),
        submission_timestamp=record.submission_timestamp.isoformat(),
        last_update_timestamp=record.last_update_timestamp.isoformat(),
        filled_quantity=record.filled_quantity,
        average_fill_price=record.avg_fill_price,
        remaining_quantity=record.remaining_quantity,
        modification_count=record.modification_count,
        metadata_json=_metadata_to_json(
            originating_thesis_id=record.originating_thesis_id,
            originating_pm_command_id=record.originating_pm_command_id,
            age_hours=record.age_hours,
        ),
    )


def row_to_record(row: OrderRow) -> OrderRecord:
    """Rehydrate an ``OrderRow`` back into the typed ``OrderRecord``."""
    submission_ts = datetime.fromisoformat(row.submission_timestamp)
    last_update_ts = datetime.fromisoformat(row.last_update_timestamp)
    thesis_id_raw, pm_cmd_id_raw, age_hours = _metadata_from_json(row.metadata_json)
    chain = tuple(AlpacaOrderId(s) for s in json.loads(row.alpaca_order_id_chain_json))
    return OrderRecord(
        order_id=OrderId(row.order_id),
        position_id=PositionId(row.position_id) if row.position_id is not None else None,
        bracket_id=BracketId(row.bracket_id),
        role=OrderRole(row.order_role),
        instrument_spec=_instrument_spec_from_dict(json.loads(row.instrument_spec_json)),
        direction=OrderDirection(row.direction),
        order_type=OrderType(row.order_type),
        order_class=OrderClass(row.order_class),
        price_parameters=_price_parameters_from_json(row.price_parameters_json),
        quantity=row.quantity,
        duration=OrderDuration(row.duration),
        status=OrderStatus(row.status),
        alpaca_order_id=AlpacaOrderId(row.alpaca_order_id),
        alpaca_order_id_chain=chain,
        submission_timestamp=submission_ts,
        last_update_timestamp=last_update_ts,
        filled_quantity=row.filled_quantity,
        avg_fill_price=row.average_fill_price,
        remaining_quantity=row.remaining_quantity,
        modification_count=row.modification_count,
        originating_thesis_id=ThesisId(thesis_id_raw) if thesis_id_raw is not None else None,
        originating_pm_command_id=CommandId(pm_cmd_id_raw) if pm_cmd_id_raw is not None else None,
        age_hours=age_hours,
    )


__all__ = ["record_to_row", "row_to_record"]
