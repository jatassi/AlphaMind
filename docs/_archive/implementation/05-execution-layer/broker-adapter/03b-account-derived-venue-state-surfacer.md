# 03b — Account-derived venue state surfacer

## Goal

Compose `AccountStateQueries.get_account()` (story 02a) with the venue constants module (story 02g) into a typed `VenueAccountState` snapshot the OMS, guardrail layer, and risk-budget accounting consume on every invocation: PDT count + flag, day-trade headroom, regt_buying_power, daytrading_buying_power, maintenance margin, equity, the resolved `MarginInterestTier` for the account's deposited equity, and a derived `pdt_qualified` flag (equity ≥ $25k AND `pattern_day_trader is True`). One `read_venue_account_state(queries)` function that runs each invocation; no caching.

## Reading

* `docs/design/05-execution-layer/venue-configuration.md` § Regulatory and account constraints — PDT rule, margin tiers, Reg T ceiling, margin interest rates.
* `docs/design/05-execution-layer/broker-adapter.md` § Account state queries — `GET /v2/account` fields the surfacer consumes.
* `src/alphamind/execution/broker_adapter/queries.py` (story 02a) — `AccountStateQueries.get_account()` returning `TradeAccountSnapshot`.
* `src/alphamind/execution/venue_configuration/constants.py` (story 02g) — `PDT_EQUITY_THRESHOLD_USD`, `PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD`, `MARGIN_INTEREST_TIERS`, `resolve_margin_interest_tier`.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Cross-feature dependencies — guardrail enforcement layer (<issue id="98fea405-1ad0-4099-b4d9-7370fe1925e7">ALP-125</issue>) consumes `VenueAccountState.daytrade_count` for risk-budget accounting.

## Depends on

* <issue id="4029f936-92d1-4f06-a99a-6f7fd8b2841d">ALP-379</issue> (02a — Account state GET wrappers).
* <issue id="c2950555-2a1c-4bc4-b07a-03999713ebdf">ALP-385</issue> (02g — Venue constants module).

## Scope

Source under `src/alphamind/execution/venue_configuration/account_state.py` (new file). Tests at `tests/execution/venue_configuration/test_account_state.py`.

### 1. `VenueAccountState` snapshot

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from alphamind.execution.venue_configuration.constants import MarginInterestTier


class VenueAccountState(BaseModel):
    """Per-invocation snapshot of Alpaca account state with derived fields.

    Composes raw /v2/account fields with venue-constants resolution.
    """

    model_config = ConfigDict(frozen=True)

    fetched_at: datetime  # tz-aware

    # Raw fields from /v2/account (mirrored from TradeAccountSnapshot)
    cash: float
    equity: float
    buying_power: float
    regt_buying_power: float
    daytrading_buying_power: float
    maintenance_margin: float
    daytrade_count: int
    pattern_day_trader: bool

    # Derived fields
    pdt_qualified: bool          # equity >= 25k AND pattern_day_trader == True
    day_trade_headroom: int      # 3 - daytrade_count when sub-PDT, else "unlimited" (-1)
    margin_interest_tier: MarginInterestTier
    margin_interest_annual_rate: float  # tier.annual_rate echoed for convenience
```

### 2. `read_venue_account_state` function

```python
def read_venue_account_state(
    queries: AccountStateQueries,
    *,
    fetched_at: datetime | None = None,
) -> VenueAccountState:
    """Compose AccountStateQueries.get_account() into a VenueAccountState.

    fetched_at defaults to datetime.now(UTC) if not supplied; explicit
    parameter for testability and for use cases where the caller wants
    the snapshot's freshness anchored to invocation start time rather
    than current wall clock.
    """
    snapshot = queries.get_account()

    pdt_qualified = (
        snapshot.equity >= PDT_EQUITY_THRESHOLD_USD
        and snapshot.pattern_day_trader
    )

    if snapshot.equity < PDT_EQUITY_THRESHOLD_USD:
        # sub-PDT account: limited to N day trades in rolling window
        day_trade_headroom = max(
            0, PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD - snapshot.daytrade_count
        )
    else:
        # PDT-qualified accounts have no rolling-window cap
        day_trade_headroom = -1  # sentinel for "unlimited"

    tier = resolve_margin_interest_tier(snapshot.equity)

    return VenueAccountState(
        fetched_at=fetched_at or datetime.now(UTC),
        cash=snapshot.cash,
        equity=snapshot.equity,
        buying_power=snapshot.buying_power,
        regt_buying_power=snapshot.regt_buying_power,
        daytrading_buying_power=snapshot.daytrading_buying_power,
        maintenance_margin=snapshot.maintenance_margin,
        daytrade_count=snapshot.daytrade_count,
        pattern_day_trader=snapshot.pattern_day_trader,
        pdt_qualified=pdt_qualified,
        day_trade_headroom=day_trade_headroom,
        margin_interest_tier=tier,
        margin_interest_annual_rate=tier.annual_rate,
    )
```

### 3. Public surface

In `src/alphamind/execution/venue_configuration/__init__.py`:

```python
from alphamind.execution.venue_configuration.account_state import (
    VenueAccountState,
    read_venue_account_state,
)
```

### Out of scope

* Caching the snapshot — read each invocation (downstream consumers may cache for the duration of an invocation, but that's their concern).
* Risk-budget mirroring (the guardrail layer's role; <issue id="98fea405-1ad0-4099-b4d9-7370fe1925e7">ALP-125</issue> consumes this snapshot to mirror PDT count and headroom into its accounting).
* Margin-interest accrual computation — paper-evaluation harness (<issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>).
* Wash-sale tracking — out of scope per `venue-configuration.md` (logged but not blocked by adapter).

## Acceptance criteria

- [ ] `src/alphamind/execution/venue_configuration/account_state.py` exists; `VenueAccountState` and `read_venue_account_state` are importable from `alphamind.execution.venue_configuration`.
- [ ] `VenueAccountState` is a frozen Pydantic model with all the documented raw + derived fields.
- [ ] `read_venue_account_state(queries)` calls `queries.get_account()` exactly once per invocation.
- [ ] For an account with `equity=$50,000` and `pattern_day_trader=True`: `pdt_qualified=True`, `day_trade_headroom=-1`, `margin_interest_tier.name="standard"`.
- [ ] For an account with `equity=$150,000` and `pattern_day_trader=True`: `pdt_qualified=True`, `day_trade_headroom=-1`, `margin_interest_tier.name="elite"`.
- [ ] For an account with `equity=$10,000`, `daytrade_count=2`, `pattern_day_trader=False`: `pdt_qualified=False`, `day_trade_headroom=1`, `margin_interest_tier.name="standard"`.
- [ ] For an account with `equity=$10,000`, `daytrade_count=3`, `pattern_day_trader=False`: `day_trade_headroom=0` (clamped, not negative).
- [ ] For an account with `equity=$10,000`, `daytrade_count=4`, `pattern_day_trader=False`: `day_trade_headroom=0` (clamped — Alpaca server-side enforcement caught this; OMS still surfaces the clamped value).
- [ ] At the boundary `equity == 25_000.0`: `pdt_qualified` matches `pattern_day_trader` (PDT threshold is inclusive).
- [ ] `margin_interest_annual_rate` echoes `margin_interest_tier.annual_rate`.
- [ ] `fetched_at` is tz-aware UTC; defaults to `datetime.now(UTC)` when not supplied; uses the supplied value when caller passes one.
- [ ] Tests cover: each PDT/non-PDT × tier combination, day-trade-headroom clamping, fetched_at default vs. explicit, threshold boundaries.
- [ ] `uv run pytest tests/execution/venue_configuration/test_account_state.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/venue_configuration/test_account_state.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check via story 05's `verify_broker_adapter.py` venue-config phase that calls `read_venue_account_state(queries)` against the live Alpaca paper account and prints the resolved tier and headroom for operator inspection.
* Lint clean per CLAUDE.md.
