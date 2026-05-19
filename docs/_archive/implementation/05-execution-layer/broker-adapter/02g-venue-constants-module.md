# 02g — Venue constants module

## Goal

Ship a pure-constants module under `src/alphamind/execution/venue_configuration/constants.py` defining the design-prescribed in-code venue parameters: settlement-day mapping per instrument class (T+1 for equities and options), PDT equity threshold ($25,000), Reg T initial/maintenance margin percentages per instrument class, margin interest tiers (Standard 6.25% / Elite 4.75% with $100,000 threshold), and the regulatory fee rate table (TAF / CAT / SEC / ORF / OCC). Per `venue.py` docstring and `configuration-management.md § In code`, these are non-tunable — they live in code, not yaml.

## Reading

* `docs/design/05-execution-layer/venue-configuration.md` § Settlement, § Regulatory and account constraints, § Configuration structure — the full constants surface.
* `docs/design/05-execution-layer/broker-adapter.md` § Fee reporting — fee categories applied per instrument and per side; the rate table reference.
* `src/alphamind/config/models/venue.py` — confirms tunable venue parameters (URLs, env-var names, session hours) live in yaml; the rest live in code.
* `docs/design/configuration-management.md § In code` — the design-prescribed boundary for code vs yaml.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (E), (F).

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton). Provides the `venue_configuration/` package init.

## Scope

Source under `src/alphamind/execution/venue_configuration/constants.py` (new file). Tests at `tests/execution/venue_configuration/test_constants.py`.

### 1. Settlement constants

```python
from typing import Final, Literal


InstrumentClass = Literal["equity", "option", "crypto"]


SETTLEMENT_DAYS_BY_INSTRUMENT: Final[dict[InstrumentClass, int]] = {
    "equity": 1,    # T+1 per venue-configuration.md
    "option": 1,    # T+1 per venue-configuration.md
    "crypto": 0,    # crypto settles same-day; not in AlphaMind's universe but completeness
}


PRE_SETTLEMENT_CREDIT_ENABLED: Final[bool] = True
"""Alpaca extends credit against unsettled proceeds on margin accounts.

AlphaMind runs on margin (required for short selling and Reg T buying
power), so the OMS treats unsettled proceeds as available subject to
the margin model. Per venue-configuration.md § Settlement.

"""
```

### 2. PDT constants

```python
PDT_EQUITY_THRESHOLD_USD: Final[float] = 25_000.0
"""Pattern day trader equity threshold per FINRA / Alpaca.

Accounts with equity below this value are limited to three day trades
in a rolling five-business-day period. Alpaca enforces server-side and
surfaces via /v2/account.daytrade_count + .pattern_day_trader. The OMS
mirrors these into risk-budget accounting per venue-configuration.md.

"""


PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD: Final[int] = 3
"""Three day trades allowed in a rolling five-business-day window for
sub-$25k accounts.

"""


PDT_ROLLING_WINDOW_BUSINESS_DAYS: Final[int] = 5
```

### 3. Reg T margin constants

```python
@dataclass(frozen=True)
class MarginRequirements:
    initial_pct: float
    maintenance_pct: float


REG_T_LONG_EQUITY: Final[MarginRequirements] = MarginRequirements(
    initial_pct=0.50, maintenance_pct=0.25
)
REG_T_SHORT_EQUITY: Final[MarginRequirements] = MarginRequirements(
    initial_pct=1.50, maintenance_pct=1.30
)
"""Short equity: 150% initial (50% margin + 100% short proceeds), 130%
maintenance, ETB-only per broker-adapter.md.

"""

REG_T_LONG_OPTION: Final[MarginRequirements] = MarginRequirements(
    initial_pct=1.00, maintenance_pct=1.00
)
"""Options buying is fully paid (100%); short options' margin is
formula-based on underlying price + strike + volatility, not constant.
The formula lives elsewhere; this constant captures the long-option
case only.

"""


OVERNIGHT_BUYING_POWER_MULTIPLIER: Final[float] = 2.0
"""Reg T standard."""

INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED: Final[float] = 4.0
"""4x for PDT-qualified accounts with >=$25k equity."""
```

### 4. Margin interest tier constants

```python
@dataclass(frozen=True)
class MarginInterestTier:
    name: Literal["standard", "elite"]
    annual_rate: float           # decimal (0.0625 = 6.25%)
    minimum_deposited_usd: float


MARGIN_INTEREST_TIERS: Final[tuple[MarginInterestTier, ...]] = (
    MarginInterestTier(name="standard", annual_rate=0.0625, minimum_deposited_usd=0.0),
    MarginInterestTier(name="elite", annual_rate=0.0475, minimum_deposited_usd=100_000.0),
)
"""Sorted ascending by minimum_deposited_usd. Resolution: walk in
reverse, return the first tier whose minimum the account meets.

"""


def resolve_margin_interest_tier(account_deposited_usd: float) -> MarginInterestTier:
    """Return the highest tier the account qualifies for."""
    ...
```

### 5. Regulatory fee rate table

```python
from typing import Literal


FeeCode = Literal["TAF", "CAT", "SEC", "ORF", "OCC"]
FeeBase = Literal["per_share", "per_contract", "per_dollar"]
FeeSide = Literal["both", "sell"]
FeeAssetClass = Literal["equity", "option"]


@dataclass(frozen=True)
class FeeRate:
    code: FeeCode
    asset_class: FeeAssetClass
    base: FeeBase
    rate: float                  # the per-unit cost
    side: FeeSide                # both / sell
    cap_per_trade: float | None  # some fees have per-trade caps (e.g., TAF)


REGULATORY_FEE_RATES: Final[tuple[FeeRate, ...]] = (
    # Equities
    FeeRate(code="CAT", asset_class="equity", base="per_share", rate=<rate>, side="both", cap_per_trade=None),
    FeeRate(code="TAF", asset_class="equity", base="per_share", rate=<rate>, side="sell", cap_per_trade=<cap>),
    FeeRate(code="SEC", asset_class="equity", base="per_dollar", rate=<rate>, side="sell", cap_per_trade=None),
    # Options
    FeeRate(code="CAT", asset_class="option", base="per_contract", rate=<rate>, side="both", cap_per_trade=None),
    FeeRate(code="SEC", asset_class="option", base="per_dollar", rate=<rate>, side="sell", cap_per_trade=None),
    FeeRate(code="ORF", asset_class="option", base="per_contract", rate=<rate>, side="both", cap_per_trade=None),
    FeeRate(code="OCC", asset_class="option", base="per_contract", rate=<rate>, side="both", cap_per_trade=None),
)
```

The actual `<rate>` values: implementer pulls current rates from FINRA/SEC/OCC published schedules and from Alpaca's documentation as of the implementation date. Each rate has a citation comment beside it pointing at the regulator's fee schedule URL. If a rate has a known scheduled change date in the future, add a comment noting it.

### 6. Public surface

In `src/alphamind/execution/venue_configuration/__init__.py`:

```python
from alphamind.execution.venue_configuration.constants import (
    FeeRate,
    INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED,
    InstrumentClass,
    MARGIN_INTEREST_TIERS,
    MarginInterestTier,
    MarginRequirements,
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
    resolve_margin_interest_tier,
)

__all__ = [
    # ... in alphabetical order
]
```

### Out of scope

* Calendar / clock cache — story 03a.
* Settlement-date calculator — story 04a.
* Account-derived venue state surfacer — story 03b.
* EOD fee-activity reader — defers to corporate-actions or paper-harness work tree per parent decision (F).
* Margin interest accrual computation — paper-evaluation harness (<issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>).

## Acceptance criteria

- [ ] `src/alphamind/execution/venue_configuration/constants.py` exists with all the documented constants.
- [ ] `SETTLEMENT_DAYS_BY_INSTRUMENT["equity"] == 1` and `SETTLEMENT_DAYS_BY_INSTRUMENT["option"] == 1`.
- [ ] `PRE_SETTLEMENT_CREDIT_ENABLED is True`.
- [ ] `PDT_EQUITY_THRESHOLD_USD == 25_000.0`, `PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD == 3`, `PDT_ROLLING_WINDOW_BUSINESS_DAYS == 5`.
- [ ] `REG_T_LONG_EQUITY.initial_pct == 0.50` and `.maintenance_pct == 0.25`.
- [ ] `REG_T_SHORT_EQUITY.initial_pct == 1.50` and `.maintenance_pct == 1.30`.
- [ ] `OVERNIGHT_BUYING_POWER_MULTIPLIER == 2.0` and `INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED == 4.0`.
- [ ] `MARGIN_INTEREST_TIERS` is a tuple of two `MarginInterestTier` records: `standard` (0.0625, $0) and `elite` (0.0475, $100k).
- [ ] `resolve_margin_interest_tier(50_000.0).name == "standard"`.
- [ ] `resolve_margin_interest_tier(150_000.0).name == "elite"`.
- [ ] `resolve_margin_interest_tier(100_000.0).name == "elite"` (boundary inclusive).
- [ ] `REGULATORY_FEE_RATES` contains exactly 7 entries: equity CAT, equity TAF, equity SEC, option CAT, option SEC, option ORF, option OCC.
- [ ] Each `FeeRate.rate` is a positive float with a comment naming the source regulator + schedule URL.
- [ ] Equity TAF carries a non-None `cap_per_trade` value (FINRA caps the per-trade total).
- [ ] All constants are `Final[...]` typed; constants are immutable (cannot be reassigned at runtime).
- [ ] Tests assert exact equality on every constant value; tier-resolution edge cases ($0, $99,999.99, $100,000, $100,001, $1,000,000); fee-rate completeness against the documented set.
- [ ] `src/alphamind/execution/venue_configuration/__init__.py` re-exports every public constant + helper.
- [ ] `uv run pytest tests/execution/venue_configuration/test_constants.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/venue_configuration/test_constants.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check by `python -c "from alphamind.execution.venue_configuration import REG_T_SHORT_EQUITY, MARGIN_INTEREST_TIERS, REGULATORY_FEE_RATES; print(REG_T_SHORT_EQUITY); print(MARGIN_INTEREST_TIERS); print(len(REGULATORY_FEE_RATES))"`.
* Lint clean per CLAUDE.md.
