"""Thesis-performance-review trigger predicate (story 01e).

The trigger applies only to the micro profile. It fires when either of two
gates is met:

- the system has completed `trade_count_threshold` theses since the last
  review, or
- the cumulative realized losses since the last review reach
  `loss_pct_threshold` of the starting capital.

This module ships the stateless predicate. The consumer owns counter tracking,
operator-pause semantics, activity-log emission, and the post-review reset.
See `docs/design/06-risk-guardrails/rules-and-limits.md` § Profile: Micro.
"""

from dataclasses import dataclass
from enum import StrEnum

from alphamind.config.models.profiles import ProfileConfig, RiskPriority


class ReviewTriggerCause(StrEnum):
    """Stable identifier for which gate fired the review trigger."""

    trade_count = "trade_count"
    cumulative_loss = "cumulative_loss"


@dataclass(frozen=True, slots=True)
class ReviewTriggerSignal:
    """Outcome of evaluating the thesis-performance-review trigger.

    `causes` is empty when not triggered; ordered as `(trade_count,
    cumulative_loss)` when both gates fire. `trade_count_threshold` and
    `loss_threshold_usd` are zero when the trigger is not applicable to the
    profile.
    """

    applicable: bool
    triggered: bool
    causes: tuple[ReviewTriggerCause, ...]
    completed_theses_since_last_review: int
    cumulative_realized_loss_usd: float
    starting_capital_usd: float
    trade_count_threshold: int
    loss_threshold_usd: float


def evaluate_thesis_performance_review_trigger(
    *,
    profile: ProfileConfig,
    completed_theses_since_last_review: int,
    cumulative_realized_loss_usd: float,
    starting_capital_usd: float,
) -> ReviewTriggerSignal:
    """Evaluate whether the operator-pause review should fire.

    Inputs are stateless — the consumer owns counter tracking. A negative
    `cumulative_realized_loss_usd` represents a net realized gain; the
    cumulative-loss gate trivially does not fire.

    Raises:
        ValueError: if `completed_theses_since_last_review < 0`,
            `starting_capital_usd <= 0`, or the profile carries
            `thesis_performance_review` while `risk_priority !=
            RiskPriority.signal_quality` (a marker mismatch indicating a
            YAML configuration error).
    """
    if completed_theses_since_last_review < 0:
        raise ValueError(
            "completed_theses_since_last_review must be non-negative, "
            f"got {completed_theses_since_last_review}"
        )
    if starting_capital_usd <= 0:
        raise ValueError(f"starting_capital_usd must be positive, got {starting_capital_usd}")

    review_config = profile.thesis_performance_review
    if review_config is None:
        return ReviewTriggerSignal(
            applicable=False,
            triggered=False,
            causes=(),
            completed_theses_since_last_review=completed_theses_since_last_review,
            cumulative_realized_loss_usd=cumulative_realized_loss_usd,
            starting_capital_usd=starting_capital_usd,
            trade_count_threshold=0,
            loss_threshold_usd=0.0,
        )

    if profile.risk_priority != RiskPriority.signal_quality:
        raise ValueError(
            "thesis_performance_review configured on a profile whose "
            f"risk_priority is {profile.risk_priority.value!r}; expected "
            f"{RiskPriority.signal_quality.value!r} — the two markers must agree."
        )

    loss_threshold_usd = starting_capital_usd * review_config.loss_pct_threshold
    causes: list[ReviewTriggerCause] = []
    if completed_theses_since_last_review >= review_config.trade_count_threshold:
        causes.append(ReviewTriggerCause.trade_count)
    if cumulative_realized_loss_usd >= loss_threshold_usd:
        causes.append(ReviewTriggerCause.cumulative_loss)

    return ReviewTriggerSignal(
        applicable=True,
        triggered=bool(causes),
        causes=tuple(causes),
        completed_theses_since_last_review=completed_theses_since_last_review,
        cumulative_realized_loss_usd=cumulative_realized_loss_usd,
        starting_capital_usd=starting_capital_usd,
        trade_count_threshold=review_config.trade_count_threshold,
        loss_threshold_usd=loss_threshold_usd,
    )
