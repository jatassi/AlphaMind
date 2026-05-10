"""Venue configuration runtime — settlement / sessions / margin / fees (ALP-121).

This package owns the in-code venue-state surface the OMS consumes for
accounting per ``docs/design/05-execution-layer/venue-configuration.md``:
settlement-day rules per instrument class, PDT / Reg T margin tiers,
regulatory fee rate tables, market-calendar caching, and account-derived
state surfacing.

Story 01 (ALP-378) ships only the package shell — populated submodules
arrive in subsequent stories per the parent-issue dependency graph
(`ALP-121 <https://linear.app/alphamind-jatassi/issue/ALP-121>`_):

* Story 02g — ``constants.py`` (settlement days, PDT threshold, Reg T tiers,
  margin-interest tiers, regulatory fee rate table).
* Story 03a — ``calendar_cache.py`` (market-calendar fetch + cache).
* Story 03b — ``account_state.py`` (account-derived venue-state surfacer).
* Story 04a — ``settlement.py`` (T+1 settlement-date calculator).
"""

from alphamind.execution.venue_configuration.account_state import (
    VenueAccountState,
    read_venue_account_state,
)
from alphamind.execution.venue_configuration.calendar_cache import (
    MarketClockSnapshot,
    TradingCalendarCache,
    get_market_clock,
)
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
    FeeAssetClass,
    FeeBase,
    FeeCode,
    FeeRate,
    FeeSide,
    InstrumentClass,
    MarginInterestTier,
    MarginRequirements,
    resolve_margin_interest_tier,
)
from alphamind.execution.venue_configuration.settlement import (
    compute_settlement_date,
    is_settled,
)

__all__ = [
    "INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED",
    "MARGIN_INTEREST_TIERS",
    "OVERNIGHT_BUYING_POWER_MULTIPLIER",
    "PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD",
    "PDT_EQUITY_THRESHOLD_USD",
    "PDT_ROLLING_WINDOW_BUSINESS_DAYS",
    "PRE_SETTLEMENT_CREDIT_ENABLED",
    "REGULATORY_FEE_RATES",
    "REG_T_LONG_EQUITY",
    "REG_T_LONG_OPTION",
    "REG_T_SHORT_EQUITY",
    "SETTLEMENT_DAYS_BY_INSTRUMENT",
    "FeeAssetClass",
    "FeeBase",
    "FeeCode",
    "FeeRate",
    "FeeSide",
    "InstrumentClass",
    "MarginInterestTier",
    "MarginRequirements",
    "MarketClockSnapshot",
    "TradingCalendarCache",
    "VenueAccountState",
    "compute_settlement_date",
    "get_market_clock",
    "is_settled",
    "read_venue_account_state",
    "resolve_margin_interest_tier",
]
