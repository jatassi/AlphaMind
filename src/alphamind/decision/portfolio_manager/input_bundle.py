"""Input-bundle assembler for the portfolio manager agent — story 04 (ALP-324).

Composes the user-message text the PM harness (story 07) sends to the LLM:
the ``=== GUARDRAIL STATE ===`` header (rendered via the state-delivery
layer's normal-mode or halt-mode renderer), a tool-reminder block, the
proposal pre-processor bundle (rendered byte-for-byte via Pydantic JSON
serialization), the synthesizer brief verbatim, and a PM-specific
portfolio-state section with three activity-log surfaces. Pure function —
no I/O, no logging, deterministic.

The user-turn order matches the PM prompt's ``<inputs>`` block:
header → tool reminder → pre-processor bundle → synthesizer brief →
portfolio state.

See ``docs/design/04-decision-layer/portfolio-manager.md`` § Inputs and
``prompts/decision/pm.md`` for the source-order contract.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alphamind._kernel.money import Money
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    PMDecisionDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegType,
    BracketRecord,
    OrderRecord,
    PriceTrigger,
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
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.halt_mode import render_pm_header_halt_mode
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CorrelationState,
    CrossConstraintImpact,
    DependencyRiskFlag,
    RegimeOverride,
    render_pm_header,
)
from alphamind.risk_guardrails.state_delivery.primitives import (
    format_dollar,
    format_pct,
)

__all__ = [
    "assemble_input_bundle_halt",
    "assemble_input_bundle_normal",
]


# ---------------------------------------------------------------------------
# Section-header constants
# ---------------------------------------------------------------------------

_TOOLS_HEADER = "=== AVAILABLE TOOLS ==="
_PRE_PROCESSOR_HEADER = "=== PROPOSAL PRE-PROCESSOR BUNDLE ==="
_PRE_PROCESSOR_FOOTER = "=== END PROPOSAL PRE-PROCESSOR BUNDLE ==="
_BRIEF_HEADER = "=== SYNTHESIZER BRIEF ==="
_PORTFOLIO_HEADER = "=== PORTFOLIO STATE ==="
_INTRA_LOG_HEADER = "=== ACTIVITY LOG (intra-invocation) ==="
_PM_LOG_HEADER = "=== ACTIVITY LOG (recent PM decisions) ==="
_RECENT_RESOLUTIONS_HEADER = "=== RECENT THESIS RESOLUTIONS ==="
_ABANDONED_HEADER = "=== ABANDONED OPENINGS / ACTIONS ==="
_THESIS_QUALITY_HEADER = "=== THESIS QUALITY AGGREGATE ==="
_NONE_LINE = "  None"

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


def assemble_input_bundle_normal(  # noqa: PLR0913 — mirrors render_pm_header
    *,
    pm_view: PortfolioManagerView,
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_brief_text: str,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: Money,
    available_for_new_positions_usd: Money,
    cross_constraint_impact: CrossConstraintImpact,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> str:
    """Compose the PM's user-message text for a normal-mode invocation.

    *prior_health_snapshots* — see strategist input bundle counterpart.
    """
    # ALP-462 — render_pm_header still takes float; cast at the boundary
    # (risk_guardrails/state_delivery/portfolio_manager is outside ALP-462).
    header = render_pm_header(
        pm_view=pm_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=state_delivery_config,
        sector_resolver=sector_resolver,
        total_portfolio_value_usd=float(total_portfolio_value_usd),
        available_for_new_positions_usd=float(available_for_new_positions_usd),
        cross_constraint_impact=cross_constraint_impact,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        active_regime_overrides=active_regime_overrides,
        correlation_state=correlation_state,
        dependency_risk_flag=dependency_risk_flag,
    )
    return _compose_user_message(
        header=header,
        pre_processor_bundle=pre_processor_bundle,
        synthesizer_brief_text=synthesizer_brief_text,
        pm_view=pm_view,
        tool_names=tool_names,
        prior_health_snapshots=prior_health_snapshots,
    )


def assemble_input_bundle_halt(  # noqa: PLR0913 — mirrors render_pm_header_halt_mode
    *,
    halt_state: HaltState,
    pm_view: PortfolioManagerView,
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_brief_text: str,
    invocation_id: str,
    timestamp: datetime,
    pending_orders: tuple[OrderRecord, ...],
    current_price_lookup: Callable[[str], float],
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: Money,
    available_for_new_positions_usd: Money,
    cross_constraint_impact: CrossConstraintImpact,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> str:
    """Compose the PM's user-message text for a halt-mode invocation.

    *prior_health_snapshots* — see :func:`assemble_input_bundle_normal`.
    """
    # ALP-462 — render_pm_header_halt_mode still takes float; cast at boundary.
    header = render_pm_header_halt_mode(
        halt_state=halt_state,
        pm_view=pm_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=state_delivery_config,
        sector_resolver=sector_resolver,
        total_portfolio_value_usd=float(total_portfolio_value_usd),
        available_for_new_positions_usd=float(available_for_new_positions_usd),
        cross_constraint_impact=cross_constraint_impact,
        pending_orders=pending_orders,
        current_price_lookup=current_price_lookup,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        active_regime_overrides=active_regime_overrides,
        correlation_state=correlation_state,
        dependency_risk_flag=dependency_risk_flag,
    )
    return _compose_user_message(
        header=header,
        pre_processor_bundle=pre_processor_bundle,
        synthesizer_brief_text=synthesizer_brief_text,
        pm_view=pm_view,
        tool_names=tool_names,
        prior_health_snapshots=prior_health_snapshots,
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _compose_user_message(
    *,
    header: str,
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_brief_text: str,
    pm_view: PortfolioManagerView,
    tool_names: tuple[str, ...],
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...],
) -> str:
    """Build the seven-block user message: header → tools → pre-processor → brief → portfolio."""
    tool_reminder = _render_tool_reminder(tool_names)
    pre_processor_block = _render_pre_processor_bundle(pre_processor_bundle)
    brief_block = f"{_BRIEF_HEADER}\n{synthesizer_brief_text}"
    portfolio_block = _render_portfolio_state_section(pm_view, prior_health_snapshots)
    return "\n\n".join(
        [
            header,
            tool_reminder,
            pre_processor_block,
            brief_block,
            portfolio_block,
        ]
    )


# ---------------------------------------------------------------------------
# Tool-reminder section
# ---------------------------------------------------------------------------


def _render_tool_reminder(tool_names: tuple[str, ...]) -> str:
    """Render the ``=== AVAILABLE TOOLS ===`` block."""
    lines: list[str] = [_TOOLS_HEADER]
    lines.extend(f"- {name}" for name in tool_names)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pre-processor bundle section (byte-for-byte JSON)
# ---------------------------------------------------------------------------


def _render_pre_processor_bundle(bundle: ProposalPreProcessorBundle) -> str:
    """Serialize the pre-processor bundle as Pydantic JSON, fenced by markers."""
    body = bundle.model_dump_json(indent=2, by_alias=True)
    return f"{_PRE_PROCESSOR_HEADER}\n{body}\n{_PRE_PROCESSOR_FOOTER}"


# ---------------------------------------------------------------------------
# Portfolio-state section
# ---------------------------------------------------------------------------


def _render_portfolio_state_section(
    pm_view: PortfolioManagerView,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...],
) -> str:
    """Compose the PM-specific portfolio-state block."""
    snapshots_by_thesis_id = {snap.thesis_id: snap for snap in prior_health_snapshots}
    blocks: list[str] = [_PORTFOLIO_HEADER]
    if pm_view.positions:
        blocks.append("Per-position records:")
        for view in pm_view.positions:
            trail = pm_view.position_modification_trail.get(view.position.position_id, ())
            prior = (
                snapshots_by_thesis_id.get(view.thesis.thesis_id)
                if view.thesis is not None
                else None
            )
            blocks.append(_render_per_position_record(view, trail, prior))
    else:
        blocks.append(f"Per-position records:\n{_NONE_LINE}")
    blocks.append(_render_activity_log_block(_INTRA_LOG_HEADER, pm_view.intra_invocation_changelog))
    blocks.append(
        _render_pm_decision_log_block(_PM_LOG_HEADER, pm_view.recent_pm_decision_log),
    )
    blocks.append(_render_recent_resolutions_block(pm_view))
    blocks.append(_render_abandoned_block(pm_view))
    blocks.append(_render_thesis_quality_block(pm_view))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Per-position record
# ---------------------------------------------------------------------------


def _render_per_position_record(
    view: StrategistPositionView,
    modification_trail: tuple[ActivityLogEntry, ...],
    prior_health_snapshot: ThesisHealthSnapshot | None,
) -> str:
    pos = view.position
    rows: list[str] = [pos.position_id]
    rows.append(_render_underlying_line(pos))
    rows.append(_render_size_line(pos))
    rows.append(_render_pnl_line(pos))
    rows.append(_render_age_line(pos))
    rows.append(_render_distance_line(pos, view.bracket))
    rows.append(_render_thesis_summary_block(view.thesis, prior_health_snapshot))
    if modification_trail:
        rows.append(_render_modification_trail(modification_trail))
    return "\n".join(rows)


def _render_underlying_line(pos: PositionView) -> str:
    ticker = _resolve_position_ticker(pos)
    direction = _DIRECTION_DISPLAY[pos.direction]
    instrument = _INSTRUMENT_TYPE_DISPLAY[pos.instrument_type]
    return f"  Underlying:    {ticker} (instrument: {instrument}, direction: {direction})"


def _resolve_position_ticker(pos: PositionRecord | PositionView) -> str:
    ticker = resolve_ticker(pos.details)
    if ticker is None:
        msg = f"position {pos.position_id!r} has no resolvable ticker"
        raise ValueError(msg)
    return ticker


def _render_size_line(pos: PositionView) -> str:
    market_value = format_dollar(float(pos.current_market_value_usd))
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
    pnl_abs = _format_signed_dollar(float(pos.unrealized_pnl_usd))
    pnl_pct = _format_signed_pct(pos.unrealized_pnl_pct)
    return f"  P/L:           {pnl_abs} since open ({pnl_pct})"


def _render_age_line(pos: PositionView) -> str:
    return f"  Age:           {_format_position_age(pos.position_age_hours)} hours"


def _render_distance_line(pos: PositionView, bracket: BracketRecord | None) -> str:
    target_str = _format_distance_pct(
        None if pos.distance_to_target_usd is None else float(pos.distance_to_target_usd),
        float(pos.current_market_value_usd),
    )
    stop_str = _format_distance_pct(
        None if pos.distance_to_stop_usd is None else float(pos.distance_to_stop_usd),
        float(pos.current_market_value_usd),
    )
    bracket_legs: list[str] = []
    if bracket is not None:
        target_leg = _find_leg(bracket, BracketLegType.TAKE_PROFIT)
        stop_leg = _find_leg(bracket, BracketLegType.PRICE_STOP)
        if target_leg is not None and isinstance(target_leg.trigger, PriceTrigger):
            bracket_legs.append(f"target {_format_price_trigger(target_leg.trigger)}")
        if stop_leg is not None and isinstance(stop_leg.trigger, PriceTrigger):
            bracket_legs.append(f"stop {_format_price_trigger(stop_leg.trigger)}")
    bracket_str = " | ".join(bracket_legs) if bracket_legs else "—"
    return (
        f"  Distance:      target {target_str}  /  stop {stop_str}\n  Bracket legs:  {bracket_str}"
    )


def _format_price_trigger(trigger: PriceTrigger) -> str:
    """Render a PriceTrigger as ``<ticker> <GTE/LTE> $<threshold>``."""
    return f"{trigger.underlying_ticker} {trigger.direction} ${trigger.threshold_usd}"


def _find_leg(bracket: BracketRecord, leg_type: BracketLegType) -> BracketLeg | None:
    for leg in bracket.protective_legs:
        if leg.leg_type == leg_type:
            return leg
    return None


def _render_thesis_summary_block(
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


def _render_modification_trail(trail: tuple[ActivityLogEntry, ...]) -> str:
    lines: list[str] = ["  Modification trail:"]
    for entry in trail:
        lines.append(_render_activity_log_row(entry))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Activity-log surfaces
# ---------------------------------------------------------------------------


def _render_activity_log_block(header: str, entries: tuple[ActivityLogEntry, ...]) -> str:
    lines: list[str] = [header]
    if not entries:
        lines.append(_NONE_LINE)
        return "\n".join(lines)
    for entry in entries:
        lines.append(_render_activity_log_row(entry))
    return "\n".join(lines)


def _render_pm_decision_log_block(header: str, entries: tuple[ActivityLogEntry, ...]) -> str:
    lines: list[str] = [header]
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
    field_changed = getattr(detail, "field_changed", None)
    old_value = getattr(detail, "old_value", None)
    new_value = getattr(detail, "new_value", None)
    rationale = getattr(detail, "rationale", None)
    if field_changed is not None and old_value is not None and new_value is not None:
        rationale_str = f" ({rationale})" if rationale else ""
        return f"{field_changed}: {old_value} → {new_value}{rationale_str}"
    return type(detail).__name__


# ---------------------------------------------------------------------------
# Recent thesis resolutions
# ---------------------------------------------------------------------------


def _render_recent_resolutions_block(pm_view: PortfolioManagerView) -> str:
    lines: list[str] = [_RECENT_RESOLUTIONS_HEADER]
    if not pm_view.recent_thesis_resolutions:
        lines.append(_NONE_LINE)
        return "\n".join(lines)
    for resolution in pm_view.recent_thesis_resolutions:
        ts_part = f" (active {resolution.active_duration_hours:.1f}h)"
        lines.append(f"  {resolution.position_id}: {resolution.resolution_category.value}{ts_part}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Abandoned openings / actions
# ---------------------------------------------------------------------------


def _render_abandoned_block(pm_view: PortfolioManagerView) -> str:
    lines: list[str] = [_ABANDONED_HEADER]
    if not pm_view.abandoned_openings and not pm_view.abandoned_actions:
        lines.append(_NONE_LINE)
        return "\n".join(lines)
    if pm_view.abandoned_openings:
        lines.append("  Openings:")
        for opening in pm_view.abandoned_openings:
            ts = opening.abandoned_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(
                f"    [{ts}] {opening.envelope_id} "
                f"{opening.direction.value} {opening.ticker} — {opening.failure_reason}"
            )
    if pm_view.abandoned_actions:
        lines.append("  Actions:")
        for action in pm_view.abandoned_actions:
            ts = action.abandoned_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            target = action.position_id or action.order_id or ""
            lines.append(
                f"    [{ts}] {action.envelope_id} {action.command_type} {target} "
                f"— {action.failure_reason}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thesis quality aggregates summary
# ---------------------------------------------------------------------------


def _render_thesis_quality_block(pm_view: PortfolioManagerView) -> str:
    aggregate = pm_view.thesis_quality_aggregates
    lines: list[str] = [_THESIS_QUALITY_HEADER]
    as_of = aggregate.as_of_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"  As of: {as_of}")
    if aggregate.resolution_counts_by_window:
        for entry in aggregate.resolution_counts_by_window:
            rate = entry.validation_rate
            rate_str = f"{rate * 100:.1f}%" if rate is not None else "—"
            lines.append(
                f"  {entry.window.value}: validated={entry.validated}/"
                f"{entry.total_resolutions} ({rate_str})"
            )
    else:
        lines.append("  Resolution counts: none recorded")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_signed_dollar(value: float) -> str:
    if value >= 0:
        return f"+{format_dollar(value)}"
    return format_dollar(value)


def _format_signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.1f}%"


def _format_position_age(hours: float) -> str:
    return f"{hours:.1f}"


def _format_distance_pct(distance_usd: float | None, market_value_usd: float) -> str:
    if distance_usd is None or market_value_usd == 0:
        return "—"
    pct = (distance_usd / market_value_usd) * 100.0
    return _format_signed_pct(pct)
