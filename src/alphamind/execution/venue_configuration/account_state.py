"""Account-derived venue state surfacer (ALP-387).

Composes ``AccountStateQueries.get_account()`` (story 02a) with the venue
constants module (story 02g) into a typed ``VenueAccountState`` snapshot.

The OMS, guardrail layer, and risk-budget accounting consume this snapshot
on every invocation.  No caching: ``read_venue_account_state`` runs each call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from alphamind.execution.venue_configuration.constants import (
    PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD,
    PDT_EQUITY_THRESHOLD_USD,
    MarginInterestTier,
    resolve_margin_interest_tier,
)

if TYPE_CHECKING:
    from alphamind.execution.broker_adapter.queries import AccountStateQueries


class VenueAccountState(BaseModel):
    """Per-invocation snapshot of Alpaca account state with derived fields.

    Composes raw /v2/account fields with venue-constants resolution.
    All fields are frozen at construction time — no mutation after creation.
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
    pdt_qualified: bool  # equity >= 25k AND pattern_day_trader == True
    day_trade_headroom: int  # 3 - daytrade_count when sub-PDT, else -1 (unlimited)
    margin_interest_tier: MarginInterestTier
    margin_interest_annual_rate: float  # tier.annual_rate echoed for convenience


def read_venue_account_state(
    queries: AccountStateQueries,
    *,
    fetched_at: datetime | None = None,
) -> VenueAccountState:
    """Compose AccountStateQueries.get_account() into a VenueAccountState.

    Args:
        queries: An ``AccountStateQueries`` instance providing ``get_account()``.
        fetched_at: Snapshot timestamp.  Defaults to ``datetime.now(UTC)`` if not
            supplied; pass an explicit value to anchor freshness to invocation
            start time or for test determinism.

    Returns:
        A frozen ``VenueAccountState`` with raw and derived fields populated.
    """
    snapshot = queries.get_account()

    pdt_qualified = snapshot.equity >= PDT_EQUITY_THRESHOLD_USD and snapshot.pattern_day_trader

    if snapshot.equity < PDT_EQUITY_THRESHOLD_USD:
        # Sub-PDT account: limited to N day trades in rolling window.
        day_trade_headroom = max(0, PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD - snapshot.daytrade_count)
    else:
        # PDT-qualified accounts have no rolling-window cap.
        day_trade_headroom = -1

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
