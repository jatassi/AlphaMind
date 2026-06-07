"""Library-shape snapshot translator (ALP-402).

Exports ``to_library_snapshot`` — the single bridge between the assembled OMS
Pydantic ``PortfolioStateSnapshot`` and the math-library dataclass
``PortfolioStateSnapshot`` (aliased as ``LibrarySnapshot`` to disambiguate).

Story 02's composition runner consumes this translator.  The translator is
pure: no clock reads, no I/O.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence

from alphamind.portfolio_state.records.orders import OrderRecord, OrderRole
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
    StrategyPositionDetails,
    position_direction,
    resolve_ticker,
)
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.borrow_cost import daily_borrow_cost_usd
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
    direction: Direction | None,
    portfolio_value_usd: float,
) -> tuple[float, float, float]:
    """Return (delta_contrib, theta_contrib, vega_contrib) as % of portfolio for one position.

    For an ``OptionsPositionDetails`` position the per-contract ``greeks`` are
    long-equivalent, so the position-level ``direction`` supplies the sign and
    the contribution scales by ``contract_count * contract_multiplier``.

    For a ``StrategyPositionDetails`` position the sign already lives in the
    net-signed ``strategy_greeks`` (ALP-588 decision C) — a net-short-delta
    strategy carries a negative ``strategy_greeks.delta`` — so no
    ``direction``-derived sign is applied. ``direction`` is the value of
    ``position_direction()``, which is ``None`` for a strategy and is never
    read on the strategy path. The contribution scales by the leg-summed
    multiplier units (Σ ``contract_count * contract_multiplier`` across legs),
    not a single leg's ``contract_count``.
    """
    if isinstance(details, OptionsPositionDetails):
        sign = 1.0 if direction == Direction.LONG else -1.0
        qty, multiplier, g = details.contract_count, details.contract_multiplier, details.greeks
        scale = sign * qty * multiplier / portfolio_value_usd * 100.0
    else:  # StrategyPositionDetails
        leg_summed_units = sum(
            leg.options.contract_count * leg.options.contract_multiplier for leg in details.legs
        )
        g = details.strategy_greeks
        scale = leg_summed_units / portfolio_value_usd * 100.0
    return g.delta * scale, g.theta * scale, g.vega * scale


# ---------------------------------------------------------------------------
# Per-position reserved-capital derivation
# ---------------------------------------------------------------------------


_ENTRY_ROLES: frozenset[OrderRole] = frozenset({OrderRole.ENTRY, OrderRole.ADD_ENTRY})


def _build_position_reservations(
    pending_orders: Sequence[OrderRecord],
) -> dict[str, float]:
    """Mirror ``_order_reserved_notional`` in ``execution/write_paths/command_execution/_shared.py``
    so each position's view of "what the OMS reserved / will release" matches what
    the write path actually subtracts. The OMS uses
    ``limit_price (or stop_trigger_price) * remaining_quantity`` for ENTRY/ADD_ENTRY
    legs; protective legs never reserve capital and market orders carry no price.

    Since ALP-741 the portfolio-level ``cash_ledger.reserved_capital_usd`` is
    maintained on this *same* notional basis end-to-end — OPEN / ADD reserve it,
    reprice adjusts it, CANCEL / fill release it — so this per-position view and
    the singleton agree. (Before ALP-741 the singleton summed the PM-command
    ``dollar_value`` instead; that basis mismatch could drive it negative and
    crash every decision-pipeline invocation.)
    """
    reservations: dict[str, float] = defaultdict(float)
    for order in pending_orders:
        if order.role not in _ENTRY_ROLES or order.position_id is None:
            continue
        pp = order.price_parameters
        px = pp.limit_price if pp.limit_price is not None else pp.stop_trigger_price
        if px is None:
            continue
        reservations[order.position_id] += float(px) * order.remaining_quantity
    return dict(reservations)


# ---------------------------------------------------------------------------
# Single-short max aggregation
# ---------------------------------------------------------------------------


def _compute_position_max_size_pct(
    all_positions: Sequence[PositionView], portfolio_value_usd: float
) -> float:
    """Return the largest position size (% of portfolio value) across all
    positions, or ``0.0`` when the book is empty or portfolio value is zero
    (ALP-624 / ALP-621).

    Basis is **underlying notional exposure** (``notional_exposure_usd``), not
    gross market value. For options/strategy positions the two differ by
    orders of magnitude — an options position's ``current_market_value_usd``
    is premium paid (e.g., $1k for 5 long calls), while
    ``notional_exposure_usd`` is the underlying exposure (e.g., $50k for the
    same 5 calls). The rule's projection math operates on proposal/position
    notional (see ``_position_max_size_project_after_batch``), so the
    read-current must use the same basis or the actual current and
    projected-after values disagree by the option leverage ratio (ALP-621
    Finding 2). Mirrors ``compute_position_weight_pct``'s abs() and
    zero-portfolio guards.
    """
    if portfolio_value_usd <= 0.0 or not all_positions:
        return 0.0
    return max(
        abs(float(p.notional_exposure_usd)) / portfolio_value_usd * 100.0 for p in all_positions
    )


def _select_single_short_max(
    open_positions: Sequence[PositionView],
) -> tuple[float, str | None]:
    """Return ``(max_weight_pct, position_id)`` for the largest open short.

    ``compute_position_weight_pct`` derives ``position_weight_pct`` from
    ``abs(position_market_value_usd)``, so the field is always >= 0; no abs()
    needed. Lexicographic ``position_id`` is the tiebreaker. Returns
    ``(0.0, None)`` when the book holds no shorts.

    Direction is read via ``position_direction()``: a multi-leg strategy yields
    ``None``, which compares unequal to ``Direction.SHORT``, so a strategy is
    correctly never selected as a short equity regardless of its inert
    position-level placeholder.
    """
    shorts = [p for p in open_positions if position_direction(p.record) == Direction.SHORT]
    if not shorts:
        return 0.0, None
    max_short = min(shorts, key=lambda p: (-p.position_weight_pct, p.position_id))
    return max_short.position_weight_pct, max_short.position_id


# ---------------------------------------------------------------------------
# Public translator
# ---------------------------------------------------------------------------


def to_library_snapshot(
    snapshot: PortfolioStateSnapshot,
    *,
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
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
        Maps a ticker to its annualized borrow fee rate as a percentage
        (``borrow_cost_daily.fee_pct``), or ``None`` for a ticker with no
        borrow data; ``daily_borrow_cost_usd`` converts the rate to a
        per-position daily USD accrual.  Required only when short-selling is
        active; ``None`` (the whole resolver) is safe for long-only portfolios.

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

    single_short_max_pct, single_short_max_position_id = _select_single_short_max(
        snapshot.open_positions
    )

    # position_max_size_pct: the actual maximum position size (% of portfolio
    # value) across open_positions + pending_positions — NOT the rule's limit
    # value (ALP-624). The rule's limit lookup still flows through
    # ``LibraryConfig.effective_limits`` keyed by ``effective_limit_key`` at
    # ``RuleSpec`` resolution time; the snapshot translator still requires the
    # rule registration to be present so the registry can build the spec.
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

    position_reservations = _build_position_reservations(snapshot.pending_orders)

    # Single pass over all positions: compute portfolio_value_usd, aggregate portfolio
    # greeks, daily borrow cost, and build the existing_positions map.
    all_positions = (*snapshot.open_positions, *snapshot.pending_positions)

    # ALP-489 — view USD fields carry Money; cash_usd is the repository-boundary
    # float. Sum in float space (the downstream pct math is float-typed); the
    # internal-positions Decimal arithmetic that protects against drift is
    # asserted at the PortfolioPnL accumulator (see computations/pnl.py).
    portfolio_value_usd = cash_usd + sum(float(p.current_market_value_usd) for p in all_positions)

    position_max_size_pct = _compute_position_max_size_pct(all_positions, portfolio_value_usd)

    options_delta_pct = 0.0
    portfolio_theta_pct_per_day = 0.0
    portfolio_vega_pct_per_iv_point = 0.0
    daily_borrow_cost_pct = 0.0
    existing_positions: dict[str, ExistingPosition] = {}

    for pos in all_positions:
        details = pos.record.details
        underlying = resolve_ticker(details)

        if underlying is None:
            logger.warning(
                "to_library_snapshot: could not resolve ticker for position %s "
                "(instrument_type=%s, strategy legs empty); skipping",
                pos.position_id,
                pos.instrument_type,
            )
            continue

        # Direction read routes through position_direction(): a strategy
        # yields None, which compares unequal to Direction.SHORT, so a
        # strategy is correctly never treated as a short equity. The
        # isinstance(EquityPositionDetails) narrowing is independently kept.
        direction = position_direction(pos.record)
        is_short_equity = direction == Direction.SHORT and isinstance(
            details, EquityPositionDetails
        )

        # Portfolio-level greek aggregation (options and strategy only)
        if portfolio_value_usd > 0.0 and not isinstance(details, EquityPositionDetails):
            d_contrib, t_contrib, v_contrib = _accumulate_portfolio_greeks(
                details, direction, portfolio_value_usd
            )
            options_delta_pct += d_contrib
            portfolio_theta_pct_per_day += t_contrib
            portfolio_vega_pct_per_iv_point += v_contrib

        # Daily borrow cost — the resolver yields an annualized fee rate;
        # daily_borrow_cost_usd converts it against the position's notional.
        # The pct accumulation must mirror the library's _borrow_cost_contribute
        # formula (cost_usd / portfolio_value * 100.0) so the read scale matches
        # the write scale exactly. See
        # src/alphamind/risk_guardrails/guardrail_evaluation/rules/shorts.py:114.
        borrow_fee_pct: float | None = (
            borrow_cost_resolver(underlying)
            if is_short_equity and borrow_cost_resolver is not None
            else None
        )
        position_borrow_cost_usd: float | None = (
            daily_borrow_cost_usd(
                notional_usd=abs(float(pos.notional_exposure_usd)),
                annual_fee_pct=borrow_fee_pct,
            )
            if borrow_fee_pct is not None
            else None
        )
        if position_borrow_cost_usd is not None and portfolio_value_usd > 0.0:
            daily_borrow_cost_pct += position_borrow_cost_usd / portfolio_value_usd * 100.0

        # ExistingPosition construction
        current_greeks: Greeks | None = (
            None if isinstance(details, EquityPositionDetails) else _build_greeks(details)
        )
        # quantity: share_count for equity, contract_count for options, first-leg for strategy
        if isinstance(details, EquityPositionDetails):
            quantity = details.share_count
        elif isinstance(details, OptionsPositionDetails):
            quantity = details.contract_count
        else:
            # StrategyPositionDetails: first-leg contract count per verify_pm.py pattern
            quantity = details.legs[0].options.contract_count if details.legs else 0.0

        # ExistingPosition.direction is an optional library Direction (ALP-603):
        # a multi-leg strategy has no meaningful position-level direction — its
        # directional sign lives per-leg / in the net-signed ``strategy_greeks``
        # — so ``position_direction()`` yields ``None`` for a strategy and a
        # mapped ``Direction`` for equity / single-leg options.
        library_direction = _DIRECTION_TO_LIBRARY[direction] if direction is not None else None

        existing_positions[pos.position_id] = ExistingPosition(
            position_id=pos.position_id,
            underlying=underlying,
            sector=sector_resolver(underlying),
            direction=library_direction,
            asset_type=_INSTRUMENT_TO_ASSET_TYPE[pos.instrument_type],
            notional_usd=float(pos.notional_exposure_usd),
            delta_adjusted_exposure_usd=float(pos.delta_adjusted_exposure_usd),
            current_greeks=current_greeks,
            daily_borrow_cost_usd=position_borrow_cost_usd,
            reserves_capital_usd=position_reservations.get(pos.position_id, 0.0),
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
        single_short_max_position_id=single_short_max_position_id,
    )
