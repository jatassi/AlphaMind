"""Library-shape snapshot translator (ALP-402).

Exports ``to_library_snapshot`` — the single bridge between the assembled OMS
Pydantic ``PortfolioStateSnapshot`` and the math-library dataclass
``PortfolioStateSnapshot`` (aliased as ``LibrarySnapshot`` to disambiguate).

Story 02's composition runner consumes this translator.  The translator is
pure: no clock reads, no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.risk_guardrails.guardrail_evaluation import (
    AssetType,
    ExistingPosition,
    Greeks,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Direction as LibraryDirection,
)

__all__ = ["LibrarySnapshot", "to_library_snapshot"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Direction / AssetType translation tables
# ---------------------------------------------------------------------------

_DIRECTION_TO_LIBRARY: dict[Direction, LibraryDirection] = {
    Direction.LONG: LibraryDirection.LONG,
    Direction.SHORT: LibraryDirection.SHORT,
}

_INSTRUMENT_TO_ASSET_TYPE: dict[InstrumentType, AssetType] = {
    InstrumentType.EQUITY: AssetType.EQUITY,
    InstrumentType.OPTIONS: AssetType.OPTION,
    InstrumentType.STRATEGY: AssetType.STRATEGY,
}


# ---------------------------------------------------------------------------
# Ticker extraction (mirrors _resolve_position_ticker in decision/strategist/runner.py)
# ---------------------------------------------------------------------------


def _resolve_ticker(
    details: EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails,
) -> str | None:
    """Extract the underlying ticker from a position's details payload.

    Mirrors ``_resolve_position_ticker`` in ``decision/strategist/runner.py``.
    Returns ``None`` when the ticker cannot be determined (e.g. empty strategy legs).
    """
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails) and details.legs:
        return details.legs[0].options.underlying_ticker
    return None


# ---------------------------------------------------------------------------
# Greeks builder
# ---------------------------------------------------------------------------


def _build_greeks(
    details: OptionsPositionDetails | StrategyPositionDetails,
) -> Greeks:
    """Build a library ``Greeks`` instance from options or strategy details.

    For ``OptionsPositionDetails``, reads the per-contract ``greeks`` field.
    For ``StrategyPositionDetails``, reads the pre-aggregated ``strategy_greeks``
    field (no per-leg aggregation needed — the assembler pre-computes this).
    """
    g = details.greeks if isinstance(details, OptionsPositionDetails) else details.strategy_greeks
    return Greeks(delta=g.delta, gamma=g.gamma, theta=g.theta, vega=g.vega)


# ---------------------------------------------------------------------------
# Portfolio greek accumulation
# ---------------------------------------------------------------------------


def _accumulate_portfolio_greeks(
    details: OptionsPositionDetails | StrategyPositionDetails,
    direction: Direction,
    portfolio_value_usd: float,
) -> tuple[float, float, float]:
    """Return (delta_contrib, theta_contrib, vega_contrib) as % of portfolio for one position.

    Contributions are signed by direction and scaled by qty * multiplier / portfolio_value * 100.
    """
    sign = 1.0 if direction == Direction.LONG else -1.0
    if isinstance(details, OptionsPositionDetails):
        qty, multiplier, g = details.contract_count, details.contract_multiplier, details.greeks
    else:  # StrategyPositionDetails
        qty = details.legs[0].options.contract_count if details.legs else 1.0
        multiplier = details.legs[0].options.contract_multiplier if details.legs else 1.0
        g = details.strategy_greeks
    scale = sign * qty * multiplier / portfolio_value_usd * 100.0
    return g.delta * scale, g.theta * scale, g.vega * scale


# ---------------------------------------------------------------------------
# Public translator
# ---------------------------------------------------------------------------


def to_library_snapshot(
    snapshot: PortfolioStateSnapshot,
    *,
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float] | None = None,
) -> LibrarySnapshot:
    """Translate a Pydantic ``PortfolioStateSnapshot`` to the library-shape dataclass.

    Parameters
    ----------
    snapshot:
        The assembled OMS snapshot.  The translator reads only fields already
        populated by the assembler — it does not re-derive what the assembler
        computes.
    sector_resolver:
        Maps an underlying ticker to its sector key string.
    borrow_cost_resolver:
        Maps a ticker to its daily borrow cost in USD.  Required only when
        short-selling is active; ``None`` is safe for long-only portfolios.

    Returns
    -------
    LibrarySnapshot
        The math-library dataclass ready for ``evaluate_proposals``.

    Raises
    ------
    ValueError
        If the ``position_max_size_pct`` rule is absent from
        ``snapshot.active_risk_parameters.entries``.
    """
    # ------------------------------------------------------------------
    # §2  Pre-aggregated portfolio-level fields
    # ------------------------------------------------------------------

    cash_usd = snapshot.cash_ledger.current_cash_usd
    reserved_for_pending_orders_usd = snapshot.cash_ledger.reserved_capital_usd

    # sector_exposure_pct: gross (long + short) per sector
    sector_exposure_pct: dict[str, float] = {
        e.sector: e.long_pct_of_portfolio + e.short_pct_of_portfolio
        for e in snapshot.sector_exposure
    }

    net_dir = snapshot.directional_exposure.net_directional_pct_of_portfolio
    net_long_pct = max(0.0, net_dir)
    net_short_pct = max(0.0, -net_dir)
    gross_pct = snapshot.directional_exposure.gross_pct_of_portfolio

    total_short_pct = sum(e.short_pct_of_portfolio for e in snapshot.sector_exposure)

    # single_short_max_pct: max weight among SHORT positions (open only per spec §2).
    # ``compute_position_weight_pct`` derives ``position_weight_pct`` from
    # ``abs(position_market_value_usd)``, so the field is always >= 0; no abs() needed.
    single_short_max_pct = max(
        (p.position_weight_pct for p in snapshot.open_positions if p.direction == Direction.SHORT),
        default=0.0,
    )

    # position_max_size_pct: read from active_risk_parameters by rule_id
    _rule = next(
        (
            e
            for e in snapshot.active_risk_parameters.entries
            if e.rule_id == "position_max_size_pct"
        ),
        None,
    )
    if _rule is None:
        msg = (
            "position_max_size_pct rule not found in snapshot.active_risk_parameters.entries; "
            "the translator cannot construct LibrarySnapshot without it"
        )
        raise ValueError(msg)
    position_max_size_pct = _rule.value

    # Single pass over all positions: compute portfolio_value_usd, aggregate portfolio
    # greeks, daily borrow cost, and build the existing_positions map.
    all_positions = (*snapshot.open_positions, *snapshot.pending_positions)

    portfolio_value_usd = cash_usd + sum(p.current_market_value_usd for p in all_positions)

    options_delta_pct = 0.0
    portfolio_theta_pct_per_day = 0.0
    portfolio_vega_pct_per_iv_point = 0.0
    daily_borrow_cost_pct = 0.0
    existing_positions: dict[str, ExistingPosition] = {}

    for pos in all_positions:
        details = pos.record.details
        underlying = _resolve_ticker(details)

        if underlying is None:
            logger.warning(
                "to_library_snapshot: could not resolve ticker for position %s "
                "(instrument_type=%s, strategy legs empty); skipping",
                pos.position_id,
                pos.instrument_type,
            )
            continue

        is_short_equity = pos.direction == Direction.SHORT and isinstance(
            details, EquityPositionDetails
        )

        # Portfolio-level greek aggregation (options and strategy only)
        if portfolio_value_usd > 0.0 and not isinstance(details, EquityPositionDetails):
            d_contrib, t_contrib, v_contrib = _accumulate_portfolio_greeks(
                details, pos.direction, portfolio_value_usd
            )
            options_delta_pct += d_contrib
            portfolio_theta_pct_per_day += t_contrib
            portfolio_vega_pct_per_iv_point += v_contrib

        # Daily borrow cost accumulation — must mirror the library's
        # _borrow_cost_contribute formula (cost_usd / portfolio_value * 100.0)
        # so the read scale matches the write scale exactly. See
        # src/alphamind/risk_guardrails/guardrail_evaluation/rules/shorts.py:108.
        if is_short_equity and borrow_cost_resolver is not None and portfolio_value_usd > 0.0:
            daily_borrow_cost_pct += borrow_cost_resolver(underlying) / portfolio_value_usd * 100.0

        # ExistingPosition construction
        current_greeks: Greeks | None = (
            None if isinstance(details, EquityPositionDetails) else _build_greeks(details)
        )
        daily_borrow_cost_usd: float | None = (
            borrow_cost_resolver(underlying)
            if is_short_equity and borrow_cost_resolver is not None
            else None
        )
        # quantity: share_count for equity, contract_count for options, first-leg for strategy
        if isinstance(details, EquityPositionDetails):
            quantity = details.share_count
        elif isinstance(details, OptionsPositionDetails):
            quantity = details.contract_count
        else:
            # StrategyPositionDetails: first-leg contract count per verify_pm.py pattern
            quantity = details.legs[0].options.contract_count if details.legs else 0.0

        existing_positions[pos.position_id] = ExistingPosition(
            position_id=pos.position_id,
            underlying=underlying,
            sector=sector_resolver(underlying),
            direction=_DIRECTION_TO_LIBRARY[pos.direction],
            asset_type=_INSTRUMENT_TO_ASSET_TYPE[pos.instrument_type],
            notional_usd=pos.notional_exposure_usd,
            delta_adjusted_exposure_usd=pos.delta_adjusted_exposure_usd,
            current_greeks=current_greeks,
            daily_borrow_cost_usd=daily_borrow_cost_usd,
            # reserves_capital_usd: 0.0 — PositionRecord does not carry this field
            reserves_capital_usd=0.0,
            quantity=quantity,
        )

    return LibrarySnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=reserved_for_pending_orders_usd,
        sector_exposure_pct=sector_exposure_pct,
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=options_delta_pct,
        portfolio_theta_pct_per_day=portfolio_theta_pct_per_day,
        portfolio_vega_pct_per_iv_point=portfolio_vega_pct_per_iv_point,
        total_short_pct=total_short_pct,
        single_short_max_pct=single_short_max_pct,
        daily_borrow_cost_pct=daily_borrow_cost_pct,
        position_max_size_pct=position_max_size_pct,
        existing_positions=existing_positions,
    )
