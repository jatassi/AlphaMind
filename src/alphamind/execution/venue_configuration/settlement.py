"""Settlement-date calculator for sell-side fills (ALP-391).

Computes the date proceeds from a fill become available cash,
per ``SETTLEMENT_DAYS_BY_INSTRUMENT`` and Alpaca's trading calendar.
"""

from __future__ import annotations

from datetime import date

from alphamind.execution.venue_configuration.calendar_cache import TradingCalendarCache
from alphamind.execution.venue_configuration.constants import (
    SETTLEMENT_DAYS_BY_INSTRUMENT,
    InstrumentClass,
)


def compute_settlement_date(
    trade_date: date,
    instrument_class: InstrumentClass,
    *,
    calendar: TradingCalendarCache,
) -> date:
    """Return the settlement date for a trade executed on ``trade_date``.

    Per venue-configuration.md § Settlement:

    * Equities and options: T+1 (one business day after trade_date).
    * Crypto: T+0 (same day; not in AlphaMind's universe).

    Business days are derived from Alpaca's trading calendar — weekends
    and holidays are excluded. Uses ``TradingCalendarCache.business_day_offset``.

    For trades executed on a non-business day (e.g., a CA settled in the
    OMS on a holiday), the trade_date is rolled forward to the next
    business day before applying the settlement offset.
    """
    settlement_days = SETTLEMENT_DAYS_BY_INSTRUMENT[instrument_class]
    effective_trade_date = calendar.business_day_offset(trade_date, 0)
    if settlement_days == 0:
        return effective_trade_date
    return calendar.business_day_offset(effective_trade_date, settlement_days)


def is_settled(
    trade_date: date,
    instrument_class: InstrumentClass,
    *,
    as_of: date,
    calendar: TradingCalendarCache,
) -> bool:
    """True if a trade executed on ``trade_date`` has settled as of ``as_of``."""
    return as_of >= compute_settlement_date(trade_date, instrument_class, calendar=calendar)
