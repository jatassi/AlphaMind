"""Tests for the breach_behavior engine-envelope assembler (story 06)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachDetails,
    EngineCloseCommand,
    EngineEnvelope,
    EngineGuardrailTriggerRecord,
    PositionSelectionAction,
    PositionSelectionResult,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
    command_id_for,
    compose_engine_envelope,
    compose_guardrail_trigger_record,
    envelope_id_for,
)


def _short_mara_position() -> PositionView:
    """A6 / A7 fixture: 140 shares short MARA at $20 avg cost, current $28."""
    fill_ts = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    record = PositionRecord(
        position_id="POS-MARA-001",
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=fill_ts,
        details=EquityPositionDetails(
            ticker="MARA",
            share_count=140.0,
            average_cost_basis_per_share=20.0,
            borrow_rate_pct=2.5,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=560.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=fill_ts,
                fill_price=20.0,
                fill_quantity=140.0,
                slippage=0.01,
                fees=1.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=3920.0,
        unrealized_pnl_usd=-1120.0,
        unrealized_pnl_pct=-40.0,
        position_weight_pct=3.92,
        position_age_hours=24.0,
        notional_exposure_usd=3920.0,
        delta_adjusted_exposure_usd=3920.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _breach_details() -> BreachDetails:
    return BreachDetails(
        current_value=3.92,
        limit_value=3.0,
        overage=0.92,
        unit="pct_of_portfolio",
    )


def _full_close_selection() -> PositionSelectionResult:
    return PositionSelectionResult(
        position_id="POS-MARA-001",
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale="position-level max loss breach on MARA (loss: -40.0% of cost, limit: -30.0%)",
    )


def _trim_selection(target_pct: float = 2.5) -> PositionSelectionResult:
    return PositionSelectionResult(
        position_id="POS-MARA-001",
        action=PositionSelectionAction.PARTIAL_TRIM,
        target_post_action_size_pct_of_portfolio=target_pct,
        rationale=(
            f"single short MARA at 3.0% > 3.0% per-position cap; trimmed to "
            f"{target_pct:.2f}% (95% of cap)"
        ),
    )


_TRIGGER_TS = datetime(2026, 4, 29, 14, 30, tzinfo=UTC)


def test_envelope_id_for_happy_path() -> None:
    assert envelope_id_for(monitor_session_id="abc123", trigger_id=1) == "MON.abc123.1"


def test_envelope_id_for_higher_trigger_id() -> None:
    assert envelope_id_for(monitor_session_id="abc123", trigger_id=42) == "MON.abc123.42"


def test_envelope_id_for_rejects_empty_session_id() -> None:
    with pytest.raises(ValueError, match="monitor_session_id"):
        envelope_id_for(monitor_session_id="", trigger_id=1)


def test_envelope_id_for_rejects_session_id_containing_dot() -> None:
    with pytest.raises(ValueError, match="monitor_session_id"):
        envelope_id_for(monitor_session_id="a.b", trigger_id=1)


def test_envelope_id_for_rejects_trigger_id_below_one() -> None:
    with pytest.raises(ValueError, match="trigger_id"):
        envelope_id_for(monitor_session_id="abc", trigger_id=0)


def test_command_id_for_happy_path_default_ordinal() -> None:
    assert command_id_for(envelope_id="MON.s.1") == "MON.s.1.1"


def test_command_id_for_higher_ordinal() -> None:
    assert command_id_for(envelope_id="MON.s.1", ordinal=3) == "MON.s.1.3"


def test_command_id_for_rejects_ordinal_below_one() -> None:
    with pytest.raises(ValueError, match="ordinal"):
        command_id_for(envelope_id="MON.s.1", ordinal=0)


def test_command_id_for_rejects_malformed_envelope_id() -> None:
    with pytest.raises(ValueError, match="envelope_id"):
        command_id_for(envelope_id="PM.s.1")


# -----------------------------------------------------------------------------
# compose_guardrail_trigger_record
# -----------------------------------------------------------------------------


def test_compose_guardrail_trigger_record_happy_path_standalone() -> None:
    selection = _full_close_selection()
    record = compose_guardrail_trigger_record(
        rule_breached="position_max_loss_equity_pct",
        trigger_timestamp=_TRIGGER_TS,
        breach_details=_breach_details(),
        position_selection=selection,
    )
    assert isinstance(record, EngineGuardrailTriggerRecord)
    assert record.rule_breached == "position_max_loss_equity_pct"
    assert record.trigger_timestamp == _TRIGGER_TS
    assert record.breach_details == _breach_details()
    assert record.position_selection_rationale == selection.rationale
    assert record.cascade_id is None
    assert record.secondary_breach_check_result is None


def test_compose_guardrail_trigger_record_carries_cascade_id() -> None:
    record = compose_guardrail_trigger_record(
        rule_breached="margin_call",
        trigger_timestamp=_TRIGGER_TS,
        breach_details=_breach_details(),
        position_selection=_full_close_selection(),
        cascade_id="cascade-abc",
    )
    assert record.cascade_id == "cascade-abc"


def test_compose_guardrail_trigger_record_carries_secondary_breach_result() -> None:
    secondary = SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.DEFERRED_TO_PM,
        notes="secondary breach introduced on: sector_concentration_pct",
    )
    record = compose_guardrail_trigger_record(
        rule_breached="position_max_loss_equity_pct",
        trigger_timestamp=_TRIGGER_TS,
        breach_details=_breach_details(),
        position_selection=_full_close_selection(),
        secondary_breach_check=secondary,
    )
    assert record.secondary_breach_check_result == secondary


def test_compose_guardrail_trigger_record_rejects_empty_rule_breached() -> None:
    with pytest.raises(ValueError, match="rule_breached"):
        compose_guardrail_trigger_record(
            rule_breached="",
            trigger_timestamp=_TRIGGER_TS,
            breach_details=_breach_details(),
            position_selection=_full_close_selection(),
        )


def test_compose_guardrail_trigger_record_rejects_naive_trigger_timestamp() -> None:
    naive = datetime(2026, 4, 29, 14, 30)  # noqa: DTZ001 — intentional naive datetime for test
    with pytest.raises(ValueError, match="trigger_timestamp"):
        compose_guardrail_trigger_record(
            rule_breached="position_max_loss_equity_pct",
            trigger_timestamp=naive,
            breach_details=_breach_details(),
            position_selection=_full_close_selection(),
        )


# -----------------------------------------------------------------------------
# compose_engine_envelope — full assembler
# -----------------------------------------------------------------------------


def test_compose_engine_envelope_a6_full_close() -> None:
    """A6 reproduction: position-level max loss → full close on POS-MARA-001."""
    selection = _full_close_selection()
    envelope = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="position_max_loss_equity_pct",
        breach_details=_breach_details(),
        position_selection=selection,
        positions_by_id={"POS-MARA-001": _short_mara_position()},
        portfolio_value_usd=100_000.0,
    )
    assert envelope.envelope_id == "MON.s1.1"
    assert envelope.trigger_timestamp == _TRIGGER_TS
    assert envelope.source_provenance == "engine_guardrail"
    assert envelope.guardrail_trigger_record.rule_breached == "position_max_loss_equity_pct"
    assert envelope.guardrail_trigger_record.position_selection_rationale == selection.rationale
    assert envelope.command.command_id == "MON.s1.1.1"
    assert envelope.command.command_type == "close"
    assert envelope.command.close_rationale_type == "risk_management"
    assert envelope.command.risk_management_subtype == "engine_guardrail"
    assert envelope.command.position_id == "POS-MARA-001"
    assert envelope.command.quantity_or_all == "all"
    assert envelope.command.execution_method == "market"
    assert envelope.command.limit_price is None


def test_compose_engine_envelope_a7_partial_trim() -> None:
    """A7-style partial trim: target 2.5% of $100K portfolio → $2,500 USD."""
    selection = _trim_selection(target_pct=2.5)
    envelope = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="single_short_max_size_pct",
        breach_details=_breach_details(),
        position_selection=selection,
        positions_by_id={"POS-MARA-001": _short_mara_position()},
        portfolio_value_usd=100_000.0,
    )
    assert envelope.command.quantity_or_all == pytest.approx(2500.0)


def test_compose_engine_envelope_cascade_chain_distinct_envelope_ids() -> None:
    selection = _full_close_selection()
    positions = {"POS-MARA-001": _short_mara_position()}
    first = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="margin_call",
        breach_details=_breach_details(),
        position_selection=selection,
        positions_by_id=positions,
        portfolio_value_usd=100_000.0,
        cascade_id="cascade-abc",
    )
    second = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=2,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="position_max_loss_equity_pct",
        breach_details=_breach_details(),
        position_selection=selection,
        positions_by_id=positions,
        portfolio_value_usd=100_000.0,
        cascade_id="cascade-abc",
    )
    assert first.envelope_id == "MON.s1.1"
    assert second.envelope_id == "MON.s1.2"
    assert first.guardrail_trigger_record.cascade_id == "cascade-abc"
    assert second.guardrail_trigger_record.cascade_id == "cascade-abc"


def test_compose_engine_envelope_carries_secondary_breach_avoided() -> None:
    secondary = SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.SECONDARY_BREACH_AVOIDED,
        notes="cascade resolution closed the secondary-breach surface",
    )
    envelope = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="position_max_loss_equity_pct",
        breach_details=_breach_details(),
        position_selection=_full_close_selection(),
        positions_by_id={"POS-MARA-001": _short_mara_position()},
        portfolio_value_usd=100_000.0,
        secondary_breach_check=secondary,
    )
    assert envelope.guardrail_trigger_record.secondary_breach_check_result == secondary


def test_compose_engine_envelope_limit_price_execution() -> None:
    envelope = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="position_max_loss_equity_pct",
        breach_details=_breach_details(),
        position_selection=_full_close_selection(),
        positions_by_id={"POS-MARA-001": _short_mara_position()},
        portfolio_value_usd=100_000.0,
        execution_method="limit",
        limit_price=18.50,
    )
    assert envelope.command.execution_method == "limit"
    assert envelope.command.limit_price == 18.50


def test_compose_engine_envelope_rejects_missing_position_in_lookup() -> None:
    selection = _full_close_selection()  # position_id="POS-MARA-001"
    with pytest.raises(ValueError, match="POS-MARA-001"):
        compose_engine_envelope(
            monitor_session_id="s1",
            trigger_id=1,
            trigger_timestamp=_TRIGGER_TS,
            rule_breached="position_max_loss_equity_pct",
            breach_details=_breach_details(),
            position_selection=selection,
            positions_by_id={},
            portfolio_value_usd=100_000.0,
        )


@pytest.mark.parametrize("portfolio_value_usd", [0.0, -1.0])
def test_compose_engine_envelope_rejects_non_positive_portfolio_value(
    portfolio_value_usd: float,
) -> None:
    with pytest.raises(ValueError, match="portfolio_value_usd"):
        compose_engine_envelope(
            monitor_session_id="s1",
            trigger_id=1,
            trigger_timestamp=_TRIGGER_TS,
            rule_breached="position_max_loss_equity_pct",
            breach_details=_breach_details(),
            position_selection=_full_close_selection(),
            positions_by_id={"POS-MARA-001": _short_mara_position()},
            portfolio_value_usd=portfolio_value_usd,
        )


def test_compose_engine_envelope_limit_method_without_limit_price_raises() -> None:
    with pytest.raises(ValueError, match="limit_price"):
        compose_engine_envelope(
            monitor_session_id="s1",
            trigger_id=1,
            trigger_timestamp=_TRIGGER_TS,
            rule_breached="position_max_loss_equity_pct",
            breach_details=_breach_details(),
            position_selection=_full_close_selection(),
            positions_by_id={"POS-MARA-001": _short_mara_position()},
            portfolio_value_usd=100_000.0,
            execution_method="limit",
            limit_price=None,
        )


def test_compose_engine_envelope_output_is_frozen() -> None:
    from pydantic import ValidationError

    envelope = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=_TRIGGER_TS,
        rule_breached="position_max_loss_equity_pct",
        breach_details=_breach_details(),
        position_selection=_full_close_selection(),
        positions_by_id={"POS-MARA-001": _short_mara_position()},
        portfolio_value_usd=100_000.0,
    )
    with pytest.raises(ValidationError):
        envelope.envelope_id = "MON.x.99"


# -----------------------------------------------------------------------------
# Cross-field invariants on EngineEnvelope itself (post-validator coverage)
# -----------------------------------------------------------------------------


def _trigger_record_for_post_validator_tests(
    *, trigger_timestamp: datetime = _TRIGGER_TS
) -> EngineGuardrailTriggerRecord:
    return EngineGuardrailTriggerRecord(
        rule_breached="position_max_loss_equity_pct",
        trigger_timestamp=trigger_timestamp,
        breach_details=_breach_details(),
        position_selection_rationale=_full_close_selection().rationale,
    )


def _close_command_for_post_validator_tests(
    *, command_id: str = "MON.s1.1.1"
) -> EngineCloseCommand:
    return EngineCloseCommand(
        command_id=command_id,
        position_id="POS-MARA-001",
        quantity_or_all="all",
    )


def test_engine_envelope_rejects_mismatched_trigger_timestamps() -> None:
    from pydantic import ValidationError

    other_ts = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="trigger_timestamp"):
        EngineEnvelope(
            envelope_id="MON.s1.1",
            trigger_timestamp=_TRIGGER_TS,
            guardrail_trigger_record=_trigger_record_for_post_validator_tests(
                trigger_timestamp=other_ts
            ),
            command=_close_command_for_post_validator_tests(),
        )


def test_engine_envelope_rejects_command_id_not_starting_with_envelope_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="command_id"):
        EngineEnvelope(
            envelope_id="MON.s1.1",
            trigger_timestamp=_TRIGGER_TS,
            guardrail_trigger_record=_trigger_record_for_post_validator_tests(),
            command=_close_command_for_post_validator_tests(command_id="MON.s1.99.1"),
        )


def test_engine_envelope_rejects_envelope_id_pattern_violation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="envelope_id"):
        EngineEnvelope(
            envelope_id="PM.s1.1",
            trigger_timestamp=_TRIGGER_TS,
            guardrail_trigger_record=_trigger_record_for_post_validator_tests(),
            command=_close_command_for_post_validator_tests(command_id="PM.s1.1.1"),
        )


def test_compose_engine_envelope_is_deterministic() -> None:
    positions = {"POS-MARA-001": _short_mara_position()}
    envelopes = [
        compose_engine_envelope(
            monitor_session_id="s1",
            trigger_id=1,
            trigger_timestamp=_TRIGGER_TS,
            rule_breached="position_max_loss_equity_pct",
            breach_details=_breach_details(),
            position_selection=_full_close_selection(),
            positions_by_id=positions,
            portfolio_value_usd=100_000.0,
        )
        for _ in range(100)
    ]
    first = envelopes[0]
    for envelope in envelopes[1:]:
        assert envelope == first
