"""Input-bundle assembler for the strategist agent — story 04 (ALP-304).

Composes the user-message text the strategist harness (story 06) sends to the
LLM: the ``=== GUARDRAIL STATE ===`` header (rendered via the state-delivery
layer's normal-mode or defensive-posture renderer), a tool-reminder block, a
strategist-specific portfolio-state section, and the synthesizer brief
verbatim. Pure function — no I/O, no logging, deterministic.

The user-turn order matches the strategist prompt's ``<inputs>`` block:
header → tool reminder → portfolio state section → synthesizer brief.

See ``docs/design/04-decision-layer/strategist.md`` § Inputs and
``prompts/decision/strategist.md`` for the source-order contract.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.strategist import (
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    PMDecisionDetail,
)
from alphamind.portfolio_state.records.capital import DrawdownState
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegType,
    BracketRecord,
    EventTrigger,
    OrderRecord,
    PriceTrigger,
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
    PositionRecord,
    resolve_ticker,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery import (
    render_strategist_header,
    render_strategist_header_halt_mode,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.primitives import (
    format_dollar,
    format_pct,
)

__all__ = [
    "assemble_input_bundle_defensive_posture",
    "assemble_input_bundle_normal",
]


# ---------------------------------------------------------------------------
# Section-header constants
# ---------------------------------------------------------------------------

_TOOLS_HEADER = "=== AVAILABLE TOOLS ==="
_PORTFOLIO_HEADER = "=== PORTFOLIO STATE ==="
_INTRA_LOG_HEADER = "=== ACTIVITY LOG (intra-invocation) ==="
_PM_LOG_HEADER = "=== ACTIVITY LOG (recent PM decisions) ==="
_BRIEF_HEADER = "=== SYNTHESIZER BRIEF ==="
_NONE_LINE = "  None"

_DEFENSIVE_POSTURE_TOOL_NOTE = (
    "Defensive posture active — `add` is not permitted; `validate_guardrail` "
    "is still used for risk-reducing actions touching breach constraints."
)

# Display dictionaries
_DIRECTION_DISPLAY: dict[Direction, str] = {
    Direction.LONG: "long",
    Direction.SHORT: "short",
}

_INSTRUMENT_TYPE_DISPLAY: dict[InstrumentType, str] = {
    InstrumentType.EQUITY: "equity",
    InstrumentType.OPTIONS: "option",
    InstrumentType.STRATEGY: "strategy",
}

_COMPONENT_DISPLAY: dict[ThesisComponentType, str] = {
    ThesisComponentType.ENTRY_RATIONALE: "Entry rationale",
    ThesisComponentType.TARGET_RATIONALE: "Target rationale",
    ThesisComponentType.INVALIDATION_RATIONALE: "Invalidation",
}


# ---------------------------------------------------------------------------
# Public assemblers
# ---------------------------------------------------------------------------


def assemble_input_bundle_normal(  # noqa: PLR0913 — mirrors render_strategist_header's signature
    *,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: SectorResolver,
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> str:
    """Compose the strategist's user-message text for a normal-mode invocation.

    *prior_health_snapshots* carries the prior invocation's per-thesis health
    re-assessments; rendered into each thesis block as ``Prior status: <status>``.
    Pass an empty tuple on the first invocation after position entry.
    """
    header = render_strategist_header(
        strategist_view=strategist_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=state_delivery_config,
        sector_resolver=sector_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
    )
    tool_reminder = _render_tool_reminder(tool_names, defensive_posture=False)
    portfolio_state_section = _render_portfolio_state_section(
        strategist_view, current_price_lookup, prior_health_snapshots
    )
    return (
        f"{header}\n\n{tool_reminder}\n\n{portfolio_state_section}"
        f"\n\n{_BRIEF_HEADER}\n{synthesizer_brief_text}"
    )


def assemble_input_bundle_defensive_posture(  # noqa: PLR0913 — mirrors render_strategist_header_halt_mode
    *,
    halt_state: HaltState,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: SectorResolver,
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> str:
    """Compose the strategist's user-message text for a defensive-posture invocation.

    *prior_health_snapshots* — see :func:`assemble_input_bundle_normal`.
    """
    header = render_strategist_header_halt_mode(
        halt_state=halt_state,
        strategist_view=strategist_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=state_delivery_config,
        sector_resolver=sector_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
    )
    tool_reminder = _render_tool_reminder(tool_names, defensive_posture=True)
    portfolio_state_section = _render_portfolio_state_section(
        strategist_view, current_price_lookup, prior_health_snapshots
    )
    return (
        f"{header}\n\n{tool_reminder}\n\n{portfolio_state_section}"
        f"\n\n{_BRIEF_HEADER}\n{synthesizer_brief_text}"
    )


# ---------------------------------------------------------------------------
# Tool-reminder section
# ---------------------------------------------------------------------------


def _render_tool_reminder(tool_names: tuple[str, ...], *, defensive_posture: bool) -> str:
    """Render the ``=== AVAILABLE TOOLS ===`` block.

    One bullet per tool name. In defensive-posture mode an extra note clarifies
    that ``add`` is blocked but ``validate_guardrail`` is still used for
    risk-reducing actions.
    """
    lines: list[str] = [_TOOLS_HEADER]
    lines.extend(f"- {name}" for name in tool_names)
    if defensive_posture:
        lines.append(_DEFENSIVE_POSTURE_TOOL_NOTE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Portfolio-state section
# ---------------------------------------------------------------------------


def _render_portfolio_state_section(
    strategist_view: StrategistView,
    current_price_lookup: Callable[[str], float],
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...],
) -> str:
    """Compose the strategist-specific portfolio-state block."""
    snapshots_by_thesis_id = {snap.thesis_id: snap for snap in prior_health_snapshots}
    blocks: list[str] = [
        _PORTFOLIO_HEADER,
        _render_aggregate_block(strategist_view),
    ]
    if strategist_view.positions:
        blocks.append("Per-position records:")
        for view in strategist_view.positions:
            prior = (
                snapshots_by_thesis_id.get(view.thesis.thesis_id)
                if view.thesis is not None
                else None
            )
            blocks.append(_render_position_record(view, current_price_lookup, prior))
    else:
        blocks.append("Per-position records:\n  None")
    blocks.append(_render_intra_invocation_changelog(strategist_view.intra_invocation_changelog))
    blocks.append(_render_pm_decision_log(strategist_view.recent_pm_decision_log))
    return "\n\n".join(blocks)


def _render_aggregate_block(strategist_view: StrategistView) -> str:
    return "\n".join(
        [
            "Aggregate:",
            _render_aggregate_pnl_line(strategist_view.portfolio_pnl),
            _render_aggregate_drawdown_line(strategist_view.drawdown),
            _render_directional_lines(strategist_view.directional_exposure),
        ]
    )


def _render_aggregate_pnl_line(pnl: PortfolioPnL) -> str:
    intraday = format_dollar(pnl.daily_total_pnl_usd)
    cumulative = format_dollar(pnl.cumulative_realized_pnl_usd)
    return f"  Portfolio P/L: intraday {intraday}, cumulative realized {cumulative}"


def _render_aggregate_drawdown_line(drawdown: DrawdownState) -> str:
    intraday = format_pct(drawdown.intraday_drawdown_pct)
    cumulative = format_pct(drawdown.current_drawdown_pct)
    tier_suffix = (
        f", tier {drawdown.cumulative_tier.value}" if drawdown.cumulative_tier is not None else ""
    )
    return (
        f"  Drawdown: daily {intraday}% [{drawdown.daily_zone.value}], "
        f"cumulative {cumulative}% [{drawdown.cumulative_zone.value}]{tier_suffix}"
    )


def _render_directional_lines(directional: DirectionalExposure) -> str:
    net = format_dollar(
        directional.total_long_delta_adjusted_usd - directional.total_short_delta_adjusted_usd
    )
    gross = format_dollar(
        directional.total_long_delta_adjusted_usd + directional.total_short_delta_adjusted_usd
    )
    net_pct = format_pct(directional.net_directional_pct_of_portfolio)
    gross_pct = format_pct(directional.gross_pct_of_portfolio)
    return (
        f"  Net long: {net} ({net_pct}% of portfolio)\n"
        f"  Gross:    {gross} ({gross_pct}% of portfolio)"
    )


# ---------------------------------------------------------------------------
# Per-position record
# ---------------------------------------------------------------------------


def _render_position_record(
    view: StrategistPositionView,
    current_price_lookup: Callable[[str], float],
    prior_health_snapshot: ThesisHealthSnapshot | None,
) -> str:
    pos = view.position
    ticker = _resolve_position_ticker(pos)
    try:
        current_price = current_price_lookup(ticker)
    except KeyError as exc:
        msg = (
            f"current_price_lookup missing price for ticker {ticker!r} "
            f"(position_id={pos.position_id!r})"
        )
        raise ValueError(msg) from exc
    rows: list[str] = [pos.position_id]
    rows.append(_render_underlying_line(pos, ticker))
    rows.append(_render_size_line(pos))
    rows.append(_render_pnl_line(pos))
    rows.append(_render_age_line(pos))
    rows.append(_render_distance_and_rr_line(pos, view.bracket, current_price))
    rows.append(_render_bracket_block(view.bracket))
    rows.append(_render_thesis_block(view.thesis, prior_health_snapshot))
    if view.pending_orders:
        rows.append(_render_pending_orders_for_position(view.pending_orders, current_price))
    if view.modification_trail:
        rows.append(_render_modification_trail(view.modification_trail))
    return "\n".join(rows)


def _resolve_position_ticker(pos: PositionRecord | PositionView) -> str:
    ticker = resolve_ticker(pos.details)
    if ticker is None:
        msg = f"position {pos.position_id!r} has no resolvable ticker"
        raise ValueError(msg)
    return ticker


def _render_underlying_line(pos: PositionView, ticker: str) -> str:
    direction = _DIRECTION_DISPLAY[pos.direction]
    instrument = _INSTRUMENT_TYPE_DISPLAY[pos.instrument_type]
    return f"  Underlying:    {ticker} (instrument: {instrument}, direction: {direction})"


def _render_size_line(pos: PositionView) -> str:
    market_value = format_dollar(pos.current_market_value_usd)
    weight = format_pct(pos.position_weight_pct)
    details = pos.details
    if isinstance(details, EquityPositionDetails):
        size_label = f"{details.share_count:.0f} shares"
    elif isinstance(details, OptionsPositionDetails):
        size_label = f"{details.contract_count:.0f} contracts"
    else:
        size_label = "—"
    return f"  Size:          {size_label}  {market_value}  ({weight}% of portfolio)"


def _render_pnl_line(pos: PositionView) -> str:
    pnl_abs = _format_signed_dollar(pos.unrealized_pnl_usd)
    pnl_pct = _format_signed_pct(pos.unrealized_pnl_pct)
    return f"  P/L:           {pnl_abs} since open ({pnl_pct})"


def _render_age_line(pos: PositionView) -> str:
    age = f"{pos.position_age_hours:.1f}"
    placed = (
        pos.entry_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        if pos.entry_timestamp is not None
        else "—"
    )
    return f"  Age:           {age} hours (placed {placed})"


def _render_distance_and_rr_line(
    pos: PositionView,
    bracket: BracketRecord | None,
    current_price: float,
) -> str:
    target_price = _bracket_leg_price(bracket, BracketLegType.TAKE_PROFIT)
    stop_price = _bracket_leg_price(bracket, BracketLegType.PRICE_STOP)
    target_pct = _signed_distance_pct(current_price, target_price)
    stop_pct = _signed_distance_pct(current_price, stop_price)
    rr = _risk_reward_at_current(pos.direction, current_price, target_price, stop_price)
    target_str = target_pct if target_pct is not None else "—"
    stop_str = stop_pct if stop_pct is not None else "—"
    rr_str = f"{rr:.1f}:1" if rr is not None else "—"
    return f"  Distance:      target {target_str}  /  stop {stop_str}  /  R/R at-current {rr_str}"


def _bracket_leg_price(
    bracket: BracketRecord | None,
    leg_type: BracketLegType,
) -> float | None:
    """Return the threshold price for the matching leg, or None when absent.

    The BracketLeg validator guarantees ``leg.trigger`` is a ``PriceTrigger``
    whenever ``leg_type`` is ``TAKE_PROFIT`` or ``PRICE_STOP``.
    """
    if bracket is None:
        return None
    for leg in bracket.protective_legs:
        if leg.leg_type != leg_type:
            continue
        if isinstance(leg.trigger, PriceTrigger):
            return leg.trigger.threshold_usd
        return None
    return None


def _signed_distance_pct(current_price: float, target: float | None) -> str | None:
    if target is None or target == 0:
        return None
    pct = ((target - current_price) / current_price) * 100.0
    return _format_signed_pct(pct)


def _risk_reward_at_current(
    direction: Direction,
    current_price: float,
    target: float | None,
    stop: float | None,
) -> float | None:
    if target is None or stop is None:
        return None
    if direction == Direction.LONG:
        reward = target - current_price
        risk = current_price - stop
    else:
        reward = current_price - target
        risk = stop - current_price
    if risk <= 0:
        return None
    return reward / risk


def _render_bracket_block(bracket: BracketRecord | None) -> str:
    if bracket is None:
        return "  Bracket: not yet activated"
    lines: list[str] = ["  Bracket:"]
    target_leg = _find_leg(bracket, BracketLegType.TAKE_PROFIT)
    stop_leg = _find_leg(bracket, BracketLegType.PRICE_STOP)
    time_leg = _find_leg(bracket, BracketLegType.TIME_EXPIRATION)
    event_leg = _find_leg(bracket, BracketLegType.EVENT_INVALIDATION)
    if target_leg is not None and isinstance(target_leg.trigger, PriceTrigger):
        lines.append(f"    target: {_format_price_trigger(target_leg.trigger)}")
    if stop_leg is not None and isinstance(stop_leg.trigger, PriceTrigger):
        lines.append(f"    stop: {_format_price_trigger(stop_leg.trigger)}")
    if time_leg is not None and isinstance(time_leg.trigger, TimeTrigger):
        lines.append(f"    time deadline: {time_leg.trigger.deadline.isoformat()}")
    if event_leg is not None and isinstance(event_leg.trigger, EventTrigger):
        lines.append(f'    event invalidation: "{event_leg.trigger.description}"')
    return "\n".join(lines)


def _format_price_trigger(trigger: PriceTrigger) -> str:
    """Render a PriceTrigger as ``<ticker> <GTE/LTE> $<threshold>``."""
    return f"{trigger.underlying_ticker} {trigger.direction} ${trigger.threshold_usd}"


def _find_leg(bracket: BracketRecord, leg_type: BracketLegType) -> BracketLeg | None:
    for leg in bracket.protective_legs:
        if leg.leg_type == leg_type:
            return leg
    return None


def _render_thesis_block(
    thesis: ThesisRecord | None,
    prior_health_snapshot: ThesisHealthSnapshot | None,
) -> str:
    if thesis is None:
        return "  Thesis: NONE — pending position"
    lines: list[str] = [f"  Thesis ({thesis.thesis_id}):"]
    lines.append(f'    Summary:    "{thesis.summary}"')
    for component_type, label in _COMPONENT_DISPLAY.items():
        component = _find_component(thesis, component_type)
        if component is not None:
            lines.append(f"    {label}: {component.narrative}")
    assumptions = _collect_key_assumptions(thesis.components)
    if assumptions:
        lines.append("    Key assumptions:")
        for assumption in assumptions:
            lines.append(f'      - "{assumption.text}"')
    if prior_health_snapshot is not None:
        # The snapshot's own `health_status` (the prior invocation's reading)
        # is what the new invocation sees as its "prior status" — not the
        # snapshot's `prior_health_status` (which is one further back).
        lines.append(f"    Prior status: {prior_health_snapshot.health_status.value}")
    return "\n".join(lines)


def _find_component(
    thesis: ThesisRecord, component_type: ThesisComponentType
) -> ThesisComponent | None:
    for component in thesis.components:
        if component.component_type == component_type:
            return component
    return None


def _collect_key_assumptions(
    components: tuple[ThesisComponent, ...],
) -> tuple[KeyAssumption, ...]:
    seen: set[str] = set()
    out: list[KeyAssumption] = []
    for component in components:
        for assumption in component.key_assumptions:
            if assumption.text in seen:
                continue
            seen.add(assumption.text)
            out.append(assumption)
    return tuple(out)


def _render_pending_orders_for_position(
    orders: tuple[OrderRecord, ...],
    current_price: float,
) -> str:
    lines: list[str] = ["  Pending orders for this position:"]
    for order in orders:
        lines.append(_render_pending_order_row(order, current_price))
    return "\n".join(lines)


def _render_pending_order_row(order: OrderRecord, current_price: float) -> str:
    role = order.role.value
    age = f"{order.age_hours:.1f}h"
    limit_price = order.price_parameters.limit_price
    if limit_price is not None and limit_price > 0:
        distance_pct = ((current_price - limit_price) / limit_price) * 100.0
        distance_str = _format_signed_pct(distance_pct)
        return (
            f"    {order.order_id}: {role} @ ${limit_price:.2f}, "
            f"age {age}, distance {distance_str} from underlying"
        )
    return f"    {order.order_id}: {role}, age {age}"


def _render_modification_trail(trail: tuple[ActivityLogEntry, ...]) -> str:
    lines: list[str] = ["  Modification trail (recent):"]
    for entry in trail:
        lines.append(_render_activity_log_row(entry))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Activity-log surfaces (intra-invocation + recent PM decisions)
# ---------------------------------------------------------------------------


def _render_intra_invocation_changelog(entries: tuple[ActivityLogEntry, ...]) -> str:
    lines: list[str] = [_INTRA_LOG_HEADER]
    if not entries:
        lines.append(_NONE_LINE)
        return "\n".join(lines)
    for entry in entries:
        lines.append(_render_activity_log_row(entry))
    return "\n".join(lines)


def _render_pm_decision_log(entries: tuple[ActivityLogEntry, ...]) -> str:
    lines: list[str] = [_PM_LOG_HEADER]
    if not entries:
        lines.append(_NONE_LINE)
        return "\n".join(lines)
    for entry in entries:
        lines.append(_render_pm_decision_row(entry))
    return "\n".join(lines)


def _render_activity_log_row(entry: ActivityLogEntry) -> str:
    ts = entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = _summarize_activity_detail(entry)
    suffix_parts: list[str] = []
    if entry.position_id is not None:
        suffix_parts.append(f"position={entry.position_id}")
    if entry.order_id is not None:
        suffix_parts.append(f"order={entry.order_id}")
    suffix = f"  ({', '.join(suffix_parts)})" if suffix_parts else ""
    return f"  [{ts}] {entry.event_type.value}: {summary}{suffix}"


def _render_pm_decision_row(entry: ActivityLogEntry) -> str:
    ts = entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = entry.detail
    if isinstance(detail, PMDecisionDetail):
        verdict = detail.verdict.value.lower()
        envelope = detail.envelope_id
        rationale = detail.evaluation_json.get("rationale", "") if detail.evaluation_json else ""
        rationale_part = f" — {rationale}" if rationale else ""
        return f"  [{ts}] verdict {verdict} on envelope {envelope}{rationale_part}"
    return _render_activity_log_row(entry)


def _summarize_activity_detail(entry: ActivityLogEntry) -> str:
    detail = entry.detail
    # BracketModifiedDetail: source-aware summary.
    field_changed = getattr(detail, "field_changed", None)
    old_value = getattr(detail, "old_value", None)
    new_value = getattr(detail, "new_value", None)
    rationale = getattr(detail, "rationale", None)
    if field_changed is not None and old_value is not None and new_value is not None:
        rationale_str = f" ({rationale})" if rationale else ""
        return f"{field_changed}: {old_value} → {new_value}{rationale_str}"
    return type(detail).__name__


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_signed_dollar(value: float) -> str:
    if value >= 0:
        return f"+{format_dollar(value)}"
    # format_dollar already prefixes negatives with -$
    return format_dollar(value)


def _format_signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.1f}%"
