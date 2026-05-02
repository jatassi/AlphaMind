"""Analyst guardrail state header renderer (story 04a)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystHeldPosition,
    AnalystView,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.primitives import (
    GROSS_RULE_ID,
    NET_LONG_RULE_ID,
    NET_SHORT_RULE_ID,
    OPTIONS_DELTA_RULE_ID,
    OPTIONS_THETA_RULE_ID,
    OPTIONS_VEGA_RULE_ID,
    SECTOR_RULE_PREFIX,
    format_pct,
    make_sector_label_resolver,
    regime_label_display,
    render_capital_block,
    render_directional_headroom_block,
    render_envelope_close,
    render_envelope_open,
    render_hard_blocks_block,
    render_options_headroom_block,
    render_regime_line,
    render_sector_headroom_block,
    require_budget_entry,
    resolve_sector_entries,
    validate_feature_flag_closure,
)

_DIRECTION_DISPLAY: dict[Direction, str] = {
    Direction.LONG: "long",
    Direction.SHORT: "short",
}

_INSTRUMENT_TYPE_DISPLAY: dict[InstrumentType, str] = {
    InstrumentType.EQUITY: "equity",
    InstrumentType.OPTIONS: "option",
    InstrumentType.STRATEGY: "strategy",
}

_HELD_POSITIONS_HEADER = (
    "Held positions (dedup — skip same underlying + direction; strategist owns hold/add/reduce):"
)
_ABANDONED_OPENINGS_HEADER = (
    "Abandoned openings from prior invocation (decide on current grounds whether to re-propose):"
)
_NONE_LINE = "  None"
_DIRECTION_COLUMN_WIDTH = len("short")


def render_analyst_header(  # noqa: PLR0913 — signature dictated by story 04a AC #2
    *,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_label_display: dict[str, str] | None = None,
) -> str:
    """Render the analyst's complete ``=== GUARDRAIL STATE ===`` header.

    The ``config`` argument carries operator-tunable knobs the projection layer
    enforces upstream (the abandoned-window lookback filter is applied to
    ``analyst_view.abandoned_openings`` before reaching this renderer).
    """
    del config
    validate_feature_flag_closure(
        risk_budget=risk_budget,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    sector_entries = resolve_sector_entries(risk_budget, active_sectors)
    sector_label_resolver = make_sector_label_resolver(sector_label_display)
    capital = analyst_view.available_capital
    regime_display = regime_label_display(active_risk_parameters.regime_label)

    blocks: list[str] = [
        "\n".join(
            [
                render_envelope_open(invocation_id, timestamp),
                render_regime_line(active_risk_parameters),
            ]
        ),
        render_capital_block(
            available_for_new_positions_usd=capital.available_for_new_positions_usd,
            available_for_new_positions_pct=capital.available_for_new_positions_pct,
            per_position_max_usd=capital.per_position_max_size_usd,
            per_position_max_pct=capital.per_position_max_size_pct,
            regime_label_display=regime_display,
        ),
        render_sector_headroom_block(sector_entries, sector_label_resolver=sector_label_resolver),
        render_directional_headroom_block(
            net_long=require_budget_entry(risk_budget, NET_LONG_RULE_ID),
            net_short=(
                risk_budget.entry_by_rule_id(NET_SHORT_RULE_ID) if short_selling_enabled else None
            ),
            gross=require_budget_entry(risk_budget, GROSS_RULE_ID),
        ),
    ]
    options_block = render_options_headroom_block(
        delta=risk_budget.entry_by_rule_id(OPTIONS_DELTA_RULE_ID),
        theta=risk_budget.entry_by_rule_id(OPTIONS_THETA_RULE_ID),
        vega=risk_budget.entry_by_rule_id(OPTIONS_VEGA_RULE_ID),
    )
    if options_block is not None:
        blocks.append(options_block)
    blocks.append(_render_held_positions_block(analyst_view.held_positions, sector_label_resolver))
    blocks.append(_render_abandoned_openings_block(analyst_view.abandoned_openings))
    hard_blocks = render_hard_blocks_block(
        breaching_entries=risk_budget.breaching_entries(),
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    if hard_blocks is not None:
        blocks.append(hard_blocks)
    return "\n\n".join(blocks) + "\n" + render_envelope_close()


# ---------------------------------------------------------------------------
# Held-positions and abandoned-openings helpers (analyst-only, stay private)
# ---------------------------------------------------------------------------


def _render_held_positions_block(
    positions: tuple[AnalystHeldPosition, ...],
    sector_label_resolver: Callable[[str], str],
) -> str:
    rows: list[str] = [_HELD_POSITIONS_HEADER]
    if not positions:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    ticker_width = max(len(p.ticker) for p in positions)
    for position in positions:
        ticker = position.ticker.ljust(ticker_width)
        direction = _DIRECTION_DISPLAY[position.direction].ljust(_DIRECTION_COLUMN_WIDTH)
        size = f"{format_pct(position.size_pct)}%".rjust(5)
        sector_label = sector_label_resolver(f"{SECTOR_RULE_PREFIX}{position.sector}")
        rows.append(f"  {ticker}  {direction}  {size}  {sector_label}")
    return "\n".join(rows)


def _render_abandoned_openings_block(
    entries: tuple[AnalystAbandonedOpening, ...],
) -> str:
    rows: list[str] = [_ABANDONED_OPENINGS_HEADER]
    if not entries:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    for entry in entries:
        ts = entry.abandoned_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        size = f"{format_pct(entry.size_pct)}%"
        direction = _DIRECTION_DISPLAY[entry.direction]
        asset_type = _INSTRUMENT_TYPE_DISPLAY[entry.instrument_type]
        rows.append(
            f"  {entry.envelope_id}  {direction} {entry.ticker} "
            f"{asset_type}  {size}  — abandoned at {ts} ({entry.failure_reason})"
        )
    return "\n".join(rows)
