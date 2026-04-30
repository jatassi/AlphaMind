"""Strategist guardrail state header renderer (story 04b)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import AnalystAbandonedOpening
from alphamind.portfolio_state.consumers.strategist import (
    StrategistAbandonedAction,
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    DrawdownState,
    DrawdownTier,
    RegimeLabel,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskZone,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionRecord,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.primitives import (
    format_pct,
    render_capital_block,
    render_directional_headroom_block,
    render_envelope_close,
    render_envelope_open,
    render_hard_blocks_block,
    render_options_headroom_block,
    render_regime_line,
    render_sector_headroom_block,
    render_zone_tag,
)

# ---------------------------------------------------------------------------
# Module-private constants
# ---------------------------------------------------------------------------

_SECTOR_RULE_PREFIX = "sector_concentration_"
_NET_LONG_RULE_ID = "net_long_pct"
_NET_SHORT_RULE_ID = "net_short_pct"
_GROSS_RULE_ID = "gross_exposure_pct"
_OPTIONS_DELTA_RULE_ID = "options_delta_pct"
_OPTIONS_THETA_RULE_ID = "portfolio_theta_pct_per_day"
_OPTIONS_VEGA_RULE_ID = "portfolio_vega_pct_per_iv_point"
_OPTIONS_RULE_IDS = (_OPTIONS_DELTA_RULE_ID, _OPTIONS_THETA_RULE_ID, _OPTIONS_VEGA_RULE_ID)

_POSITION_MAX_SIZE_RULE_ID = "position_max_size_pct"
_DAILY_DRAWDOWN_RULE_ID = "daily_drawdown_pct"
_CUMULATIVE_DRAWDOWN_RULE_ID = "cumulative_drawdown_pct"
_POSITION_MAX_LOSS_EQUITY_RULE_ID = "position_max_loss_equity_pct"
_POSITION_MAX_LOSS_OPTIONS_RULE_ID = "position_max_loss_options_pct"

_ZONE_WARNING_THRESHOLD = 0.70
_ZONE_CRITICAL_THRESHOLD = 0.85
_ZONE_BLOCKED_THRESHOLD = 0.95

_REGIME_LABEL_DISPLAY: dict[RegimeLabel, str] = {
    RegimeLabel.LOW_VOL: "low-vol compression",
    RegimeLabel.NORMAL: "normal",
    RegimeLabel.ELEVATED: "elevated",
    RegimeLabel.CRISIS: "crisis",
}

_DIRECTION_DISPLAY: dict[Direction, str] = {
    Direction.LONG: "long",
    Direction.SHORT: "short",
}

_INSTRUMENT_TYPE_DISPLAY: dict[InstrumentType, str] = {
    InstrumentType.EQUITY: "equity",
    InstrumentType.OPTIONS: "option",
    InstrumentType.STRATEGY: "strategy",
}

_DRAWDOWN_TIER_DISPLAY: dict[DrawdownTier, str] = {
    DrawdownTier.CONSTRAINED: "constrained",
    DrawdownTier.HEAVILY_CONSTRAINED: "heavily constrained",
    DrawdownTier.FULL_HALT: "full halt",
}

_DRAWDOWN_TIER_RESTRICTIONS: dict[DrawdownTier, str] = {
    DrawdownTier.CONSTRAINED: (
        "max position size 3%, max gross 80%, positions w/ unrealized loss > 10% flagged"
    ),
    DrawdownTier.HEAVILY_CONSTRAINED: (
        "max position size 2%, max gross 60%, positions w/ unrealized loss > 15% flagged"
    ),
    DrawdownTier.FULL_HALT: "no new positions; orderly reductions only",
}

_ABANDONED_OPENINGS_HEADER = (
    "Abandoned openings from prior invocation (portfolio awareness; analyst owns re-evaluation):"
)
_ABANDONED_ACTIONS_HEADER = (
    "Abandoned position actions from prior invocation "
    "(decide on current grounds whether to re-propose):"
)
_POSITION_PROXIMITY_HEADER = "Position-level constraint proximity:"
_SECTOR_BREAKDOWN_HEADER = "Sector exposure breakdown (per position):"
_DRAWDOWN_HEADER = "Drawdown state:"
_REGIME_TRANSITION_BREACHES_HEADER = "Regime-transition breaches (if any):"
_NONE_LINE = "  None"
_UNCLASSIFIED_GROUP_LABEL = "Unclassified"


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
    _validate_feature_flag_closure(
        risk_budget=risk_budget,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    _validate_no_options_positions_when_disabled(
        positions=strategist_view.positions,
        options_enabled=options_enabled,
    )
    sector_entries = _resolve_sector_entries(risk_budget, active_sectors)
    sector_label_resolver = _make_sector_label_resolver(sector_label_display)
    regime_display = _REGIME_LABEL_DISPLAY[active_risk_parameters.regime_label]
    per_position_max_pct = _require_param(active_risk_parameters, _POSITION_MAX_SIZE_RULE_ID).value
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
            net_long=_require_entry(risk_budget, _NET_LONG_RULE_ID),
            net_short=(
                risk_budget.entry_by_rule_id(_NET_SHORT_RULE_ID) if short_selling_enabled else None
            ),
            gross=_require_entry(risk_budget, _GROSS_RULE_ID),
        ),
    ]
    options_block = render_options_headroom_block(
        delta=risk_budget.entry_by_rule_id(_OPTIONS_DELTA_RULE_ID),
        theta=risk_budget.entry_by_rule_id(_OPTIONS_THETA_RULE_ID),
        vega=risk_budget.entry_by_rule_id(_OPTIONS_VEGA_RULE_ID),
    )
    if options_block is not None:
        blocks.append(options_block)
    blocks.append(
        _render_position_proximity_block(
            positions=strategist_view.positions,
            active_risk_parameters=active_risk_parameters,
        )
    )
    blocks.append(
        _render_sector_breakdown_block(
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
    breaches_block = _render_regime_transition_breaches_block(
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
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_feature_flag_closure(
    *,
    risk_budget: RiskBudgetConsumption,
    options_enabled: bool,
    short_selling_enabled: bool,
) -> None:
    if not options_enabled:
        for rule_id in _OPTIONS_RULE_IDS:
            if risk_budget.entry_by_rule_id(rule_id) is not None:
                msg = f"options_enabled is False but risk_budget contains options rule {rule_id!r}"
                raise ValueError(msg)
    if not short_selling_enabled and risk_budget.entry_by_rule_id(_NET_SHORT_RULE_ID) is not None:
        msg = (
            f"short_selling_enabled is False but risk_budget contains {_NET_SHORT_RULE_ID!r} entry"
        )
        raise ValueError(msg)


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


def _require_entry(risk_budget: RiskBudgetConsumption, rule_id: str) -> RiskBudgetEntry:
    entry = risk_budget.entry_by_rule_id(rule_id)
    if entry is None:
        msg = f"risk_budget missing required entry for rule_id {rule_id!r}"
        raise ValueError(msg)
    return entry


def _require_param(active: ActiveRiskParameterSet, rule_id: str) -> ActiveRiskParameterEntry:
    for entry in active.entries:
        if entry.rule_id == rule_id:
            return entry
    msg = f"active_risk_parameters missing required entry for rule_id {rule_id!r}"
    raise ValueError(msg)


def _resolve_sector_entries(
    risk_budget: RiskBudgetConsumption,
    active_sectors: tuple[str, ...],
) -> tuple[RiskBudgetEntry, ...]:
    entries: list[RiskBudgetEntry] = []
    for sector_key in active_sectors:
        rule_id = f"{_SECTOR_RULE_PREFIX}{sector_key}"
        entry = risk_budget.entry_by_rule_id(rule_id)
        if entry is None:
            msg = f"risk_budget missing required entry for rule_id {rule_id!r}"
            raise ValueError(msg)
        entries.append(entry)
    return tuple(entries)


def _make_sector_label_resolver(
    sector_label_display: dict[str, str] | None,
) -> Callable[[str], str]:
    """Return a resolver mapping ``sector_concentration_<key>`` rule_ids to display labels."""

    def _resolver(rule_id: str) -> str:
        sector_key = rule_id.removeprefix(_SECTOR_RULE_PREFIX)
        if sector_label_display is not None and sector_key in sector_label_display:
            return sector_label_display[sector_key]
        return sector_key.capitalize()

    return _resolver


# ---------------------------------------------------------------------------
# Position-level constraint proximity (block 4 in the design)
# ---------------------------------------------------------------------------


def _classify_position_zone(value: float, limit: float) -> RiskZone:
    if limit <= 0:
        return RiskZone.NORMAL
    ratio = value / limit
    if ratio >= _ZONE_BLOCKED_THRESHOLD:
        return RiskZone.BLOCKED
    if ratio >= _ZONE_CRITICAL_THRESHOLD:
        return RiskZone.CRITICAL
    if ratio >= _ZONE_WARNING_THRESHOLD:
        return RiskZone.WARNING
    return RiskZone.NORMAL


def _render_position_proximity_block(
    *,
    positions: tuple[StrategistPositionView, ...],
    active_risk_parameters: ActiveRiskParameterSet,
) -> str:
    rows: list[str] = [_POSITION_PROXIMITY_HEADER]
    if not positions:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    per_position_max_pct = _require_param(active_risk_parameters, _POSITION_MAX_SIZE_RULE_ID).value
    max_loss_equity = _max_loss_for(active_risk_parameters, _POSITION_MAX_LOSS_EQUITY_RULE_ID)
    max_loss_options = _max_loss_for(active_risk_parameters, _POSITION_MAX_LOSS_OPTIONS_RULE_ID)
    id_width = max(len(view.position.position_id) for view in positions)
    for view in positions:
        rows.append(
            _render_proximity_row(
                view=view,
                id_width=id_width,
                per_position_max_pct=per_position_max_pct,
                max_loss_equity=max_loss_equity,
                max_loss_options=max_loss_options,
            )
        )
    return "\n".join(rows)


def _render_proximity_row(
    *,
    view: StrategistPositionView,
    id_width: int,
    per_position_max_pct: float,
    max_loss_equity: float | None,
    max_loss_options: float | None,
) -> str:
    pos = view.position
    padded_id = f"{pos.position_id}:".ljust(id_width + 1)
    weight = f"{pos.position_weight_pct:.1f}".rjust(4)
    max_pct = f"{per_position_max_pct:.1f}"
    pnl = f"{pos.unrealized_pnl_pct:+.1f}"
    suffix = ""
    max_loss = _max_loss_for_position(pos, max_loss_equity, max_loss_options)
    if max_loss is not None:
        suffix = f" (max loss: -{max_loss:.1f}%)"
    zone = _classify_position_zone(pos.position_weight_pct, per_position_max_pct)
    zone_tag = f" [{render_zone_tag(zone)}]" if zone != RiskZone.NORMAL else ""
    return (
        f"  {padded_id} {weight}% of portfolio (max {max_pct}%) "
        f"— P/L: {pnl}% of cost{suffix}{zone_tag}"
    )


def _max_loss_for(active: ActiveRiskParameterSet, rule_id: str) -> float | None:
    for entry in active.entries:
        if entry.rule_id == rule_id:
            return entry.value
    return None


def _max_loss_for_position(
    pos: PositionRecord,
    max_loss_equity: float | None,
    max_loss_options: float | None,
) -> float | None:
    if pos.instrument_type == InstrumentType.EQUITY:
        return max_loss_equity
    if pos.instrument_type in (InstrumentType.OPTIONS, InstrumentType.STRATEGY):
        return max_loss_options
    return None


# ---------------------------------------------------------------------------
# Sector exposure breakdown (block 5 in the design)
# ---------------------------------------------------------------------------


def _render_sector_breakdown_block(
    *,
    positions: tuple[StrategistPositionView, ...],
    active_sectors: tuple[str, ...],
    sector_label_resolver: Callable[[str], str],
    sector_resolver: SectorResolver,
    risk_budget: RiskBudgetConsumption,
) -> str:
    rows: list[str] = [_SECTOR_BREAKDOWN_HEADER]
    grouped: dict[str, list[StrategistPositionView]] = {sector: [] for sector in active_sectors}
    unclassified: list[StrategistPositionView] = []
    for view in positions:
        sector_key = sector_resolver(view.position)
        if sector_key is None:
            unclassified.append(view)
        elif sector_key in grouped:
            grouped[sector_key].append(view)
        else:
            unclassified.append(view)
    for sector_key in active_sectors:
        sector_entry = _require_entry(risk_budget, f"{_SECTOR_RULE_PREFIX}{sector_key}")
        label = sector_label_resolver(f"{_SECTOR_RULE_PREFIX}{sector_key}")
        rows.append(
            f"  {label} ({format_pct(sector_entry.current_value)}% / "
            f"{format_pct(sector_entry.limit_value)}%):"
        )
        if not grouped[sector_key]:
            rows.append("    (no positions)")
            continue
        for view in grouped[sector_key]:
            rows.append(_render_sector_row(view))
    if unclassified:
        rows.append(f"  {_UNCLASSIFIED_GROUP_LABEL}:")
        for view in unclassified:
            rows.append(_render_sector_row(view))
    return "\n".join(rows)


def _render_sector_row(view: StrategistPositionView) -> str:
    pos = view.position
    base = f"    {pos.position_id}: {format_pct(pos.position_weight_pct)}% (delta-adj)"
    # The PositionRecord discriminator validator pairs instrument_type with the
    # matching detail block; the ``or 0.0`` defensive fallback satisfies mypy
    # since the type system can't see the runtime invariant.
    if pos.instrument_type == InstrumentType.OPTIONS and pos.options_details is not None:
        return f"{base} [options, delta {pos.options_details.greeks.delta:.2f}]"
    if pos.instrument_type == InstrumentType.STRATEGY and pos.strategy_details is not None:
        return f"{base} [strategy, delta {pos.strategy_details.strategy_greeks.delta:.2f}]"
    return base


# ---------------------------------------------------------------------------
# Drawdown state (block 6 in the design)
# ---------------------------------------------------------------------------


def _render_drawdown_state_block(
    *,
    drawdown: DrawdownState,
    active_risk_parameters: ActiveRiskParameterSet,
) -> str:
    daily_limit = _require_param(active_risk_parameters, _DAILY_DRAWDOWN_RULE_ID).value
    cumulative_limit = _require_param(active_risk_parameters, _CUMULATIVE_DRAWDOWN_RULE_ID).value
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
        tier_label = _DRAWDOWN_TIER_DISPLAY[drawdown.cumulative_tier]
        restrictions = _DRAWDOWN_TIER_RESTRICTIONS[drawdown.cumulative_tier]
        rows.append(f"  Cumulative tier: {tier_label} — {restrictions}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Regime-transition breaches (block 7 in the design)
# ---------------------------------------------------------------------------


def _render_regime_transition_breaches_block(
    *,
    breaches: tuple[RegimeTransitionBreach, ...],
    regime_label_display: str,
) -> str | None:
    if not breaches:
        return None
    rows = [_REGIME_TRANSITION_BREACHES_HEADER]
    for breach in breaches:
        if not breach.unit.startswith("%"):
            msg = (
                f"RegimeTransitionBreach.unit must be a percentage form for "
                f"rule_id={breach.rule_id!r}; got unit={breach.unit!r}"
            )
            raise ValueError(msg)
        prefix = breach.position_id if breach.position_id is not None else breach.rule_label
        suffix = ""
        if breach.position_id is not None and breach.rule_id != _POSITION_MAX_SIZE_RULE_ID:
            suffix = f" [{breach.rule_label}]"
        rows.append(
            f"  {prefix}: {breach.current_value:.1f}% exceeds "
            f"{regime_label_display} regime limit of {breach.new_limit_value:.1f}% "
            f"— overage {breach.overage:.1f}%{suffix}"
        )
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
