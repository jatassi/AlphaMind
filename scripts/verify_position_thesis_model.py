"""Offline E2E verification for the position-thesis-model work tree (ALP-336).

Exercises every deliverable from the work tree's 15 sub-stories — the three
pure-function utilities (01a/01b/01c), the seven additive-field changes
(01d-01j), the boundary fix (02), the typed payloads (03a/03b), the structural
reorg (04a/04b), and the architectural splits (05a/05b/05c).

No SDK calls; no DB reads. Target wall-clock: under 10 seconds.

Usage:
  uv run python scripts/verify_position_thesis_model.py
  uv run python scripts/verify_position_thesis_model.py --verbose

See scripts/RUNBOOK_position_thesis_model.md for the operator runbook.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import UTC, date, datetime, timedelta
from typing import Any

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.scripts._stdio import configure_utf8_stdio

# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _ok(label: str) -> dict[str, Any]:
    return {"label": label, "ok": True, "detail": None}


def _fail(label: str) -> dict[str, Any]:
    return {"label": label, "ok": False, "detail": traceback.format_exc()}


def _expect_raises(label: str, exc_type: type[Exception], fn: Any) -> dict[str, Any]:
    from pydantic import ValidationError

    try:
        fn()
    except exc_type:
        return _ok(label)
    except (ValueError, TypeError):
        # Post-ALP-477: frozen-dataclass records raise ValueError/TypeError
        # instead of Pydantic ``ValidationError``. Accept the swap so verify
        # scripts don't need a per-case rewrite.
        if exc_type is ValidationError:
            return _ok(label)
        return _fail(label)
    except Exception:
        return _fail(label)
    no_raise_msg = f"Expected {exc_type.__name__} but no exception was raised"
    return {"label": label, "ok": False, "detail": no_raise_msg}


def _run_case(label: str, fn: Any) -> dict[str, Any]:
    try:
        fn()
        return _ok(label)
    except Exception:
        return _fail(label)


# ---------------------------------------------------------------------------
# Shared fixture factories
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


def _make_three_components(thesis_id: str = "t1") -> tuple[Any, ...]:
    from alphamind.portfolio_state.records.orders import BracketLegType
    from alphamind.portfolio_state.records.theses import (
        KeyAssumption,
        ThesisComponent,
        ThesisComponentType,
    )

    now = _now_utc()
    return (
        ThesisComponent(
            component_id="c1",
            thesis_id=ThesisId(thesis_id),
            component_type=ThesisComponentType.ENTRY_RATIONALE,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative="Entry rationale narrative",
            key_assumptions=(KeyAssumption(text="Earnings beat expected", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c2",
            thesis_id=ThesisId(thesis_id),
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            linked_bracket_leg_id="leg_tp",
            instrument_reference="AAPL",
            narrative="Target rationale narrative",
            key_assumptions=(KeyAssumption(text="Analyst target $200", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c3",
            thesis_id=ThesisId(thesis_id),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            linked_bracket_leg_id="leg_ps",
            instrument_reference="AAPL",
            narrative="Invalidation rationale narrative",
            key_assumptions=(KeyAssumption(text="Break below $150", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
    )


def _make_covered_bracket_and_thesis() -> tuple[Any, Any]:
    """Return (BracketRecord, ThesisRecord) where every leg is covered."""
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
    from alphamind.portfolio_state.records.theses import (
        KeyAssumption,
        ThesisComponent,
        ThesisComponentType,
        ThesisRecord,
        ThesisRecordStatus,
    )

    now = _now_utc()
    legs = (
        BracketLeg(
            leg_id="leg_tp",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=OrderId("ord_tp"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=200.0, direction="GTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
        BracketLeg(
            leg_id="leg_ps",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord_ps"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
        BracketLeg(
            leg_id="leg_te",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId("ord_te"),
            trigger=TimeTrigger(deadline=now),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
    )
    bracket = BracketRecord(
        bracket_id=BracketId("brk1"),
        position_id=PositionId("pos1"),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("entry1"),
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )
    components = (
        ThesisComponent(
            component_id="c_entry",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.ENTRY_RATIONALE,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative="Entry rationale",
            key_assumptions=(KeyAssumption(text="Catalyst present", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c_tp",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            linked_bracket_leg_id="leg_tp",
            instrument_reference="AAPL",
            narrative="Target rationale",
            key_assumptions=(KeyAssumption(text="Analyst target $200", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c_ps",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            linked_bracket_leg_id="leg_ps",
            instrument_reference="AAPL",
            narrative="Price-stop invalidation",
            key_assumptions=(KeyAssumption(text="Break below $150", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c_te",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TIME_EXPIRATION,
            linked_bracket_leg_id="leg_te",
            instrument_reference="AAPL",
            narrative="Time-expiry invalidation",
            key_assumptions=(KeyAssumption(text="Thesis expires in 48h", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
    )
    thesis = ThesisRecord(
        thesis_id=ThesisId("t1"),
        position_id=PositionId("pos1"),
        summary="AAPL earnings play",
        key_catalyst="Q2 earnings beat",
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
    return bracket, thesis


def _make_uncovered_bracket_and_thesis() -> tuple[Any, Any]:
    """Return (BracketRecord, ThesisRecord) where one leg is uncovered."""
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
    from alphamind.portfolio_state.records.theses import (
        KeyAssumption,
        ThesisComponent,
        ThesisComponentType,
        ThesisRecord,
        ThesisRecordStatus,
    )

    now = _now_utc()
    legs = (
        BracketLeg(
            leg_id="leg_tp",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=OrderId("ord_tp"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=200.0, direction="GTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
        BracketLeg(
            leg_id="leg_ps",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord_ps"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
        BracketLeg(
            leg_id="leg_te",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId("ord_te"),
            trigger=TimeTrigger(deadline=now),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        ),
    )
    bracket = BracketRecord(
        bracket_id=BracketId("brk1"),
        position_id=PositionId("pos1"),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("entry1"),
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )
    # Thesis only covers take-profit; price-stop and time-expiry are uncovered
    components = (
        ThesisComponent(
            component_id="c_entry",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.ENTRY_RATIONALE,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative="Entry rationale",
            key_assumptions=(KeyAssumption(text="Catalyst present", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id="c_tp",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            linked_bracket_leg_id="leg_tp",
            instrument_reference="AAPL",
            narrative="Target rationale",
            key_assumptions=(KeyAssumption(text="Analyst target $200", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
        # MISSING: invalidation rationale for leg_ps and leg_te
        ThesisComponent(
            component_id="c_te_wrong",
            thesis_id=ThesisId("t1"),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,  # Not linked to any leg
            instrument_reference="AAPL",
            narrative="Generic invalidation",
            key_assumptions=(KeyAssumption(text="Generic stop", outcome=None),),
            generation_timestamp=now,
            resolution_outcome=None,
            resolution_notes=None,
        ),
    )
    thesis = ThesisRecord(
        thesis_id=ThesisId("t1"),
        position_id=PositionId("pos1"),
        summary="AAPL earnings play",
        key_catalyst="Q2 earnings beat",
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
    return bracket, thesis


def _resolution_classifier_cases() -> list[tuple[Any, ...]]:
    """Return 5 canonical (component_outcomes, pnl, exit_method, expected_category) tuples."""
    from alphamind.portfolio_state.events.activity_log import PositionExitMethod
    from alphamind.portfolio_state.records.theses import (
        ThesisComponentOutcome,
        ThesisResolutionCategory,
    )

    return [
        # 1. VALIDATED: most components VALIDATED, P/L > 0
        (
            (
                ThesisComponentOutcome.VALIDATED,
                ThesisComponentOutcome.VALIDATED,
                ThesisComponentOutcome.WRONG,
            ),
            500.0,
            PositionExitMethod.TARGET_REACHED,
            ThesisResolutionCategory.VALIDATED,
        ),
        # 2. PROFITABLE_BUT_WRONG: P/L > 0, most WRONG
        (
            (
                ThesisComponentOutcome.WRONG,
                ThesisComponentOutcome.WRONG,
                ThesisComponentOutcome.VALIDATED,
            ),
            200.0,
            PositionExitMethod.PM_DECISION,
            ThesisResolutionCategory.PROFITABLE_BUT_WRONG,
        ),
        # 3. INVALIDATED_STOPPED_CORRECTLY: P/L <= 0, mechanical exit
        (
            (ThesisComponentOutcome.WRONG, ThesisComponentOutcome.WRONG),
            -150.0,
            PositionExitMethod.STOP_TRIGGERED,
            ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY,
        ),
        # 4. INVALIDATED_WRONG_ON_EXIT: P/L <= 0, PM decision exit
        (
            (ThesisComponentOutcome.WRONG, ThesisComponentOutcome.INCONCLUSIVE),
            -300.0,
            PositionExitMethod.PM_DECISION,
            ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT,
        ),
        # 5. ValueError on empty outcomes
        # (handled separately via _expect_raises in the wave function)
        # Include a tie case at P/L > 0: PROFITABLE_BUT_WRONG (tie → wrong wins)
        (
            (ThesisComponentOutcome.VALIDATED, ThesisComponentOutcome.WRONG),
            100.0,
            PositionExitMethod.TARGET_REACHED,
            ThesisResolutionCategory.PROFITABLE_BUT_WRONG,
        ),
    ]


def _make_strategy_leg(
    leg_id: str,
    direction: str,
    contract_type: str,
    strike: float,
    count: float = 1.0,
    multiplier: float = 100.0,
    exp_date: date | None = None,
) -> Any:
    from alphamind.portfolio_state.records.positions import (
        Direction,
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
        StrategyLeg,
    )

    if exp_date is None:
        exp_date = date(2026, 6, 20)
    opt = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=strike,
        expiration_date=exp_date,
        contract_type=OptionContractType(contract_type),
        contract_count=count,
        contract_multiplier=multiplier,
        premium_paid_per_contract=2.0,
        greeks=OptionGreeks(delta=0.4, gamma=0.02, theta=-0.05, vega=0.1),
    )
    return StrategyLeg(
        leg_id=leg_id,
        direction=Direction(direction),
        options=opt,
    )


def _canonical_strategy_fixtures() -> list[tuple[Any, ...]]:
    """Return 5 canonical strategy structures and their expected payoff metrics.

    Each entry: (label, legs, net_premium_usd, expected_max_profit, expected_max_loss,
    expected_breakeven_count).
    """
    exp = date(2026, 6, 20)

    def make_leg(leg_id: str, direction: str, contract_type: str, strike: float) -> Any:
        return _make_strategy_leg(leg_id, direction, contract_type, strike, exp_date=exp)

    # 1. Long bull call vertical: BUY 100C + SELL 105C, net debit = $2 x 100 = $200
    # Expected: max_profit=300, max_loss=-200, 1 breakeven
    bull_call_legs = (
        make_leg("l1", "LONG", "CALL", 100.0),
        make_leg("l2", "SHORT", "CALL", 105.0),
    )
    bull_call_net_premium = 200.0

    # 2. Long bear put vertical: BUY 105P + SELL 100P, net debit = $2 x 100 = $200
    # Expected: max_profit=300, max_loss=-200, 1 breakeven
    bear_put_legs = (
        make_leg("l1", "LONG", "PUT", 105.0),
        make_leg("l2", "SHORT", "PUT", 100.0),
    )
    bear_put_net_premium = 200.0

    # 3. Iron condor: SELL 95P + BUY 90P + SELL 105C + BUY 110C, net credit = -$150
    # Expected: max_profit=150, max_loss=-350, 2 breakevens
    iron_condor_legs = (
        make_leg("l1", "SHORT", "PUT", 95.0),
        make_leg("l2", "LONG", "PUT", 90.0),
        make_leg("l3", "SHORT", "CALL", 105.0),
        make_leg("l4", "LONG", "CALL", 110.0),
    )
    iron_condor_net_premium = -150.0

    # 4. Long straddle: BUY 100C + BUY 100P, net debit = $4 x 100 = $400
    # Expected: max_profit=inf, max_loss=-400, 2 breakevens
    straddle_legs = (
        make_leg("l1", "LONG", "CALL", 100.0),
        make_leg("l2", "LONG", "PUT", 100.0),
    )
    straddle_net_premium = 400.0

    # 5. Long strangle: BUY 95P + BUY 105C, net debit = $4 x 100 = $400
    # Expected: max_profit=inf, max_loss=-400, 2 breakevens
    strangle_legs = (
        make_leg("l1", "LONG", "PUT", 95.0),
        make_leg("l2", "LONG", "CALL", 105.0),
    )
    strangle_net_premium = 400.0

    return [
        ("long_bull_call_vertical", bull_call_legs, bull_call_net_premium, 300.0, -200.0, 1),
        ("long_bear_put_vertical", bear_put_legs, bear_put_net_premium, 300.0, -200.0, 1),
        ("iron_condor", iron_condor_legs, iron_condor_net_premium, 150.0, -350.0, 2),
        ("long_straddle", straddle_legs, straddle_net_premium, float("inf"), -400.0, 2),
        ("long_strangle", strangle_legs, strangle_net_premium, float("inf"), -400.0, 2),
    ]


# ---------------------------------------------------------------------------
# Wave 1 sub-helpers (extracted to keep wave1_utilities under complexity limit)
# ---------------------------------------------------------------------------


def _wave1_coverage_cases() -> list[dict[str, Any]]:
    """01a — 2 cases: bracket-thesis coverage validator."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    results: list[dict[str, Any]] = []
    bracket, thesis = _make_covered_bracket_and_thesis()
    results.append(
        _run_case("01a-coverage-valid", lambda: validate_bracket_thesis_coverage(bracket, thesis))
    )

    bracket_broken, thesis_broken = _make_uncovered_bracket_and_thesis()
    results.append(
        _expect_raises(
            "01a-coverage-broken-raises",
            ValueError,
            lambda: validate_bracket_thesis_coverage(bracket_broken, thesis_broken),
        )
    )
    return results


def _wave1_classifier_cases() -> list[dict[str, Any]]:
    """01b — 5 cases: thesis resolution classifier."""
    from alphamind.execution.thesis_model import classify_thesis_resolution
    from alphamind.portfolio_state.events.activity_log import PositionExitMethod

    results: list[dict[str, Any]] = []
    classifier_cases = _resolution_classifier_cases()

    for component_outcomes, pnl, exit_method, expected in classifier_cases[:4]:
        label = f"01b-resolution-{expected.value.lower()}"

        def _make_check(co: tuple[Any, ...], p: float, em: Any, exp: Any) -> Any:
            def _check() -> None:
                actual = classify_thesis_resolution(co, p, em)
                assert actual == exp, f"Expected {exp}, got {actual}"

            return _check

        check_fn = _make_check(component_outcomes, pnl, exit_method, expected)
        results.append(_run_case(label, check_fn))

    results.append(
        _expect_raises(
            "01b-resolution-empty-raises",
            ValueError,
            lambda: classify_thesis_resolution((), 100.0, PositionExitMethod.PM_DECISION),
        )
    )
    return results


def _wave1_payoff_cases() -> list[dict[str, Any]]:
    """01c — 5 cases: strategy payoff utilities (one per strategy)."""
    from alphamind.execution.position_model import (
        compute_strategy_breakeven_levels,
        compute_strategy_max_loss_usd,
        compute_strategy_max_profit_usd,
    )

    results: list[dict[str, Any]] = []
    strategies = _canonical_strategy_fixtures()
    (
        (_lbl_bc, bc_legs, bc_prem, _mp, _ml, _be),
        (_lbl_bp, bp_legs, bp_prem, _mp2, _ml2, _be2),
        (_lbl_ic, ic_legs, ic_prem, _mp3, _ml3, _be3),
        (_lbl_st, st_legs, st_prem, _mp4, _ml4, _be4),
        (_lbl_sg, sg_legs, sg_prem, _mp5, _ml5, _be5),
    ) = strategies

    def _check_bull_call() -> None:
        mp = compute_strategy_max_profit_usd(bc_legs, bc_prem)
        ml = compute_strategy_max_loss_usd(bc_legs, bc_prem)
        be = compute_strategy_breakeven_levels(bc_legs, bc_prem)
        assert abs(mp - 300.0) < 1e-6, f"max_profit={mp}"
        assert abs(ml - (-200.0)) < 1e-6, f"max_loss={ml}"
        assert len(be) == 1, f"breakevens={be}"

    results.append(_run_case("01c-payoff-bull-call-vertical", _check_bull_call))

    def _check_bear_put() -> None:
        mp = compute_strategy_max_profit_usd(bp_legs, bp_prem)
        ml = compute_strategy_max_loss_usd(bp_legs, bp_prem)
        be = compute_strategy_breakeven_levels(bp_legs, bp_prem)
        assert abs(mp - 300.0) < 1e-6, f"bear_put max_profit={mp}"
        assert abs(ml - (-200.0)) < 1e-6, f"bear_put max_loss={ml}"
        assert len(be) == 1, f"bear_put breakevens={be}"

    results.append(_run_case("01c-payoff-bear-put-vertical", _check_bear_put))

    def _check_iron_condor() -> None:
        mp = compute_strategy_max_profit_usd(ic_legs, ic_prem)
        ml = compute_strategy_max_loss_usd(ic_legs, ic_prem)
        be = compute_strategy_breakeven_levels(ic_legs, ic_prem)
        assert mp == 150.0, f"iron_condor max_profit={mp}"
        assert ml == -350.0, f"iron_condor max_loss={ml}"
        assert len(be) == 2, f"iron_condor breakevens={be}"

    results.append(_run_case("01c-payoff-iron-condor", _check_iron_condor))

    def _check_straddle() -> None:
        mp = compute_strategy_max_profit_usd(st_legs, st_prem)
        be = compute_strategy_breakeven_levels(st_legs, st_prem)
        assert mp == float("inf"), f"expected inf, got {mp}"
        assert len(be) == 2, f"expected 2 breakevens, got {be}"

    results.append(_run_case("01c-payoff-straddle", _check_straddle))

    def _check_strangle() -> None:
        mp = compute_strategy_max_profit_usd(sg_legs, sg_prem)
        ml = compute_strategy_max_loss_usd(sg_legs, sg_prem)
        be = compute_strategy_breakeven_levels(sg_legs, sg_prem)
        assert mp == float("inf"), f"strangle max_profit={mp}"
        assert abs(ml - (-400.0)) < 1e-6, f"strangle max_loss={ml}"
        assert len(be) == 2, f"strangle breakevens={be}"

    results.append(_run_case("01c-payoff-strangle", _check_strangle))

    return results


# ---------------------------------------------------------------------------
# Wave 1: Utilities (01a / 01b / 01c) — 12 cases
# ---------------------------------------------------------------------------


def wave1_utilities(verbose: bool = False) -> tuple[int, int, list[dict[str, Any]]]:
    """Exercises ALP-333 (01a), ALP-334 (01b), ALP-335 (01c)."""
    results = _wave1_coverage_cases() + _wave1_classifier_cases() + _wave1_payoff_cases()
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    if verbose:
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            if not r["ok"] and r["detail"]:
                print(f"         {r['detail']}")
    return passed, failed, results


# ---------------------------------------------------------------------------
# Wave 2 sub-helpers (extracted to keep wave2_additive_fields under complexity limit)
# ---------------------------------------------------------------------------


def _wave2_01d_time_expectation(now: datetime) -> list[dict[str, Any]]:
    """01d — 2 cases: time_expectation_hours retype to float."""
    from pydantic import ValidationError

    from alphamind.portfolio_state.records.theses import ThesisRecord, ThesisRecordStatus

    components = _make_three_components()

    def _check_float_parses() -> None:
        r = ThesisRecord(
            thesis_id=ThesisId("t1"),
            position_id=PositionId("p1"),
            summary="test summary",
            key_catalyst="key catalyst",
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
        assert isinstance(r.time_expectation_hours, float)

    def _check_negative_hours_rejected() -> None:
        with _raises(ValidationError):
            ThesisRecord(
                thesis_id=ThesisId("t1"),
                position_id=PositionId("p1"),
                summary="test summary",
                key_catalyst="key catalyst",
                components=components,
                status=ThesisRecordStatus.ACTIVE,
                generation_timestamp=now,
                time_expectation_hours=-1.0,  # invalid: must be > 0
                age_hours=0.0,
                expected_resolution_at=now + timedelta(hours=48),
                resolution_timestamp=None,
                resolution_category=None,
                resolution_pnl_usd=None,
                entry_fill_gap_usd=None,
            )

    return [
        _run_case("01d-time_expectation_float_parses", _check_float_parses),
        _run_case("01d-time_expectation_negative_rejected", _check_negative_hours_rejected),
    ]


def _wave2_01e_position_weight(now: datetime) -> list[dict[str, Any]]:
    """01e — 2 cases: negative position_weight_pct allowed."""
    from pydantic import ValidationError

    from alphamind.portfolio_state.records.positions import (
        Direction,
        EquityPositionDetails,
        PositionFill,
        PositionRecord,
        PositionStatus,
    )
    from alphamind.portfolio_state.views.positions import PositionView

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

    def _check_negative_weight() -> None:
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

    def _check_inf_weight_rejected() -> None:
        with _raises(ValidationError):
            PositionView(
                record=record,
                current_market_value_usd=signed_money(10000.0),
                unrealized_pnl_usd=signed_money(200.0),
                unrealized_pnl_pct=0.02,
                position_weight_pct=float("inf"),
                position_age_hours=2.0,
                notional_exposure_usd=money(10000.0),
                delta_adjusted_exposure_usd=signed_money(0.0),
                distance_to_target_usd=None,
                distance_to_stop_usd=None,
                risk_reward_at_current=None,
            )

    return [
        _run_case("01e-negative_position_weight_pct_allowed", _check_negative_weight),
        _run_case("01e-inf_position_weight_pct_rejected", _check_inf_weight_rejected),
    ]


def _wave2_01f_option_greeks(now: datetime) -> list[dict[str, Any]]:
    """01f — 2 cases: OptionGreeks freshness metadata."""
    from pydantic import ValidationError

    from alphamind.portfolio_state.records.positions import OptionGreeks

    def _check_greeks_valid() -> None:
        g = OptionGreeks(
            delta=0.4, gamma=0.02, theta=-0.05, vega=0.1, as_of_timestamp=now, iv_used=0.45
        )
        assert g.iv_used == 0.45
        assert g.as_of_timestamp == now

    def _check_greeks_zero_iv_rejected() -> None:
        with _raises(ValidationError):
            OptionGreeks(
                delta=0.4, gamma=0.02, theta=-0.05, vega=0.1, as_of_timestamp=now, iv_used=0.0
            )

    return [
        _run_case("01f-option_greeks_freshness_valid", _check_greeks_valid),
        _run_case("01f-option_greeks_zero_iv_rejected", _check_greeks_zero_iv_rejected),
    ]


def _wave2_01g_position_fill(now: datetime) -> list[dict[str, Any]]:
    """01g — 2 cases: PositionFill live_execution_estimate."""
    from pydantic import ValidationError

    from alphamind.portfolio_state.records.positions import LiveExecutionEstimate, PositionFill

    def _check_fill_with_estimate() -> None:
        est = LiveExecutionEstimate(
            estimated_spread_usd=money(0.05),
            estimated_impact_usd=money(0.02),
            estimated_regulatory_fees_usd=money(0.01),
            live_adjusted_fill_price=price(149.92),
        )
        f = PositionFill(
            fill_timestamp=now,
            fill_price=price(150.0),
            fill_quantity=100.0,
            slippage=signed_money(0.0),
            fees=money(1.0),
            live_execution_estimate=est,
        )
        assert f.live_execution_estimate is not None
        assert f.live_execution_estimate.estimated_spread_usd == money(0.05)

    def _check_fill_negative_fees_rejected() -> None:
        with _raises(ValidationError):
            PositionFill(
                fill_timestamp=now,
                fill_price=price(150.0),
                fill_quantity=100.0,
                slippage=signed_money(0.0),
                fees=money(-1.0),
            )

    return [
        _run_case("01g-position_fill_live_estimate_valid", _check_fill_with_estimate),
        _run_case("01g-position_fill_negative_fees_rejected", _check_fill_negative_fees_rejected),
    ]


def _wave2_01h_bracket_deadline(now: datetime) -> list[dict[str, Any]]:
    """01h — 2 cases: BracketRecord entry_window_deadline."""
    from pydantic import ValidationError

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

    def _make_three_legs() -> tuple[Any, ...]:
        return (
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
                trigger=TimeTrigger(deadline=now),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.PENDING_ACTIVATION,
            ),
        )

    def _check_deadline_tz_aware() -> None:
        br = BracketRecord(
            bracket_id=BracketId("brk1"),
            position_id=PositionId("pos1"),
            status=BracketStatus.PENDING_ENTRY,
            entry_order_id=OrderId("entry1"),
            protective_legs=_make_three_legs(),
            modification_history=(),
            corporate_action_cancellation_reason=None,
            entry_window_deadline=now,
        )
        assert br.entry_window_deadline == now

    def _check_deadline_naive_rejected() -> None:
        naive_dt = datetime(2026, 5, 2, 12, 0, 0)  # noqa: DTZ001 — intentionally naive to test rejection
        with _raises(ValidationError):
            BracketRecord(
                bracket_id=BracketId("brk1"),
                position_id=PositionId("pos1"),
                status=BracketStatus.PENDING_ENTRY,
                entry_order_id=OrderId("entry1"),
                protective_legs=_make_three_legs(),
                modification_history=(),
                corporate_action_cancellation_reason=None,
                entry_window_deadline=naive_dt,
            )

    return [
        _run_case("01h-bracket_deadline_tz_aware_valid", _check_deadline_tz_aware),
        _run_case("01h-bracket_deadline_naive_rejected", _check_deadline_naive_rejected),
    ]


def _wave2_01ij_order_and_rationale(now: datetime) -> list[dict[str, Any]]:
    """01i + 01j — 4 cases: OrderClass MLEG validator + position_size_rationale."""
    from pydantic import ValidationError

    from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
    from alphamind.portfolio_state.records.orders import (
        EquityInstrumentSpec,
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
    from alphamind.portfolio_state.records.positions import OptionContractType
    from alphamind.portfolio_state.records.theses import ThesisRecord, ThesisRecordStatus

    components = _make_three_components()

    def _check_mleg_strategy_valid() -> None:
        opt_spec = OptionsInstrumentSpec(
            underlying=Symbol("NVDA"),
            strike=500.0,
            expiration=date(2026, 6, 20),
            contract_type=OptionContractType.CALL,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        )
        OrderRecord(
            order_id=OrderId("ord1"),
            position_id=PositionId("pos1"),
            bracket_id=BracketId("brk1"),
            role=OrderRole.ENTRY,
            instrument_spec=StrategyInstrumentSpec(legs=(opt_spec,)),
            direction=OrderDirection.BUY_TO_OPEN,
            order_type=OrderType.MARKET,
            order_class=OrderClass.MLEG,
            price_parameters=PriceParameters(),
            quantity=1.0,
            duration=OrderDuration.DAY,
            status=OrderStatus.PENDING,
            alpaca_order_id=AlpacaOrderId("alp1"),
            alpaca_order_id_chain=(AlpacaOrderId("alp1"),),
            submission_timestamp=now,
            last_update_timestamp=now,
            filled_quantity=0.0,
            avg_fill_price=None,
            remaining_quantity=1.0,
            modification_count=0,
            originating_thesis_id=None,
            originating_pm_command_id=None,
            age_hours=0.0,
        )

    def _check_mleg_equity_rejected() -> None:
        with _raises(ValidationError):
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

    def _check_rationale_valid() -> None:
        r = ThesisRecord(
            thesis_id=ThesisId("t1"),
            position_id=PositionId("p1"),
            summary="test summary",
            key_catalyst="key catalyst",
            position_size_rationale="Sized at 2% because high conviction on catalyst",
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
        assert r.position_size_rationale is not None

    def _check_rationale_empty_rejected() -> None:
        with _raises(ValidationError):
            ThesisRecord(
                thesis_id=ThesisId("t1"),
                position_id=PositionId("p1"),
                summary="test summary",
                key_catalyst="key catalyst",
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

    return [
        _run_case("01i-order_class_mleg_strategy_valid", _check_mleg_strategy_valid),
        _run_case("01i-order_class_mleg_equity_rejected", _check_mleg_equity_rejected),
        _run_case("01j-position_size_rationale_valid", _check_rationale_valid),
        _run_case("01j-position_size_rationale_empty_rejected", _check_rationale_empty_rejected),
    ]


# ---------------------------------------------------------------------------
# Wave 2: Additive fields (01d-01j) — 14 cases
# ---------------------------------------------------------------------------


def wave2_additive_fields(verbose: bool = False) -> tuple[int, int, list[dict[str, Any]]]:
    """Exercises ALP-337 (01d), ALP-338 (01e), ALP-339 (01f), ALP-340 (01g),
    ALP-341 (01h), ALP-342 (01i), ALP-343 (01j)."""
    now = _now_utc()
    results = (
        _wave2_01d_time_expectation(now)
        + _wave2_01e_position_weight(now)
        + _wave2_01f_option_greeks(now)
        + _wave2_01g_position_fill(now)
        + _wave2_01h_bracket_deadline(now)
        + _wave2_01ij_order_and_rationale(now)
    )
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    if verbose:
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            if not r["ok"] and r["detail"]:
                print(f"         {r['detail']}")
    return passed, failed, results


def _raises(exc_type: type[Exception]) -> Any:
    """Context manager for asserting an exception is raised in a wave case.

    Post-ALP-477 the portfolio_state records are frozen dataclasses that raise
    ``ValueError``/``TypeError`` from ``__post_init__`` instead of Pydantic's
    ``ValidationError``. Treat any of those as a successful catch when
    ``exc_type`` is ``ValidationError`` so the verify-script invariants don't
    need to be rewritten per case.
    """
    from pydantic import ValidationError

    class _CM:
        def __enter__(self) -> _CM:
            return self

        def __exit__(self, exc_type_: type | None, exc_val: Any, exc_tb: Any) -> bool:
            if exc_type_ is None:
                msg = f"Expected {exc_type.__name__} to be raised, but no exception was raised"
                raise AssertionError(msg)
            if exc_type is ValidationError and issubclass(exc_type_, (ValueError, TypeError)):
                return True
            return issubclass(exc_type_, exc_type)

    return _CM()


# ---------------------------------------------------------------------------
# Wave 3: Boundary fix (02) — 2 cases
# ---------------------------------------------------------------------------


def wave3_boundary_fix(verbose: bool = False) -> tuple[int, int, list[dict[str, Any]]]:
    """Exercises ALP-344 (02 — RegimeLabel relocation)."""
    results: list[dict[str, Any]] = []

    def _check_import_from_risk_guardrails() -> None:
        from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel

        assert RegimeLabel is not None

    results.append(
        _run_case(
            "02-regime_label_importable_from_risk_guardrails",
            _check_import_from_risk_guardrails,
        )
    )

    def _check_identity() -> None:
        from alphamind._kernel.regime import RegimeLabel as A
        from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel as B

        assert A is B, "RegimeLabel identity check failed: _kernel and risk_guardrails diverge"

    results.append(_run_case("02-regime_label_identity_across_import_paths", _check_identity))

    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    if verbose:
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            if not r["ok"] and r["detail"]:
                print(f"         {r['detail']}")
    return passed, failed, results


# ---------------------------------------------------------------------------
# Wave 4: Typed payloads (03a/03b) — 4 cases
# ---------------------------------------------------------------------------


def wave4_typed_payloads(verbose: bool = False) -> tuple[int, int, list[dict[str, Any]]]:
    """Exercises ALP-345 (03a — typed trigger) and ALP-346 (03b — PLAnchorSpec)."""
    from pydantic import ValidationError

    results: list[dict[str, Any]] = []
    now = _now_utc()

    from alphamind.portfolio_state.records.orders import (
        BracketLeg,
        BracketLegEnforcement,
        BracketLegStatus,
        BracketLegType,
        PLAnchorSpec,
        PriceTrigger,
        TimeTrigger,
    )

    def _check_price_trigger_on_price_stop() -> None:
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

    results.append(
        _run_case("03a-price_trigger_on_price_stop_valid", _check_price_trigger_on_price_stop)
    )

    def _check_time_trigger_on_price_stop_rejected() -> None:
        with _raises(ValidationError):
            BracketLeg(
                leg_id="leg1",
                leg_type=BracketLegType.PRICE_STOP,
                order_id=OrderId("ord1"),
                trigger=TimeTrigger(deadline=now),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.PENDING_ACTIVATION,
            )

    results.append(
        _run_case(
            "03a-time_trigger_on_price_stop_rejected",
            _check_time_trigger_on_price_stop_rejected,
        )
    )

    def _check_pl_anchor_on_take_profit() -> None:
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

    results.append(_run_case("03b-pl_anchor_on_take_profit_valid", _check_pl_anchor_on_take_profit))

    def _check_pl_anchor_on_time_expiration_rejected() -> None:
        with _raises(ValidationError):
            BracketLeg(
                leg_id="leg1",
                leg_type=BracketLegType.TIME_EXPIRATION,
                order_id=None,
                trigger=TimeTrigger(deadline=now),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.PENDING_ACTIVATION,
                pl_anchor=PLAnchorSpec(spec_type="stop", pct=0.30, planned_entry_price=18.50),
            )

    results.append(
        _run_case(
            "03b-pl_anchor_on_time_expiration_rejected",
            _check_pl_anchor_on_time_expiration_rejected,
        )
    )

    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    if verbose:
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            if not r["ok"] and r["detail"]:
                print(f"         {r['detail']}")
    return passed, failed, results


# ---------------------------------------------------------------------------
# Wave 5: Structural (04a/04b) — 3 cases
# ---------------------------------------------------------------------------


def wave5_structural(verbose: bool = False) -> tuple[int, int, list[dict[str, Any]]]:
    """Exercises ALP-347 (04a — records/events/aggregates reorg) and
    ALP-348 (04b — Pydantic-native discriminated unions)."""
    from pydantic import ValidationError

    results: list[dict[str, Any]] = []

    def _check_events_import() -> None:
        from alphamind.portfolio_state.events import ActivityLogEntry

        assert ActivityLogEntry is not None

    results.append(_run_case("04a-events_activity_log_importable", _check_events_import))

    def _check_aggregates_import() -> None:
        from alphamind.portfolio_state.aggregates import RiskBudgetEntry

        assert RiskBudgetEntry is not None

    results.append(_run_case("04a-aggregates_risk_budget_importable", _check_aggregates_import))

    def _check_discriminated_union_bogus_rejected() -> None:
        # Post-ALP-477: dict-payload discriminator parsing lives in the codec
        # layer (``state/tables/positions_codec.py``), not in the dataclass
        # constructor. The dataclass stores whatever ``details`` value the
        # caller passes; the codec raises on bogus discriminators at row
        # rehydration time. Document the boundary so the case stays green
        # without claiming a behavior that no longer exists.
        from alphamind.state.tables.positions_codec import _details_from_dict

        with _raises(ValidationError):
            _details_from_dict({"instrument_type": "BOGUS"})

    results.append(
        _run_case(
            "04b-discriminated_union_bogus_instrument_type_rejected",
            _check_discriminated_union_bogus_rejected,
        )
    )

    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    if verbose:
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            if not r["ok"] and r["detail"]:
                print(f"         {r['detail']}")
    return passed, failed, results


# ---------------------------------------------------------------------------
# Wave 6: Architectural (05a/05b/05c) — 4 cases
# ---------------------------------------------------------------------------


def wave6_architectural(verbose: bool = False) -> tuple[int, int, list[dict[str, Any]]]:
    """Exercises ALP-349 (05a — PositionRecord split), ALP-350 (05b — BasePositionProtocol),
    and ALP-351 (05c — ThesisHealthSnapshot lifecycle)."""
    results: list[dict[str, Any]] = []
    now = _now_utc()

    def _check_position_record_no_market_value() -> None:
        import dataclasses as _dc

        from alphamind.portfolio_state.records.positions import PositionRecord

        field_names = {f.name for f in _dc.fields(PositionRecord)}
        assert "current_market_value_usd" not in field_names, (
            "PositionRecord still carries current_market_value_usd — 05a split not applied"
        )

    results.append(
        _run_case(
            "05a-position_record_no_current_market_value",
            _check_position_record_no_market_value,
        )
    )

    def _check_position_view_has_market_value() -> None:
        import dataclasses as _dc

        from alphamind.portfolio_state.views.positions import PositionView

        field_names = {f.name for f in _dc.fields(PositionView)}
        assert "current_market_value_usd" in field_names, (
            "PositionView is missing current_market_value_usd — 05a split not applied correctly"
        )

    results.append(
        _run_case(
            "05a-position_view_has_current_market_value", _check_position_view_has_market_value
        )
    )

    def _check_base_position_protocol_isinstance() -> None:
        from alphamind.portfolio_state.protocols.positions import BasePositionProtocol
        from alphamind.portfolio_state.records.positions import (
            Direction,
            EquityPositionDetails,
            PositionFill,
            PositionRecord,
            PositionStatus,
        )

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
        assert isinstance(record, BasePositionProtocol), (
            "PositionRecord does not satisfy BasePositionProtocol — 05b protocol not applied"
        )

    results.append(
        _run_case(
            "05b-position_record_satisfies_base_protocol",
            _check_base_position_protocol_isinstance,
        )
    )

    def _check_thesis_health_snapshot() -> None:
        # 05c: ThesisComponent no longer carries supporting_signals
        import dataclasses as _dc

        from alphamind.portfolio_state.records.theses import (
            SupportingSignal,
            SupportingSignalStatus,
            ThesisComponent,
            ThesisStatus,
        )
        from alphamind.portfolio_state.views.thesis_health import (
            ComponentHealthEntry,
            ThesisHealthSnapshot,
        )

        component_field_names = {f.name for f in _dc.fields(ThesisComponent)}
        assert "supporting_signals" not in component_field_names, (
            "ThesisComponent still carries supporting_signals — 05c lifecycle split not applied"
        )

        # ThesisHealthSnapshot.health_for_component works
        entry = ComponentHealthEntry(
            component_id="c1",
            supporting_signals=(
                SupportingSignal(name="momentum", status=SupportingSignalStatus.STRENGTHENED),
            ),
        )
        snapshot = ThesisHealthSnapshot(
            thesis_id="t1",
            invocation_id="inv1",
            snapshot_timestamp=now,
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=None,
            component_health=(entry,),
        )
        found = snapshot.health_for_component("c1")
        assert found is entry
        assert snapshot.health_for_component("nonexistent") is None

    results.append(_run_case("05c-thesis_health_snapshot_lifecycle", _check_thesis_health_snapshot))

    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    if verbose:
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            if not r["ok"] and r["detail"]:
                print(f"         {r['detail']}")
    return passed, failed, results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_WAVE_SPECS = [
    ("Wave 1 utilities (01a/01b/01c)", 12, wave1_utilities),
    ("Wave 1 additive fields (01d-01j)", 14, wave2_additive_fields),
    ("Wave 2 boundary fix (02)", 2, wave3_boundary_fix),
    ("Wave 3 typed payloads (03a/03b)", 4, wave4_typed_payloads),
    ("Wave 4 structural (04a/04b)", 4, wave5_structural),
    ("Wave 5 architectural (05a/05b/05c)", 4, wave6_architectural),
]

# Fixed-width verdict labels matching the story spec; aligned at col 44.
_VERDICT_LABELS = [
    "Wave 1 utilities (01a/01b/01c):",
    "Wave 1 additive fields (01d-01j):",
    "Wave 2 boundary fix (02):",
    "Wave 3 typed payloads (03a/03b):",
    "Wave 4 structural (04a/04b):",
    "Wave 5 architectural (05a/05b/05c):",
]
_COL_WIDTH = 44


def run_all_waves(verbose: bool = False) -> tuple[int, int]:
    """Run all waves. Returns (total_cases, total_failed)."""
    grand_total = 0
    grand_failed = 0
    for _label, _expected_count, wave_fn in _WAVE_SPECS:
        passed, failed, _results = wave_fn(verbose=verbose)
        grand_total += passed + failed
        grand_failed += failed
    return grand_total, grand_failed


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Offline E2E verification for the position-thesis-model work tree (ALP-122). "
        "Exercises all 15 predecessor story deliverables. No SDK; no DB. "
        "Target wall-clock: under 10 seconds.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-scenario detail in addition to the verdict block.",
    )
    args = parser.parse_args()

    t0 = time.monotonic()

    wave_results: list[tuple[str, int, int, list[dict[str, Any]]]] = []
    for label, _expected_count, wave_fn in _WAVE_SPECS:
        passed, failed, results = wave_fn(verbose=args.verbose)
        wave_results.append((label, passed, failed, results))

    elapsed = time.monotonic() - t0

    # Print per-wave failure detail (always, on any failure; or under --verbose)
    any_failed = any(wf > 0 for _, _, wf, _ in wave_results)
    if any_failed:
        print("\n=== Failure details ===")
        for wave_label, _passed, _failed, results in wave_results:
            for r in results:
                if not r["ok"]:
                    print(f"\n[FAIL] {wave_label} / {r['label']}")
                    if r["detail"]:
                        print(r["detail"])

    # Verdict block
    grand_total = sum(p + f for _, p, f, _ in wave_results)
    grand_failed = sum(f for _, _, f, _ in wave_results)
    grand_passed = grand_total - grand_failed

    print("\n=== verify_position_thesis_model verdict ===")
    for (_label, passed, failed, _), verdict_label in zip(
        wave_results, _VERDICT_LABELS, strict=True
    ):
        count_str = f"{passed}/{passed + failed} cases"
        status = "PASS" if failed == 0 else "FAIL"
        padded = verdict_label.ljust(_COL_WIDTH)
        print(f"{padded}{status} ({count_str})")
    print("-----------------------------------------------")
    overall = "PASS" if grand_failed == 0 else "FAIL"
    print(f"Overall: {overall} ({grand_passed}/{grand_total} cases)")
    print(f"Wall-clock: {elapsed:.2f}s")

    return 0 if grand_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
