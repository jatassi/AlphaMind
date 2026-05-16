"""Round-trip codec between ``PositionRecord`` and ``PositionRow``.

The typed frozen-dataclass ``PositionRecord`` is the authoritative shape; the
SQL row mirrors it. ``record_to_row`` projects the record to its row form for
INSERT/UPDATE; ``row_to_record`` rehydrates a row back into the typed model
for the read path. Both are pure functions.

The discriminated ``PositionDetailsPayload`` (equity / options / strategy
variants) and the ``tuple[PositionFill, ...]`` execution history serialise via
hand-written JSON encode/decode helpers — every optional sub-field, every
freshness-metadata element, every greek round-trips through JSON without
information loss.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from alphamind._kernel.ids import BracketId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LiveExecutionEstimate,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionDetailsPayload,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.state.tables.positions import PositionRow


def _json_default(obj: object) -> object:
    """JSON ``default=`` hook for Decimals serialised by the codec.

    Encode Decimals as strings to preserve full precision; the decode path
    re-parses via :class:`Decimal` when the destination dataclass field is
    Money/Price-typed.
    """
    if isinstance(obj, Decimal):
        return str(obj)
    msg = f"object of type {type(obj).__name__} is not JSON-serializable"
    raise TypeError(msg)


def _encode_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decode_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _greeks_to_dict(g: OptionGreeks) -> dict[str, Any]:
    return {
        "delta": g.delta,
        "gamma": g.gamma,
        "theta": g.theta,
        "vega": g.vega,
        "as_of_timestamp": _encode_dt(g.as_of_timestamp),
        "iv_used": g.iv_used,
        "refresh_failed": g.refresh_failed,
    }


def _greeks_from_dict(payload: dict[str, Any]) -> OptionGreeks:
    return OptionGreeks(
        delta=payload["delta"],
        gamma=payload["gamma"],
        theta=payload["theta"],
        vega=payload["vega"],
        as_of_timestamp=_decode_dt(payload.get("as_of_timestamp")),
        iv_used=payload.get("iv_used"),
        refresh_failed=payload.get("refresh_failed", False),
    )


def _live_estimate_to_dict(le: LiveExecutionEstimate) -> dict[str, Any]:
    return {
        "estimated_spread_usd": le.estimated_spread_usd,
        "estimated_impact_usd": le.estimated_impact_usd,
        "estimated_regulatory_fees_usd": le.estimated_regulatory_fees_usd,
        "live_adjusted_fill_price": le.live_adjusted_fill_price,
    }


def _live_estimate_from_dict(payload: dict[str, Any]) -> LiveExecutionEstimate:
    # ALP-489 — Money/Price fields land as Decimal-as-text JSON values (see
    # ``_json_default``). Wrap via the boundary constructors so the typed
    # record carries the NewType alias rather than a bare ``Decimal``.
    return LiveExecutionEstimate(
        estimated_spread_usd=money(payload["estimated_spread_usd"]),
        estimated_impact_usd=money(payload["estimated_impact_usd"]),
        estimated_regulatory_fees_usd=money(payload["estimated_regulatory_fees_usd"]),
        live_adjusted_fill_price=price(payload["live_adjusted_fill_price"]),
    )


def _fill_to_dict(fill: PositionFill) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fill_timestamp": fill.fill_timestamp.isoformat(),
        "fill_price": fill.fill_price,
        "fill_quantity": fill.fill_quantity,
        "slippage": fill.slippage,
        "fees": fill.fees,
    }
    if fill.live_execution_estimate is not None:
        payload["live_execution_estimate"] = _live_estimate_to_dict(fill.live_execution_estimate)
    else:
        payload["live_execution_estimate"] = None
    return payload


def _fill_from_dict(payload: dict[str, Any]) -> PositionFill:
    live = payload.get("live_execution_estimate")
    return PositionFill(
        fill_timestamp=datetime.fromisoformat(payload["fill_timestamp"]),
        fill_price=price(payload["fill_price"]),
        fill_quantity=payload["fill_quantity"],
        slippage=signed_money(payload["slippage"]),
        fees=money(payload["fees"]),
        live_execution_estimate=_live_estimate_from_dict(live) if live is not None else None,
    )


def _details_to_dict(details: PositionDetailsPayload) -> dict[str, Any]:
    if isinstance(details, EquityPositionDetails):
        return {
            "instrument_type": details.instrument_type.value,
            "ticker": details.ticker,
            "share_count": details.share_count,
            "average_cost_basis_per_share": details.average_cost_basis_per_share,
            "borrow_rate_pct": details.borrow_rate_pct,
            "locate_status": (
                details.locate_status.value if details.locate_status is not None else None
            ),
            "margin_held_usd": details.margin_held_usd,
        }
    if isinstance(details, OptionsPositionDetails):
        return {
            "instrument_type": details.instrument_type.value,
            "underlying_ticker": details.underlying_ticker,
            "strike_price": details.strike_price,
            "expiration_date": details.expiration_date.isoformat(),
            "contract_type": details.contract_type.value,
            "contract_count": details.contract_count,
            "contract_multiplier": details.contract_multiplier,
            "premium_paid_per_contract": details.premium_paid_per_contract,
            "greeks": _greeks_to_dict(details.greeks),
        }
    # StrategyPositionDetails
    return {
        "instrument_type": details.instrument_type.value,
        "strategy_type_label": details.strategy_type_label,
        "legs": [
            {
                "leg_id": leg.leg_id,
                "direction": leg.direction.value if leg.direction is not None else None,
                "options": _details_to_dict(leg.options),
            }
            for leg in details.legs
        ],
        "net_premium_usd": details.net_premium_usd,
        "max_profit_usd": details.max_profit_usd,
        "max_loss_usd": details.max_loss_usd,
        "breakeven_levels": list(details.breakeven_levels),
        "strategy_greeks": _greeks_to_dict(details.strategy_greeks),
    }


def _equity_from_dict(payload: dict[str, Any]) -> EquityPositionDetails:
    locate_raw = payload.get("locate_status")
    return EquityPositionDetails(
        ticker=Symbol(payload["ticker"]),
        share_count=payload["share_count"],
        average_cost_basis_per_share=payload["average_cost_basis_per_share"],
        borrow_rate_pct=payload.get("borrow_rate_pct"),
        locate_status=LocateStatus(locate_raw) if locate_raw is not None else None,
        margin_held_usd=payload.get("margin_held_usd"),
    )


def _options_from_dict(payload: dict[str, Any]) -> OptionsPositionDetails:
    return OptionsPositionDetails(
        underlying_ticker=Symbol(payload["underlying_ticker"]),
        strike_price=payload["strike_price"],
        expiration_date=date.fromisoformat(payload["expiration_date"]),
        contract_type=OptionContractType(payload["contract_type"]),
        contract_count=payload["contract_count"],
        contract_multiplier=payload["contract_multiplier"],
        premium_paid_per_contract=payload["premium_paid_per_contract"],
        greeks=_greeks_from_dict(payload["greeks"]),
    )


def _strategy_leg_from_dict(payload: dict[str, Any]) -> StrategyLeg:
    direction_raw = payload.get("direction")
    return StrategyLeg(
        leg_id=payload["leg_id"],
        options=_options_from_dict(payload["options"]),
        direction=Direction(direction_raw) if direction_raw is not None else None,
    )


def _details_from_dict(payload: dict[str, Any]) -> PositionDetailsPayload:
    kind = payload["instrument_type"]
    if kind == InstrumentType.EQUITY.value:
        return _equity_from_dict(payload)
    if kind == InstrumentType.OPTIONS.value:
        return _options_from_dict(payload)
    if kind == InstrumentType.STRATEGY.value:
        return StrategyPositionDetails(
            strategy_type_label=payload["strategy_type_label"],
            legs=tuple(_strategy_leg_from_dict(leg) for leg in payload["legs"]),
            net_premium_usd=payload["net_premium_usd"],
            max_profit_usd=payload["max_profit_usd"],
            max_loss_usd=payload["max_loss_usd"],
            breakeven_levels=tuple(payload["breakeven_levels"]),
            strategy_greeks=_greeks_from_dict(payload["strategy_greeks"]),
        )
    msg = f"unknown instrument_type discriminator: {kind!r}"
    raise ValueError(msg)


def record_to_row(record: PositionRecord) -> PositionRow:
    """Project a ``PositionRecord`` to its ``PositionRow`` form.

    The frozen-dataclass ``PositionRecord`` ``__post_init__`` validator has
    already enforced status / direction / spinoff invariants by the time this
    function runs, so the projection is straightforward.
    """
    return PositionRow(
        position_id=record.position_id,
        thesis_id=record.thesis_id,
        bracket_id=record.bracket_id,
        status=record.status.value,
        direction=record.direction.value,
        entry_timestamp=(
            record.entry_timestamp.isoformat() if record.entry_timestamp is not None else None
        ),
        instrument_type=record.instrument_type.value,
        details_json=json.dumps(_details_to_dict(record.details), default=_json_default),
        execution_history_json=json.dumps(
            [_fill_to_dict(f) for f in record.execution_history], default=_json_default
        ),
        realized_pnl_to_date_usd=record.realized_pnl_to_date_usd,
        corporate_action_adjustment_needed=1 if record.corporate_action_adjustment_needed else 0,
        parent_position_id=record.parent_position_id,
        origin=record.origin,
    )


def row_to_record(row: PositionRow) -> PositionRecord:
    """Rehydrate a ``PositionRow`` back into the typed ``PositionRecord``.

    Validates that ``row.instrument_type`` agrees with the discriminator
    embedded in ``row.details_json`` — a mismatch can only arise from a
    hand-crafted INSERT that bypassed the codec, so it is treated as a
    fail-closed read error.
    """
    details = _details_from_dict(json.loads(row.details_json))
    if details.instrument_type.value != row.instrument_type:
        msg = (
            f"row.instrument_type={row.instrument_type!r} disagrees with "
            f"details_json discriminator={details.instrument_type.value!r} "
            f"for position_id={row.position_id!r}"
        )
        raise ValueError(msg)

    return PositionRecord(
        position_id=PositionId(row.position_id),
        thesis_id=ThesisId(row.thesis_id) if row.thesis_id is not None else None,
        bracket_id=BracketId(row.bracket_id) if row.bracket_id is not None else None,
        status=PositionStatus(row.status),
        direction=Direction(row.direction),
        entry_timestamp=(
            datetime.fromisoformat(row.entry_timestamp) if row.entry_timestamp is not None else None
        ),
        details=details,
        execution_history=tuple(_fill_from_dict(f) for f in json.loads(row.execution_history_json)),
        realized_pnl_to_date_usd=row.realized_pnl_to_date_usd,
        corporate_action_adjustment_needed=bool(row.corporate_action_adjustment_needed),
        parent_position_id=PositionId(row.parent_position_id)
        if row.parent_position_id is not None
        else None,
        origin=row.origin,
    )


__all__ = ["record_to_row", "row_to_record"]
