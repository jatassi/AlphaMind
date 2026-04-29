"""Tests for profile boundary detection (story 01b).

The profile boundary detection function classifies a portfolio's
``total_equity`` against the active profile's ``capital_range_usd`` so the
command-center alert framework can fire the ``Profile boundary crossed``
rule. The function is pure: every input is a parameter; no I/O or logging.
"""

from collections.abc import Mapping
from pathlib import Path

import pytest

from alphamind.config.loaders import load_profiles
from alphamind.config.models import Profile
from alphamind.config.models.profiles import ProfileConfig
from alphamind.risk_guardrails.rules_and_limits import (
    ProfileBoundaryEvaluation,
    ProfileBoundaryStatus,
    evaluate_profile_boundary,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture(scope="module")
def profiles() -> Mapping[Profile, ProfileConfig]:
    return load_profiles(CONFIG_DIR)


def test_in_range_equity_returns_within(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    small = profiles[Profile.small]

    result = evaluate_profile_boundary(total_equity_usd=10000.0, profile=small)

    assert result.status is ProfileBoundaryStatus.within


def test_equity_at_lower_bound_is_within(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    micro = profiles[Profile.micro]

    result = evaluate_profile_boundary(total_equity_usd=0.0, profile=micro)

    assert result.status is ProfileBoundaryStatus.within


def test_equity_at_upper_bound_is_within(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    micro = profiles[Profile.micro]

    result = evaluate_profile_boundary(total_equity_usd=4999.0, profile=micro)

    assert result.status is ProfileBoundaryStatus.within


def test_equity_above_upper_bound_is_above(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    micro = profiles[Profile.micro]

    result = evaluate_profile_boundary(total_equity_usd=5000.0, profile=micro)

    assert result.status is ProfileBoundaryStatus.above


def test_equity_below_lower_bound_is_below(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    """Equity strictly less than ``capital_range_usd[0]`` is `below`.

    Uses small (``[5000, 15000]``) to probe a positive lower bound; micro's
    lower bound is 0 and is shadowed by the negative-equity ``ValueError``.
    """
    small = profiles[Profile.small]

    result = evaluate_profile_boundary(total_equity_usd=4999.0, profile=small)

    assert result.status is ProfileBoundaryStatus.below


def test_negative_equity_raises_value_error_naming_offending_value(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    micro = profiles[Profile.micro]

    with pytest.raises(ValueError, match=r"-1\.0"):
        evaluate_profile_boundary(total_equity_usd=-1.0, profile=micro)


def test_returned_evaluation_carries_inputs(
    profiles: Mapping[Profile, ProfileConfig],
) -> None:
    medium = profiles[Profile.medium]

    result = evaluate_profile_boundary(total_equity_usd=10000.0, profile=medium)

    assert result.total_equity_usd == 10000.0
    assert result.lower_bound_usd == medium.capital_range_usd[0]
    assert result.upper_bound_usd == medium.capital_range_usd[1]


@pytest.mark.parametrize("profile_name", list(Profile))
def test_midpoint_of_each_shipped_profile_is_within(
    profile_name: Profile, profiles: Mapping[Profile, ProfileConfig]
) -> None:
    profile = profiles[profile_name]
    lower, upper = profile.capital_range_usd
    midpoint = (lower + upper) / 2

    result = evaluate_profile_boundary(total_equity_usd=midpoint, profile=profile)

    assert result.status is ProfileBoundaryStatus.within


def test_evaluation_is_frozen_and_uses_slots() -> None:
    """``ProfileBoundaryEvaluation`` is a frozen, slots dataclass.

    ``__dataclass_params__`` is set by the ``@dataclass`` decorator and is
    the standard introspection surface for these flags. ``vars()`` is used
    so the lookup goes through the class ``__dict__`` rather than the
    attribute-typed ``type`` interface (the params attribute is not
    declared in mypy's stubs for ``type``).
    """
    params = vars(ProfileBoundaryEvaluation)["__dataclass_params__"]
    assert params.frozen is True
    assert params.slots is True
