"""Snapshot assembler — orchestrates production of a PortfolioStateSnapshot (story 06).

``assemble_snapshot`` is the single public entry point. It fetches raw records
from the repository, enriches per-position and per-order computed fields using
market prices and the computation modules, and seals the snapshot.

Assembly sequence (16 steps — see function body for inline step labels):

  1.  Fetch invocation metadata and prior context.
  2.  Fetch all raw OMS records (14 sequential reads — see step 2 note).
  3.  Fetch brackets for known positions.
  4.  Fetch position-modification trail.
  5.  Fetch current prices for all positions.
  6.  Enrich each position (first pass — without weight).
  7.  Compute total portfolio value.
  8.  Enrich positions with weight (second pass).
  9.  Enrich pending orders with age.
  10. Compute portfolio P/L rollup.
  11. Enrich CashLedger with computed fields.
  12. Enrich DrawdownState with drawdown_by_source_pct if empty.
  13. Compute sector and directional exposure.
  14. Compute parameter change flag.
  15. Normalise activity-log projections (already supplied by repository).
  16. Construct and return the snapshot.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime

from alphamind._kernel.money import money, signed_money
from alphamind.execution.regt_margin_attribution.aggregates import RegTExcessAggregates
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.computations.exposure import (
    SectorResolver,
    compute_directional_exposure,
    compute_sector_exposure,
)
from alphamind.portfolio_state.computations.pnl import (
    compute_drawdown_by_source_pct,
    compute_portfolio_pnl,
    compute_total_portfolio_value_usd,
)
from alphamind.portfolio_state.computations.positions import (
    compute_delta_adjusted_exposure_usd,
    compute_distance_to_stop_usd,
    compute_distance_to_target_usd,
    compute_market_value_usd,
    compute_notional_exposure_usd,
    compute_position_age_hours,
    compute_position_weight_pct,
    compute_risk_reward_at_current,
    compute_strategy_delta_adjusted_exposure_usd,
    compute_strategy_market_value_usd,
    compute_strategy_notional_exposure_usd,
    compute_strategy_unrealized_pnl_pct,
    compute_unrealized_pnl_pct,
    compute_unrealized_pnl_usd,
)
from alphamind.portfolio_state.computations.risk_budget import (
    compute_cash_pct_of_portfolio,
    compute_order_age_hours,
    compute_parameter_change_flag,
    compute_true_deployable_capital_usd,
)
from alphamind.portfolio_state.freshness import (
    AssembledSnapshot,
    PriceFetchOutcomes,
    compute_snapshot_freshness,
)
from alphamind.portfolio_state.pricing import (
    CurrentPriceProvider,
    OptionPriceProvider,
    PriceQuote,
)
from alphamind.portfolio_state.records.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
    occ_symbol_for_options,
    position_direction,
)
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.repository import (
    PortfolioPnLInputs,
    PortfolioStateRepository,
)
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.positions import PositionView

log = logging.getLogger(__name__)


def _classify_position_price_fetch(
    pos: PositionRecord,
    price_map: dict[str, PriceQuote],
) -> tuple[str, datetime | None]:
    """Classify a position's pricing outcome.

    Returns:
        A 2-tuple of (category, as_of_timestamp):
        - category: "fresh" | "stale" | "unknown"
        - as_of_timestamp: the quote's as_of_timestamp when category is "fresh", else None
    """
    details = pos.details
    ticker: str | None
    if isinstance(details, EquityPositionDetails):
        ticker = details.ticker
    elif isinstance(details, OptionsPositionDetails):
        ticker = details.underlying_ticker
    else:  # STRATEGY — use first leg's underlying
        ticker = details.legs[0].options.underlying_ticker if details.legs else None
    quote = price_map.get(ticker or "")
    if quote is None:
        return "unknown", None
    if quote.is_stale:
        return "stale", None
    return "fresh", quote.as_of_timestamp


def _build_assembled_snapshot(
    snapshot: PortfolioStateSnapshot,
    ids_fresh: set[str],
    ids_stale: set[str],
    ids_unknown: set[str],
    oldest_price_as_of: datetime | None,
    config: PortfolioStateConfig,
    price_map: dict[str, PriceQuote],
) -> AssembledSnapshot:
    """Build and return the AssembledSnapshot; emit structured warnings when needed."""
    fetch_outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(ids_fresh),
        position_ids_priced_stale=frozenset(ids_stale),
        position_ids_unknown_ticker=frozenset(ids_unknown),
        oldest_price_as_of=oldest_price_as_of,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=fetch_outcomes, config=config)
    if not freshness.phase1_to_snapshot_within_threshold:
        log.warning(
            "phase1→snapshot latency exceeded threshold: %.3fs > %.3fs",
            freshness.phase1_to_snapshot_seconds,
            freshness.max_phase1_to_snapshot_seconds,
        )
    stale_count = freshness.count_priced_stale + freshness.count_unknown_ticker
    if stale_count > 0:
        log.warning(
            "%d position(s) have stale or unknown-ticker prices: %s",
            stale_count,
            sorted(freshness.position_ids_priced_stale | freshness.position_ids_unknown_ticker),
        )
    return AssembledSnapshot(snapshot=snapshot, freshness=freshness, price_map=price_map)


def _enrich_positions_with_price_classification(
    positions: tuple[PositionRecord, ...],
    price_map: dict[str, PriceQuote],
    option_price_map: dict[str, PriceQuote],
    brackets_by_bracket_id: dict[str, BracketRecord],
    now: datetime,
    ids_fresh: set[str],
    ids_stale: set[str],
    ids_unknown: set[str],
) -> tuple[list[PositionView], datetime | None]:
    """Enrich positions (first pass) and accumulate price-fetch outcome classification.

    Mutates *ids_fresh*, *ids_stale*, *ids_unknown* in place.
    Returns (enriched_list, oldest_price_as_of).
    """
    enriched: list[PositionView] = []
    oldest: datetime | None = None
    for pos in positions:
        category, as_of = _classify_position_price_fetch(pos, price_map)
        if category == "fresh":
            ids_fresh.add(pos.position_id)
            if as_of is not None and (oldest is None or as_of < oldest):
                oldest = as_of
        elif category == "stale":
            ids_stale.add(pos.position_id)
        else:
            ids_unknown.add(pos.position_id)
        enriched.append(
            _enrich_position_first_pass(
                pos, price_map, option_price_map, brackets_by_bracket_id, now
            )
        )
    return enriched, oldest


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_pricing_tickers(
    positions: tuple[PositionRecord, ...],
) -> tuple[str, ...]:
    """Return a deduplicated, ordered tuple of tickers needed to price *positions*.

    Equity   → equity_details.ticker
    Options  → options_details.underlying_ticker (notional + delta-adjusted
               exposure are computed against the underlying spot; option mark-
               to-market is driven by :func:`_resolve_option_occ_symbols`)
    Strategy → each leg's options.underlying_ticker
    """
    seen: dict[str, None] = {}  # preserves insertion order
    for pos in positions:
        details = pos.details
        if isinstance(details, EquityPositionDetails):
            seen[details.ticker] = None
        elif isinstance(details, OptionsPositionDetails):
            seen[details.underlying_ticker] = None
        else:  # STRATEGY
            for leg in details.legs:
                seen[leg.options.underlying_ticker] = None
    return tuple(seen)


def _resolve_option_occ_symbols(
    positions: tuple[PositionRecord, ...],
) -> tuple[str, ...]:
    """Return a deduplicated, ordered tuple of OCC contract symbols to mark to market.

    Options  → ``occ_symbol_for_options(options_details)``
    Strategy → each leg's ``occ_symbol_for_options(leg.options)``
    Equity positions contribute no symbols.
    """
    seen: dict[str, None] = {}
    for pos in positions:
        details = pos.details
        if isinstance(details, OptionsPositionDetails):
            seen[occ_symbol_for_options(details)] = None
        elif isinstance(details, StrategyPositionDetails):
            for leg in details.legs:
                seen[occ_symbol_for_options(leg.options)] = None
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class _PriceFields:
    """Value object carrying the five price-derived fields for one position."""

    current_market_value_usd: float
    notional_exposure_usd: float
    delta_adjusted_exposure_usd: float
    cost_basis: float
    current_price_usd: float


_ZERO_PRICE_FIELDS = _PriceFields(
    current_market_value_usd=0.0,
    notional_exposure_usd=0.0,
    delta_adjusted_exposure_usd=0.0,
    cost_basis=0.0,
    current_price_usd=0.0,
)


def _price_fields_equity(
    position: PositionRecord,
    price_map: dict[str, PriceQuote],
) -> _PriceFields:
    assert isinstance(position.details, EquityPositionDetails)
    details = position.details
    ticker = details.ticker
    raw_quote = price_map.get(ticker)
    if raw_quote is None:
        log.warning(
            "price missing for position %s ticker %s; using stale sentinel",
            position.position_id,
            ticker,
        )
        return _ZERO_PRICE_FIELDS
    if raw_quote.is_stale:
        return _ZERO_PRICE_FIELDS
    cost_basis = details.share_count * details.average_cost_basis_per_share
    return _PriceFields(
        current_market_value_usd=compute_market_value_usd(position, raw_quote),
        notional_exposure_usd=compute_notional_exposure_usd(position, raw_quote),
        delta_adjusted_exposure_usd=compute_delta_adjusted_exposure_usd(position, raw_quote),
        cost_basis=cost_basis,
        current_price_usd=raw_quote.price_usd,
    )


def _option_mark_price(
    *,
    position_id: str,
    occ_symbol: str,
    option_price_map: dict[str, PriceQuote],
    entry_premium: float,
) -> float:
    """Pick the per-contract MTM price: live quote when fresh, else entry premium.

    Mirrors the equity path's fall-through pattern but never zeroes out — when
    no live quote is available the position is still worth the premium paid,
    and the operator wants a finite MV in the view instead of a sentinel that
    silently drops the position out of weight / drawdown attribution. The
    fallback is logged at WARNING so an operator monitoring the assembler can
    distinguish "live MTM is driving the snapshot" from "every option fell
    back to entry premium" — the symptom of a stalled collector.
    """
    quote = option_price_map.get(occ_symbol)
    if quote is None:
        log.warning(
            "option price snapshot missing for position %s contract %s; "
            "falling back to entry premium %.4f",
            position_id,
            occ_symbol,
            entry_premium,
        )
        return entry_premium
    if quote.is_stale:
        log.warning(
            "option price snapshot stale for position %s contract %s "
            "(as_of=%s); falling back to entry premium %.4f",
            position_id,
            occ_symbol,
            quote.as_of_timestamp.isoformat(),
            entry_premium,
        )
        return entry_premium
    return quote.price_usd


def _price_fields_options(
    position: PositionRecord,
    price_map: dict[str, PriceQuote],
    option_price_map: dict[str, PriceQuote],
) -> _PriceFields:
    assert isinstance(position.details, OptionsPositionDetails)
    details = position.details
    underlying_ticker = details.underlying_ticker
    raw_underlying = price_map.get(underlying_ticker)
    if raw_underlying is None:
        log.warning(
            "price missing for options position %s underlying %s; using stale sentinel",
            position.position_id,
            underlying_ticker,
        )
        return _ZERO_PRICE_FIELDS
    if raw_underlying.is_stale:
        return _ZERO_PRICE_FIELDS
    mv_price = _option_mark_price(
        position_id=position.position_id,
        occ_symbol=occ_symbol_for_options(details),
        option_price_map=option_price_map,
        entry_premium=details.premium_paid_per_contract,
    )
    mv_quote = dataclasses.replace(raw_underlying, price_usd=mv_price)
    cost_basis = (
        details.contract_count * details.contract_multiplier * details.premium_paid_per_contract
    )
    return _PriceFields(
        current_market_value_usd=compute_market_value_usd(position, mv_quote),
        notional_exposure_usd=compute_notional_exposure_usd(position, raw_underlying),
        delta_adjusted_exposure_usd=compute_delta_adjusted_exposure_usd(position, raw_underlying),
        cost_basis=cost_basis,
        current_price_usd=raw_underlying.price_usd,
    )


def _price_fields_strategy(
    position: PositionRecord,
    price_map: dict[str, PriceQuote],
    option_price_map: dict[str, PriceQuote],
) -> _PriceFields:
    assert isinstance(position.details, StrategyPositionDetails)
    details = position.details
    leg_prices: dict[str, PriceQuote] = {}
    for leg in details.legs:
        underlying_ticker = leg.options.underlying_ticker
        raw_leg = price_map.get(underlying_ticker)
        if raw_leg is None:
            log.warning(
                "price missing for strategy position %s leg %s underlying %s; stale",
                position.position_id,
                leg.leg_id,
                underlying_ticker,
            )
            return _ZERO_PRICE_FIELDS
        leg_prices[leg.leg_id] = raw_leg

    mv_leg_prices: dict[str, PriceQuote] = {
        leg.leg_id: dataclasses.replace(
            leg_prices[leg.leg_id],
            price_usd=_option_mark_price(
                position_id=position.position_id,
                occ_symbol=occ_symbol_for_options(leg.options),
                option_price_map=option_price_map,
                entry_premium=leg.options.premium_paid_per_contract,
            ),
        )
        for leg in details.legs
    }
    first_leg = details.legs[0] if details.legs else None
    return _PriceFields(
        current_market_value_usd=compute_strategy_market_value_usd(position, mv_leg_prices),
        notional_exposure_usd=compute_strategy_notional_exposure_usd(position, leg_prices),
        delta_adjusted_exposure_usd=compute_strategy_delta_adjusted_exposure_usd(
            position, leg_prices
        ),
        cost_basis=details.net_premium_usd,
        current_price_usd=leg_prices[first_leg.leg_id].price_usd if first_leg else 0.0,
    )


def _enrich_position_first_pass(
    position: PositionRecord,
    price_map: dict[str, PriceQuote],
    option_price_map: dict[str, PriceQuote],
    brackets_by_bracket_id: dict[str, BracketRecord],
    now: datetime,
) -> PositionView:
    """Compute all per-position enrichment fields except position_weight_pct.

    Constructs a :class:`PositionView` wrapping the persistent record. The
    ``position_weight_pct`` field is set to 0.0 here as a placeholder; the
    second-pass enrichment (after total portfolio value is known) updates it.

    Missing-price / stale-price handling: when the underlying-pricing ticker
    is absent from *price_map*, or the returned quote has ``is_stale=True``,
    all market-value / exposure fields are set to 0.0 and assembly continues.
    For OPTIONS / STRATEGY positions a missing or stale option-contract quote
    falls back to ``premium_paid_per_contract`` (per-leg for strategies) so
    the MV stays finite even when ``options_contract_snapshots`` is empty.
    """
    if position.instrument_type == InstrumentType.EQUITY:
        pf = _price_fields_equity(position, price_map)
    elif position.instrument_type == InstrumentType.OPTIONS:
        pf = _price_fields_options(position, price_map, option_price_map)
    elif position.instrument_type == InstrumentType.STRATEGY:
        pf = _price_fields_strategy(position, price_map, option_price_map)
    else:
        msg = f"unhandled instrument_type: {position.instrument_type}"
        raise AssertionError(msg)
    bracket = brackets_by_bracket_id.get(position.bracket_id or "")

    direction = position_direction(position)

    # A STRATEGY position's P/L USD is the directionless ``market_value -
    # net_premium_usd`` (current net value minus what was paid to open — the
    # net credit makes ``cost_basis`` negative, so this stays correct for both
    # debit and credit strategies). Its P/L percentage divides by the magnitude
    # of capital at risk (``abs(max_loss_usd)``) rather than ``cost_basis``,
    # which would invert the sign for a net credit (ALP-599 / parent ALP-588 §
    # Pre-resolved decision B). Equity / single-leg option positions route P/L
    # USD through the direction-keyed ``compute_unrealized_pnl_usd`` and divide
    # the percentage by their own cost basis.
    if isinstance(position.details, StrategyPositionDetails):
        unrealized_pnl_usd = pf.current_market_value_usd - pf.cost_basis
        unrealized_pnl_pct = compute_strategy_unrealized_pnl_pct(
            unrealized_pnl_usd, position.details.max_loss_usd
        )
    else:
        assert direction is not None  # narrowed: non-strategy carries a Direction
        unrealized_pnl_usd = compute_unrealized_pnl_usd(
            pf.current_market_value_usd, pf.cost_basis, direction
        )
        unrealized_pnl_pct = compute_unrealized_pnl_pct(unrealized_pnl_usd, pf.cost_basis)

    position_age_hours = (
        compute_position_age_hours(position.entry_timestamp, now)
        if position.entry_timestamp is not None
        else 0.0
    )

    distance_to_target_usd: float | None = None
    distance_to_stop_usd: float | None = None
    risk_reward_at_current: float | None = None
    # The distance / risk-reward metrics are direction-keyed and measure the
    # underlying-price distance to each protective leg's threshold, so they
    # apply only to a directional position. A multi-leg strategy carries
    # ``direction=None`` (ALP-591) and a P/L-anchored take-profit leg (built by
    # ``_strategy_target_to_bracket_leg``), so its metrics stay ``None``.
    if bracket is not None and direction is not None:
        distance_to_target_usd = compute_distance_to_target_usd(
            pf.current_price_usd, bracket, direction
        )
        distance_to_stop_usd = compute_distance_to_stop_usd(
            pf.current_price_usd, bracket, direction
        )
        risk_reward_at_current = compute_risk_reward_at_current(
            distance_to_target_usd, distance_to_stop_usd
        )

    return PositionView(
        record=position,
        current_market_value_usd=signed_money(pf.current_market_value_usd),
        unrealized_pnl_usd=signed_money(unrealized_pnl_usd),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=0.0,  # second pass overwrites once total portfolio value is known
        position_age_hours=position_age_hours,
        notional_exposure_usd=money(pf.notional_exposure_usd),
        delta_adjusted_exposure_usd=signed_money(pf.delta_adjusted_exposure_usd),
        distance_to_target_usd=(
            signed_money(distance_to_target_usd) if distance_to_target_usd is not None else None
        ),
        distance_to_stop_usd=(
            signed_money(distance_to_stop_usd) if distance_to_stop_usd is not None else None
        ),
        risk_reward_at_current=risk_reward_at_current,
    )


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def assemble_snapshot(
    *,
    repository: PortfolioStateRepository,
    price_provider: CurrentPriceProvider,
    option_price_provider: OptionPriceProvider,
    sector_resolver: SectorResolver,
    config: PortfolioStateConfig,
    now: datetime,
) -> AssembledSnapshot:
    """Assemble an AssembledSnapshot (snapshot + freshness sidecar) from repository and prices.

    All parameters are keyword-only. ``now`` is supplied by the caller so the
    assembler is testable without a clock fixture. ``option_price_provider``
    supplies live option-contract mark-to-market quotes keyed by OCC symbol;
    a missing or stale quote falls back to ``premium_paid_per_contract``.

    Raises:
        RepositoryReadError / RepositoryConsistencyError: propagated without
            modification.
        MissingLegPriceError: propagated when a strategy leg's underlying ticker
            is absent from the price map (setup error; not in-band staleness).
        ValueError: from computation modules.
    """
    # ------------------------------------------------------------------
    # Step 1 — Fetch invocation metadata and prior context
    # ------------------------------------------------------------------
    metadata = repository.get_current_invocation_metadata()
    prior_context = repository.get_prior_invocation_context()

    # ------------------------------------------------------------------
    # Step 2 — Fetch raw OMS records (14 sequential reads)
    #
    # Per ALP-454 Pre-resolved decision (C): the prior ``asyncio.gather``
    # over 14 reads collapsed to a sequential loop since SQLite serializes
    # access anyway; concurrency over a single-writer DB delivers no real
    # parallelism.
    # ------------------------------------------------------------------
    open_positions_raw: tuple[PositionRecord, ...] = repository.get_open_positions()
    pending_positions_raw: tuple[PositionRecord, ...] = repository.get_pending_positions()
    drawdown_state_raw: DrawdownState = repository.get_drawdown_state()
    portfolio_pnl_inputs: PortfolioPnLInputs = repository.get_portfolio_pnl_inputs()
    active_theses: tuple[ThesisRecord, ...] = repository.get_active_theses()
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...] = (
        repository.get_recent_thesis_resolutions(
            lookback_trading_days=config.thesis_resolutions_lookback_trading_days
        )
    )
    cash_ledger_raw: CashLedger = repository.get_cash_ledger()
    pending_orders_raw: tuple[OrderRecord, ...] = repository.get_pending_orders()
    # Phase-1 snapshot emits an empty ``risk_budget``; the decision pipeline
    # populates the real consumption via ``build_risk_budget_consumption``
    # between ``to_library_snapshot`` and the per-consumer view projection.
    risk_budget = RiskBudgetConsumption(entries=())
    active_risk_parameters_raw: ActiveRiskParameterSet = repository.get_active_risk_parameters()
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (
        repository.get_intra_invocation_changelog(invocation_id=metadata.invocation_id)
    )
    recent_pm_decision_log: tuple[ActivityLogEntry, ...] = repository.get_recent_pm_decision_log(
        sliding_window_invocations=config.pm_decision_log_sliding_window_invocations
    )
    thesis_quality_aggregates: ThesisQualityAggregate = repository.get_thesis_quality_aggregates()
    regt_excess_aggregates: RegTExcessAggregates = repository.get_regt_excess_aggregates(now)

    # ------------------------------------------------------------------
    # Step 3 — Fetch brackets for known positions
    # ------------------------------------------------------------------
    position_ids = tuple(p.position_id for p in (*open_positions_raw, *pending_positions_raw))
    brackets_tuple = repository.get_brackets_for_positions(position_ids=position_ids)

    # ------------------------------------------------------------------
    # Step 4 — Fetch position-modification trail
    # ------------------------------------------------------------------
    position_modification_trail = repository.get_position_modification_trail(
        position_ids=position_ids
    )

    # ------------------------------------------------------------------
    # Step 5 — Fetch current prices (underlyings + option contracts)
    # ------------------------------------------------------------------
    all_positions: tuple[PositionRecord, ...] = (*open_positions_raw, *pending_positions_raw)
    pricing_tickers = _resolve_pricing_tickers(all_positions)
    price_map = price_provider.get_quotes(
        tickers=pricing_tickers,
        freshness_threshold_seconds=config.snapshot_freshness_max_price_age_seconds,
    )
    # Option-contract MTM. The OCC-symbol pull is independent of the
    # underlying pull; equity-only portfolios resolve no OCC symbols and the
    # provider returns ``{}``. Freshness uses
    # ``snapshot_freshness_max_option_price_age_seconds`` (default 2100s)
    # rather than the underlying knob because the collector's snapshot
    # cadence (~30 min, per ``config/collector_schedule.yaml``) is
    # materially slower than the live underlying-quote stream.
    option_occ_symbols = _resolve_option_occ_symbols(all_positions)
    option_price_map = option_price_provider.get_quotes(
        option_occ_symbols,
        freshness_threshold_seconds=config.snapshot_freshness_max_option_price_age_seconds,
    )

    # ------------------------------------------------------------------
    # Step 6 — Enrich each position (first pass — without weight)
    #          and accumulate per-position price-fetch outcomes
    # ------------------------------------------------------------------
    brackets_by_bracket_id: dict[str, BracketRecord] = {b.bracket_id: b for b in brackets_tuple}

    _ids_fresh: set[str] = set()
    _ids_stale: set[str] = set()
    _ids_unknown: set[str] = set()

    enriched_open, oldest_open = _enrich_positions_with_price_classification(
        open_positions_raw,
        price_map,
        option_price_map,
        brackets_by_bracket_id,
        now,
        _ids_fresh,
        _ids_stale,
        _ids_unknown,
    )
    enriched_pending, oldest_pending = _enrich_positions_with_price_classification(
        pending_positions_raw,
        price_map,
        option_price_map,
        brackets_by_bracket_id,
        now,
        _ids_fresh,
        _ids_stale,
        _ids_unknown,
    )
    _oldest_price_as_of: datetime | None = (
        min(t for t in (oldest_open, oldest_pending) if t is not None)
        if (oldest_open is not None or oldest_pending is not None)
        else None
    )

    # ------------------------------------------------------------------
    # Step 7 — Compute total portfolio value
    # ------------------------------------------------------------------
    total_portfolio_value = compute_total_portfolio_value_usd(
        open_positions=tuple(enriched_open),
        pending_positions=tuple(enriched_pending),
        cash_ledger=cash_ledger_raw,
    )

    # ------------------------------------------------------------------
    # Step 8 — Enrich positions with weight (second pass) + sort
    # ------------------------------------------------------------------
    final_open: list[PositionView] = sorted(
        (
            dataclasses.replace(
                view,
                position_weight_pct=compute_position_weight_pct(
                    float(view.current_market_value_usd), total_portfolio_value
                ),
            )
            for view in enriched_open
        ),
        key=lambda v: v.position_id,
    )

    final_pending: list[PositionView] = sorted(
        (
            dataclasses.replace(
                view,
                position_weight_pct=compute_position_weight_pct(
                    float(view.current_market_value_usd), total_portfolio_value
                ),
            )
            for view in enriched_pending
        ),
        key=lambda v: v.position_id,
    )

    # ------------------------------------------------------------------
    # Step 9 — Enrich pending orders with age, sort by submission_timestamp
    # ------------------------------------------------------------------
    enriched_orders: list[OrderRecord] = sorted(
        (
            dataclasses.replace(
                order, age_hours=compute_order_age_hours(order.submission_timestamp, now)
            )
            for order in pending_orders_raw
        ),
        key=lambda o: o.submission_timestamp,
    )

    # ------------------------------------------------------------------
    # Step 10 — Compute portfolio P/L rollup
    # ------------------------------------------------------------------
    portfolio_pnl = compute_portfolio_pnl(
        open_positions=tuple(final_open),
        inputs=portfolio_pnl_inputs,
        total_portfolio_value_usd=total_portfolio_value,
    )

    # ------------------------------------------------------------------
    # Step 11 — Enrich CashLedger with computed fields
    # ------------------------------------------------------------------
    # ``available_buying_power_usd`` is a derived field; the persisted
    # ``cash_ledger`` row is whatever the seed left there (Phase 1 / Phase 2
    # write paths no longer maintain it). The assembler is the single
    # source of truth and computes it here using the same canonical
    # formula as ``true_deployable_capital_usd``.
    #
    # The three ``regt_excess_*`` fields land via the Step 2-fetched
    # ``regt_excess_aggregates`` (computed by summing fill-record metadata
    # across calendar-day-anchored windows; see
    # ``regt-margin-attribution.md § Aggregation and delivery``).
    true_deployable = compute_true_deployable_capital_usd(cash_ledger_raw)
    enriched_cash: CashLedger = dataclasses.replace(
        cash_ledger_raw,
        cash_pct_of_portfolio=compute_cash_pct_of_portfolio(
            cash_ledger_raw.current_cash_usd, total_portfolio_value
        ),
        true_deployable_capital_usd=true_deployable,
        available_buying_power_usd=true_deployable,
        regt_excess_trailing_30d_usd=regt_excess_aggregates.trailing_30d_usd,
        regt_excess_trailing_90d_usd=regt_excess_aggregates.trailing_90d_usd,
        regt_excess_lifetime_usd=regt_excess_aggregates.lifetime_usd,
    )

    # ------------------------------------------------------------------
    # Step 12 — Enrich DrawdownState with drawdown_by_source_pct if empty
    # ------------------------------------------------------------------
    enriched_drawdown: DrawdownState
    if (
        drawdown_state_raw.drawdown_by_source_pct == {}
        and drawdown_state_raw.current_drawdown_pct > 0
    ):
        enriched_drawdown = dataclasses.replace(
            drawdown_state_raw,
            drawdown_by_source_pct=compute_drawdown_by_source_pct(
                open_positions=tuple(final_open),
                current_drawdown_pct=drawdown_state_raw.current_drawdown_pct,
            ),
        )
    else:
        enriched_drawdown = drawdown_state_raw

    # ------------------------------------------------------------------
    # Step 13 — Compute sector and directional exposure
    #
    # Aggregate over OPEN + PENDING: both lifecycle states are live, appear
    # in every consumer view, and feed into ``compute_total_portfolio_value_usd``
    # — so the numerators must match the denominator.
    # ------------------------------------------------------------------
    live_positions = (*final_open, *final_pending)
    sector_exposure = compute_sector_exposure(
        positions=live_positions,
        resolver=sector_resolver,
        total_portfolio_value_usd=total_portfolio_value,
    )
    directional_exposure = compute_directional_exposure(
        positions=live_positions,
        total_portfolio_value_usd=total_portfolio_value,
    )

    # ------------------------------------------------------------------
    # Step 14 — Compute parameter change flag
    # ------------------------------------------------------------------
    enriched_risk_parameters = dataclasses.replace(
        active_risk_parameters_raw,
        parameter_change_flag=compute_parameter_change_flag(
            current=active_risk_parameters_raw,
            prior=prior_context.prior_active_risk_parameters,
        ),
    )

    # ------------------------------------------------------------------
    # Steps 15/16 — Normalise activity-log projections and construct snapshot
    # ------------------------------------------------------------------
    snapshot = PortfolioStateSnapshot(
        invocation_id=metadata.invocation_id,
        phase1_committed_at=metadata.phase1_committed_at,
        snapshot_assembled_at=now,
        pipeline_invocation_started_at=metadata.pipeline_invocation_started_at,
        open_positions=tuple(final_open),
        pending_positions=tuple(final_pending),
        sector_exposure=sector_exposure,
        directional_exposure=directional_exposure,
        portfolio_pnl=portfolio_pnl,
        drawdown=enriched_drawdown,
        active_theses=active_theses,
        recent_thesis_resolutions=recent_thesis_resolutions,
        cash_ledger=enriched_cash,
        pending_orders=tuple(enriched_orders),
        risk_budget=risk_budget,
        active_risk_parameters=enriched_risk_parameters,
        intra_invocation_changelog=intra_invocation_changelog,
        recent_pm_decision_log=recent_pm_decision_log,
        position_modification_trail=position_modification_trail,
        thesis_quality_aggregates=thesis_quality_aggregates,
        brackets=brackets_tuple,
    )

    # ------------------------------------------------------------------
    # Step 17 — Compute freshness sidecar and emit structured warnings
    # ------------------------------------------------------------------
    return _build_assembled_snapshot(
        snapshot,
        _ids_fresh,
        _ids_stale,
        _ids_unknown,
        _oldest_price_as_of,
        config,
        price_map,
    )
