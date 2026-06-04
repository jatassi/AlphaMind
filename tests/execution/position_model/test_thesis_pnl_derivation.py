"""Pure per-thesis PnL/cost-basis derivation from the broker-event log (ALP-851).

Invariant 3 (ADR-0001/0002): per-thesis realized PnL + cost basis reproduces
from a thesis's ``broker_event_log`` events alone (fills ∪ activities), with no
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
    at_seconds: int = 0,
) -> BrokerEventRecord:
    payload: dict[str, object] = {"activity_id": event_key}
    if realized_pnl_delta_usd is not None:
        payload["realized_pnl_delta_usd"] = realized_pnl_delta_usd
    if cost_basis_delta_usd is not None:
        payload["cost_basis_delta_usd"] = cost_basis_delta_usd
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
    """A FILL event whose payload mirrors a ``FillReport`` raw order side."""
    payload = {
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "raw_event_payload": {"order": {"side": side}},
    }
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
