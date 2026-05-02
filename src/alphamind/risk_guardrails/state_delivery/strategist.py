"""Strategist guardrail state header renderer (story 04b)."""

from __future__ import annotations

from datetime import datetime

from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import AnalystAbandonedOpening
from alphamind.portfolio_state.consumers.strategist import (
    StrategistAbandonedAction,
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    DrawdownState,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.primitives import (
    DRAWDOWN_TIER_DISPLAY,
    DRAWDOWN_TIER_RESTRICTIONS,
    GROSS_RULE_ID,
    NET_LONG_RULE_ID,
    NET_SHORT_RULE_ID,
    OPTIONS_DELTA_RULE_ID,
    OPTIONS_THETA_RULE_ID,
    OPTIONS_VEGA_RULE_ID,
    POSITION_MAX_SIZE_RULE_ID,
    format_pct,
    make_sector_label_resolver,
    regime_label_display,
    render_capital_block,
    render_directional_headroom_block,
    render_envelope_close,
    render_envelope_open,
    render_hard_blocks_block,
    render_options_headroom_block,
    render_position_proximity_block,
    render_regime_line,
    render_regime_transition_breaches_block,
    render_sector_breakdown_block,
    render_sector_headroom_block,
    render_zone_tag,
    require_budget_entry,
    require_param_entry,
    resolve_sector_entries,
    validate_feature_flag_closure,
)

# ---------------------------------------------------------------------------
# Module-private constants
# ---------------------------------------------------------------------------

_DAILY_DRAWDOWN_RULE_ID = "daily_drawdown_pct"
_CUMULATIVE_DRAWDOWN_RULE_ID = "cumulative_drawdown_pct"

_DIRECTION_DISPLAY: dict[Direction, str] = {
    Direction.LONG: "long",
    Direction.SHORT: "short",
}

_INSTRUMENT_TYPE_DISPLAY: dict[InstrumentType, str] = {
    InstrumentType.EQUITY: "equity",
    InstrumentType.OPTIONS: "option",
    InstrumentType.STRATEGY: "strategy",
}

_ABANDONED_OPENINGS_HEADER = (
    "Abandoned openings from prior invocation (portfolio awareness; analyst owns re-evaluation):"
)
_ABANDONED_ACTIONS_HEADER = (
    "Abandoned position actions from prior invocation "
    "(decide on current grounds whether to re-propose):"
)
_DRAWDOWN_HEADER = "Drawdown state:"
_NONE_LINE = "  None"


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------


def render_strategist_header(  # noqa: PLR0913 — signature dictated by story 04b AC #3
    *,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_resolver: SectorResolver,
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str:
    """Render the strategist's complete ``=== GUARDRAIL STATE ===`` header.

    The ``config`` argument carries operator-tunable knobs the projection layer
    enforces upstream (the abandoned-window lookback filter is applied to
    ``strategist_view.abandoned_openings`` and ``abandoned_actions`` before
    reaching this renderer).
    """
    del config
    risk_budget = strategist_view.risk_budget
    active_risk_parameters = strategist_view.active_risk_parameters
    validate_feature_flag_closure(
        risk_budget=risk_budget,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    _validate_no_options_positions_when_disabled(
        positions=strategist_view.positions,
        options_enabled=options_enabled,
    )
    sector_entries = resolve_sector_entries(risk_budget, active_sectors)
    sector_label_resolver = make_sector_label_resolver(sector_label_display)
    regime_display = regime_label_display(active_risk_parameters.regime_label)
    per_position_max_pct = require_param_entry(
        active_risk_parameters, POSITION_MAX_SIZE_RULE_ID
    ).value
    available_pct = (
        (available_for_new_positions_usd / total_portfolio_value_usd) * 100.0
        if total_portfolio_value_usd > 0
        else 0.0
    )
    per_position_max_usd = (
        per_position_max_pct / 100.0 * total_portfolio_value_usd
        if total_portfolio_value_usd > 0
        else 0.0
    )

    blocks: list[str] = [
        "\n".join(
            [
                render_envelope_open(invocation_id, timestamp),
                render_regime_line(active_risk_parameters),
            ]
        ),
        render_capital_block(
            available_for_new_positions_usd=available_for_new_positions_usd,
            available_for_new_positions_pct=available_pct,
            per_position_max_usd=per_position_max_usd,
            per_position_max_pct=per_position_max_pct,
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
    blocks.append(
        render_position_proximity_block(
            positions=strategist_view.positions,
            active_risk_parameters=active_risk_parameters,
        )
    )
    blocks.append(
        render_sector_breakdown_block(
            positions=strategist_view.positions,
            active_sectors=active_sectors,
            sector_label_resolver=sector_label_resolver,
            sector_resolver=sector_resolver,
            risk_budget=risk_budget,
        )
    )
    blocks.append(
        _render_drawdown_state_block(
            drawdown=strategist_view.drawdown,
            active_risk_parameters=active_risk_parameters,
        )
    )
    breaches_block = render_regime_transition_breaches_block(
        breaches=regime_transition_breaches,
        regime_label_display=regime_display,
    )
    if breaches_block is not None:
        blocks.append(breaches_block)
    blocks.append(_render_strategist_abandoned_openings_block(strategist_view.abandoned_openings))
    blocks.append(_render_strategist_abandoned_actions_block(strategist_view.abandoned_actions))
    hard_blocks = render_hard_blocks_block(
        breaching_entries=risk_budget.breaching_entries(),
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    if hard_blocks is not None:
        blocks.append(hard_blocks)
    return "\n\n".join(blocks) + "\n" + render_envelope_close()


# ---------------------------------------------------------------------------
# Strategist-only validation helpers
# ---------------------------------------------------------------------------


def _validate_no_options_positions_when_disabled(
    *,
    positions: tuple[StrategistPositionView, ...],
    options_enabled: bool,
) -> None:
    if options_enabled:
        return
    for view in positions:
        if view.position.instrument_type == InstrumentType.OPTIONS:
            msg = (
                f"options_enabled is False but positions include OPTIONS position "
                f"{view.position.position_id!r}"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Drawdown state (block 6 in the design)
# ---------------------------------------------------------------------------


def _render_drawdown_state_block(
    *,
    drawdown: DrawdownState,
    active_risk_parameters: ActiveRiskParameterSet,
) -> str:
    daily_limit = require_param_entry(active_risk_parameters, _DAILY_DRAWDOWN_RULE_ID).value
    cumulative_limit = require_param_entry(
        active_risk_parameters, _CUMULATIVE_DRAWDOWN_RULE_ID
    ).value
    daily_zone_tag = render_zone_tag(drawdown.daily_zone)
    cumulative_zone_tag = render_zone_tag(drawdown.cumulative_zone)
    rows = [
        _DRAWDOWN_HEADER,
        f"  Daily:      {format_pct(drawdown.intraday_drawdown_pct)}% / "
        f"{format_pct(daily_limit)}% [{daily_zone_tag}]",
        f"  Cumulative: {format_pct(drawdown.current_drawdown_pct)}% / "
        f"{format_pct(cumulative_limit)}% [{cumulative_zone_tag}]",
    ]
    if drawdown.cumulative_tier is not None:
        tier_label = DRAWDOWN_TIER_DISPLAY[drawdown.cumulative_tier]
        restrictions = DRAWDOWN_TIER_RESTRICTIONS[drawdown.cumulative_tier]
        rows.append(f"  Cumulative tier: {tier_label} — {restrictions}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Abandoned openings + abandoned actions (blocks 8a/8b in the design)
# ---------------------------------------------------------------------------


def _render_strategist_abandoned_openings_block(
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


def _render_strategist_abandoned_actions_block(
    entries: tuple[StrategistAbandonedAction, ...],
) -> str:
    rows: list[str] = [_ABANDONED_ACTIONS_HEADER]
    if not entries:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    for entry in entries:
        rows.append(_render_abandoned_action_row(entry))
    return "\n".join(rows)


def _render_abandoned_action_row(entry: StrategistAbandonedAction) -> str:
    ts = entry.abandoned_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if entry.command_type == "CANCEL":
        if entry.order_id is None:
            msg = (
                f"StrategistAbandonedAction with command_type=CANCEL requires order_id; "
                f"envelope_id={entry.envelope_id!r}"
            )
            raise ValueError(msg)
        return (
            f"  {entry.envelope_id}: CANCEL on {entry.order_id} — "
            f"abandoned at {ts} ({entry.failure_reason})"
        )
    if entry.position_id is None:
        msg = (
            f"StrategistAbandonedAction with command_type={entry.command_type!r} "
            f"requires position_id; envelope_id={entry.envelope_id!r}"
        )
        raise ValueError(msg)
    return (
        f"  {entry.envelope_id}: {entry.command_type} on {entry.position_id} — "
        f"abandoned at {ts} ({entry.failure_reason})"
    )
