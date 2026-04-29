"""Profile boundary detection (story 01b).

Pure predicate that classifies a portfolio's ``total_equity`` against the
active profile's ``capital_range_usd`` as ``within``, ``above``
(graduation candidate), or ``below`` (downgrade candidate). Backs the
``Profile boundary crossed`` alert in the command-center default rule set
and the operational advisory in
``docs/design/06-risk-guardrails/rules-and-limits.md`` § Transitioning
between profiles. Bounds are inclusive on both ends; the shipped profiles'
ranges are non-overlapping by design (semantic-self-test invariant in
story 06b), so an equity at exactly an endpoint maps unambiguously to a
single profile.
"""

from dataclasses import dataclass
from enum import StrEnum

from alphamind.config.models.profiles import ProfileConfig


class ProfileBoundaryStatus(StrEnum):
    """Where a portfolio's equity sits relative to the active profile's bounds."""

    within = "within"
    above = "above"
    below = "below"


@dataclass(frozen=True, slots=True)
class ProfileBoundaryEvaluation:
    """Outcome of comparing equity against a profile's ``capital_range_usd``.

    Carries the comparison context so alert payloads can render the bounds
    and equity without re-fetching the profile.
    """

    status: ProfileBoundaryStatus
    total_equity_usd: float
    lower_bound_usd: int
    upper_bound_usd: int


def evaluate_profile_boundary(
    *, total_equity_usd: float, profile: ProfileConfig
) -> ProfileBoundaryEvaluation:
    """Classify ``total_equity_usd`` against ``profile.capital_range_usd``.

    Bounds are inclusive on both ends: equity at exactly the lower or upper
    bound is ``within``. Raises ``ValueError`` on a negative equity since a
    negative ``portfolio_summary.total_equity`` indicates upstream sign error
    or a bracketed margin call mid-cascade — both structural failures that
    should surface loudly rather than silently classify as ``below``.
    """
    if total_equity_usd < 0:
        raise ValueError(f"total_equity_usd must be non-negative, got {total_equity_usd}")
    lower, upper = profile.capital_range_usd
    if total_equity_usd < lower:
        status = ProfileBoundaryStatus.below
    elif total_equity_usd > upper:
        status = ProfileBoundaryStatus.above
    else:
        status = ProfileBoundaryStatus.within
    return ProfileBoundaryEvaluation(
        status=status,
        total_equity_usd=total_equity_usd,
        lower_bound_usd=lower,
        upper_bound_usd=upper,
    )
