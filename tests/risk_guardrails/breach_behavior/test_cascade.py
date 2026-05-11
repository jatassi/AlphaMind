"""Tests for the cascade orchestrator (story 07).

Stub objects deliberately satisfy the documented Protocols structurally — the
production guardrail-evaluation ``RuleProjection`` / ``LibraryOutput`` shapes
are compatible. Tests do not import the production library so this story stays
decoupled from the upstream layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.config.models.guardrails import BreachResponse
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
    BreachBehaviorConfig,
    CascadeContext,
    CascadeStepLimitExceeded,
    EngineEnvelope,
    MarginCallEvent,
    PositionLiquidity,
    PositionRiskReward,
    PositionSelectionAction,
    PositionSelectionResult,
    ProposedClose,
    RegimeLabel,
    SecondaryBreachOutcome,
    generate_cascade_id,
    orchestrate_breach_cascade,
    orchestrate_margin_call_cascade,
    search_for_alternate_position,
)
from alphamind.risk_guardrails.breach_behavior.types import BreachDetails

# ---------------------------------------------------------------------------
# Stubs — structural stand-ins for guardrail-evaluation protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubRuleProjection:
    rule: str
    status: str
    current: float
    limit: float
    projected_after: float
    headroom_remaining: float
    unit: str
    inverse: bool = False


@dataclass(frozen=True)
class _StubLibraryOutput:
    per_rule: tuple[_StubRuleProjection, ...]


@dataclass(frozen=True)
class _StubLibraryConfig:
    effective_limits: dict[str, float]


@dataclass(frozen=True)
class _StubPortfolioState:
    label: str = "default"


@dataclass(frozen=True)
class _StubMarketInputs:
    pass


@dataclass
class _ScriptedLibrary:
    """Drives ``evaluate_proposals`` with caller-scripted outputs.

    Returns ``outputs[i]`` on the i-th call. Records every invocation so tests
    can assert call counts and proposal shapes.

    The fixture is *input-aware*: it does not derive its return values from
    call index alone — it only matches by call index given a script. To
    additionally validate that the orchestrator actually plumbs closes through
    to the library projection, callers can use :class:`_InputAwareLibrary`
    instead, which dispatches outputs based on whether ``proposals`` is empty
    or carries specific position ids.
    """

    outputs: list[_StubLibraryOutput]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        state: Any,
        proposals: Sequence[Any],
        config: Any,
        market: Any,
        delta_buffer_factor: float = 1.0,
    ) -> _StubLibraryOutput:
        self.calls.append(
            {
                "state": state,
                "proposals": tuple(proposals),
                "config": config,
                "market": market,
                "delta_buffer_factor": delta_buffer_factor,
            }
        )
        idx = min(len(self.calls) - 1, len(self.outputs) - 1)
        return self.outputs[idx]


@dataclass
class _InputAwareLibrary:
    """Input-aware ``evaluate_proposals`` stub for cascade-projection tests.

    Returns ``baseline_output`` when called with ``proposals=()``; otherwise
    looks up an output keyed by the *frozen set of position ids* the caller
    threaded through as CLOSE-action ProposedDeltas. Tests script outputs by
    "after closing positions {A}, projection looks like X" semantics, which
    forces the orchestrator to actually plumb closes through to the library
    or the test will fail.
    """

    baseline_output: _StubLibraryOutput
    post_close_outputs: dict[frozenset[str], _StubLibraryOutput]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        state: Any,
        proposals: Sequence[Any],
        config: Any,
        market: Any,
        delta_buffer_factor: float = 1.0,
    ) -> _StubLibraryOutput:
        proposals_tuple = tuple(proposals)
        self.calls.append(
            {
                "state": state,
                "proposals": proposals_tuple,
                "config": config,
                "market": market,
                "delta_buffer_factor": delta_buffer_factor,
            }
        )
        if not proposals_tuple:
            return self.baseline_output
        position_ids = frozenset(p.existing_position_id for p in proposals_tuple)
        if position_ids in self.post_close_outputs:
            return self.post_close_outputs[position_ids]
        msg = (
            f"_InputAwareLibrary: no scripted output for proposals "
            f"{[p.existing_position_id for p in proposals_tuple]!r}; "
            f"available keys: {sorted(map(sorted, self.post_close_outputs))!r}"
        )
        raise AssertionError(msg)


def _proj(
    rule: str,
    status: str,
    *,
    current: float = 10.0,
    limit: float = 25.0,
    projected_after: float | None = None,
) -> _StubRuleProjection:
    pa = projected_after if projected_after is not None else current
    return _StubRuleProjection(
        rule=rule,
        status=status,
        current=current,
        limit=limit,
        projected_after=pa,
        headroom_remaining=limit - pa,
        unit="% of portfolio",
    )


# ---------------------------------------------------------------------------
# Position-record fixtures — A7 short positions (COIN, SQ, HOOD)
# ---------------------------------------------------------------------------


def _short_position(
    *,
    position_id: str,
    ticker: str,
    weight_pct: float,
    market_value_usd: float,
) -> PositionView:
    fill_ts = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    record = PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=fill_ts,
        details=EquityPositionDetails(
            ticker=ticker,
            share_count=100.0,
            average_cost_basis_per_share=market_value_usd / 100.0,
            borrow_rate_pct=2.5,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=market_value_usd / 5.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=fill_ts,
                fill_price=market_value_usd / 100.0,
                fill_quantity=100.0,
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
        current_market_value_usd=market_value_usd,
        unrealized_pnl_usd=-market_value_usd * 0.1,
        unrealized_pnl_pct=-10.0,
        position_weight_pct=weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=market_value_usd,
        delta_adjusted_exposure_usd=market_value_usd,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _long_position(
    *,
    position_id: str,
    ticker: str,
    weight_pct: float,
    market_value_usd: float,
) -> PositionView:
    fill_ts = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    record = PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=fill_ts,
        details=EquityPositionDetails(
            ticker=ticker,
            share_count=100.0,
            average_cost_basis_per_share=market_value_usd / 100.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=fill_ts,
                fill_price=market_value_usd / 100.0,
                fill_quantity=100.0,
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
        current_market_value_usd=market_value_usd,
        unrealized_pnl_usd=market_value_usd * 0.05,
        unrealized_pnl_pct=5.0,
        position_weight_pct=weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=market_value_usd,
        delta_adjusted_exposure_usd=market_value_usd,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


_TRIGGER_TS = datetime(2026, 4, 29, 14, 30, tzinfo=UTC)


@pytest.fixture
def default_config() -> BreachBehaviorConfig:
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=15,
        drawdown_velocity_threshold_pct_of_daily_limit=80.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )


@pytest.fixture
def default_context(default_config: BreachBehaviorConfig) -> CascadeContext:
    return CascadeContext(
        monitor_session_id="s1",
        initial_trigger_id=5,
        cascade_id=generate_cascade_id(monitor_session_id="s1", initial_trigger_id=5),
        trigger_timestamp=_TRIGGER_TS,
        portfolio_value_usd=98_000.0,
        config=default_config,
    )


@pytest.fixture
def a7_positions() -> tuple[PositionView, ...]:
    """A7 fixture: 3 short positions COIN 8% / SQ 7% / HOOD 7% on $98K portfolio."""
    return (
        _short_position(
            position_id="POS-COIN-001",
            ticker="COIN",
            weight_pct=8.0,
            market_value_usd=7840.0,
        ),
        _short_position(
            position_id="POS-SQ-001",
            ticker="SQ",
            weight_pct=7.0,
            market_value_usd=6860.0,
        ),
        _short_position(
            position_id="POS-HOOD-001",
            ticker="HOOD",
            weight_pct=7.0,
            market_value_usd=6860.0,
        ),
    )


@pytest.fixture
def a7_liquidity() -> tuple[PositionLiquidity, ...]:
    return (
        PositionLiquidity(position_id="POS-COIN-001", adv_to_position_size_ratio=10.0),
        PositionLiquidity(position_id="POS-SQ-001", adv_to_position_size_ratio=5.0),
        PositionLiquidity(position_id="POS-HOOD-001", adv_to_position_size_ratio=5.0),
    )


@pytest.fixture
def a7_risk_reward() -> tuple[PositionRiskReward, ...]:
    """COIN has the worst R/R (lowest ratio)."""
    return (
        PositionRiskReward(position_id="POS-COIN-001", risk_reward_ratio=0.2),
        PositionRiskReward(position_id="POS-SQ-001", risk_reward_ratio=0.5),
        PositionRiskReward(position_id="POS-HOOD-001", risk_reward_ratio=0.7),
    )


# ---------------------------------------------------------------------------
# Cascade ID generation
# ---------------------------------------------------------------------------


def test_generate_cascade_id_happy_path() -> None:
    assert generate_cascade_id(monitor_session_id="s1", initial_trigger_id=5) == "CASCADE.s1.5"


def test_generate_cascade_id_rejects_empty_session() -> None:
    with pytest.raises(ValueError, match="monitor_session_id"):
        generate_cascade_id(monitor_session_id="", initial_trigger_id=5)


def test_generate_cascade_id_rejects_dot_in_session() -> None:
    with pytest.raises(ValueError, match="monitor_session_id"):
        generate_cascade_id(monitor_session_id="a.b", initial_trigger_id=5)


def test_generate_cascade_id_rejects_trigger_id_below_one() -> None:
    with pytest.raises(ValueError, match="initial_trigger_id"):
        generate_cascade_id(monitor_session_id="s1", initial_trigger_id=0)


# ---------------------------------------------------------------------------
# Margin call cascade — A7 tracer bullet (single envelope, no secondary breach)
# ---------------------------------------------------------------------------


def test_orchestrate_margin_call_cascade_a7_single_envelope_clean(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """A7: closing COIN short (worst R/R) introduces no secondary breaches."""
    # Pre-close baseline: total_short FAIL'd (the breach driving the close), other rules PASS.
    baseline = _StubLibraryOutput(
        per_rule=(
            _proj("total_short_pct", "FAIL", current=22.0, limit=25.0, projected_after=22.0),
            _proj("net_long_pct", "PASS", current=30.0, limit=45.0),
            _proj("gross_exposure_pct", "PASS", current=74.0, limit=90.0),
        ),
    )
    # Post-close: COIN short closed; total_short cured; net_long up but still PASS; gross down.
    post_close = _StubLibraryOutput(
        per_rule=(
            _proj("total_short_pct", "PASS", current=22.0, limit=25.0, projected_after=14.0),
            _proj("net_long_pct", "PASS", current=30.0, limit=45.0, projected_after=38.0),
            _proj("gross_exposure_pct", "PASS", current=74.0, limit=90.0, projected_after=66.0),
        ),
    )
    library = _ScriptedLibrary(outputs=[baseline, post_close])

    margin_call = MarginCallEvent(
        issued_at=_TRIGGER_TS,
        additional_margin_required_usd=3_000.0,
    )

    envelopes = orchestrate_margin_call_cascade(
        margin_call_event=margin_call,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        risk_reward_metric=a7_risk_reward,
        current_state=_StubPortfolioState(label="pre"),
        library_config=_StubLibraryConfig(
            effective_limits={
                "margin_call": 0.0,
                "total_short_pct": 25.0,
                "net_long_pct": 45.0,
                "gross_exposure_pct": 90.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.ELEVATED,
        context=default_context,
        evaluate_proposals=library,
    )

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert isinstance(envelope, EngineEnvelope)
    assert envelope.envelope_id == "MON.s1.5"
    assert envelope.guardrail_trigger_record.rule_breached == "margin_call"
    assert envelope.guardrail_trigger_record.cascade_id == "CASCADE.s1.5"
    assert envelope.guardrail_trigger_record.secondary_breach_check_result is not None
    assert (
        envelope.guardrail_trigger_record.secondary_breach_check_result.result
        == SecondaryBreachOutcome.NO_SECONDARY_BREACH
    )
    assert envelope.command.position_id == "POS-COIN-001"
    assert envelope.command.quantity_or_all == "all"


def test_orchestrate_margin_call_cascade_secondary_breach_is_deferred(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """Margin-call cascade NEVER alternate-searches; secondary surfaces as DEFERRED_TO_PM."""
    baseline = _StubLibraryOutput(
        per_rule=(_proj("net_long_pct", "PASS", current=20.0, limit=25.0, projected_after=20.0),),
    )
    post_close = _StubLibraryOutput(
        per_rule=(_proj("net_long_pct", "FAIL", current=20.0, limit=25.0, projected_after=50.0),),
    )
    library = _ScriptedLibrary(outputs=[baseline, post_close])

    envelopes = orchestrate_margin_call_cascade(
        margin_call_event=MarginCallEvent(
            issued_at=_TRIGGER_TS,
            additional_margin_required_usd=3_000.0,
        ),
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        risk_reward_metric=a7_risk_reward,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"margin_call": 0.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.ELEVATED,
        context=default_context,
        evaluate_proposals=library,
    )

    assert len(envelopes) == 1  # No alternate-search; primary always wins.
    secondary = envelopes[0].guardrail_trigger_record.secondary_breach_check_result
    assert secondary is not None
    assert secondary.result == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert secondary.notes is not None
    assert "net_long_pct" in secondary.notes


# ---------------------------------------------------------------------------
# Non-margin breach cascade
# ---------------------------------------------------------------------------


def _proposed_close_for_position(
    position: PositionView, *, portfolio_value_usd: float
) -> ProposedClose:
    pre_pct = position.position_weight_pct
    pre_usd = pre_pct / 100.0 * portfolio_value_usd
    return ProposedClose(
        position_id=position.position_id,
        ticker=(
            position.details.ticker if isinstance(position.details, EquityPositionDetails) else "X"
        ),
        asset_type="equity",
        direction="long" if position.direction == Direction.LONG else "short",
        pre_close_size_pct_of_portfolio=pre_pct,
        close_size_pct_of_portfolio=pre_pct,
        pre_close_size_usd=pre_usd,
        close_size_usd=pre_usd,
    )


def _full_close_selection_on(position_id: str) -> PositionSelectionResult:
    return PositionSelectionResult(
        position_id=position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale=f"primary close on {position_id}",
    )


def test_orchestrate_breach_cascade_no_secondary_emits_single_envelope(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """Sector-concentration breach where the primary close has no secondary breach."""
    primary_close = _proposed_close_for_position(
        a7_positions[0], portfolio_value_usd=default_context.portfolio_value_usd
    )
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)

    baseline = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "FAIL", current=27.0, limit=25.0, projected_after=27.0),
            _proj("net_long_pct", "PASS", current=20.0, limit=45.0),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "PASS", current=27.0, limit=25.0, projected_after=19.0),
            _proj("net_long_pct", "PASS", current=20.0, limit=45.0, projected_after=12.0),
        ),
    )
    library = _ScriptedLibrary(outputs=[baseline, post_close])

    envelopes = orchestrate_breach_cascade(
        primary_rule="sector_tech_pct",
        primary_breach_details=BreachDetails(
            current_value=27.0, limit_value=25.0, overage=2.0, unit="% of portfolio"
        ),
        proposed_close=primary_close,
        primary_position_selection=primary_selection,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"sector_tech_pct": 25.0, "net_long_pct": 45.0},
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.NORMAL,
        context=default_context,
        evaluate_proposals=library,
    )

    assert len(envelopes) == 1
    secondary = envelopes[0].guardrail_trigger_record.secondary_breach_check_result
    assert secondary is not None
    assert secondary.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH
    assert envelopes[0].command.position_id == a7_positions[0].position_id
    assert envelopes[0].guardrail_trigger_record.cascade_id == default_context.cascade_id


def test_orchestrate_breach_cascade_alternate_found_emits_swap(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """Closing position A causes secondary; closing position B clears → alternate wins."""
    primary_close = _proposed_close_for_position(
        a7_positions[0], portfolio_value_usd=default_context.portfolio_value_usd
    )
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)

    # Primary check: secondary FAIL'd on net_long_pct
    primary_baseline = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "FAIL", current=27.0, limit=25.0, projected_after=27.0),
            _proj("net_long_pct", "PASS", current=20.0, limit=25.0),
        ),
    )
    primary_post = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "PASS", current=27.0, limit=25.0, projected_after=19.0),
            _proj("net_long_pct", "FAIL", current=20.0, limit=25.0, projected_after=30.0),
        ),
    )
    # Alternate check on second candidate (POS-SQ-001): clean
    alt_baseline = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "FAIL", current=27.0, limit=25.0, projected_after=27.0),
            _proj("net_long_pct", "PASS", current=20.0, limit=25.0),
        ),
    )
    alt_post = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "PASS", current=27.0, limit=25.0, projected_after=20.0),
            _proj("net_long_pct", "PASS", current=20.0, limit=25.0, projected_after=24.0),
        ),
    )
    library = _ScriptedLibrary(outputs=[primary_baseline, primary_post, alt_baseline, alt_post])

    envelopes = orchestrate_breach_cascade(
        primary_rule="sector_tech_pct",
        primary_breach_details=BreachDetails(
            current_value=27.0, limit_value=25.0, overage=2.0, unit="% of portfolio"
        ),
        proposed_close=primary_close,
        primary_position_selection=primary_selection,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"sector_tech_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.NORMAL,
        context=default_context,
        evaluate_proposals=library,
        primary_rule_breach_type="other",
    )

    assert len(envelopes) == 1
    secondary = envelopes[0].guardrail_trigger_record.secondary_breach_check_result
    assert secondary is not None
    assert secondary.result == SecondaryBreachOutcome.SECONDARY_BREACH_AVOIDED
    assert secondary.notes is not None
    # Alternate is the second candidate (POS-SQ-001) since the first iter is on POS-COIN-001 itself.
    assert envelopes[0].command.position_id == a7_positions[1].position_id
    assert a7_positions[0].position_id in secondary.notes
    assert a7_positions[1].position_id in secondary.notes


def test_orchestrate_breach_cascade_no_alternate_emits_original_with_deferred(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """No candidate clears; emit original close with DEFERRED_TO_PM."""
    primary_close = _proposed_close_for_position(
        a7_positions[0], portfolio_value_usd=default_context.portfolio_value_usd
    )
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)

    # Every secondary check returns DEFERRED_TO_PM (post-close FAILs net_long_pct).
    baseline = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "FAIL", current=27.0, limit=25.0, projected_after=27.0),
            _proj("net_long_pct", "PASS", current=20.0, limit=25.0),
        ),
    )
    post_close_with_secondary = _StubLibraryOutput(
        per_rule=(
            _proj("sector_tech_pct", "PASS", current=27.0, limit=25.0, projected_after=19.0),
            _proj("net_long_pct", "FAIL", current=20.0, limit=25.0, projected_after=30.0),
        ),
    )
    # 1 primary + 2 alternate candidates x 2 calls each = 6 calls; library padded with last.
    library = _ScriptedLibrary(
        outputs=[
            baseline,
            post_close_with_secondary,
            baseline,
            post_close_with_secondary,
            baseline,
            post_close_with_secondary,
        ],
    )

    envelopes = orchestrate_breach_cascade(
        primary_rule="sector_tech_pct",
        primary_breach_details=BreachDetails(
            current_value=27.0, limit_value=25.0, overage=2.0, unit="% of portfolio"
        ),
        proposed_close=primary_close,
        primary_position_selection=primary_selection,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"sector_tech_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.NORMAL,
        context=default_context,
        evaluate_proposals=library,
        primary_rule_breach_type="other",
    )

    assert len(envelopes) == 1
    assert envelopes[0].command.position_id == a7_positions[0].position_id
    secondary = envelopes[0].guardrail_trigger_record.secondary_breach_check_result
    assert secondary is not None
    assert secondary.result == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert secondary.notes is not None
    # The notes name the secondary rule indirectly (via the wrapping notes).
    assert "net_long_pct" in secondary.notes


# ---------------------------------------------------------------------------
# search_for_alternate_position direct tests
# ---------------------------------------------------------------------------


def test_search_for_alternate_position_returns_first_clean_candidate(
    default_config: BreachBehaviorConfig,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """First-tried candidate fails; second-tried clears → returns the second."""
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)

    bad_baseline = _StubLibraryOutput(
        per_rule=(_proj("sector_tech_pct", "FAIL", projected_after=27.0),),
    )
    bad_post = _StubLibraryOutput(
        per_rule=(_proj("net_long_pct", "FAIL", projected_after=30.0, limit=25.0),),
    )
    good_baseline = _StubLibraryOutput(
        per_rule=(_proj("sector_tech_pct", "FAIL", projected_after=27.0),),
    )
    good_post = _StubLibraryOutput(
        per_rule=(_proj("net_long_pct", "PASS", projected_after=22.0, limit=25.0),),
    )
    library = _ScriptedLibrary(
        outputs=[bad_baseline, bad_post, good_baseline, good_post],
    )

    result = search_for_alternate_position(
        primary_rule="sector_tech_pct",
        primary_position_selection=primary_selection,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"sector_tech_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        config=default_config,
        evaluate_proposals=library,
        portfolio_value_usd=98_000.0,
    )

    assert result is not None
    selection, close = result
    assert selection.position_id == a7_positions[2].position_id  # 2nd candidate after primary
    assert close.position_id == a7_positions[2].position_id


def test_search_for_alternate_position_returns_none_when_no_candidate_clears(
    default_config: BreachBehaviorConfig,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """Every candidate causes secondary breach → returns None."""
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)
    baseline = _StubLibraryOutput(per_rule=(_proj("sector_tech_pct", "FAIL"),))
    bad_post = _StubLibraryOutput(per_rule=(_proj("net_long_pct", "FAIL", projected_after=30.0),))
    library = _ScriptedLibrary(outputs=[baseline, bad_post] * 5)

    result = search_for_alternate_position(
        primary_rule="sector_tech_pct",
        primary_position_selection=primary_selection,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"sector_tech_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        config=default_config,
        evaluate_proposals=library,
        portfolio_value_usd=98_000.0,
    )

    assert result is None


def test_search_for_alternate_position_bound_by_cascade_max_steps(
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """Search visits at most cascade_max_steps candidates; later candidates skipped."""
    config_max_2 = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=15,
        drawdown_velocity_threshold_pct_of_daily_limit=80.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=2,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)
    # Both candidates the search visits cause secondary; would-be-clean 3rd never reached.
    bad_baseline = _StubLibraryOutput(per_rule=(_proj("sector_tech_pct", "FAIL"),))
    bad_post = _StubLibraryOutput(per_rule=(_proj("net_long_pct", "FAIL", projected_after=30.0),))
    library = _ScriptedLibrary(outputs=[bad_baseline, bad_post] * 5)

    result = search_for_alternate_position(
        primary_rule="sector_tech_pct",
        primary_position_selection=primary_selection,
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"sector_tech_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        config=config_max_2,
        evaluate_proposals=library,
        portfolio_value_usd=98_000.0,
    )

    assert result is None
    # Each visit invokes the library twice (baseline + post-close).
    assert len(library.calls) == 2 * config_max_2.cascade_max_steps


def test_search_for_alternate_position_directional_filters_to_same_side(
    default_config: BreachBehaviorConfig,
) -> None:
    """For directional breaches, candidates restricted to same-side positions."""
    portfolio_value = 100_000.0
    primary = _short_position(
        position_id="POS-PRIMARY-SHORT",
        ticker="AAA",
        weight_pct=8.0,
        market_value_usd=8_000.0,
    )
    other_short = _short_position(
        position_id="POS-OTHER-SHORT",
        ticker="BBB",
        weight_pct=7.0,
        market_value_usd=7_000.0,
    )
    a_long = _long_position(
        position_id="POS-LONG-001",
        ticker="LLL",
        weight_pct=10.0,
        market_value_usd=10_000.0,
    )
    open_positions = (primary, other_short, a_long)
    liquidity = (
        PositionLiquidity(position_id="POS-PRIMARY-SHORT", adv_to_position_size_ratio=1.0),
        PositionLiquidity(position_id="POS-OTHER-SHORT", adv_to_position_size_ratio=1.0),
        PositionLiquidity(position_id="POS-LONG-001", adv_to_position_size_ratio=1.0),
    )
    primary_selection = _full_close_selection_on(primary.position_id)

    baseline = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),))
    clean_post = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),))
    library = _ScriptedLibrary(outputs=[baseline, clean_post])

    result = search_for_alternate_position(
        primary_rule="total_short_pct",
        primary_position_selection=primary_selection,
        open_positions=open_positions,
        liquidity=liquidity,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(effective_limits={"total_short_pct": 25.0}),
        market_inputs=_StubMarketInputs(),
        config=default_config,
        evaluate_proposals=library,
        portfolio_value_usd=portfolio_value,
        primary_rule_breach_type="directional",
    )

    assert result is not None
    selection, _ = result
    # Long position must NOT be considered; alternate must be the other short.
    assert selection.position_id == other_short.position_id


# ---------------------------------------------------------------------------
# Cascade chain (multi-envelope)
# ---------------------------------------------------------------------------


def _follow_up_full_close_selector(
    *,
    rule_id: str,
    rule_projection: Any,
    post_liquidation_positions: tuple[PositionView, ...],
    liquidity: tuple[PositionLiquidity, ...],
    portfolio_value_usd: float,
) -> tuple[PositionSelectionResult, ProposedClose]:
    """Test stand-in: pick the first remaining position; full close."""
    _ = rule_projection, liquidity
    target = post_liquidation_positions[0]
    selection = PositionSelectionResult(
        position_id=target.position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale=f"follow-up close on {target.position_id} for {rule_id}",
    )
    pre_pct = target.position_weight_pct
    pre_usd = pre_pct / 100.0 * portfolio_value_usd
    close = ProposedClose(
        position_id=target.position_id,
        ticker=(
            target.details.ticker if isinstance(target.details, EquityPositionDetails) else "X"
        ),
        asset_type="equity",
        direction="long" if target.direction == Direction.LONG else "short",
        pre_close_size_pct_of_portfolio=pre_pct,
        close_size_pct_of_portfolio=pre_pct,
        pre_close_size_usd=pre_usd,
        close_size_usd=pre_usd,
    )
    return (selection, close)


def test_orchestrate_margin_call_cascade_chain_shares_cascade_id_distinct_triggers(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """Post-liquidation introduces an immediate-engine breach; cascade emits a 2nd envelope."""
    # Library call sequence (1 + N pattern, per design):
    #   1-2: initial primary secondary-breach check (baseline + post-close)
    #   3:   pre-cascade baseline (computed once)
    #   4:   cascade loop iter 1 post-state — daily_drawdown FAIL (newly failed)
    #   5-6: follow-up secondary-breach check on cascaded close (baseline + post-close)
    #   7:   cascade loop iter 2 post-state — no new breaches, exit
    initial_baseline = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),))
    initial_post = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),))

    pre_cascade_baseline = _StubLibraryOutput(
        per_rule=(_proj("daily_drawdown_pct", "PASS", current=1.0, limit=2.5),),
    )
    iter1_post_state = _StubLibraryOutput(
        per_rule=(
            _proj("daily_drawdown_pct", "FAIL", current=1.0, limit=2.5, projected_after=3.0),
        ),
    )

    follow_baseline = _StubLibraryOutput(per_rule=(_proj("daily_drawdown_pct", "FAIL"),))
    follow_post = _StubLibraryOutput(per_rule=(_proj("daily_drawdown_pct", "PASS"),))

    iter2_post_state = _StubLibraryOutput(per_rule=(_proj("daily_drawdown_pct", "PASS"),))

    library = _ScriptedLibrary(
        outputs=[
            initial_baseline,
            initial_post,
            pre_cascade_baseline,
            iter1_post_state,
            follow_baseline,
            follow_post,
            iter2_post_state,
        ],
    )

    envelopes = orchestrate_margin_call_cascade(
        margin_call_event=MarginCallEvent(
            issued_at=_TRIGGER_TS, additional_margin_required_usd=3_000.0
        ),
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        risk_reward_metric=a7_risk_reward,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "margin_call": 0.0,
                "total_short_pct": 25.0,
                "daily_drawdown_pct": 2.5,
            },
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.ELEVATED,
        context=default_context,
        evaluate_proposals=library,
        breach_classification={
            "daily_drawdown_pct": BreachResponse.immediate_engine,
            "total_short_pct": BreachResponse.deferred_to_pm,
        },
        follow_up_selector=_follow_up_full_close_selector,
    )

    assert len(envelopes) == 2
    assert envelopes[0].guardrail_trigger_record.cascade_id == default_context.cascade_id
    assert envelopes[1].guardrail_trigger_record.cascade_id == default_context.cascade_id
    assert envelopes[0].envelope_id == "MON.s1.5"
    assert envelopes[1].envelope_id == "MON.s1.6"
    assert envelopes[1].guardrail_trigger_record.rule_breached == "daily_drawdown_pct"


def test_orchestrate_margin_call_cascade_step_limit_exceeded(
    default_config: BreachBehaviorConfig,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """Synthetic infinite cascade; with cascade_max_steps=2, raises after 2 envelopes."""
    config_max_2 = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=15,
        drawdown_velocity_threshold_pct_of_daily_limit=80.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=2,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )
    _ = default_config  # suppress unused-fixture lint
    context = CascadeContext(
        monitor_session_id="s1",
        initial_trigger_id=5,
        cascade_id=generate_cascade_id(monitor_session_id="s1", initial_trigger_id=5),
        trigger_timestamp=_TRIGGER_TS,
        portfolio_value_usd=98_000.0,
        config=config_max_2,
    )

    # Library reports a new immediate-engine breach on a different rule each iteration.
    # The pre-cascade baseline shows all PASS so each iteration's post-state introduces a
    # genuinely-new breach.
    rules = ["rule_a", "rule_b", "rule_c", "rule_d", "rule_e", "rule_f", "rule_g"]
    all_pass = _StubLibraryOutput(per_rule=tuple(_proj(rule, "PASS") for rule in rules))
    outputs: list[_StubLibraryOutput] = [
        # Initial primary check (2 calls)
        _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),)),
        _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),)),
        # Pre-cascade baseline (1 call)
        all_pass,
    ]
    # Per cascade iteration: post-state (1 call) then follow-up secondary check (2 calls)
    for rule in rules:
        post_state = _StubLibraryOutput(
            per_rule=tuple(
                _proj(
                    r,
                    "FAIL" if r == rule else "PASS",
                    projected_after=30.0 if r == rule else 5.0,
                )
                for r in rules
            ),
        )
        outputs.append(post_state)
        # Follow-up secondary check on this rule's close: baseline FAIL → post PASS = clean.
        outputs.append(_StubLibraryOutput(per_rule=(_proj(rule, "FAIL"),)))
        outputs.append(_StubLibraryOutput(per_rule=(_proj(rule, "PASS"),)))
    library = _ScriptedLibrary(outputs=outputs)

    classification = {rule: BreachResponse.immediate_engine for rule in rules}

    with pytest.raises(CascadeStepLimitExceeded) as exc_info:
        orchestrate_margin_call_cascade(
            margin_call_event=MarginCallEvent(
                issued_at=_TRIGGER_TS, additional_margin_required_usd=3_000.0
            ),
            open_positions=a7_positions,
            liquidity=a7_liquidity,
            risk_reward_metric=a7_risk_reward,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(
                effective_limits={
                    "margin_call": 0.0,
                    "total_short_pct": 25.0,
                    **{rule: 25.0 for rule in rules},
                },
            ),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.ELEVATED,
            context=context,
            evaluate_proposals=library,
            breach_classification=classification,
            follow_up_selector=_follow_up_full_close_selector,
        )

    assert exc_info.value.max_steps == 2
    assert exc_info.value.chain_length == config_max_2.cascade_max_steps + 1
    assert exc_info.value.last_breach_rule is not None


def test_orchestrate_margin_call_cascade_threads_emitted_closes_through_post_state_projection(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """H1 regression: post-close state projection must include emitted close as a ProposedDelta.

    Uses an *input-aware* library stub keyed by the set of position ids in the
    proposals tuple. If the orchestrator fails to thread the cascade close
    through to the library, the stub raises (no scripted output for the
    empty-proposals projection on cascade-iter-1) and the test fails.

    Scenario: closing COIN (envelope #1) introduces a daily-drawdown FAIL on
    rule X; the cascade selects the next position and emits envelope #2.
    """
    initial_baseline = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),))
    initial_post_close = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),))

    pre_cascade_baseline = _StubLibraryOutput(
        per_rule=(_proj("daily_drawdown_pct", "PASS", current=1.0, limit=2.5),),
    )
    # Iter 1 post-state with COIN close in flight: daily_drawdown FAILs.
    iter1_post_state = _StubLibraryOutput(
        per_rule=(
            _proj("daily_drawdown_pct", "FAIL", current=1.0, limit=2.5, projected_after=3.0),
        ),
    )
    # Iter 2 post-state with COIN + follow-up close in flight: clean.
    iter2_post_state = _StubLibraryOutput(per_rule=(_proj("daily_drawdown_pct", "PASS"),))

    follow_baseline = _StubLibraryOutput(per_rule=(_proj("daily_drawdown_pct", "FAIL"),))
    follow_post = _StubLibraryOutput(per_rule=(_proj("daily_drawdown_pct", "PASS"),))

    # Two libraries cooperating: the secondary-breach checks (initial + follow-up)
    # invoke the library twice each (baseline `proposals=()` + post-close
    # `proposals=(close,)`); the cascade loop invokes it once per iteration with
    # the in-flight closes as proposals. The input-aware library demands the
    # caller threads the right closes through.
    library = _InputAwareLibrary(
        baseline_output=pre_cascade_baseline,
        post_close_outputs={
            # When evaluated with only the initial COIN close in flight:
            frozenset({"POS-COIN-001"}): iter1_post_state,
            # When evaluated with the COIN close + follow-up close in flight:
            frozenset({"POS-COIN-001", a7_positions[1].position_id}): iter2_post_state,
        },
    )
    # The secondary-breach calls are interleaved with the cascade-loop calls;
    # _SequenceAwareLibrary tags each invocation by position in the call
    # sequence and dispatches to either a scripted secondary-check output or
    # to the input-aware library (cascade-loop calls), making the H1 close-
    # threading observable.

    @dataclass
    class _SequenceAwareLibrary:
        """Two-tier dispatcher: the initial primary-check + follow-up-check
        invocations consume from a sequence; everything else routes through
        the input-aware library.

        The cascade-loop's post-state projection MUST land in
        ``_InputAwareLibrary``, which demands the proposals carry the right
        position ids. This makes the H1 fix observable: without it the cascade
        sends ``proposals=()`` and the input-aware library has no scripted
        output, raising AssertionError.
        """

        primary_baseline: _StubLibraryOutput
        primary_post: _StubLibraryOutput
        followup_baseline: _StubLibraryOutput
        followup_post: _StubLibraryOutput
        input_aware: _InputAwareLibrary
        secondary_calls_remaining: list[str] = field(
            default_factory=lambda: [
                "primary_baseline",
                "primary_post",
                # cascade-loop calls (pre-cascade baseline + iter 1 post-state)
                # — empty placeholders here; they hit the input-aware library.
                "cascade_pre_baseline",
                "cascade_iter1_post",
                # follow-up secondary check
                "followup_baseline",
                "followup_post",
                # cascade-loop iter 2 post-state — routes to input-aware
                "cascade_iter2_post",
            ]
        )
        calls: list[dict[str, Any]] = field(default_factory=list)

        def __call__(
            self,
            *,
            state: Any,
            proposals: Sequence[Any],
            config: Any,
            market: Any,
            delta_buffer_factor: float = 1.0,
        ) -> _StubLibraryOutput:
            self.calls.append(
                {
                    "state": state,
                    "proposals": tuple(proposals),
                    "config": config,
                    "market": market,
                    "delta_buffer_factor": delta_buffer_factor,
                }
            )
            tag = self.secondary_calls_remaining.pop(0)
            if tag == "primary_baseline":
                return self.primary_baseline
            if tag == "primary_post":
                return self.primary_post
            if tag == "followup_baseline":
                return self.followup_baseline
            if tag == "followup_post":
                return self.followup_post
            # cascade-loop calls — must succeed via the input-aware library.
            return self.input_aware(
                state=state,
                proposals=proposals,
                config=config,
                market=market,
                delta_buffer_factor=delta_buffer_factor,
            )

    seq_library = _SequenceAwareLibrary(
        primary_baseline=initial_baseline,
        primary_post=initial_post_close,
        followup_baseline=follow_baseline,
        followup_post=follow_post,
        input_aware=library,
    )

    envelopes = orchestrate_margin_call_cascade(
        margin_call_event=MarginCallEvent(
            issued_at=_TRIGGER_TS, additional_margin_required_usd=3_000.0
        ),
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        risk_reward_metric=a7_risk_reward,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "margin_call": 0.0,
                "total_short_pct": 25.0,
                "daily_drawdown_pct": 2.5,
            },
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.ELEVATED,
        context=default_context,
        evaluate_proposals=seq_library,
        breach_classification={
            "daily_drawdown_pct": BreachResponse.immediate_engine,
            "total_short_pct": BreachResponse.deferred_to_pm,
        },
        follow_up_selector=_follow_up_full_close_selector,
    )

    # Two envelopes emitted: the original margin-call close and the cascaded close.
    assert len(envelopes) == 2
    assert envelopes[0].command.position_id == "POS-COIN-001"
    assert envelopes[1].guardrail_trigger_record.rule_breached == "daily_drawdown_pct"
    # Verify the input-aware library actually saw the close as a proposal in the
    # cascade-loop call — the H1 contract.
    cascade_loop_calls = [
        c
        for c in library.calls
        if len(c["proposals"]) > 0  # filter non-baseline
    ]
    assert len(cascade_loop_calls) >= 1
    # The first cascade-loop post-state call carried the initial close.
    assert any(p.existing_position_id == "POS-COIN-001" for p in cascade_loop_calls[0]["proposals"])


def test_orchestrate_margin_call_cascade_rejects_one_of_classification_pair(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """M3: providing only one of (breach_classification, follow_up_selector) raises."""
    baseline = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),))
    post = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),))
    library = _ScriptedLibrary(outputs=[baseline, post])

    with pytest.raises(ValueError, match="must be both provided or both None"):
        orchestrate_margin_call_cascade(
            margin_call_event=MarginCallEvent(
                issued_at=_TRIGGER_TS, additional_margin_required_usd=3_000.0
            ),
            open_positions=a7_positions,
            liquidity=a7_liquidity,
            risk_reward_metric=a7_risk_reward,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(
                effective_limits={"margin_call": 0.0, "total_short_pct": 25.0},
            ),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.ELEVATED,
            context=default_context,
            evaluate_proposals=library,
            breach_classification={"x": BreachResponse.immediate_engine},
            # follow_up_selector intentionally omitted
        )


def test_orchestrate_breach_cascade_rejects_one_of_classification_pair(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
) -> None:
    """M3: same rule for the non-margin orchestrator."""
    primary_close = _proposed_close_for_position(
        a7_positions[0], portfolio_value_usd=default_context.portfolio_value_usd
    )
    primary_selection = _full_close_selection_on(a7_positions[0].position_id)

    baseline = _StubLibraryOutput(
        per_rule=(_proj("sector_tech_pct", "FAIL", current=27.0, limit=25.0),),
    )
    post_close = _StubLibraryOutput(
        per_rule=(_proj("sector_tech_pct", "PASS", current=27.0, limit=25.0),),
    )
    library = _ScriptedLibrary(outputs=[baseline, post_close])

    with pytest.raises(ValueError, match="must be both provided or both None"):
        orchestrate_breach_cascade(
            primary_rule="sector_tech_pct",
            primary_breach_details=BreachDetails(
                current_value=27.0, limit_value=25.0, overage=2.0, unit="%"
            ),
            proposed_close=primary_close,
            primary_position_selection=primary_selection,
            open_positions=a7_positions,
            liquidity=a7_liquidity,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(effective_limits={"sector_tech_pct": 25.0}),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.NORMAL,
            context=default_context,
            evaluate_proposals=library,
            follow_up_selector=_follow_up_full_close_selector,
            # breach_classification intentionally omitted
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_orchestrate_margin_call_cascade_rejects_empty_positions(
    default_context: CascadeContext,
) -> None:
    library = _ScriptedLibrary(outputs=[_StubLibraryOutput(per_rule=())])
    with pytest.raises(ValueError, match="open_positions"):
        orchestrate_margin_call_cascade(
            margin_call_event=MarginCallEvent(
                issued_at=_TRIGGER_TS, additional_margin_required_usd=1.0
            ),
            open_positions=(),
            liquidity=(),
            risk_reward_metric=(),
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(effective_limits={"margin_call": 0.0}),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.NORMAL,
            context=default_context,
            evaluate_proposals=library,
        )


def test_orchestrate_breach_cascade_rejects_empty_positions(
    default_context: CascadeContext,
) -> None:
    primary_close = ProposedClose(
        position_id="POS-FAKE",
        ticker="X",
        asset_type="equity",
        direction="long",
        pre_close_size_pct_of_portfolio=5.0,
        close_size_pct_of_portfolio=5.0,
        pre_close_size_usd=5_000.0,
        close_size_usd=5_000.0,
    )
    library = _ScriptedLibrary(outputs=[_StubLibraryOutput(per_rule=())])
    with pytest.raises(ValueError, match="open_positions"):
        orchestrate_breach_cascade(
            primary_rule="sector_tech_pct",
            primary_breach_details=BreachDetails(
                current_value=27.0, limit_value=25.0, overage=2.0, unit="%"
            ),
            proposed_close=primary_close,
            primary_position_selection=_full_close_selection_on("POS-FAKE"),
            open_positions=(),
            liquidity=(),
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(effective_limits={"sector_tech_pct": 25.0}),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.NORMAL,
            context=default_context,
            evaluate_proposals=library,
        )


def test_orchestrate_margin_call_cascade_rejects_incomplete_liquidity(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """Liquidity covers only 2 of 3 positions → ValueError."""
    incomplete_liquidity = (
        PositionLiquidity(position_id="POS-COIN-001", adv_to_position_size_ratio=10.0),
        PositionLiquidity(position_id="POS-SQ-001", adv_to_position_size_ratio=5.0),
    )
    library = _ScriptedLibrary(outputs=[_StubLibraryOutput(per_rule=())])
    with pytest.raises(ValueError, match="liquidity"):
        orchestrate_margin_call_cascade(
            margin_call_event=MarginCallEvent(
                issued_at=_TRIGGER_TS, additional_margin_required_usd=1.0
            ),
            open_positions=a7_positions,
            liquidity=incomplete_liquidity,
            risk_reward_metric=a7_risk_reward,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(effective_limits={"margin_call": 0.0}),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.NORMAL,
            context=default_context,
            evaluate_proposals=library,
        )


# ---------------------------------------------------------------------------
# Determinism / immutability
# ---------------------------------------------------------------------------


def test_orchestrate_margin_call_cascade_is_deterministic(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    """Repeated calls with identical inputs produce equal envelope tuples."""

    def _run() -> tuple[EngineEnvelope, ...]:
        baseline = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),))
        post = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),))
        library = _ScriptedLibrary(outputs=[baseline, post])
        return orchestrate_margin_call_cascade(
            margin_call_event=MarginCallEvent(
                issued_at=_TRIGGER_TS, additional_margin_required_usd=3_000.0
            ),
            open_positions=a7_positions,
            liquidity=a7_liquidity,
            risk_reward_metric=a7_risk_reward,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(
                effective_limits={"margin_call": 0.0, "total_short_pct": 25.0},
            ),
            market_inputs=_StubMarketInputs(),
            active_regime=RegimeLabel.ELEVATED,
            context=default_context,
            evaluate_proposals=library,
        )

    first = _run()
    for _ in range(99):
        assert _run() == first


def test_returned_envelopes_are_frozen(
    default_context: CascadeContext,
    a7_positions: tuple[PositionView, ...],
    a7_liquidity: tuple[PositionLiquidity, ...],
    a7_risk_reward: tuple[PositionRiskReward, ...],
) -> None:
    from pydantic import ValidationError

    baseline = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "FAIL"),))
    post = _StubLibraryOutput(per_rule=(_proj("total_short_pct", "PASS"),))
    library = _ScriptedLibrary(outputs=[baseline, post])

    envelopes = orchestrate_margin_call_cascade(
        margin_call_event=MarginCallEvent(
            issued_at=_TRIGGER_TS, additional_margin_required_usd=3_000.0
        ),
        open_positions=a7_positions,
        liquidity=a7_liquidity,
        risk_reward_metric=a7_risk_reward,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"margin_call": 0.0, "total_short_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        active_regime=RegimeLabel.ELEVATED,
        context=default_context,
        evaluate_proposals=library,
    )

    with pytest.raises(ValidationError):
        envelopes[0].envelope_id = "MON.x.99"
