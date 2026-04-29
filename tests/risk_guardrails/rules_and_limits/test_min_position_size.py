"""Tests for the minimum position size pre-check (story 01c).

The pre-check is a profile-aware structural floor: positions whose dollar size
falls below the active profile's ``min_position_size_usd`` are rejected before
any percentage-based guardrail rules run. The tests below pin both the boundary
semantics (inclusive on the floor) and the per-profile coverage of the four
shipped configs (micro $50, small $75, medium $75, large $100).
"""

import dataclasses
from pathlib import Path

import pytest

from alphamind.config.loaders import load_profiles
from alphamind.config.models import Profile
from alphamind.risk_guardrails.rules_and_limits import (
    MinPositionSizeResult,
    MinPositionSizeStatus,
    check_min_position_size,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def test_at_floor_passes_for_micro() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    result = check_min_position_size(command_size_usd=50.0, profile=micro)
    assert result.status is MinPositionSizeStatus.pass_
    assert result.shortfall_usd == 0.0


def test_just_below_floor_fails_for_micro_with_correct_shortfall() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    result = check_min_position_size(command_size_usd=49.99, profile=micro)
    assert result.status is MinPositionSizeStatus.fail
    # Float tolerance: subtraction of 49.99 from int 50 yields ~0.01.
    assert result.shortfall_usd == pytest.approx(0.01)


def test_one_dollar_above_floor_passes() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    result = check_min_position_size(
        command_size_usd=float(micro.min_position_size_usd + 1), profile=micro
    )
    assert result.status is MinPositionSizeStatus.pass_
    assert result.shortfall_usd == 0.0


def test_one_dollar_below_floor_fails_with_unit_shortfall() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    result = check_min_position_size(
        command_size_usd=float(micro.min_position_size_usd - 1), profile=micro
    )
    assert result.status is MinPositionSizeStatus.fail
    assert result.shortfall_usd == 1.0


def test_far_below_floor_fails_with_floor_minus_one_shortfall() -> None:
    profiles = load_profiles(CONFIG_DIR)
    for profile_key in Profile:
        profile = profiles[profile_key]
        result = check_min_position_size(command_size_usd=1.0, profile=profile)
        assert result.status is MinPositionSizeStatus.fail
        assert result.shortfall_usd == float(profile.min_position_size_usd - 1)


def test_zero_command_size_raises_value_error() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    with pytest.raises(ValueError, match="command_size_usd must be > 0"):
        check_min_position_size(command_size_usd=0.0, profile=micro)


def test_negative_command_size_raises_value_error() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    with pytest.raises(ValueError, match="command_size_usd must be > 0"):
        check_min_position_size(command_size_usd=-100.0, profile=micro)


def test_at_floor_passes_for_small_profile() -> None:
    profiles = load_profiles(CONFIG_DIR)
    small = profiles[Profile.small]
    result = check_min_position_size(command_size_usd=75.0, profile=small)
    assert result.status is MinPositionSizeStatus.pass_


@pytest.mark.parametrize("profile_key", list(Profile))
def test_at_floor_passes_for_every_shipped_profile(profile_key: Profile) -> None:
    profiles = load_profiles(CONFIG_DIR)
    profile = profiles[profile_key]
    result = check_min_position_size(
        command_size_usd=float(profile.min_position_size_usd), profile=profile
    )
    assert result.status is MinPositionSizeStatus.pass_
    assert result.shortfall_usd == 0.0


def test_function_reads_floor_from_passed_profile_not_default() -> None:
    """A $60 command passes against micro (min $50) but fails against small (min $75)."""
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    small = profiles[Profile.small]

    against_micro = check_min_position_size(command_size_usd=60.0, profile=micro)
    against_small = check_min_position_size(command_size_usd=60.0, profile=small)

    assert against_micro.status is MinPositionSizeStatus.pass_
    assert against_small.status is MinPositionSizeStatus.fail
    assert against_small.shortfall_usd == pytest.approx(15.0)


def test_result_dataclass_carries_all_inputs_and_computed_shortfall() -> None:
    profiles = load_profiles(CONFIG_DIR)
    small = profiles[Profile.small]  # min_position_size_usd == 75
    result = check_min_position_size(command_size_usd=70.0, profile=small)
    assert isinstance(result, MinPositionSizeResult)
    assert result.status is MinPositionSizeStatus.fail
    assert result.command_size_usd == 70.0
    assert result.min_size_usd == 75
    assert result.shortfall_usd == pytest.approx(5.0)


def test_result_is_frozen_and_slotted_dataclass() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    result = check_min_position_size(command_size_usd=50.0, profile=micro)
    # frozen=True surfaces as a FrozenInstanceError on attribute assignment.
    # The dunder form mirrors ``tests/config/test_loader.py``'s convention and
    # avoids mypy's read-only-field warning that direct assignment would trip.
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.__setattr__("command_size_usd", 999.0)
    # slots=True: instances have no __dict__, only __slots__.
    assert not hasattr(result, "__dict__")
    assert hasattr(type(result), "__slots__")


def test_status_enum_members_are_exactly_pass_and_fail() -> None:
    assert {member.name for member in MinPositionSizeStatus} == {"pass_", "fail"}
    # StrEnum: members carry the documented ``value`` string. We compare via
    # ``.value`` so mypy doesn't flag a member-vs-literal comparison as
    # non-overlapping (the runtime equality is well-defined for StrEnum).
    assert MinPositionSizeStatus.pass_.value == "pass"
    assert MinPositionSizeStatus.fail.value == "fail"
