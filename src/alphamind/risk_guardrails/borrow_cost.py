"""Borrow-cost resolver and daily-accrual conversion (ALP-586).

The ``borrow_cost_resolver`` threaded through the decision pipeline maps a
ticker to its **annualized borrow fee rate** — the percentage figure stored in
``borrow_cost_daily.fee_pct`` (e.g. ``15.0`` for a 15%/yr fee). It returns
``None`` for a ticker the iBorrowDesk store has no current row for, so callers
can tell an uncovered ticker apart from a genuinely zero fee.

The resolver deliberately does not return a USD figure: daily borrow cost in
USD is ``notional * fee_rate / trading-days``, and notional is a property of
the proposal/position, not the ticker — so a ticker-only pure function cannot
compute it. :func:`daily_borrow_cost_usd` performs that conversion at the two
call sites that know the notional: the validation tool (proposal size) and the
library-snapshot translator (existing-position market value).
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.persistence.models import BorrowCostDaily

# Borrow fees accrue per trading day; ``borrow_cost_daily.fee_pct`` is the
# annualized rate, so the per-day accrual divides by the trading-day count.
_TRADING_DAYS_PER_YEAR = 252

__all__ = ["build_borrow_cost_resolver", "daily_borrow_cost_usd"]


def daily_borrow_cost_usd(*, notional_usd: float, annual_fee_pct: float) -> float:
    """Convert an annualized borrow-fee rate to a one-day USD accrual.

    ``annual_fee_pct`` is the ``borrow_cost_daily.fee_pct`` value — the
    annualized borrow fee as a percentage (``15.0`` for 15%/yr). The daily
    accrual is ``notional * (fee_pct / 100) / trading-days``.
    """
    return notional_usd * (annual_fee_pct / 100.0) / _TRADING_DAYS_PER_YEAR


def build_borrow_cost_resolver(session: Session) -> Callable[[str], float | None]:
    """Build a ticker → annualized-borrow-fee resolver from ``borrow_cost_daily``.

    Reads the latest ``fee_pct`` per ticker into a dict once; the returned
    resolver is that dict's lookup. It is pure (a fixed mapping for the whole
    invocation) and total (returns ``None`` rather than raising for an
    uncovered ticker), satisfying the
    ``ValidationToolState.borrow_cost_resolver`` purity contract.

    A ticker whose most recent row carries a NULL ``fee_pct`` (iBorrowDesk
    returned no fee that day) resolves to ``None`` — a stale earlier fee is
    not served as if current.
    """
    rows = session.execute(
        select(BorrowCostDaily.ticker, BorrowCostDaily.fee_pct).order_by(
            BorrowCostDaily.ticker, BorrowCostDaily.observation_date
        )
    ).all()
    # Ascending observation_date → the last row seen per ticker is its latest.
    latest_fee_pct: dict[str, float] = {}
    for ticker, fee_pct in rows:
        if fee_pct is None:
            latest_fee_pct.pop(ticker, None)
        else:
            latest_fee_pct[ticker] = fee_pct

    def _resolve(ticker: str) -> float | None:
        return latest_fee_pct.get(ticker)

    return _resolve
