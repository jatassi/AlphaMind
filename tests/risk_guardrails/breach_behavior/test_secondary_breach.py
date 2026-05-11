"""Tests for ``check_secondary_breach`` and ``ProposedClose`` (story 05b).

Stub objects deliberately satisfy the documented Protocols structurally — the
production guardrail-evaluation ``RuleProjection`` / ``LibraryOutput`` shapes
are compatible. Tests do not import the production library so this story stays
decoupled from the upstream layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    ProposedClose,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
    check_secondary_breach,
)

# ---------------------------------------------------------------------------
# Stubs — structural stand-ins for guardrail-evaluation protocols and inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubRuleProjection:
    """Structural stand-in for ``RuleProjectionProtocol``."""

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
    """Structural stand-in for ``LibraryOutputProtocol``."""

    per_rule: tuple[_StubRuleProjection, ...]


@dataclass(frozen=True)
class _StubLibraryConfig:
    """Structural stand-in for ``LibraryConfigProtocol``.

    The primitive only reads ``effective_limits`` (the rule-id key set) for
    the primary-rule-id-in-active-set check.
    """

    effective_limits: dict[str, float]


@dataclass(frozen=True)
class _StubPortfolioState:
    """Structural stand-in for ``PortfolioStateSnapshotProtocol``.

    The primitive treats this as an opaque token threaded into the library
    callable; structural typing only requires the attribute set the library
    consumes downstream. For the primitive itself, no fields are read.
    """


@dataclass(frozen=True)
class _StubMarketInputs:
    """Structural stand-in for ``MarketInputsProtocol``. Opaque to the primitive."""


@dataclass
class _ScriptedLibrary:
    """Test-time stand-in for the ``evaluate_proposals`` callable.

    Returns ``baseline_output`` on the first call (zero proposals) and
    ``post_close_output`` on the second call (one proposal). Records every
    invocation for assertions.
    """

    baseline_output: _StubLibraryOutput
    post_close_output: _StubLibraryOutput
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_call: int | None = None
    raise_exception: Exception | None = None

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
        if self.raise_on_call is not None and len(self.calls) == self.raise_on_call:
            assert self.raise_exception is not None
            raise self.raise_exception
        return self.baseline_output if len(self.calls) == 1 else self.post_close_output


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def default_config() -> BreachBehaviorConfig:
    """Default ``BreachBehaviorConfig``; buffer factor is ``1.0`` (no override)."""
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=15,
        drawdown_velocity_threshold_pct_of_daily_limit=80.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=3,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )


@pytest.fixture
def proposed_close() -> ProposedClose:
    """A garden-variety partial trim of a long equity position."""
    return ProposedClose(
        position_id="pos_long_aapl_001",
        ticker="AAPL",
        asset_type="equity",
        direction="long",
        pre_close_size_pct_of_portfolio=10.0,
        close_size_pct_of_portfolio=10.0,
        pre_close_size_usd=10_000.0,
        close_size_usd=10_000.0,
    )


def _projection(
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
# Tracer bullet: no secondary breach (all PASS pre, all PASS post)
# ---------------------------------------------------------------------------


def test_no_secondary_breach_when_all_rules_pass_pre_and_post(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("net_long_pct", "PASS"),
            _projection("net_short_pct", "PASS"),
            _projection("gross_exposure_pct", "PASS"),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("net_long_pct", "PASS"),
            _projection("net_short_pct", "PASS"),
            _projection("gross_exposure_pct", "PASS"),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "net_long_pct": 25.0,
                "net_short_pct": 25.0,
                "gross_exposure_pct": 50.0,
                "position_max_loss_equity_pct": 2.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert isinstance(result, SecondaryBreachCheckResult)
    assert result.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH


# ---------------------------------------------------------------------------
# Deferred-to-PM: closing a short pushes net long over limit
# ---------------------------------------------------------------------------


def test_deferred_to_pm_when_close_introduces_new_fail(
    default_config: BreachBehaviorConfig,
) -> None:
    """Closing a $30K short pushes net long from 20% to 50% over a 25% limit."""
    proposed = ProposedClose(
        position_id="pos_short_xyz_001",
        ticker="XYZ",
        asset_type="equity",
        direction="short",
        pre_close_size_pct_of_portfolio=30.0,
        close_size_pct_of_portfolio=30.0,
        pre_close_size_usd=30_000.0,
        close_size_usd=30_000.0,
    )
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("total_short_pct", "FAIL", current=30.0, limit=25.0, projected_after=30.0),
            _projection("net_long_pct", "PASS", current=20.0, limit=25.0, projected_after=20.0),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("total_short_pct", "PASS", current=30.0, limit=25.0, projected_after=0.0),
            _projection("net_long_pct", "FAIL", current=20.0, limit=25.0, projected_after=50.0),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"total_short_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="total_short_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert result.notes is not None
    assert "net_long_pct" in result.notes


def test_multiple_secondary_breaches_named_in_notes(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("position_max_loss_equity_pct", "FAIL", current=2.5, limit=2.0),
            _projection("net_long_pct", "PASS"),
            _projection("gross_exposure_pct", "PASS"),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("position_max_loss_equity_pct", "PASS", current=2.5, limit=2.0),
            _projection("net_long_pct", "FAIL"),
            _projection("gross_exposure_pct", "FAIL"),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "position_max_loss_equity_pct": 2.0,
                "net_long_pct": 25.0,
                "gross_exposure_pct": 50.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert result.notes is not None
    assert "net_long_pct" in result.notes
    assert "gross_exposure_pct" in result.notes


def test_pre_existing_concurrent_fail_is_not_secondary(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    """A rule that was FAIL'd both pre and post-close was not introduced; it pre-existed."""
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("total_short_pct", "FAIL", current=30.0, limit=25.0),
            _projection("net_long_pct", "FAIL", current=30.0, limit=25.0),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("total_short_pct", "PASS", current=30.0, limit=25.0, projected_after=0.0),
            _projection("net_long_pct", "FAIL", current=30.0, limit=25.0, projected_after=27.0),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"total_short_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="total_short_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH


def test_primary_rule_excluded_from_secondary_detection(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    """Even if the primary rule remains FAIL post-close, it never counts as secondary."""
    baseline = _StubLibraryOutput(
        per_rule=(_projection("position_max_loss_equity_pct", "FAIL", current=2.5, limit=2.0),),
    )
    post_close = _StubLibraryOutput(
        per_rule=(_projection("position_max_loss_equity_pct", "FAIL", current=2.5, limit=2.0),),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"position_max_loss_equity_pct": 2.0},
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_primary_breach_rule_id_not_in_effective_limits_raises(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    library = _ScriptedLibrary(
        baseline_output=_StubLibraryOutput(per_rule=()),
        post_close_output=_StubLibraryOutput(per_rule=()),
    )
    with pytest.raises(ValueError, match=r"primary_breach_rule_id.*nonexistent_rule"):
        check_secondary_breach(
            proposed_close=proposed_close,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(effective_limits={"net_long_pct": 25.0}),
            market_inputs=_StubMarketInputs(),
            primary_breach_rule_id="nonexistent_rule",
            config=default_config,
            evaluate_proposals=library,
        )


def test_library_evaluation_error_propagates_with_wrapper(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    library = _ScriptedLibrary(
        baseline_output=_StubLibraryOutput(per_rule=()),
        post_close_output=_StubLibraryOutput(per_rule=()),
        raise_on_call=2,
        raise_exception=ValueError("upstream library failed"),
    )
    with pytest.raises(ValueError, match=r"secondary[- ]breach"):
        check_secondary_breach(
            proposed_close=proposed_close,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(
                effective_limits={"position_max_loss_equity_pct": 2.0},
            ),
            market_inputs=_StubMarketInputs(),
            primary_breach_rule_id="position_max_loss_equity_pct",
            config=default_config,
            evaluate_proposals=library,
        )


def test_proposed_close_rejects_close_pct_exceeding_pre_close_pct() -> None:
    with pytest.raises(ValidationError, match=r"close_size_pct_of_portfolio"):
        ProposedClose(
            position_id="pos",
            ticker="AAPL",
            asset_type="equity",
            direction="long",
            pre_close_size_pct_of_portfolio=10.0,
            close_size_pct_of_portfolio=12.0,
            pre_close_size_usd=10_000.0,
            close_size_usd=10_000.0,
        )


def test_proposed_close_rejects_close_usd_exceeding_pre_close_usd() -> None:
    with pytest.raises(ValidationError, match=r"close_size_usd"):
        ProposedClose(
            position_id="pos",
            ticker="AAPL",
            asset_type="equity",
            direction="long",
            pre_close_size_pct_of_portfolio=10.0,
            close_size_pct_of_portfolio=10.0,
            pre_close_size_usd=10_000.0,
            close_size_usd=12_000.0,
        )


def test_proposed_close_rejects_zero_or_negative_close_pct() -> None:
    with pytest.raises(ValidationError, match=r"must be > 0"):
        ProposedClose(
            position_id="pos",
            ticker="AAPL",
            asset_type="equity",
            direction="long",
            pre_close_size_pct_of_portfolio=10.0,
            close_size_pct_of_portfolio=0.0,
            pre_close_size_usd=10_000.0,
            close_size_usd=0.0,
        )


def test_proposed_close_full_close_round_trip() -> None:
    """A FULL_CLOSE has close_size == pre_close_size; construction succeeds."""
    pc = ProposedClose(
        position_id="pos_full",
        ticker="AAPL",
        asset_type="equity",
        direction="long",
        pre_close_size_pct_of_portfolio=10.0,
        close_size_pct_of_portfolio=10.0,
        pre_close_size_usd=10_000.0,
        close_size_usd=10_000.0,
    )
    assert pc.close_size_pct_of_portfolio == pc.pre_close_size_pct_of_portfolio


def test_proposed_close_partial_trim_round_trip() -> None:
    """A PARTIAL_TRIM has close_size < pre_close_size; construction succeeds."""
    pc = ProposedClose(
        position_id="pos_trim",
        ticker="AAPL",
        asset_type="equity",
        direction="long",
        pre_close_size_pct_of_portfolio=10.0,
        close_size_pct_of_portfolio=5.0,
        pre_close_size_usd=10_000.0,
        close_size_usd=5_000.0,
    )
    assert pc.close_size_pct_of_portfolio < pc.pre_close_size_pct_of_portfolio


def test_curing_the_primary_yields_no_secondary_breach(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    """Primary FAIL pre, PASS post; no other rule changes → NO_SECONDARY_BREACH."""
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("position_max_loss_equity_pct", "FAIL", current=2.5, limit=2.0),
            _projection("net_long_pct", "PASS"),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("position_max_loss_equity_pct", "PASS", current=2.5, limit=2.0),
            _projection("net_long_pct", "PASS"),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "position_max_loss_equity_pct": 2.0,
                "net_long_pct": 25.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH


# ---------------------------------------------------------------------------
# Worked examples
# ---------------------------------------------------------------------------


def test_a6_short_squeeze_protective_close_no_secondary(
    default_config: BreachBehaviorConfig,
) -> None:
    """A6 from scenario-tests.md: closing a max-loss short reduces risk; no secondary."""
    proposed = ProposedClose(
        position_id="pos_short_breached",
        ticker="GME",
        asset_type="equity",
        direction="short",
        pre_close_size_pct_of_portfolio=4.0,
        close_size_pct_of_portfolio=4.0,
        pre_close_size_usd=4_000.0,
        close_size_usd=4_000.0,
    )
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("position_max_loss_equity_pct", "FAIL", current=2.5, limit=2.0),
            _projection("net_long_pct", "PASS"),
            _projection("net_short_pct", "PASS"),
            _projection("total_short_pct", "PASS"),
            _projection("gross_exposure_pct", "PASS"),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("position_max_loss_equity_pct", "PASS", current=2.5, limit=2.0),
            _projection("net_long_pct", "PASS"),
            _projection("net_short_pct", "PASS"),
            _projection("total_short_pct", "PASS"),
            _projection("gross_exposure_pct", "PASS"),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "position_max_loss_equity_pct": 2.0,
                "net_long_pct": 25.0,
                "net_short_pct": 25.0,
                "total_short_pct": 25.0,
                "gross_exposure_pct": 50.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH


def test_constructed_short_close_pushes_net_long_over_limit(
    default_config: BreachBehaviorConfig,
) -> None:
    """$30K short on $100K portfolio; closing brings net long from 20% to 50% over 25%."""
    proposed = ProposedClose(
        position_id="pos_short_30k",
        ticker="XYZ",
        asset_type="equity",
        direction="short",
        pre_close_size_pct_of_portfolio=30.0,
        close_size_pct_of_portfolio=30.0,
        pre_close_size_usd=30_000.0,
        close_size_usd=30_000.0,
    )
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("total_short_pct", "FAIL", current=30.0, limit=25.0, projected_after=30.0),
            _projection("net_long_pct", "PASS", current=20.0, limit=25.0, projected_after=20.0),
        ),
    )
    post_close = _StubLibraryOutput(
        per_rule=(
            _projection("total_short_pct", "PASS", current=30.0, limit=25.0, projected_after=0.0),
            _projection("net_long_pct", "FAIL", current=20.0, limit=25.0, projected_after=50.0),
        ),
    )
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={"total_short_pct": 25.0, "net_long_pct": 25.0},
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="total_short_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert result.result == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert result.notes is not None
    assert "net_long_pct" in result.notes


# ---------------------------------------------------------------------------
# Determinism / immutability
# ---------------------------------------------------------------------------


def test_repeated_calls_produce_equal_results(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    baseline = _StubLibraryOutput(
        per_rule=(
            _projection("net_long_pct", "PASS"),
            _projection("net_short_pct", "PASS"),
        ),
    )
    post_close = _StubLibraryOutput(per_rule=baseline.per_rule)

    def _run() -> SecondaryBreachCheckResult:
        library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)
        return check_secondary_breach(
            proposed_close=proposed_close,
            current_state=_StubPortfolioState(),
            library_config=_StubLibraryConfig(
                effective_limits={
                    "net_long_pct": 25.0,
                    "net_short_pct": 25.0,
                    "position_max_loss_equity_pct": 2.0,
                },
            ),
            market_inputs=_StubMarketInputs(),
            primary_breach_rule_id="position_max_loss_equity_pct",
            config=default_config,
            evaluate_proposals=library,
        )

    first = _run()
    for _ in range(99):
        assert _run() == first


def test_returned_result_is_frozen(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    baseline = _StubLibraryOutput(per_rule=(_projection("net_long_pct", "PASS"),))
    post_close = _StubLibraryOutput(per_rule=(_projection("net_long_pct", "PASS"),))
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    result = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "net_long_pct": 25.0,
                "position_max_loss_equity_pct": 2.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    with pytest.raises(ValidationError):
        result.result = SecondaryBreachOutcome.DEFERRED_TO_PM


# ---------------------------------------------------------------------------
# Library invocation discipline
# ---------------------------------------------------------------------------


def test_library_called_twice_baseline_then_with_close_delta(
    default_config: BreachBehaviorConfig,
    proposed_close: ProposedClose,
) -> None:
    baseline = _StubLibraryOutput(per_rule=(_projection("net_long_pct", "PASS"),))
    post_close = _StubLibraryOutput(per_rule=(_projection("net_long_pct", "PASS"),))
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "net_long_pct": 25.0,
                "position_max_loss_equity_pct": 2.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=default_config,
        evaluate_proposals=library,
    )

    assert len(library.calls) == 2
    assert library.calls[0]["proposals"] == ()
    assert len(library.calls[1]["proposals"]) == 1


# ---------------------------------------------------------------------------
# Buffer-factor propagation
# ---------------------------------------------------------------------------


def test_buffer_factor_propagated_to_library(proposed_close: ProposedClose) -> None:
    """``config.delta_buffer_secondary_check_buffer_factor`` reaches both library calls."""
    config = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=15,
        drawdown_velocity_threshold_pct_of_daily_limit=80.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=3,
        delta_buffer_secondary_check_buffer_factor=1.25,
        emergency_invocation_cooldown_minutes=30,
    )
    baseline = _StubLibraryOutput(per_rule=(_projection("net_long_pct", "PASS"),))
    post_close = _StubLibraryOutput(per_rule=(_projection("net_long_pct", "PASS"),))
    library = _ScriptedLibrary(baseline_output=baseline, post_close_output=post_close)

    check_secondary_breach(
        proposed_close=proposed_close,
        current_state=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "net_long_pct": 25.0,
                "position_max_loss_equity_pct": 2.0,
            },
        ),
        market_inputs=_StubMarketInputs(),
        primary_breach_rule_id="position_max_loss_equity_pct",
        config=config,
        evaluate_proposals=library,
    )

    assert len(library.calls) == 2
    assert library.calls[0]["delta_buffer_factor"] == 1.25
    assert library.calls[1]["delta_buffer_factor"] == 1.25
