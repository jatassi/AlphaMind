"""Tests for the breach-behavior → OMS engine envelope adapter (ALP-438)."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import (
    BracketId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind.commands.engine_envelope import (
    EngineEnvelope as OmsEngineEnvelope,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.envelope_adapter import (
    to_oms_engine_envelope,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachDetails,
    EngineEnvelope,
    PositionSelectionAction,
    PositionSelectionResult,
    RegimeLabel,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
    compose_engine_envelope,
)

_NOW = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)


def _make_position_view(
    *,
    position_id: str = "POS-1",
    ticker: str = "NVDA",
) -> PositionView:
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THE-{position_id}"),
        bracket_id=BracketId(f"BRK-{position_id}"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        details=EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=10.0,
            average_cost_basis_per_share=150.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=150.0,
                fill_quantity=10.0,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=1500.0,
        unrealized_pnl_usd=-100.0,
        unrealized_pnl_pct=-6.67,
        position_weight_pct=10.0,
        position_age_hours=2.0,
        notional_exposure_usd=1500.0,
        delta_adjusted_exposure_usd=1500.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_bb_envelope(
    *,
    monitor_session_id: str = "monsession-a",
    trigger_id: int = 1,
    secondary: SecondaryBreachCheckResult | None = None,
    cascade_id: str | None = None,
) -> EngineEnvelope:
    position = _make_position_view()
    selection = PositionSelectionResult(
        position_id=position.position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale="largest unrealized loss",
    )
    return compose_engine_envelope(
        monitor_session_id=monitor_session_id,
        trigger_id=trigger_id,
        trigger_timestamp=_NOW,
        rule_breached="per_position_max_loss",
        breach_details=BreachDetails(
            current_value=-3.5,
            limit_value=-3.0,
            overage=0.5,
            unit="pct",
            regime_at_breach=RegimeLabel.ELEVATED,
        ),
        position_selection=selection,
        positions_by_id={position.position_id: position},
        portfolio_value_usd=100_000.0,
        cascade_id=cascade_id,
        secondary_breach_check=secondary,
    )


def test_translates_to_oms_envelope_with_matching_envelope_id() -> None:
    """The OMS envelope inherits ``envelope_id`` verbatim."""
    bb = _make_bb_envelope(trigger_id=42)
    oms = to_oms_engine_envelope(bb)
    assert isinstance(oms, OmsEngineEnvelope)
    assert oms.envelope_id == "MON.monsession-a.42"


def test_oms_envelope_carries_single_close_command_with_engine_guardrail_subtype() -> None:
    """The OMS envelope's commands tuple holds exactly one CLOSE with engine-guardrail subtype."""
    bb = _make_bb_envelope()
    oms = to_oms_engine_envelope(bb)
    assert len(oms.commands) == 1
    cmd = oms.commands[0]
    assert cmd.command_type == "close"
    assert cmd.close_rationale_type == "risk_management"
    assert cmd.risk_management_subtype == "engine_guardrail"


def test_oms_close_command_id_is_none_so_oms_derives_ordinal_zero() -> None:
    """The translator strips the breach-behavior command_id so ``submit_engine_envelope``
    derives the canonical ``MON.<session>.<trigger>.0`` form."""
    bb = _make_bb_envelope(trigger_id=7)
    oms = to_oms_engine_envelope(bb)
    assert oms.commands[0].command_id is None


def test_oms_envelope_preserves_position_id_and_quantity_or_all() -> None:
    """The CLOSE's ``position_id`` and ``quantity`` (full close → "all") survive translation."""
    bb = _make_bb_envelope()
    oms = to_oms_engine_envelope(bb)
    cmd = oms.commands[0]
    assert cmd.position_id == "POS-1"
    assert cmd.quantity == "all"
    assert cmd.order_type == "market"


def test_oms_envelope_preserves_trigger_record() -> None:
    """The trigger record's rule + breach details + rationale survive translation."""
    bb = _make_bb_envelope()
    oms = to_oms_engine_envelope(bb)
    rec = oms.guardrail_trigger_record
    assert rec.rule_breached == "per_position_max_loss"
    assert rec.position_selection_rationale == "largest unrealized loss"
    assert rec.breach_details.current_value == -3.5
    assert rec.breach_details.unit == "pct"


def test_oms_envelope_carries_secondary_breach_check_when_set() -> None:
    """A secondary-breach check on the source envelope propagates to OMS-side."""
    secondary = SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.NO_SECONDARY_BREACH,
        notes="clean",
    )
    bb = _make_bb_envelope(secondary=secondary)
    oms = to_oms_engine_envelope(bb)
    sbcr = oms.guardrail_trigger_record.secondary_breach_check_result
    assert sbcr is not None
    assert sbcr.result == "no_secondary_breach"
    assert sbcr.notes == "clean"


def test_oms_envelope_carries_cascade_id_when_set() -> None:
    """A cascade_id on the source envelope propagates to OMS-side."""
    bb = _make_bb_envelope(cascade_id="CASCADE.monsession-a.1")
    oms = to_oms_engine_envelope(bb)
    assert oms.guardrail_trigger_record.cascade_id == "CASCADE.monsession-a.1"
