"""Non-tunable venue constants for the Alpaca execution environment (ALP-385).

Per ``docs/design/configuration-management.md § In code``, these values are
regulatory or structurally fixed — they live in source, not yaml.  Callers
that need the tunable surface (URLs, API-key env-var names, session windows)
should use the ``VenueConfig`` Pydantic model loaded from ``venue.yaml``.

Reference design docs:
* ``docs/design/05-execution-layer/venue-configuration.md`` — settlement,
  regulatory constraints, margin tiers, fee schedule reference.
* ``docs/design/05-execution-layer/broker-adapter.md § Fee reporting``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

InstrumentClass = Literal["equity", "option", "crypto"]
FeeCode = Literal["TAF", "CAT", "SEC", "ORF", "OCC"]
FeeBase = Literal["per_share", "per_contract", "per_dollar"]
FeeSide = Literal["both", "sell"]
FeeAssetClass = Literal["equity", "option"]

# ---------------------------------------------------------------------------
# 1. Settlement constants
# ---------------------------------------------------------------------------

SETTLEMENT_DAYS_BY_INSTRUMENT: Final[dict[InstrumentClass, int]] = {
    "equity": 1,  # T+1 per venue-configuration.md § Settlement
    "option": 1,  # T+1 per venue-configuration.md § Settlement
    "crypto": 0,  # same-day; outside AlphaMind's universe, included for completeness
}

PRE_SETTLEMENT_CREDIT_ENABLED: Final[bool] = True
"""Alpaca extends credit against unsettled proceeds on margin accounts.

AlphaMind runs on margin (required for short selling and Reg T buying
power), so the OMS treats unsettled proceeds as available subject to
the margin model.  Per venue-configuration.md § Settlement.
"""

# ---------------------------------------------------------------------------
# 2. PDT constants
# ---------------------------------------------------------------------------

PDT_EQUITY_THRESHOLD_USD: Final[float] = 25_000.0
"""Pattern day trader equity threshold per FINRA / Alpaca.

Accounts with equity below this value are limited to three day trades
in a rolling five-business-day period.  Alpaca enforces server-side and
surfaces via /v2/account.daytrade_count + .pattern_day_trader.  The OMS
mirrors these into risk-budget accounting per venue-configuration.md.
"""

PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD: Final[int] = 3
"""Three day trades allowed in a rolling five-business-day window for
sub-$25k accounts.
"""

PDT_ROLLING_WINDOW_BUSINESS_DAYS: Final[int] = 5

# ---------------------------------------------------------------------------
# 3. Reg T margin constants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarginRequirements:
    """Initial and maintenance margin percentages for a single instrument side."""

    initial_pct: float
    maintenance_pct: float


REG_T_LONG_EQUITY: Final[MarginRequirements] = MarginRequirements(
    initial_pct=0.50,
    maintenance_pct=0.25,
)

REG_T_SHORT_EQUITY: Final[MarginRequirements] = MarginRequirements(
    initial_pct=1.50,
    maintenance_pct=1.30,
)
"""Short equity: 150% initial (50% margin + 100% short proceeds), 130%
maintenance.  ETB-only per broker-adapter.md.
"""

REG_T_LONG_OPTION: Final[MarginRequirements] = MarginRequirements(
    initial_pct=1.00,
    maintenance_pct=1.00,
)
"""Options buying is fully paid (100%).  Short options' margin is
formula-based on underlying price + strike + volatility, not constant.
The formula lives elsewhere; this constant captures the long-option case only.
"""

OVERNIGHT_BUYING_POWER_MULTIPLIER: Final[float] = 2.0
"""Reg T standard overnight buying power multiplier."""

INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED: Final[float] = 4.0
"""4x for PDT-qualified accounts with >= $25k equity."""

# ---------------------------------------------------------------------------
# 4. Margin interest tier constants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarginInterestTier:
    """A named margin-interest rate bracket keyed by minimum deposited equity."""

    name: Literal["standard", "elite"]
    annual_rate: float  # decimal (0.0625 = 6.25 %)
    minimum_deposited_usd: float


MARGIN_INTEREST_TIERS: Final[tuple[MarginInterestTier, ...]] = (
    MarginInterestTier(name="standard", annual_rate=0.0625, minimum_deposited_usd=0.0),
    MarginInterestTier(name="elite", annual_rate=0.0475, minimum_deposited_usd=100_000.0),
)
"""Sorted ascending by minimum_deposited_usd.

Resolution: walk in reverse, return the first tier whose minimum the
account meets.  Standard is the default; Elite requires >= $100k deposited.

Per venue-configuration.md § Regulatory and account constraints:
* Standard: 6.25 % annual
* Elite (accounts >= $100k deposited): 4.75 % annual
"""


def resolve_margin_interest_tier(account_deposited_usd: float) -> MarginInterestTier:
    """Return the highest margin-interest tier the account qualifies for.

    Args:
        account_deposited_usd: The account's total deposited equity in USD.

    Returns:
        The :class:`MarginInterestTier` with the lowest rate the account
        is eligible for (i.e. the *best* rate).
    """
    for tier in reversed(MARGIN_INTEREST_TIERS):
        if account_deposited_usd >= tier.minimum_deposited_usd:
            return tier
    # Unreachable: the standard tier has minimum_deposited_usd == 0.0
    return MARGIN_INTEREST_TIERS[0]


# ---------------------------------------------------------------------------
# 5. Regulatory fee rate table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeeRate:
    """A single regulatory fee rate entry."""

    code: FeeCode
    asset_class: FeeAssetClass
    base: FeeBase
    rate: float  # per-unit cost in USD
    side: FeeSide
    cap_per_trade: float | None  # USD cap per trade, if any


REGULATORY_FEE_RATES: Final[tuple[FeeRate, ...]] = (
    # ------------------------------------------------------------------
    # Equities
    # ------------------------------------------------------------------
    FeeRate(
        # FINRA Consolidated Audit Trail fee — all executed shares, both sides.
        # Source: https://www.finra.org/rules-guidance/consolidated-audit-trail/fees
        code="CAT",
        asset_class="equity",
        base="per_share",
        rate=0.000130,
        side="both",
        cap_per_trade=None,
    ),
    FeeRate(
        # FINRA Trading Activity Fee — equity sells only.
        # Capped at $8.30 per trade.
        # Source: https://www.finra.org/registration-exams-ce/broker-dealers/taf
        code="TAF",
        asset_class="equity",
        base="per_share",
        rate=0.000166,
        side="sell",
        cap_per_trade=8.30,
    ),
    FeeRate(
        # SEC Section 31 fee — equity sells only.
        # Rate: $27.80 per $1,000,000 of proceeds = $0.0000278 per dollar.
        # Effective 2025-02-18.
        # Source: https://www.sec.gov/rules-regulations/2024/10/section31-transaction-fees
        code="SEC",
        asset_class="equity",
        base="per_dollar",
        rate=0.0000278,
        side="sell",
        cap_per_trade=None,
    ),
    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    FeeRate(
        # FINRA Consolidated Audit Trail fee — all contracts, both sides.
        # Source: https://www.finra.org/rules-guidance/consolidated-audit-trail/fees
        code="CAT",
        asset_class="option",
        base="per_contract",
        rate=0.000130,
        side="both",
        cap_per_trade=None,
    ),
    FeeRate(
        # SEC Section 31 fee — option premium proceeds, sell side only.
        # Rate: $27.80 per $1,000,000 of proceeds = $0.0000278 per dollar.
        # Effective 2025-02-18.
        # Source: https://www.sec.gov/rules-regulations/2024/10/section31-transaction-fees
        code="SEC",
        asset_class="option",
        base="per_dollar",
        rate=0.0000278,
        side="sell",
        cap_per_trade=None,
    ),
    FeeRate(
        # FINRA Options Regulatory Fee — all contracts, both sides.
        # Rate effective 2025-01-01.
        # Source: https://www.finra.org/rules-guidance/rulebooks/finra-rules/6110
        code="ORF",
        asset_class="option",
        base="per_contract",
        rate=0.01870,
        side="both",
        cap_per_trade=None,
    ),
    FeeRate(
        # OCC (Options Clearing Corporation) clearing fee — all contracts, both sides.
        # Source: https://www.theocc.com/company-information/financial-highlights/fees
        code="OCC",
        asset_class="option",
        base="per_contract",
        rate=0.02,
        side="both",
        cap_per_trade=None,
    ),
)
