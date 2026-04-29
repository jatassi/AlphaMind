"""Tests for the thesis-performance-review trigger predicate (story 01e)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from alphamind.config.loaders import load_profiles
from alphamind.config.models.main import Profile
from alphamind.config.models.profiles import (
    FeatureFlags,
    ProfileConfig,
    RiskPriority,
    ThesisPerformanceReviewConfig,
    TokenBudgetRange,
)
from alphamind.risk_guardrails.rules_and_limits import (
    ReviewTriggerCause,
    ReviewTriggerSignal,
    evaluate_thesis_performance_review_trigger,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def micro_profile() -> ProfileConfig:
    return load_profiles(CONFIG_DIR)[Profile.micro]


def _synthetic_profile(
    *,
    risk_priority: RiskPriority,
    thesis_performance_review: ThesisPerformanceReviewConfig | None,
) -> ProfileConfig:
    """Build a minimal valid ProfileConfig with the requested marker pair."""
    return ProfileConfig.model_validate(
        {
            "capital_range_usd": [25000, 50000],
            "risk_priority": risk_priority.value,
            "feature_flags": FeatureFlags(
                options_enabled=False,
                short_selling_enabled=False,
                fractional_shares_required=True,
            ).model_dump(),
            "active_sectors": ["tech"],
            "min_position_size_usd": 50,
            "rule_values": {"position_max_size_pct": 5.0},
            "agent_token_budgets": {
                "analyst": TokenBudgetRange(context=(1500, 2500), output=(1200, 2500)).model_dump(),
            },
            "thesis_performance_review": (
                thesis_performance_review.model_dump()
                if thesis_performance_review is not None
                else None
            ),
        }
    )


def test_micro_at_threshold_trade_count_fires_with_trade_count_cause(
    micro_profile: ProfileConfig,
) -> None:
    """20 trades, no losses → triggered with trade_count cause."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=20,
        cumulative_realized_loss_usd=0.0,
        starting_capital_usd=1500.0,
    )
    assert isinstance(signal, ReviewTriggerSignal)
    assert signal.applicable is True
    assert signal.triggered is True
    assert signal.causes == (ReviewTriggerCause.trade_count,)


# ---------------------------------------------------------------------------
# Non-applicability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_member", [Profile.small, Profile.medium, Profile.large])
def test_non_micro_profile_returns_not_applicable(profile_member: Profile) -> None:
    profile = load_profiles(CONFIG_DIR)[profile_member]
    signal = evaluate_thesis_performance_review_trigger(
        profile=profile,
        completed_theses_since_last_review=20,
        cumulative_realized_loss_usd=300.0,
        starting_capital_usd=10000.0,
    )
    assert signal.applicable is False
    assert signal.triggered is False
    assert signal.causes == ()
    assert signal.trade_count_threshold == 0
    assert signal.loss_threshold_usd == 0.0
    # Input echoes preserved.
    assert signal.completed_theses_since_last_review == 20
    assert signal.cumulative_realized_loss_usd == 300.0
    assert signal.starting_capital_usd == 10000.0


# ---------------------------------------------------------------------------
# Micro gate behavior
# ---------------------------------------------------------------------------


def test_micro_below_both_gates_does_not_fire(micro_profile: ProfileConfig) -> None:
    """19 trades, $0 loss → applicable but not triggered."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=19,
        cumulative_realized_loss_usd=0.0,
        starting_capital_usd=1500.0,
    )
    assert signal.applicable is True
    assert signal.triggered is False
    assert signal.causes == ()


def test_micro_above_trade_count_threshold_fires(micro_profile: ProfileConfig) -> None:
    """21 trades, $0 loss → triggered with trade_count cause."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=21,
        cumulative_realized_loss_usd=0.0,
        starting_capital_usd=1500.0,
    )
    assert signal.triggered is True
    assert signal.causes == (ReviewTriggerCause.trade_count,)


def test_micro_cumulative_loss_at_exact_threshold_fires(micro_profile: ProfileConfig) -> None:
    """19 trades, $225 loss on $1500 starting (15%) → cumulative_loss fires."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=19,
        cumulative_realized_loss_usd=225.0,
        starting_capital_usd=1500.0,
    )
    assert signal.triggered is True
    assert signal.causes == (ReviewTriggerCause.cumulative_loss,)


def test_micro_cumulative_loss_just_below_threshold_does_not_fire(
    micro_profile: ProfileConfig,
) -> None:
    """19 trades, $224.99 loss on $1500 starting → no fire."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=19,
        cumulative_realized_loss_usd=224.99,
        starting_capital_usd=1500.0,
    )
    assert signal.triggered is False
    assert signal.causes == ()


def test_micro_both_gates_fire_in_canonical_order(micro_profile: ProfileConfig) -> None:
    """20 trades, $300 loss → both gates fire, ordered (trade_count, cumulative_loss)."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=20,
        cumulative_realized_loss_usd=300.0,
        starting_capital_usd=1500.0,
    )
    assert signal.triggered is True
    assert signal.causes == (
        ReviewTriggerCause.trade_count,
        ReviewTriggerCause.cumulative_loss,
    )


def test_micro_net_realized_gain_treated_as_no_loss(micro_profile: ProfileConfig) -> None:
    """Negative cumulative loss (a net gain) → cumulative_loss gate does not fire."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=19,
        cumulative_realized_loss_usd=-100.0,
        starting_capital_usd=1500.0,
    )
    assert signal.triggered is False
    assert signal.causes == ()


# ---------------------------------------------------------------------------
# Threshold sourcing
# ---------------------------------------------------------------------------


def test_returned_signal_sources_thresholds_from_profile(micro_profile: ProfileConfig) -> None:
    """Signal echoes the profile's thresholds and the computed loss threshold."""
    signal = evaluate_thesis_performance_review_trigger(
        profile=micro_profile,
        completed_theses_since_last_review=10,
        cumulative_realized_loss_usd=50.0,
        starting_capital_usd=1500.0,
    )
    review_config = micro_profile.thesis_performance_review
    assert review_config is not None
    assert signal.trade_count_threshold == review_config.trade_count_threshold
    assert signal.loss_threshold_usd == 1500.0 * review_config.loss_pct_threshold
    # The shipped micro values are the design's (20, 0.15) per the YAML.
    assert signal.trade_count_threshold == 20
    assert signal.loss_threshold_usd == pytest.approx(225.0)


def test_custom_thresholds_via_synthetic_profile() -> None:
    """Operators tune thresholds via YAML; tests build synthetic profiles."""
    profile = _synthetic_profile(
        risk_priority=RiskPriority.signal_quality,
        thesis_performance_review=ThesisPerformanceReviewConfig(
            trade_count_threshold=10, loss_pct_threshold=0.10
        ),
    )
    # Trade-count gate fires at 10 trades with the custom threshold.
    by_trade = evaluate_thesis_performance_review_trigger(
        profile=profile,
        completed_theses_since_last_review=10,
        cumulative_realized_loss_usd=0.0,
        starting_capital_usd=1500.0,
    )
    assert by_trade.triggered is True
    assert by_trade.causes == (ReviewTriggerCause.trade_count,)
    # Loss gate fires at 10% of starting capital ($150 on $1500).
    by_loss = evaluate_thesis_performance_review_trigger(
        profile=profile,
        completed_theses_since_last_review=9,
        cumulative_realized_loss_usd=150.0,
        starting_capital_usd=1500.0,
    )
    assert by_loss.triggered is True
    assert by_loss.causes == (ReviewTriggerCause.cumulative_loss,)


# ---------------------------------------------------------------------------
# Defensive validation
# ---------------------------------------------------------------------------


def test_marker_mismatch_raises_value_error() -> None:
    """A profile with thesis_performance_review but the wrong risk_priority raises."""
    profile = _synthetic_profile(
        risk_priority=RiskPriority.concentration_management,
        thesis_performance_review=ThesisPerformanceReviewConfig(
            trade_count_threshold=20, loss_pct_threshold=0.15
        ),
    )
    with pytest.raises(ValueError, match="risk_priority"):
        evaluate_thesis_performance_review_trigger(
            profile=profile,
            completed_theses_since_last_review=20,
            cumulative_realized_loss_usd=0.0,
            starting_capital_usd=1500.0,
        )


def test_negative_completed_theses_raises(micro_profile: ProfileConfig) -> None:
    with pytest.raises(ValueError, match="completed_theses_since_last_review"):
        evaluate_thesis_performance_review_trigger(
            profile=micro_profile,
            completed_theses_since_last_review=-1,
            cumulative_realized_loss_usd=0.0,
            starting_capital_usd=1500.0,
        )


@pytest.mark.parametrize("starting_capital", [0.0, -100.0])
def test_non_positive_starting_capital_raises(
    micro_profile: ProfileConfig, starting_capital: float
) -> None:
    with pytest.raises(ValueError, match="starting_capital_usd"):
        evaluate_thesis_performance_review_trigger(
            profile=micro_profile,
            completed_theses_since_last_review=10,
            cumulative_realized_loss_usd=0.0,
            starting_capital_usd=starting_capital,
        )


# ---------------------------------------------------------------------------
# ThesisPerformanceReviewConfig validation
# ---------------------------------------------------------------------------


def test_config_rejects_zero_trade_count_threshold() -> None:
    with pytest.raises(ValidationError):
        ThesisPerformanceReviewConfig(trade_count_threshold=0, loss_pct_threshold=0.15)


def test_config_rejects_negative_loss_pct_threshold() -> None:
    with pytest.raises(ValidationError):
        ThesisPerformanceReviewConfig(trade_count_threshold=20, loss_pct_threshold=-0.05)


def test_config_rejects_loss_pct_above_one() -> None:
    """The fraction-space invariant: 0 < loss_pct_threshold <= 1."""
    with pytest.raises(ValidationError):
        ThesisPerformanceReviewConfig(trade_count_threshold=20, loss_pct_threshold=1.5)


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------


def test_micro_yaml_round_trips_with_documented_thresholds(
    micro_profile: ProfileConfig,
) -> None:
    review_config = micro_profile.thesis_performance_review
    assert review_config is not None
    assert review_config.trade_count_threshold == 20
    assert review_config.loss_pct_threshold == 0.15


@pytest.mark.parametrize("profile_member", [Profile.small, Profile.medium, Profile.large])
def test_non_micro_yaml_round_trips_with_no_review_block(profile_member: Profile) -> None:
    profile = load_profiles(CONFIG_DIR)[profile_member]
    assert profile.thesis_performance_review is None
