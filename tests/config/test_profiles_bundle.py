"""Tests for profiles bundle and loader (story 04a)."""

from pathlib import Path

import pytest

from alphamind.config.loaders import load_profiles
from alphamind.config.models import Profile

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


_OPTIONS_RULE_KEYS = frozenset(
    {
        "position_max_loss_options_pct",
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
    }
)
_SHORT_RULE_KEYS = frozenset(
    {
        "net_short_pct",
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
    }
)
_ALL_NINETEEN_RULE_KEYS = frozenset(
    {
        "position_max_size_pct",
        "position_max_loss_equity_pct",
        "position_max_loss_options_pct",
        "sector_concentration_pct",
        "net_long_pct",
        "net_short_pct",
        "gross_exposure_pct",
        "daily_drawdown_pct",
        "cumulative_drawdown_pct",
        "correlation_max",
        "thesis_dependency_flag_pct",
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
        "min_cash_reserve_pct",
        "pending_order_capital_pct",
    }
)


def test_load_profiles_returns_entry_per_profile_enum_member() -> None:
    profiles = load_profiles(CONFIG_DIR)
    assert set(profiles.keys()) == set(Profile)
    assert len(profiles) == 4


def test_micro_profile_disables_options_and_shorts_and_omits_their_rules() -> None:
    profiles = load_profiles(CONFIG_DIR)
    micro = profiles[Profile.micro]
    assert micro.feature_flags.options_enabled is False
    assert micro.feature_flags.short_selling_enabled is False
    assert micro.active_sectors == ["tech", "semis"]
    keys = set(micro.rule_values)
    assert keys.isdisjoint(_OPTIONS_RULE_KEYS)
    assert keys.isdisjoint(_SHORT_RULE_KEYS)


def test_small_profile_disables_options_and_shorts_and_omits_their_rules() -> None:
    profiles = load_profiles(CONFIG_DIR)
    small = profiles[Profile.small]
    assert small.feature_flags.options_enabled is False
    assert small.feature_flags.short_selling_enabled is False
    assert small.active_sectors == ["tech", "semis", "financials"]
    keys = set(small.rule_values)
    assert keys.isdisjoint(_OPTIONS_RULE_KEYS)
    assert keys.isdisjoint(_SHORT_RULE_KEYS)


def test_medium_profile_enables_full_feature_set_and_carries_all_19_rules() -> None:
    profiles = load_profiles(CONFIG_DIR)
    medium = profiles[Profile.medium]
    assert medium.feature_flags.options_enabled is True
    assert medium.feature_flags.short_selling_enabled is True
    assert medium.active_sectors == ["tech", "semis", "financials", "energy"]
    assert set(medium.rule_values) == _ALL_NINETEEN_RULE_KEYS


def test_large_profile_matches_medium_flags_sectors_and_carries_all_19_rules() -> None:
    profiles = load_profiles(CONFIG_DIR)
    large = profiles[Profile.large]
    assert large.feature_flags.options_enabled is True
    assert large.feature_flags.short_selling_enabled is True
    assert large.active_sectors == ["tech", "semis", "financials", "energy"]
    assert set(large.rule_values) == _ALL_NINETEEN_RULE_KEYS


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _minimal_profile_payload(**overrides: object) -> dict[str, object]:
    """Profile payload that validates cleanly; per-test overrides patch a field."""
    payload: dict[str, object] = {
        "capital_range_usd": [25000, 50000],
        "risk_priority": "exposure_management",
        "feature_flags": {
            "options_enabled": True,
            "short_selling_enabled": True,
            "fractional_shares_required": False,
        },
        "active_sectors": ["tech", "semis"],
        "min_position_size_usd": 75,
        "rule_values": {"position_max_size_pct": 5.0},
        "agent_token_budgets": {
            "pm": {"context": [1500, 2500], "output": [1200, 2500]},
        },
    }
    payload.update(overrides)
    return payload


def test_inverted_capital_range_raises() -> None:
    from alphamind.config.models import ProfileConfig

    with pytest.raises((ValueError, TypeError), match="capital_range_usd"):
        ProfileConfig.model_validate(_minimal_profile_payload(capital_range_usd=[50000, 25000]))


def test_inverted_pm_context_token_budget_raises() -> None:
    from alphamind.config.models import ProfileConfig

    with pytest.raises((ValueError, TypeError), match="Token budget lower must be <= upper"):
        ProfileConfig.model_validate(
            _minimal_profile_payload(
                agent_token_budgets={
                    "pm": {"context": [2500, 1500], "output": [1200, 2500]},
                }
            )
        )


def test_empty_active_sectors_raises() -> None:
    from alphamind.config.models import ProfileConfig

    with pytest.raises((ValueError, TypeError), match="active_sectors must not be empty"):
        ProfileConfig.model_validate(_minimal_profile_payload(active_sectors=[]))


def test_uppercase_rule_values_key_raises() -> None:
    from alphamind.config.models import ProfileConfig

    with pytest.raises((ValueError, TypeError), match="does not match"):
        ProfileConfig.model_validate(
            _minimal_profile_payload(rule_values={"PositionMaxSizePct": 5.0})
        )


def test_duplicate_active_sectors_raises() -> None:
    from alphamind.config.models import ProfileConfig

    with pytest.raises((ValueError, TypeError), match="duplicate"):
        ProfileConfig.model_validate(_minimal_profile_payload(active_sectors=["tech", "tech"]))


def test_load_profiles_raises_when_profile_file_missing(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    # Only ship three of four profile files; the loader must reject the gap.
    for name in ("micro", "small", "medium"):
        (profiles_dir / f"{name}.yaml").write_text(
            (CONFIG_DIR / "profiles" / f"{name}.yaml").read_text()
        )
    with pytest.raises(FileNotFoundError, match="large"):
        load_profiles(tmp_path)
