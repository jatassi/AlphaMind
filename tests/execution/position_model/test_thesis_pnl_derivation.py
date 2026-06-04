"""Pure per-thesis PnL/cost-basis derivation from the broker-event log (ALP-851).

Invariant 3 (ADR-0001/0002): per-thesis realized PnL + cost basis reproduces
from a thesis's ``broker_event_log`` events alone (fills + activities), with no
join to a lose-able ``orders`` table. The fold is pure and deterministic for a
fixed event set — these tests pin that contract.
"""

from __future__ import annotations

import datetime as dt
import json

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
from alphamind._kernel.money import money, signed_money
from alphamind.execution.position_model.thesis_pnl_derivation import derive_thesis_pnl
from alphamind.state.records_broker_event_log import BrokerEventRecord, BrokerEventType

_THESIS = ThesisId("thesis-1")
_POS = PositionId("pos-1")
_INV = InvocationId("inv-1")
_T0 = dt.datetime(2026, 6, 1, 14, 0, 0, tzinfo=dt.UTC)


def _at(seconds: int) -> dt.datetime:
    return _T0 + dt.timedelta(seconds=seconds)


def _lifecycle_event(
    *,
    event_key: str,
    event_type: BrokerEventType,
    realized_pnl_delta_usd: str | None = None,
    cost_basis_delta_usd: str | None = None,
    closed_contract_qty: float | None = None,
    equity_qty: float | None = None,
    at_seconds: int = 0,
) -> BrokerEventRecord:
    payload: dict[str, object] = {"activity_id": event_key}
    if realized_pnl_delta_usd is not None:
        payload["realized_pnl_delta_usd"] = realized_pnl_delta_usd
    if cost_basis_delta_usd is not None:
        payload["cost_basis_delta_usd"] = cost_basis_delta_usd
    if closed_contract_qty is not None:
        payload["closed_contract_qty"] = closed_contract_qty
    if equity_qty is not None:
        payload["equity_qty"] = equity_qty
    return BrokerEventRecord(
        event_key=event_key,
        event_type=event_type,
        thesis_id=_THESIS,
        invocation_id=_INV,
        position_id=_POS,
        raw_payload_json=json.dumps(payload, sort_keys=True),
        broker_timestamp=_at(at_seconds),
        captured_at=_at(at_seconds),
    )


def _fill_event(
    *,
    event_key: str,
    side: str,
    fill_price: float,
    fill_quantity: float,
    at_seconds: int = 0,
) -> BrokerEventRecord:
    """A FILL event whose payload mirrors a websocket ``FillReport`` raw order side.

    The websocket translator (``translate_trade_update``) sets
    ``raw_event_payload = TradeUpdate.model_dump()`` — the side lives at
    ``raw_event_payload['order']['side']``.
    """
    payload = {
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "raw_event_payload": {"order": {"side": side}},
    }
    return _fill_record(event_key, payload, at_seconds)


def _rest_recovered_fill_event(
    *,
    event_key: str,
    side: str,
    fill_price: float,
    fill_quantity: float,
    at_seconds: int = 0,
) -> BrokerEventRecord:
    """A FILL event whose payload mirrors a REST-recovered ``FillReport``.

    The recovery translator (``order_snapshot_to_fill_reports``) sets
    ``raw_event_payload = OrderSnapshot.model_dump()`` — there is NO nested
    ``order`` key; the side lives at the top level (``raw_event_payload['side']``).
    """
    payload = {
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "raw_event_payload": {"side": side},
    }
    return _fill_record(event_key, payload, at_seconds)


def _fill_record(event_key: str, payload: dict[str, object], at_seconds: int) -> BrokerEventRecord:
    return BrokerEventRecord(
        event_key=event_key,
        event_type=BrokerEventType.FILL,
        thesis_id=_THESIS,
        invocation_id=_INV,
        position_id=_POS,
        raw_payload_json=json.dumps(payload, sort_keys=True),
        broker_timestamp=_at(at_seconds),
        captured_at=_at(at_seconds),
    )


def test_rest_recovered_fill_side_reads_from_top_level_order_snapshot() -> None:
    """FIX 1: a REST-recovered fill (OrderSnapshot shape, top-level ``side``) folds.

    03b's ``recover_missed_fills_since`` builds ``raw_event_payload`` from
    ``OrderSnapshot.model_dump()`` — the side is top-level, with no nested
    ``order`` key. Reading only the websocket ``order.side`` path crashes the
    fold with ``ValueError`` for any thesis carrying a recovered fill.
    """
    events = (
        _rest_recovered_fill_event(
            event_key="rest-open", side="buy", fill_price=100.0, fill_quantity=10.0
        ),
        _rest_recovered_fill_event(
            event_key="rest-close",
            side="sell",
            fill_price=130.0,
            fill_quantity=10.0,
            at_seconds=60,
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("300.00")
    assert derivation.cost_basis_usd == money("0")


def test_otm_expiry_books_negative_premium() -> None:
    """An OPEXP event carrying a -premium delta folds to that realized PnL."""
    events = (
        _lifecycle_event(
            event_key="activity:exp-1",
            event_type=BrokerEventType.OPEXP,
            realized_pnl_delta_usd="-1250.00",
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("-1250.00")
    assert derivation.cost_basis_usd == money("0")


def test_open_only_fill_builds_cost_basis_no_realized_pnl() -> None:
    """A buy-to-open fill ties up capital as cost basis; nothing is realized yet."""
    events = (_fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),)

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("0")
    assert derivation.cost_basis_usd == money("1000.00")


def test_round_trip_long_fill_realizes_gain_and_releases_basis() -> None:
    """Buy 10@100 then sell 10@130 → +300 realized, cost basis back to zero."""
    events = (
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-close", side="sell", fill_price=130.0, fill_quantity=10.0, at_seconds=60
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("300.00")
    assert derivation.cost_basis_usd == money("0")


def test_partial_close_realizes_proportionally_and_retains_basis() -> None:
    """Buy 10@100, sell 4@130 → +120 realized; 6 shares of basis remain."""
    events = (
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-close", side="sell", fill_price=130.0, fill_quantity=4.0, at_seconds=60
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("120.00")
    assert derivation.cost_basis_usd == money("600.00")


def test_short_round_trip_realizes_gain_on_buy_to_cover() -> None:
    """Sell 10@100 (open short) then buy 10@80 (cover) → +200 realized."""
    events = (
        _fill_event(event_key="fevt-open", side="sell", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-cover", side="buy", fill_price=80.0, fill_quantity=10.0, at_seconds=60
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("200.00")
    assert derivation.cost_basis_usd == money("0")


def test_add_then_close_uses_average_cost() -> None:
    """Buy 10@100, buy 10@140 (avg 120), sell 20@130 → (130-120)*20 = +200."""
    events = (
        _fill_event(event_key="fevt-a", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-b", side="buy", fill_price=140.0, fill_quantity=10.0, at_seconds=30
        ),
        _fill_event(
            event_key="fevt-c", side="sell", fill_price=130.0, fill_quantity=20.0, at_seconds=60
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("200.00")
    assert derivation.cost_basis_usd == money("0")


def test_fill_and_expiry_both_contribute_to_same_thesis() -> None:
    """AC#4: an OPEXP activity and a fill both fold into one thesis's realized PnL."""
    events = (
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-close", side="sell", fill_price=130.0, fill_quantity=10.0, at_seconds=60
        ),
        _lifecycle_event(
            event_key="activity:exp-1",
            event_type=BrokerEventType.OPEXP,
            realized_pnl_delta_usd="-1250.00",
            at_seconds=120,
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    # +300 from the equity round-trip, -1250 from the worthless expiry.
    assert derivation.realized_pnl_usd == signed_money("-950.00")


def test_assignment_optrd_contributes_equity_cost_basis() -> None:
    """An OPASN (-premium) + paired OPTRD (equity basis) fold to PnL + cost basis."""
    events = (
        _lifecycle_event(
            event_key="activity:asn-1",
            event_type=BrokerEventType.OPASN,
            realized_pnl_delta_usd="-1250.00",
        ),
        _lifecycle_event(
            event_key="activity:trd-1",
            event_type=BrokerEventType.OPTRD,
            cost_basis_delta_usd="75000.00",
            at_seconds=1,
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("-1250.00")
    assert derivation.cost_basis_usd == money("75000.00")


def test_bought_option_fill_then_opexp_closes_the_lot() -> None:
    """FIX 2: an option bought via a FILL is closed by its OPEXP lifecycle exit.

    The buy fill opens the option lot (cost basis = contracts x premium). OPEXP
    must close that lot — without it the option still shows as held: cost basis
    stays non-zero (the phantom held option) even though it expired worthless.
    """
    events = (
        # Buy 5 option contracts at 250/contract → 1250 cost basis.
        _fill_event(event_key="opt-buy", side="buy", fill_price=250.0, fill_quantity=5.0),
        _lifecycle_event(
            event_key="activity:exp-1",
            event_type=BrokerEventType.OPEXP,
            realized_pnl_delta_usd="-1250.00",
            closed_contract_qty=5.0,
            at_seconds=60,
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    # The option expired worthless: full premium lost, no remaining cost basis.
    assert derivation.realized_pnl_usd == signed_money("-1250.00")
    assert derivation.cost_basis_usd == money("0")


def test_bought_option_fill_then_opasn_closes_option_and_opens_equity() -> None:
    """FIX 2: an OPASN closes the bought-option lot AND opens the equity at strike.

    Buy 5 contracts (1250 basis). On assignment the option closes (-premium) and
    the paired OPTRD opens the equity leg at the strike — 500 shares x 150 =
    75000 cost basis. The lingering option contracts must not survive into the
    equity-leg cost basis, and the equity sell must later classify as a close.
    """
    events = (
        _fill_event(event_key="opt-buy", side="buy", fill_price=250.0, fill_quantity=5.0),
        _lifecycle_event(
            event_key="activity:asn-1",
            event_type=BrokerEventType.OPASN,
            realized_pnl_delta_usd="-1250.00",
            closed_contract_qty=5.0,
            at_seconds=60,
        ),
        _lifecycle_event(
            event_key="activity:trd-1",
            event_type=BrokerEventType.OPTRD,
            cost_basis_delta_usd="75000.00",
            equity_qty=500.0,
            at_seconds=61,
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("-1250.00")
    # Only the equity leg's basis remains; the closed option contributes nothing.
    assert derivation.cost_basis_usd == money("75000.00")


def test_assigned_equity_fully_sold_clears_cost_basis() -> None:
    """FIX 3: equity opened at strike by an assignment, fully sold, leaves no basis.

    The OPTRD opens the equity leg at the strike (cost basis set). When the
    equity is later fully sold, the lot fully closes — cost basis must return to
    zero, not leave a phantom strike-priced basis behind.
    """
    events = (
        _lifecycle_event(
            event_key="activity:asn-1",
            event_type=BrokerEventType.OPASN,
            realized_pnl_delta_usd="-1250.00",
            closed_contract_qty=5.0,
        ),
        _lifecycle_event(
            event_key="activity:trd-1",
            event_type=BrokerEventType.OPTRD,
            cost_basis_delta_usd="75000.00",
            equity_qty=500.0,
            at_seconds=1,
        ),
        # Sell all 500 assigned shares at 160 → realizes (160-150)*500 = +5000.
        _fill_event(
            event_key="eq-sell", side="sell", fill_price=160.0, fill_quantity=500.0, at_seconds=60
        ),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.realized_pnl_usd == signed_money("3750.00")  # -1250 + 5000
    assert derivation.cost_basis_usd == money("0")


def test_derivation_is_deterministic_for_a_fixed_event_set() -> None:
    """AC#1/#5: the same event set yields the same figures across repeated folds."""
    events = (
        _fill_event(event_key="fevt-a", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-b", side="sell", fill_price=130.0, fill_quantity=4.0, at_seconds=30
        ),
        _lifecycle_event(
            event_key="activity:exp-1",
            event_type=BrokerEventType.OPEXP,
            realized_pnl_delta_usd="-250.00",
            at_seconds=60,
        ),
    )

    first = derive_thesis_pnl(_THESIS, events)
    second = derive_thesis_pnl(_THESIS, events)

    assert first == second


def test_provenance_lists_contributing_event_keys_in_order() -> None:
    """AC#3: the derivation carries the event keys it folded from, time-ordered."""
    events = (
        _fill_event(
            event_key="fevt-late", side="sell", fill_price=130.0, fill_quantity=10.0, at_seconds=60
        ),
        _fill_event(event_key="fevt-early", side="buy", fill_price=100.0, fill_quantity=10.0),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.provenance_event_keys == ("fevt-early", "fevt-late")


def test_events_for_other_theses_are_ignored() -> None:
    """A foreign thesis's events do not leak into this thesis's figures."""
    other = _fill_event(event_key="fevt-other", side="buy", fill_price=999.0, fill_quantity=1.0)
    other = other.model_copy(update={"thesis_id": ThesisId("thesis-other")})
    events = (
        other,
        _fill_event(event_key="fevt-mine", side="buy", fill_price=100.0, fill_quantity=10.0),
    )

    derivation = derive_thesis_pnl(_THESIS, events)

    assert derivation.cost_basis_usd == money("1000.00")
    assert derivation.provenance_event_keys == ("fevt-mine",)
