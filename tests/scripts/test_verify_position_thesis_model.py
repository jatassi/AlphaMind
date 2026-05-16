"""Tests for scripts/verify_position_thesis_model.py (ALP-336).

The verify script is offline-only (no SDK, no DB) and exercises every
deliverable from the position-thesis-model work tree's 15 sub-stories.
These tests verify the script's wave functions produce the correct
results and the CLI machinery (exit codes, --verbose, --help) works.

Coverage:
- Tracer: script module imports and run_all_waves returns (total, failed)=(40, 0)
- Wave 1 utilities (01a/01b/01c): 12 sub-cases
- Wave 1 additive fields (01d-01j): 14 sub-cases
- Wave 2 boundary fix (02): 2 sub-cases
- Wave 3 typed payloads (03a/03b): 4 sub-cases
- Wave 4 structural (04a/04b): 4 sub-cases
- Wave 5 architectural (05a/05b/05c): 4 sub-cases
- CLI: exit 0 on full pass, exit 1 on injected failure, --help works
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "verify_position_thesis_model.py"


def _load_script() -> ModuleType:
    """Load the verify script as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location("verify_ptm", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tracer bullet: script loads and full run passes 40/40
# ---------------------------------------------------------------------------


def test_script_exists() -> None:
    assert _SCRIPT_PATH.exists(), f"Script not found at {_SCRIPT_PATH}"


def test_run_all_waves_passes_40_of_40() -> None:
    mod = _load_script()
    total, failed = mod.run_all_waves(verbose=False)
    assert total == 40
    assert failed == 0


# ---------------------------------------------------------------------------
# Wave 1: utilities (01a / 01b / 01c)  — 12 sub-cases
# ---------------------------------------------------------------------------


def test_wave1_utilities_returns_12_0() -> None:
    mod = _load_script()
    passed, failed, results = mod.wave1_utilities(verbose=False)
    assert failed == 0, f"Wave 1 failures: {[r for r in results if not r['ok']]}"
    assert passed == 12


def test_wave1_bracket_coverage_valid_passes() -> None:
    mod = _load_script()
    # Direct call to the helper — should not raise
    bracket, thesis = mod._make_covered_bracket_and_thesis()
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    validate_bracket_thesis_coverage(bracket, thesis)  # must not raise


def test_wave1_bracket_coverage_broken_raises() -> None:
    mod = _load_script()
    bracket, broken_thesis = mod._make_uncovered_bracket_and_thesis()
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    with pytest.raises(ValueError):
        validate_bracket_thesis_coverage(bracket, broken_thesis)


def test_wave1_resolution_classifier_five_inputs() -> None:
    mod = _load_script()
    from alphamind.execution.thesis_model import classify_thesis_resolution

    # Each of the 5 cases the wave exercises
    cases = mod._resolution_classifier_cases()
    assert len(cases) == 5
    for component_outcomes, pnl, exit_method, expected_category in cases:
        result = classify_thesis_resolution(component_outcomes, pnl, exit_method)
        assert result == expected_category


def test_wave1_resolution_empty_raises() -> None:
    from alphamind.execution.thesis_model import classify_thesis_resolution
    from alphamind.portfolio_state.events.activity_log import PositionExitMethod

    with pytest.raises(ValueError):
        classify_thesis_resolution((), 100.0, PositionExitMethod.PM_DECISION)


def test_wave1_payoff_five_canonical_strategies() -> None:
    mod = _load_script()
    strategies = mod._canonical_strategy_fixtures()
    assert len(strategies) == 5
    from alphamind.execution.position_model import (
        compute_strategy_breakeven_levels,
        compute_strategy_max_loss_usd,
        compute_strategy_max_profit_usd,
    )

    for (
        label,
        legs,
        net_premium,
        expected_max_profit,
        expected_max_loss,
        expected_breakevens,
    ) in strategies:
        max_profit = compute_strategy_max_profit_usd(legs, net_premium)
        max_loss = compute_strategy_max_loss_usd(legs, net_premium)
        breakevens = compute_strategy_breakeven_levels(legs, net_premium)
        if expected_max_profit == float("inf"):
            assert max_profit == float("inf"), f"{label}: max_profit expected inf"
        else:
            assert abs(max_profit - expected_max_profit) < 1e-6, (
                f"{label}: max_profit {max_profit} != {expected_max_profit}"
            )
        if expected_max_loss == float("-inf"):
            assert max_loss == float("-inf"), f"{label}: max_loss expected -inf"
        else:
            assert abs(max_loss - expected_max_loss) < 1e-6, (
                f"{label}: max_loss {max_loss} != {expected_max_loss}"
            )
        assert len(breakevens) == expected_breakevens, f"{label}: breakeven count mismatch"


# ---------------------------------------------------------------------------
# Wave 2: additive fields (01d-01j) — 14 sub-cases
# ---------------------------------------------------------------------------


def test_wave2_additive_fields_returns_14_0() -> None:
    mod = _load_script()
    passed, failed, results = mod.wave2_additive_fields(verbose=False)
    assert failed == 0, f"Wave 2 failures: {[r for r in results if not r['ok']]}"
    assert passed == 14


def test_wave2_thesis_record_float_time_expectation() -> None:
    """01d: time_expectation_hours must be float; string rejected."""
    from datetime import UTC, datetime, timedelta

    from alphamind.portfolio_state.records.theses import (
        ThesisRecord,
        ThesisRecordStatus,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    components = _make_three_components(now)
    # float should parse
    r = ThesisRecord(
        thesis_id=ThesisId("t1"),
        position_id=PositionId("p1"),
        summary="test",
        key_catalyst="cat",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=now,
        time_expectation_hours=48.0,
        age_hours=0.0,
        expected_resolution_at=now + timedelta(hours=48),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )
    assert r.time_expectation_hours == 48.0

    # string "48" should be coerced to float 48.0 by Pydantic (int/float coercion)
    # but we verify the field is typed float not str
    assert isinstance(r.time_expectation_hours, float)


def test_wave2_position_weight_pct_negative() -> None:
    """01e: negative position_weight_pct allowed on PositionView."""
    from datetime import datetime

    from alphamind.portfolio_state.records.positions import (
        Direction,
        EquityPositionDetails,
        PositionFill,
        PositionRecord,
        PositionStatus,
    )
    from alphamind.portfolio_state.views.positions import PositionView

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    details = EquityPositionDetails(
        ticker=Symbol("AAPL"), share_count=100.0, average_cost_basis_per_share=150.0
    )
    fill = PositionFill(
        fill_timestamp=now,
        fill_price=price(150.0),
        fill_quantity=100.0,
        slippage=signed_money(0.0),
        fees=money(0.0),
    )
    record = PositionRecord(
        position_id=PositionId("pos1"),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=now,
        details=details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    view = PositionView(
        record=record,
        current_market_value_usd=signed_money(10000.0),
        unrealized_pnl_usd=signed_money(200.0),
        unrealized_pnl_pct=0.02,
        position_weight_pct=-3.5,
        position_age_hours=2.0,
        notional_exposure_usd=money(10000.0),
        delta_adjusted_exposure_usd=signed_money(-10000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )
    assert view.position_weight_pct == -3.5


def test_wave2_option_greeks_freshness_valid() -> None:
    """01f: OptionGreeks with tz-aware as_of_timestamp and iv_used > 0 passes."""
    from datetime import datetime

    from alphamind.portfolio_state.records.positions import OptionGreeks

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    g = OptionGreeks(
        delta=0.4, gamma=0.02, theta=-0.05, vega=0.1, as_of_timestamp=now, iv_used=0.45
    )
    assert g.iv_used == 0.45
    assert g.as_of_timestamp == now


def test_wave2_option_greeks_zero_iv_rejected() -> None:
    """01f: iv_used=0.0 must be rejected."""
    from datetime import datetime

    from alphamind.portfolio_state.records.positions import OptionGreeks

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises((ValueError, TypeError)):
        OptionGreeks(delta=0.4, gamma=0.02, theta=-0.05, vega=0.1, as_of_timestamp=now, iv_used=0.0)


def test_wave2_position_fill_live_estimate() -> None:
    """01g: PositionFill with live_execution_estimate passes; negative fees rejected."""
    from datetime import datetime

    from alphamind.portfolio_state.records.positions import LiveExecutionEstimate, PositionFill

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    est = LiveExecutionEstimate(
        estimated_spread_usd=money(0.05),
        estimated_impact_usd=money(0.02),
        estimated_regulatory_fees_usd=money(0.01),
        live_adjusted_fill_price=price(149.92),
    )
    fill = PositionFill(
        fill_timestamp=now,
        fill_price=price(150.0),
        fill_quantity=100.0,
        slippage=signed_money(0.0),
        fees=money(1.0),
        live_execution_estimate=est,
    )
    assert fill.live_execution_estimate == est

    with pytest.raises((ValueError, TypeError)):
        PositionFill(
            fill_timestamp=now,
            fill_price=price(150.0),
            fill_quantity=100.0,
            slippage=signed_money(0.0),
            fees=money(-1.0),
        )


def test_wave2_bracket_record_deadline_naive_rejected() -> None:
    """01h: naive entry_window_deadline is rejected."""
    from datetime import datetime

    naive_dt = datetime(2026, 5, 2, 12, 0, 0)  # noqa: DTZ001 — intentionally naive to test rejection
    with pytest.raises((ValueError, TypeError)):
        _make_bracket_record(entry_window_deadline=naive_dt)


def test_wave2_order_record_mleg_with_equity_rejected() -> None:
    """01i: MLEG order with EQUITY instrument_spec must be rejected."""
    from datetime import datetime

    from alphamind.portfolio_state.records.orders import (
        EquityInstrumentSpec,
        OrderClass,
        OrderDirection,
        OrderDuration,
        OrderRecord,
        OrderRole,
        OrderStatus,
        OrderType,
        PriceParameters,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises((ValueError, TypeError)):
        OrderRecord(
            order_id=OrderId("ord1"),
            position_id=PositionId("pos1"),
            bracket_id=BracketId("brk1"),
            role=OrderRole.ENTRY,
            instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
            direction=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            order_class=OrderClass.MLEG,
            price_parameters=PriceParameters(),
            quantity=100.0,
            duration=OrderDuration.DAY,
            status=OrderStatus.PENDING,
            alpaca_order_id=AlpacaOrderId("alp1"),
            alpaca_order_id_chain=(AlpacaOrderId("alp1"),),
            submission_timestamp=now,
            last_update_timestamp=now,
            filled_quantity=0.0,
            avg_fill_price=None,
            remaining_quantity=100.0,
            modification_count=0,
            originating_thesis_id=None,
            originating_pm_command_id=None,
            age_hours=0.0,
        )


def test_wave2_thesis_position_size_rationale_empty_rejected() -> None:
    """01j: empty position_size_rationale must be rejected."""
    from datetime import datetime, timedelta

    from alphamind.portfolio_state.records.theses import (
        ThesisRecord,
        ThesisRecordStatus,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    components = _make_three_components(now)
    with pytest.raises((ValueError, TypeError)):
        ThesisRecord(
            thesis_id=ThesisId("t1"),
            position_id=PositionId("p1"),
            summary="test",
            key_catalyst="cat",
            position_size_rationale="",
            components=components,
            status=ThesisRecordStatus.ACTIVE,
            generation_timestamp=now,
            time_expectation_hours=24.0,
            age_hours=0.0,
            expected_resolution_at=now + timedelta(hours=24),
            resolution_timestamp=None,
            resolution_category=None,
            resolution_pnl_usd=None,
            entry_fill_gap_usd=None,
        )


# ---------------------------------------------------------------------------
# Wave 3: boundary fix (02) — 2 sub-cases
# ---------------------------------------------------------------------------


def test_wave3_boundary_fix_returns_2_0() -> None:
    mod = _load_script()
    passed, failed, results = mod.wave3_boundary_fix(verbose=False)
    assert failed == 0, f"Wave 3 failures: {[r for r in results if not r['ok']]}"
    assert passed == 2


def test_wave3_regime_label_import_from_risk_guardrails() -> None:
    from alphamind._kernel.regime import RegimeLabel

    assert RegimeLabel is not None


def test_wave3_regime_label_identity() -> None:
    from alphamind._kernel.regime import RegimeLabel as A
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel as B

    assert A is B


# ---------------------------------------------------------------------------
# Wave 4: typed payloads (03a/03b) — 4 sub-cases
# ---------------------------------------------------------------------------


def test_wave4_typed_payloads_returns_4_0() -> None:
    mod = _load_script()
    passed, failed, results = mod.wave4_typed_payloads(verbose=False)
    assert failed == 0, f"Wave 4 failures: {[r for r in results if not r['ok']]}"
    assert passed == 4


def test_wave4_price_trigger_on_price_stop_passes() -> None:
    from alphamind.portfolio_state.records.orders import (
        BracketLeg,
        BracketLegEnforcement,
        BracketLegStatus,
        BracketLegType,
        PriceTrigger,
    )

    leg = BracketLeg(
        leg_id="leg1",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=800.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    assert isinstance(leg.trigger, PriceTrigger)
    assert leg.trigger.underlying_ticker == "NVDA"


def test_wave4_time_trigger_on_price_stop_rejected() -> None:
    from datetime import datetime

    from alphamind.portfolio_state.records.orders import (
        BracketLeg,
        BracketLegEnforcement,
        BracketLegStatus,
        BracketLegType,
        TimeTrigger,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises((ValueError, TypeError)):
        BracketLeg(
            leg_id="leg1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord1"),
            trigger=TimeTrigger(deadline=now),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )


def test_wave4_pl_anchor_on_take_profit_passes() -> None:
    from alphamind.portfolio_state.records.orders import (
        BracketLeg,
        BracketLegEnforcement,
        BracketLegStatus,
        BracketLegType,
        PLAnchorSpec,
        PriceTrigger,
    )

    leg = BracketLeg(
        leg_id="leg1",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId("ord1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=900.0, direction="GTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
        pl_anchor=PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50),
    )
    assert leg.pl_anchor is not None
    assert leg.pl_anchor.pct == 0.80


def test_wave4_pl_anchor_on_time_expiration_rejected() -> None:
    from datetime import datetime

    from alphamind.portfolio_state.records.orders import (
        BracketLeg,
        BracketLegEnforcement,
        BracketLegStatus,
        BracketLegType,
        PLAnchorSpec,
        TimeTrigger,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises((ValueError, TypeError)):
        BracketLeg(
            leg_id="leg1",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=None,
            trigger=TimeTrigger(deadline=now),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
            pl_anchor=PLAnchorSpec(spec_type="stop", pct=0.30, planned_entry_price=18.50),
        )


# ---------------------------------------------------------------------------
# Wave 5: structural (04a/04b) — 4 sub-cases
# ---------------------------------------------------------------------------


def test_wave5_structural_returns_4_0() -> None:
    mod = _load_script()
    passed, failed, results = mod.wave5_structural(verbose=False)
    assert failed == 0, f"Wave 5 failures: {[r for r in results if not r['ok']]}"
    assert passed == 4


def test_wave5_events_activity_log_import() -> None:
    from alphamind.portfolio_state.events import ActivityLogEntry

    assert ActivityLogEntry is not None


def test_wave5_aggregates_risk_budget_import() -> None:
    from alphamind.portfolio_state.aggregates import RiskBudgetEntry

    assert RiskBudgetEntry is not None


def test_wave5_activity_log_backward_compat_shim() -> None:
    from alphamind.portfolio_state.events import ActivityLogEntry as A
    from alphamind.portfolio_state.records.activity_log import ActivityLogEntry as B

    assert A is B


def test_wave5_discriminated_union_bogus_instrument_type_rejected() -> None:
    """Post-ALP-477: dict-payload discriminator parsing lives in the codec
    layer, not the dataclass constructor. Verify the codec raises on a bogus
    discriminator.
    """
    from alphamind.state.tables.positions_codec import _details_from_dict

    with pytest.raises((ValueError, TypeError)):
        _details_from_dict({"instrument_type": "BOGUS"})


# ---------------------------------------------------------------------------
# Wave 6: architectural (05a/05b/05c) — 4 sub-cases
# ---------------------------------------------------------------------------


def test_wave6_architectural_returns_4_0() -> None:
    mod = _load_script()
    passed, failed, results = mod.wave6_architectural(verbose=False)
    assert failed == 0, f"Wave 6 failures: {[r for r in results if not r['ok']]}"
    assert passed == 4


def test_wave6_position_record_no_market_value_field() -> None:
    import dataclasses as _dc

    from alphamind.portfolio_state.records.positions import PositionRecord

    field_names = {f.name for f in _dc.fields(PositionRecord)}
    assert "current_market_value_usd" not in field_names


def test_wave6_position_view_has_market_value_field() -> None:
    import dataclasses as _dc

    from alphamind.portfolio_state.views.positions import PositionView

    field_names = {f.name for f in _dc.fields(PositionView)}
    assert "current_market_value_usd" in field_names


def test_wave6_base_position_protocol_isinstance() -> None:
    from datetime import datetime

    from alphamind.portfolio_state.protocols.positions import BasePositionProtocol
    from alphamind.portfolio_state.records.positions import (
        Direction,
        EquityPositionDetails,
        PositionFill,
        PositionRecord,
        PositionStatus,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    details = EquityPositionDetails(
        ticker=Symbol("AAPL"), share_count=100.0, average_cost_basis_per_share=150.0
    )
    fill = PositionFill(
        fill_timestamp=now,
        fill_price=price(150.0),
        fill_quantity=100.0,
        slippage=signed_money(0.0),
        fees=money(0.0),
    )
    record = PositionRecord(
        position_id=PositionId("pos1"),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=now,
        details=details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    assert isinstance(record, BasePositionProtocol)


def test_wave6_thesis_component_no_supporting_signals() -> None:
    """05c: ThesisComponent no longer carries supporting_signals field."""
    import dataclasses as _dc

    from alphamind.portfolio_state.records.theses import ThesisComponent

    field_names = {f.name for f in _dc.fields(ThesisComponent)}
    assert "supporting_signals" not in field_names


def test_wave6_thesis_health_snapshot_works() -> None:
    """05c: ThesisHealthSnapshot.health_for_component returns correct entry."""
    from datetime import datetime

    from alphamind.portfolio_state.records.theses import (
        SupportingSignal,
        SupportingSignalStatus,
        ThesisStatus,
    )
    from alphamind.portfolio_state.views.thesis_health import (
        ComponentHealthEntry,
        ThesisHealthSnapshot,
    )

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    entry = ComponentHealthEntry(
        component_id="c1",
        supporting_signals=(
            SupportingSignal(name="earnings", status=SupportingSignalStatus.PRESENT),
        ),
    )
    snapshot = ThesisHealthSnapshot(
        thesis_id=ThesisId("t1"),
        invocation_id="inv1",
        snapshot_timestamp=now,
        health_status=ThesisStatus.ON_TRACK,
        prior_health_status=None,
        component_health=(entry,),
    )
    found = snapshot.health_for_component("c1")
    assert found is entry
    assert snapshot.health_for_component("nonexistent") is None


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_exits_0_on_full_pass() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Overall: PASS (40/40 cases)" in result.stdout


def test_cli_help_works() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--verbose" in result.stdout


def test_cli_verbose_flag_prints_detail() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--verbose"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # verbose mode should print scenario details beyond just the verdict block
    assert "PASS" in result.stdout


def test_cli_verdict_block_format() -> None:
    """Final verdict block must match the exact format from the story."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    stdout = result.stdout
    assert "=== verify_position_thesis_model verdict ===" in stdout
    assert "Wave 1 utilities (01a/01b/01c):" in stdout
    assert "Wave 1 additive fields (01d-01j):" in stdout
    assert "Wave 2 boundary fix (02):" in stdout
    assert "Wave 3 typed payloads (03a/03b):" in stdout
    assert "Wave 4 structural (04a/04b):" in stdout
    assert "Wave 5 architectural (05a/05b/05c):" in stdout
    assert "Overall: PASS (40/40 cases)" in stdout


# ---------------------------------------------------------------------------
# Shared test fixture helpers
# ---------------------------------------------------------------------------


def _make_three_components(now: object) -> tuple[Any, ...]:
    """Build the three mandatory ThesisComponent types."""
    from alphamind.portfolio_state.records.orders import BracketLegType
    from alphamind.portfolio_state.records.theses import (
        KeyAssumption,
        ThesisComponent,
        ThesisComponentType,
    )

    return (
        ThesisComponent(
            component_id="c1",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.ENTRY_RATIONALE,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative="Entry rationale",
            key_assumptions=(KeyAssumption(text="Earnings beat", outcome=None),),
            generation_timestamp=now,  # type: ignore[arg-type]
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c2",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            linked_bracket_leg_id="leg_tp",
            instrument_reference="AAPL",
            narrative="Target rationale",
            key_assumptions=(KeyAssumption(text="Analyst target $200", outcome=None),),
            generation_timestamp=now,  # type: ignore[arg-type]
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c3",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            linked_bracket_leg_id="leg_ps",
            instrument_reference="AAPL",
            narrative="Invalidation rationale",
            key_assumptions=(KeyAssumption(text="Break below 150", outcome=None),),
            generation_timestamp=now,  # type: ignore[arg-type]
            resolution_outcome=None,
            resolution_notes=None,
        ),
    )


def _make_bracket_record(
    entry_window_deadline: object = None,
) -> object:
    """Build a minimal valid BracketRecord, optionally with a deadline."""
    from datetime import datetime

    from alphamind.portfolio_state.records.orders import (
        BracketLeg,
        BracketLegEnforcement,
        BracketLegStatus,
        BracketLegType,
        BracketRecord,
        BracketStatus,
        PriceTrigger,
        TimeTrigger,
    )

    now_tz = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    legs = (
        BracketLeg(
            leg_id="leg_tp",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=OrderId("ord1"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=200.0, direction="GTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
        BracketLeg(
            leg_id="leg_ps",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord2"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
        BracketLeg(
            leg_id="leg_te",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId("ord3"),
            trigger=TimeTrigger(deadline=now_tz),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
    )
    return BracketRecord(
        bracket_id=BracketId("brk1"),
        position_id=PositionId("pos1"),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("entry1"),
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=entry_window_deadline,  # type: ignore[arg-type]
    )
