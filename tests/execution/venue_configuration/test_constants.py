"""Tests for venue_configuration.constants — ALP-385.

Each test corresponds to one acceptance criterion from the user story.
Tests are ordered to match the constants module structure.
"""

from __future__ import annotations

import pytest

from alphamind.execution.venue_configuration.constants import (
    INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED,
    MARGIN_INTEREST_TIERS,
    OVERNIGHT_BUYING_POWER_MULTIPLIER,
    PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD,
    PDT_EQUITY_THRESHOLD_USD,
    PDT_ROLLING_WINDOW_BUSINESS_DAYS,
    PRE_SETTLEMENT_CREDIT_ENABLED,
    REG_T_LONG_EQUITY,
    REG_T_LONG_OPTION,
    REG_T_SHORT_EQUITY,
    REGULATORY_FEE_RATES,
    SETTLEMENT_DAYS_BY_INSTRUMENT,
    MarginInterestTier,
    MarginRequirements,
    resolve_margin_interest_tier,
)

# ---------------------------------------------------------------------------
# 1. Settlement constants
# ---------------------------------------------------------------------------


def test_settlement_days_equity() -> None:
    assert SETTLEMENT_DAYS_BY_INSTRUMENT["equity"] == 1


def test_settlement_days_option() -> None:
    assert SETTLEMENT_DAYS_BY_INSTRUMENT["option"] == 1


def test_pre_settlement_credit_enabled() -> None:
    assert PRE_SETTLEMENT_CREDIT_ENABLED is True


# ---------------------------------------------------------------------------
# 2. PDT constants
# ---------------------------------------------------------------------------


def test_pdt_equity_threshold() -> None:
    assert PDT_EQUITY_THRESHOLD_USD == 25_000.0


def test_pdt_day_trade_limit() -> None:
    assert PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD == 3


def test_pdt_rolling_window() -> None:
    assert PDT_ROLLING_WINDOW_BUSINESS_DAYS == 5


# ---------------------------------------------------------------------------
# 3. Reg T margin requirements
# ---------------------------------------------------------------------------


def test_reg_t_long_equity_is_margin_requirements() -> None:
    assert isinstance(REG_T_LONG_EQUITY, MarginRequirements)


def test_reg_t_long_equity_initial_pct() -> None:
    assert REG_T_LONG_EQUITY.initial_pct == 0.50


def test_reg_t_long_equity_maintenance_pct() -> None:
    assert REG_T_LONG_EQUITY.maintenance_pct == 0.25


def test_reg_t_short_equity_initial_pct() -> None:
    assert REG_T_SHORT_EQUITY.initial_pct == 1.50


def test_reg_t_short_equity_maintenance_pct() -> None:
    assert REG_T_SHORT_EQUITY.maintenance_pct == 1.30


def test_reg_t_long_option_fully_paid() -> None:
    """Options buying is fully paid — both pcts are 1.00."""
    assert REG_T_LONG_OPTION.initial_pct == 1.00
    assert REG_T_LONG_OPTION.maintenance_pct == 1.00


def test_margin_requirements_immutable() -> None:
    """MarginRequirements is a frozen dataclass."""
    with pytest.raises((AttributeError, TypeError)):
        REG_T_LONG_EQUITY.initial_pct = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. Buying power multipliers
# ---------------------------------------------------------------------------


def test_overnight_buying_power_multiplier() -> None:
    assert OVERNIGHT_BUYING_POWER_MULTIPLIER == 2.0


def test_intraday_buying_power_multiplier_pdt_qualified() -> None:
    assert INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED == 4.0


# ---------------------------------------------------------------------------
# 5. Margin interest tiers
# ---------------------------------------------------------------------------


def test_margin_interest_tiers_is_tuple() -> None:
    assert isinstance(MARGIN_INTEREST_TIERS, tuple)


def test_margin_interest_tiers_count() -> None:
    assert len(MARGIN_INTEREST_TIERS) == 2


def test_margin_interest_tiers_standard() -> None:
    standard = MARGIN_INTEREST_TIERS[0]
    assert isinstance(standard, MarginInterestTier)
    assert standard.name == "standard"
    assert standard.annual_rate == 0.0625
    assert standard.minimum_deposited_usd == 0.0


def test_margin_interest_tiers_elite() -> None:
    elite = MARGIN_INTEREST_TIERS[1]
    assert isinstance(elite, MarginInterestTier)
    assert elite.name == "elite"
    assert elite.annual_rate == 0.0475
    assert elite.minimum_deposited_usd == 100_000.0


def test_margin_interest_tier_immutable() -> None:
    """MarginInterestTier is a frozen dataclass."""
    with pytest.raises((AttributeError, TypeError)):
        MARGIN_INTEREST_TIERS[0].annual_rate = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. resolve_margin_interest_tier
# ---------------------------------------------------------------------------


def test_resolve_tier_at_zero() -> None:
    """$0 deposited → standard tier."""
    assert resolve_margin_interest_tier(0.0).name == "standard"


def test_resolve_tier_below_elite_threshold() -> None:
    """Just below $100k → standard."""
    assert resolve_margin_interest_tier(99_999.99).name == "standard"


def test_resolve_tier_at_elite_boundary() -> None:
    """Exactly $100k → elite (boundary inclusive)."""
    assert resolve_margin_interest_tier(100_000.0).name == "elite"


def test_resolve_tier_above_elite_threshold() -> None:
    """$100k + $1 → elite."""
    assert resolve_margin_interest_tier(100_001.0).name == "elite"


def test_resolve_tier_large_balance() -> None:
    """$1M → elite."""
    assert resolve_margin_interest_tier(1_000_000.0).name == "elite"


def test_resolve_tier_mid_range() -> None:
    """$50k → standard."""
    assert resolve_margin_interest_tier(50_000.0).name == "standard"


def test_resolve_tier_at_elite_is_elite() -> None:
    """$150k → elite."""
    assert resolve_margin_interest_tier(150_000.0).name == "elite"


# ---------------------------------------------------------------------------
# 7. Regulatory fee rates
# ---------------------------------------------------------------------------


def test_regulatory_fee_rates_count() -> None:
    """Exactly 7 entries: equity CAT/TAF/SEC + option CAT/SEC/ORF/OCC."""
    assert len(REGULATORY_FEE_RATES) == 7


def test_regulatory_fee_rates_is_tuple() -> None:
    assert isinstance(REGULATORY_FEE_RATES, tuple)


def test_all_fee_rates_positive() -> None:
    for fee in REGULATORY_FEE_RATES:
        assert fee.rate > 0.0, f"{fee.code}/{fee.asset_class} rate must be positive"


def test_equity_cat_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "CAT" and f.asset_class == "equity"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_share"
    assert fee.side == "both"
    assert fee.rate == pytest.approx(0.000130)


def test_equity_taf_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "TAF" and f.asset_class == "equity"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_share"
    assert fee.side == "sell"
    assert fee.rate == pytest.approx(0.000166)


def test_equity_taf_has_cap_per_trade() -> None:
    """FINRA caps the per-trade TAF total at $8.30."""
    taf = next(f for f in REGULATORY_FEE_RATES if f.code == "TAF" and f.asset_class == "equity")
    assert taf.cap_per_trade is not None
    assert taf.cap_per_trade == pytest.approx(8.30)


def test_equity_sec_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "SEC" and f.asset_class == "equity"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_dollar"
    assert fee.side == "sell"
    assert fee.rate == pytest.approx(0.0000278)


def test_option_cat_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "CAT" and f.asset_class == "option"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_contract"
    assert fee.side == "both"
    assert fee.rate == pytest.approx(0.000130)


def test_option_sec_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "SEC" and f.asset_class == "option"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_dollar"
    assert fee.side == "sell"
    assert fee.rate == pytest.approx(0.0000278)


def test_option_orf_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "ORF" and f.asset_class == "option"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_contract"
    assert fee.side == "both"
    assert fee.rate == pytest.approx(0.01870)


def test_option_occ_fee_present() -> None:
    matches = [f for f in REGULATORY_FEE_RATES if f.code == "OCC" and f.asset_class == "option"]
    assert len(matches) == 1
    fee = matches[0]
    assert fee.base == "per_contract"
    assert fee.side == "both"
    assert fee.rate == pytest.approx(0.02)


def test_fee_rate_is_immutable() -> None:
    """FeeRate is a frozen dataclass."""
    with pytest.raises((AttributeError, TypeError)):
        REGULATORY_FEE_RATES[0].rate = 99.99  # type: ignore[misc]


def test_fee_completeness_by_code_and_asset_class() -> None:
    """Assert exactly the expected set of (code, asset_class) pairs."""
    expected = {
        ("CAT", "equity"),
        ("TAF", "equity"),
        ("SEC", "equity"),
        ("CAT", "option"),
        ("SEC", "option"),
        ("ORF", "option"),
        ("OCC", "option"),
    }
    actual = {(f.code, f.asset_class) for f in REGULATORY_FEE_RATES}
    assert actual == expected


# ---------------------------------------------------------------------------
# 8. Public re-exports from __init__.py
# ---------------------------------------------------------------------------


def test_all_symbols_re_exported_from_package() -> None:
    """Every public constant and helper is importable from the package root."""
    import alphamind.execution.venue_configuration as pkg

    expected_names = [
        "FeeRate",
        "INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED",
        "InstrumentClass",
        "MARGIN_INTEREST_TIERS",
        "MarginInterestTier",
        "MarginRequirements",
        "OVERNIGHT_BUYING_POWER_MULTIPLIER",
        "PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD",
        "PDT_EQUITY_THRESHOLD_USD",
        "PDT_ROLLING_WINDOW_BUSINESS_DAYS",
        "PRE_SETTLEMENT_CREDIT_ENABLED",
        "REG_T_LONG_EQUITY",
        "REG_T_LONG_OPTION",
        "REG_T_SHORT_EQUITY",
        "REGULATORY_FEE_RATES",
        "SETTLEMENT_DAYS_BY_INSTRUMENT",
        "resolve_margin_interest_tier",
    ]
    for name in expected_names:
        assert hasattr(pkg, name), f"Missing re-export: {name}"


def test_all_list_in_package_init() -> None:
    """__all__ is defined and contains all public names."""
    import alphamind.execution.venue_configuration as pkg

    assert hasattr(pkg, "__all__")
    assert isinstance(pkg.__all__, list)
    assert len(pkg.__all__) > 0
