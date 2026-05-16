"""Portfolio P/L and drawdown rollup computations (story 05b).

Pure functions with no I/O. All functions are deterministic: identical inputs
produce identical outputs across repeated calls.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from alphamind._kernel.money import DECIMAL_ZERO, Money, signed_money
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.repository import PortfolioPnLInputs
from alphamind.portfolio_state.snapshot import PortfolioPnL
from alphamind.portfolio_state.views.positions import PositionView

_REQUIRED_ROLLING_KEYS: tuple[Literal["1d", "3d", "5d", "20d"], ...] = (
    "1d",
    "3d",
    "5d",
    "20d",
)


def compute_portfolio_pnl(
    open_positions: tuple[PositionView, ...],
    inputs: PortfolioPnLInputs,
    total_portfolio_value_usd: float,
) -> PortfolioPnL:
    """Produce a PortfolioPnL from open positions and OMS-aggregate inputs.

    ALP-489 — the ``total_unrealized_pnl_usd`` accumulator sums ``Money``
    values via Decimal arithmetic. ``PortfolioPnLInputs`` carries scalar
    floats at the repository boundary; each is converted via ``signed_money``
    once on entry, so the only float→Decimal hop happens at the boundary, not
    inside the accumulator.

    Raises:
        ValueError: If ``total_portfolio_value_usd`` is negative.
        KeyError: If ``inputs.rolling_realized_pnl`` is missing any of the
            four required keys (``"1d"``, ``"3d"``, ``"5d"``, ``"20d"``).
    """
    if total_portfolio_value_usd < 0:
        msg = f"total_portfolio_value_usd must be >= 0; got {total_portfolio_value_usd} (negative)"
        raise ValueError(msg)

    total_unrealized_pnl_usd = signed_money(
        sum((p.unrealized_pnl_usd for p in open_positions), DECIMAL_ZERO)
    )

    # Percentage of portfolio (zero-safe)
    if total_portfolio_value_usd == 0.0:
        total_unrealized_pnl_pct_of_portfolio = 0.0
    else:
        total_unrealized_pnl_pct_of_portfolio = (
            float(total_unrealized_pnl_usd) / total_portfolio_value_usd
        ) * 100.0

    daily_realized_pnl_usd = signed_money(inputs.daily_realized_pnl_usd)

    rolling_realized_pnl: dict[Literal["1d", "3d", "5d", "20d"], Money] = {}
    for key in _REQUIRED_ROLLING_KEYS:
        if key not in inputs.rolling_realized_pnl:
            raise KeyError(key)
        rolling_realized_pnl[key] = signed_money(inputs.rolling_realized_pnl[key])

    return PortfolioPnL(
        total_unrealized_pnl_usd=total_unrealized_pnl_usd,
        total_unrealized_pnl_pct_of_portfolio=total_unrealized_pnl_pct_of_portfolio,
        daily_realized_pnl_usd=daily_realized_pnl_usd,
        daily_total_pnl_usd=signed_money(daily_realized_pnl_usd + total_unrealized_pnl_usd),
        cumulative_realized_pnl_usd=signed_money(inputs.cumulative_realized_pnl_usd),
        rolling_realized_pnl=rolling_realized_pnl,
        win_rate_pct=inputs.win_rate_pct,
        average_win_size_usd=(
            signed_money(inputs.average_win_size_usd)
            if inputs.average_win_size_usd is not None
            else None
        ),
        average_loss_size_usd=(
            signed_money(inputs.average_loss_size_usd)
            if inputs.average_loss_size_usd is not None
            else None
        ),
        profit_factor=inputs.profit_factor,
    )


def compute_drawdown_by_source_pct(
    open_positions: tuple[PositionView, ...],
    current_drawdown_pct: float,
) -> dict[str, float]:
    """Return per-position contributions to the current drawdown percentage.

    Each key is a ``position_id``; the value is the position's proportional
    share of the total negative unrealized P/L, scaled to
    ``current_drawdown_pct``.

    Returns ``{}`` when ``current_drawdown_pct == 0.0`` or when no positions
    have negative unrealized P/L.

    ALP-489 — accumulators sum ``Money`` magnitudes via Decimal arithmetic;
    the returned percentage values are floats (derived ratios).

    Raises:
        ValueError: If ``current_drawdown_pct`` is negative.
    """
    if current_drawdown_pct < 0:
        msg = (
            f"current_drawdown_pct must be >= 0 (drawdown is a non-negative magnitude); "
            f"got {current_drawdown_pct}"
        )
        raise ValueError(msg)

    if current_drawdown_pct == 0.0:
        return {}

    negative_positions: list[tuple[str, Decimal]] = []
    total_negative: Decimal = DECIMAL_ZERO
    for position in open_positions:
        if position.unrealized_pnl_usd < 0:
            magnitude = -position.unrealized_pnl_usd
            negative_positions.append((position.position_id, magnitude))
            total_negative += magnitude

    if total_negative == 0:
        return {}

    return {
        pid: float(magnitude / total_negative) * current_drawdown_pct
        for pid, magnitude in negative_positions
    }


def compute_total_portfolio_value_usd(
    open_positions: tuple[PositionView, ...],
    pending_positions: tuple[PositionView, ...],
    cash_ledger: CashLedger,
) -> float:
    """Return total portfolio value as cash plus the magnitude of all position market values.

    Uses ``abs()`` on ``current_market_value_usd`` so short positions (which
    carry a negative signed market value per story 05a) contribute their
    magnitude rather than subtracting.

    ALP-489 — magnitudes accumulate via Decimal then convert to float for the
    cash-plus-magnitudes return (cash_ledger.current_cash_usd remains float at
    its repository-boundary type).
    """
    magnitudes = sum(
        (abs(p.current_market_value_usd) for p in (*open_positions, *pending_positions)),
        DECIMAL_ZERO,
    )
    return cash_ledger.current_cash_usd + float(magnitudes)
