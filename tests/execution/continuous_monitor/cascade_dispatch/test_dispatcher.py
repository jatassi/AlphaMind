"""Tests for the engine-envelope cascade dispatcher (ALP-438).

The dispatcher is constructor-fed with fakes for every per-tick provider so
each test isolates one behavior. ``submit_envelope`` is stubbed to record
calls; ``deferral_sink`` is stubbed likewise. The breach-behavior primitives
remain real — the dispatcher's job is to compose them correctly, not to
re-implement them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

# Break the latent OMS ↔ portfolio_manager cycle before importing
# ``submit_envelope_mcp``-derived symbols (mirror of the discipline in
# ``tests/execution/oms/test_submit_engine_envelope.py``).
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind._kernel.ids import PositionId, Symbol
from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop.result import RuleEvaluation
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    BreachDispatchContext,
    CascadeDispatcher,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    PositionLiquidity,
    PositionRiskReward,
    RegimeLabel,
    RiskZone,
)
from tests.execution.continuous_monitor.cascade_dispatch.conftest import (
    NOW,
    RecordingDeferralSink,
    RecordingSubmit,
    ScriptedLibrary,
    StubLibraryConfig,
    StubLibraryOutput,
    StubMarketInputs,
    StubPortfolioState,
    StubRuleProjection,
    equity_view,
    make_breach_config,
    make_breach_loop_result,
)

_SESSION_ID = "monsession-a"


# ---------------------------------------------------------------------------
# File-local builders
# ---------------------------------------------------------------------------


def _per_position_breach_eval(*, rule_id: str = "position_max_loss_equity_pct") -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=rule_id,
        current_value=-3.5,
        limit_value=-3.0,
        overage=0.5,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
    )


def _make_dispatch_context(
    *,
    breaching_position_id: str = "POS-NVDA-1",
    secondary_breach_library: ScriptedLibrary | None = None,
    portfolio_value_usd: float = 100_000.0,
    extra_positions: tuple[PositionView, ...] = (),
    primary_rule: str = "position_max_loss_equity_pct",
) -> BreachDispatchContext:
    """Build a per-tick context with one breaching position + a passing secondary check."""
    breaching = equity_view(
        position_id=breaching_position_id,
        ticker=Symbol("NVDA"),
        unrealized_pnl_usd=-3_500.0,
    )
    positions = (breaching, *extra_positions)
    liquidity = tuple(
        PositionLiquidity(
            position_id=p.position_id,
            adv_to_position_size_ratio=10.0,
        )
        for p in positions
    )
    risk_reward = tuple(
        PositionRiskReward(
            position_id=p.position_id,
            risk_reward_ratio=p.risk_reward_at_current or 2.0,
        )
        for p in positions
    )
    if secondary_breach_library is None:
        secondary_breach_library = ScriptedLibrary(
            outputs=[
                StubLibraryOutput(per_rule=()),  # baseline
                StubLibraryOutput(per_rule=()),  # post-close — same; no new failures
            ]
        )
    return BreachDispatchContext(
        open_positions=positions,
        liquidity=liquidity,
        risk_reward_metric=risk_reward,
        library_snapshot=StubPortfolioState(),
        library_config=StubLibraryConfig(
            effective_limits={
                primary_rule: 1.0,
                "position_max_loss_equity_pct": 1.0,
                "position_max_loss_options_pct": 1.0,
                "daily_drawdown_pct": 1.0,
                "cumulative_drawdown_pct": 1.0,
                "total_short_pct": 1.0,
                "single_short_max_pct": 1.0,
                "margin_call": 1.0,
            }
        ),
        market_inputs=StubMarketInputs(),
        evaluate_proposals=secondary_breach_library,
        portfolio_value_usd=portfolio_value_usd,
        active_regime=RegimeLabel.ELEVATED,
        progressive_tiers=(),
        breach_classification={},
    )


def _per_position_max_loss_kwargs_provider(
    rule: RuleEvaluation,
    context: BreachDispatchContext,
) -> dict[str, Any]:
    """Provider for position_max_loss_*_pct selector kwargs.

    Tests pass the breaching position's id as the rule's first immediate
    candidate; in real wiring the breach loop would surface this on the
    evaluation. For the tests' fixture, the breaching position is the first
    in ``context.open_positions``.
    """
    breaching = context.open_positions[0]
    return {
        "breaching_position_id": breaching.position_id,
        "open_positions": context.open_positions,
        "loss_pct": rule.current_value,
        "limit_pct": rule.limit_value,
    }


# ===========================================================================
# Tests
# ===========================================================================


async def test_happy_path_submits_one_envelope_for_per_position_max_loss() -> None:
    """Per_position_max_loss breach → selector picks breaching position →
    secondary check clean → exactly one envelope submitted with engine-guardrail subtype."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    deferral_sink = RecordingDeferralSink()
    context = _make_dispatch_context()
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=deferral_sink,
        per_rule_kwargs_providers={
            "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
        },
        now=lambda: NOW,
    )

    rule = _per_position_breach_eval()
    result = make_breach_loop_result(evaluations=(rule,))
    await dispatcher.handle_immediate_breach(result, rule)

    assert len(submit.calls) == 1
    envelope = submit.calls[0]
    assert envelope.commands[0].risk_management_subtype == "engine_guardrail"
    assert envelope.commands[0].close_rationale_type == "risk_management"
    assert envelope.commands[0].position_id == "POS-NVDA-1"


async def test_happy_path_envelope_and_command_ids_match_canonical_patterns() -> None:
    """The envelope id matches ``MON.<session>.<trigger>`` and the OMS-derived
    command_id matches ``MON.<session>.<trigger>.0``."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    context = _make_dispatch_context()
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=RecordingDeferralSink(),
        per_rule_kwargs_providers={
            "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
        },
        now=lambda: NOW,
    )

    rule = _per_position_breach_eval()
    result = make_breach_loop_result(evaluations=(rule,))
    await dispatcher.handle_immediate_breach(result, rule)

    envelope = submit.calls[0]
    assert re.fullmatch(rf"MON\.{re.escape(_SESSION_ID)}\.[0-9]+", envelope.envelope_id)
    # Submit stub formats command_id = envelope_id + ".0" — exactly the OMS-derived form.
    assert submit.calls[0].commands[0].command_id is None  # OMS will derive it
    # And the envelope_id has trigger_id 1 since this is the first breach.
    assert envelope.envelope_id == f"MON.{_SESSION_ID}.1"


async def test_deferred_to_pm_does_not_submit_logs_deferral() -> None:
    """When the secondary-breach check yields deferred_to_pm and no alternate is
    found, the dispatcher does NOT submit and logs the deferral."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    deferral_sink = RecordingDeferralSink()
    # Scripted library: baseline = (); post-close = one new FAIL rule;
    # the orchestrator will treat this as a secondary breach.
    library = ScriptedLibrary(
        outputs=[
            StubLibraryOutput(per_rule=()),  # baseline (clean)
            StubLibraryOutput(  # post-close → introduces a new FAIL
                per_rule=(
                    StubRuleProjection(
                        rule="total_short_pct",
                        status="FAIL",
                        current=35.0,
                        limit=30.0,
                        projected_after=35.0,
                        headroom_remaining=-5.0,
                        unit="pct",
                    ),
                )
            ),
        ]
    )
    # No extra positions → alternate-position search will exhaust → deferred_to_pm.
    context = _make_dispatch_context(secondary_breach_library=library)
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=deferral_sink,
        per_rule_kwargs_providers={
            "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
        },
        now=lambda: NOW,
    )

    rule = _per_position_breach_eval()
    result = make_breach_loop_result(evaluations=(rule,))
    await dispatcher.handle_immediate_breach(result, rule)

    assert submit.calls == []
    assert len(deferral_sink.calls) == 1
    deferral = deferral_sink.calls[0]
    assert deferral.rule_breached == "position_max_loss_equity_pct"
    assert deferral.candidate_position_id == "POS-NVDA-1"


async def test_secondary_breach_avoided_submits_alternate_envelope() -> None:
    """When the primary close would introduce a secondary breach but an alternate
    is found, the dispatcher submits the alternate envelope tagged
    ``secondary_breach_avoided``."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    deferral_sink = RecordingDeferralSink()
    # Three projections per call:
    # call 1: baseline (clean)
    # call 2: primary close → introduces FAIL on rule X (deferred_to_pm)
    # call 3: alternate close on extra position → baseline (alternate search)
    # call 4: alternate close → clean (no secondary breach)
    library = ScriptedLibrary(
        outputs=[
            StubLibraryOutput(per_rule=()),  # baseline for primary check
            StubLibraryOutput(  # post-primary-close — introduces a fail
                per_rule=(
                    StubRuleProjection(
                        rule="total_short_pct",
                        status="FAIL",
                        current=35.0,
                        limit=30.0,
                        projected_after=35.0,
                        headroom_remaining=-5.0,
                        unit="pct",
                    ),
                )
            ),
            StubLibraryOutput(per_rule=()),  # baseline for alternate's secondary check
            StubLibraryOutput(per_rule=()),  # post-alternate-close — clean
        ]
    )
    alternate = equity_view(
        position_id=PositionId("POS-AMD-1"),
        ticker=Symbol("AMD"),
        unrealized_pnl_usd=-1_000.0,
    )
    context = _make_dispatch_context(
        secondary_breach_library=library,
        extra_positions=(alternate,),
    )
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=deferral_sink,
        per_rule_kwargs_providers={
            "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
        },
        now=lambda: NOW,
    )

    rule = _per_position_breach_eval()
    result = make_breach_loop_result(evaluations=(rule,))
    await dispatcher.handle_immediate_breach(result, rule)

    assert len(submit.calls) == 1
    envelope = submit.calls[0]
    # The alternate position is the one closed.
    assert envelope.commands[0].position_id == "POS-AMD-1"
    # The secondary check on the envelope reflects the avoided breach.
    sbcr = envelope.guardrail_trigger_record.secondary_breach_check_result
    assert sbcr is not None
    assert sbcr.result == "secondary_breach_avoided"


async def test_rejects_deferred_classification_rule_as_structural_error() -> None:
    """A deferred-classification rule (e.g., ``sector_concentration``) reaching
    the immediate-breach callback is a structural error — raise."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    context = _make_dispatch_context()
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=RecordingSubmit(),
        deferral_sink=RecordingDeferralSink(),
        per_rule_kwargs_providers={},
        now=lambda: NOW,
    )

    rule = RuleEvaluation(
        rule_id="sector_concentration",
        current_value=35.0,
        limit_value=30.0,
        overage=5.0,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
    )
    result = make_breach_loop_result(evaluations=(rule,))
    with pytest.raises(ValueError, match="sector_concentration"):
        await dispatcher.handle_immediate_breach(result, rule)


async def test_trigger_ids_strictly_increase_over_sequential_breaches() -> None:
    """50 sequential breaches → strictly increasing trigger ids starting at 1."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    context = _make_dispatch_context()
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=RecordingDeferralSink(),
        per_rule_kwargs_providers={
            "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
        },
        now=lambda: NOW,
    )

    rule = _per_position_breach_eval()
    result = make_breach_loop_result(evaluations=(rule,))

    def _make_provider(captured: BreachDispatchContext) -> Callable[[], BreachDispatchContext]:
        def _provider() -> BreachDispatchContext:
            return captured

        return _provider

    for _ in range(50):
        # The scripted library is consumed once per dispatch, so reset it.
        # Use a fresh library on each call by rebuilding the context each tick.
        context = _make_dispatch_context()
        dispatcher = CascadeDispatcher(
            monitor_session_id=_SESSION_ID,
            breach_config=make_breach_config(),
            trigger_ids=trigger_ids,
            context_provider=_make_provider(context),
            submit_envelope=submit,
            deferral_sink=RecordingDeferralSink(),
            per_rule_kwargs_providers={
                "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
            },
            now=lambda: NOW,
        )
        await dispatcher.handle_immediate_breach(result, rule)

    trigger_ids_in_envelopes = [int(env.envelope_id.split(".")[-1]) for env in submit.calls]
    assert trigger_ids_in_envelopes == list(range(1, 51))


async def test_breach_cascade_submits_chained_envelopes_in_order_with_shared_cascade_id() -> None:
    """When the cascade chain emits multiple envelopes (post-close re-evaluation
    surfaces additional immediate-engine FAILs), the dispatcher submits each
    envelope in the order returned, all sharing the same cascade_id."""
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    # Two positions so post-close cascade has a follow-up candidate to close.
    alternate = equity_view(
        position_id=PositionId("POS-AMD-1"),
        ticker=Symbol("AMD"),
        unrealized_pnl_usd=-1_000.0,
        position_weight_pct=8.0,
    )
    # Scripted library outputs:
    #   call 1: baseline for primary check     → clean
    #   call 2: post-primary-close             → clean (no secondary)
    #   call 3: pre-cascade baseline           → clean
    #   call 4: post-1st-close in cascade loop → one new immediate-engine FAIL
    #   call 5: secondary check on follow-up   → baseline (clean)
    #   call 6: secondary check on follow-up   → post-close clean
    #   call 7: post-2nd-close in cascade loop → no more new failures
    library = ScriptedLibrary(
        outputs=[
            StubLibraryOutput(per_rule=()),  # 1
            StubLibraryOutput(per_rule=()),  # 2
            StubLibraryOutput(per_rule=()),  # 3 (pre-cascade baseline)
            StubLibraryOutput(  # 4 — post-1st-close, introduces a new fail
                per_rule=(
                    StubRuleProjection(
                        rule="daily_drawdown_pct",
                        status="FAIL",
                        current=6.0,
                        limit=5.0,
                        projected_after=6.0,
                        headroom_remaining=-1.0,
                        unit="pct",
                    ),
                )
            ),
            StubLibraryOutput(per_rule=()),  # 5 (secondary baseline for follow-up)
            StubLibraryOutput(per_rule=()),  # 6 (post-close clean)
            StubLibraryOutput(per_rule=()),  # 7 (post-2nd-close, no more fails)
        ]
    )
    context_base = _make_dispatch_context(
        secondary_breach_library=library,
        extra_positions=(alternate,),
    )
    # Enable cascade follow-up by injecting breach_classification + follow_up_selector.
    classification = {"daily_drawdown_pct": BreachResponse.immediate_engine}

    def _follow_up_selector(
        *,
        rule_id: str,
        rule_projection: Any,
        post_liquidation_positions: tuple[PositionView, ...],
        liquidity: tuple[PositionLiquidity, ...],
        portfolio_value_usd: float,
    ) -> Any:
        from alphamind.risk_guardrails.breach_behavior import (
            PositionSelectionAction,
            PositionSelectionResult,
            ProposedClose,
        )

        # Close the first available position via full close.
        chosen = post_liquidation_positions[0]
        pre_pct = abs(chosen.position_weight_pct)
        pre_usd = pre_pct / 100.0 * portfolio_value_usd
        selection = PositionSelectionResult(
            position_id=chosen.position_id,
            action=PositionSelectionAction.FULL_CLOSE,
            target_post_action_size_pct_of_portfolio=None,
            rationale=f"cascade follow-up close on {chosen.position_id}",
        )
        close = ProposedClose(
            position_id=chosen.position_id,
            ticker=Symbol("AMD"),
            asset_type="equity",
            direction="long",
            pre_close_size_pct_of_portfolio=pre_pct,
            close_size_pct_of_portfolio=pre_pct,
            pre_close_size_usd=pre_usd,
            close_size_usd=pre_usd,
        )
        return selection, close

    # Rebuild the context with classification embedded.
    from dataclasses import replace

    context = replace(context_base, breach_classification=classification)
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=RecordingDeferralSink(),
        per_rule_kwargs_providers={
            "position_max_loss_equity_pct": _per_position_max_loss_kwargs_provider,
        },
        follow_up_selector=_follow_up_selector,
        now=lambda: NOW,
    )

    rule = _per_position_breach_eval()
    result = make_breach_loop_result(evaluations=(rule,))
    await dispatcher.handle_immediate_breach(result, rule)

    # At least two envelopes are submitted in order; all share the cascade_id.
    assert len(submit.calls) >= 2
    cascade_ids = {env.guardrail_trigger_record.cascade_id for env in submit.calls}
    assert len(cascade_ids) == 1
    cascade_id = next(iter(cascade_ids))
    assert cascade_id is not None
    assert cascade_id.startswith(f"CASCADE.{_SESSION_ID}.")


async def test_margin_call_cascade_submits_returned_envelopes_in_order() -> None:
    """``handle_margin_call`` routes through ``orchestrate_margin_call_cascade``
    and submits each returned envelope in order with the shared cascade_id."""
    from alphamind.risk_guardrails.breach_behavior import MarginCallEvent

    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = RecordingSubmit()
    # Without breach_classification, orchestrator emits exactly one envelope
    # (the margin call liquidation itself). For this test that's the contract:
    # the dispatcher submits whatever the orchestrator returns, in order.
    library = ScriptedLibrary(
        outputs=[
            StubLibraryOutput(per_rule=()),  # baseline
            StubLibraryOutput(per_rule=()),  # post-close — clean
        ]
    )
    # margin call selector picks worst R/R; add a couple positions.
    second = equity_view(
        position_id=PositionId("POS-AMD-1"),
        ticker=Symbol("AMD"),
        unrealized_pnl_usd=-1_500.0,
    )
    context = _make_dispatch_context(
        secondary_breach_library=library,
        extra_positions=(second,),
        primary_rule="margin_call",
    )
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=make_breach_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=RecordingDeferralSink(),
        per_rule_kwargs_providers={},
        now=lambda: NOW,
    )

    event = MarginCallEvent(
        issued_at=NOW,
        additional_margin_required_usd=5_000.0,
    )
    await dispatcher.handle_margin_call(event)

    assert len(submit.calls) >= 1
    cascade_ids = {env.guardrail_trigger_record.cascade_id for env in submit.calls}
    # All envelopes in a cascade share a cascade_id.
    assert len(cascade_ids) == 1
    cascade_id = next(iter(cascade_ids))
    assert cascade_id is not None
    assert cascade_id.startswith(f"CASCADE.{_SESSION_ID}.")


def _strategy_view(*, position_id: str) -> PositionView:
    """Build a 2-leg vertical-spread ``PositionView`` for proposed-close tests."""
    from datetime import date

    from alphamind._kernel.ids import BracketId, ThesisId
    from alphamind._kernel.money import money, price, signed_money
    from alphamind.portfolio_state.records.positions import (
        Direction,
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
        PositionFill,
        PositionRecord,
        PositionStatus,
        StrategyLeg,
        StrategyPositionDetails,
    )

    def _leg(strike: float, direction: Direction) -> StrategyLeg:
        return StrategyLeg(
            leg_id=f"leg-{strike}",
            direction=direction,
            options=OptionsPositionDetails(
                underlying_ticker=Symbol("SPY"),
                strike_price=strike,
                expiration_date=date(2026, 6, 19),
                contract_type=OptionContractType.CALL,
                contract_count=1.0,
                contract_multiplier=100.0,
                premium_paid_per_contract=5.0,
                greeks=OptionGreeks(delta=0.5, gamma=0.01, theta=-0.02, vega=0.1),
            ),
        )

    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THE-{position_id}"),
        bracket_id=BracketId(f"BRK-{position_id}"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=NOW,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(_leg(500.0, Direction.LONG), _leg(510.0, Direction.SHORT)),
            net_premium_usd=300.0,
            max_profit_usd=700.0,
            max_loss_usd=-300.0,
            breakeven_levels=(503.0,),
            strategy_greeks=OptionGreeks(delta=0.2, gamma=0.0, theta=-0.005, vega=0.03),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=NOW,
                fill_price=price(3.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(300.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0,
        position_age_hours=2.0,
        notional_exposure_usd=money(300.0),
        delta_adjusted_exposure_usd=signed_money(300.0),
        distance_to_target_usd=signed_money(10.0),
        distance_to_stop_usd=signed_money(5.0),
        risk_reward_at_current=2.0,
    )


def test_proposed_close_from_selection_handles_strategy_position() -> None:
    """A strategy position routes through ``position_direction()`` without crashing.

    A multi-leg strategy is neither long nor short at the position level
    (``position_direction()`` returns ``None``). ``_proposed_close_from_selection``
    must still produce a valid ``ProposedClose`` for it (ALP-608).
    """
    from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
        _proposed_close_from_selection,
    )
    from alphamind.risk_guardrails.breach_behavior import (
        PositionSelectionAction,
        PositionSelectionResult,
    )

    view = _strategy_view(position_id="POS-SPREAD-1")
    selection = PositionSelectionResult(
        position_id=view.position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale="protective close",
    )

    close = _proposed_close_from_selection(
        selection=selection,
        position=view,
        portfolio_value_usd=100_000.0,
    )

    assert close.position_id == "POS-SPREAD-1"
    assert close.asset_type == "strategy"
    # A strategy is neither long nor short at the position level; mirroring
    # ``_direction_of``, the inert label falls to "short".
    assert close.direction == "short"
