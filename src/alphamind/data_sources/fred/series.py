"""
Configurable FRED series list for the macro collector.

Sourced from:
- docs/design/01-data-layer/api-key-checklist.md § FRED
- docs/implementation/01-data-layer/collector/05b-fred-vendor-adapter.md § Scope

Series are split by native frequency so ``bootstrap_series`` can apply the
correct lookback:
- Daily series  → 90-day backfill
- Monthly series → 24-month backfill
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Daily series (observation frequency: daily / weekly)
# ---------------------------------------------------------------------------
# All daily-native series; weekly series (STLFSI4, credit spreads) are also
# included here so the 90-day bootstrap window covers a meaningful sample.

DAILY_SERIES: list[str] = [
    # Treasury yields
    "DGS2",  # 2-Year Treasury Constant Maturity Rate
    "DGS5",  # 5-Year Treasury Constant Maturity Rate
    "DGS10",  # 10-Year Treasury Constant Maturity Rate
    "DGS30",  # 30-Year Treasury Constant Maturity Rate
    "T10Y2Y",  # 10Y-2Y Treasury yield spread
    "T10Y3M",  # 10Y-3M Treasury yield spread
    # Breakeven inflation (TIPS-implied)
    "T5YIE",  # 5-Year Breakeven Inflation Rate
    "T10YIE",  # 10-Year Breakeven Inflation Rate
    # Real yields
    "DFII5",  # 5-Year TIPS yield (real)
    "DFII10",  # 10-Year TIPS yield (real)
    # Funding / repo
    "SOFR",  # Secured Overnight Financing Rate
    "RRPONTSYD",  # Overnight Reverse Repo: Accepted Bids Amount (RRP usage)
    "DFEDTARU",  # Federal Funds Target Range - Upper Limit
    "DFEDTARL",  # Federal Funds Target Range - Lower Limit
    # Credit spreads (ICE BofA indices — weekly/daily)
    "BAMLH0A0HYM2",  # ICE BofA US High Yield Option-Adjusted Spread
    "BAMLC0A0CM",  # ICE BofA US Corporate Option-Adjusted Spread (IG)
    # Volatility / risk
    "VIXCLS",  # CBOE Volatility Index (VIX)
    # Dollar
    "DTWEXBGS",  # Nominal Broad U.S. Dollar Index
    # Commodity
    "DCOILWTICO",  # WTI Crude Oil Spot Price
    # St. Louis Financial Stress Index (weekly)
    "STLFSI4",  # St. Louis Fed Financial Stress Index
]

# ---------------------------------------------------------------------------
# Monthly series (observation frequency: monthly / quarterly)
# ---------------------------------------------------------------------------

MONTHLY_SERIES: list[str] = [
    # Inflation
    "CPIAUCSL",  # CPI All Urban Consumers (headline)
    "CPILFESL",  # CPI Less Food and Energy (core CPI)
    "PCEPI",  # PCE Price Index (headline)
    "PCEPILFE",  # PCE excluding food and energy (core PCE)
    # Employment (monthly NFP-adjacent)
    "UNRATE",  # Civilian Unemployment Rate
    "PAYEMS",  # Total Nonfarm Payrolls (monthly change)
    "AHETOT",  # Average Hourly Earnings of All Employees
    # PMI / ISM proxies (monthly)
    "MANEMP",  # Manufacturing Sector Employment (proxy)
    # Consumer
    "RSXFS",  # Advance Retail Sales (monthly)
    # Money market fund flows (monthly)
    "WRMFSL",  # Retail Money Market Fund Assets
    "WRMFNS",  # Non-Retail (Institutional) Money Market Fund Assets
]
