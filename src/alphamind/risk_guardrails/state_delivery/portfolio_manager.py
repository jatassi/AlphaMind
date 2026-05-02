"""Portfolio manager guardrail state header renderer (story 04c)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionReducedDetail,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    DrawdownTier,
    RegimeLabel,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskZone,
)
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.primitives import (
    DRAWDOWN_TIER_DISPLAY,
    DRAWDOWN_TIER_RESTRICTIONS,
    format_dollar,
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
from alphamind.risk_guardrails.state_delivery.strategist import (
    _render_regime_transition_breaches_block,
)

_PM_HARD_BLOCKS_HEADER = "Hard blocks (do NOT issue commands violating):"

_SECTOR_RULE_PREFIX = "sector_concentration_"
_PER_POSITION_MAX_RULE_ID = "position_max_size_pct"
_DAILY_DRAWDOWN_RULE_ID = "daily_drawdown_pct"
_NET_LONG_RULE_ID = "net_long_pct"
_NET_SHORT_RULE_ID = "net_short_pct"
_GROSS_RULE_ID = "gross_exposure_pct"
_OPTIONS_DELTA_RULE_ID = "options_delta_pct"
_OPTIONS_THETA_RULE_ID = "portfolio_theta_pct_per_day"
_OPTIONS_VEGA_RULE_ID = "portfolio_vega_pct_per_iv_point"
_OPTIONS_RULE_IDS = (_OPTIONS_DELTA_RULE_ID, _OPTIONS_THETA_RULE_ID, _OPTIONS_VEGA_RULE_ID)

_REGIME_LABEL_DISPLAY: dict[RegimeLabel, str] = {
    RegimeLabel.LOW_VOL: "low-vol compression",
    RegimeLabel.NORMAL: "normal",
    RegimeLabel.ELEVATED: "elevated",
    RegimeLabel.CRISIS: "crisis",
}


class CrossConstraintImpactPerRule(BaseModel):
    """One per-rule projection inside a :class:`CrossConstraintImpact`."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_label: str
    current: float
    projected_after: float
    limit: float
    unit: str


class CrossConstraintImpact(BaseModel):
    """Pre-computed projection of pending proposals' impact across all rules."""

    model_config = ConfigDict(frozen=True)

    per_rule: tuple[CrossConstraintImpactPerRule, ...]
    flagged_rule_ids: tuple[str, ...]
    available_capital_before_usd: float
    available_capital_after_usd: float


class RegimeOverride(BaseModel):
    """An active regime overlay (pre-event tightening, stress overlay, ...)."""

    model_config = ConfigDict(frozen=True)

    overlay_name: str
    description: str
    expires_at: datetime | None


class CorrelationState(BaseModel):
    """Portfolio-level correlation snapshot for the PM block."""

    model_config = ConfigDict(frozen=True)

    weighted_avg_correlation: float
    correlation_limit: float
    zone: RiskZone
    highest_pairwise_position_a: str
    highest_pairwise_position_b: str
    highest_pairwise_value: float


class DependencyRiskFlag(BaseModel):
    """Catalyst-failure dependency snapshot for the PM block."""

    model_config = ConfigDict(frozen=True)

    max_catalyst_failure_exposure_pct: float
    catalyst_failure_limit_pct: float
    zone: RiskZone
    effective_independent_thesis_count: int
    worst_shared_catalyst_label: str
    worst_shared_catalyst_position_ids: tuple[str, ...]


def render_pm_header(  # noqa: PLR0913 — keyword-only signature dictated by story 04c AC #3
    *,
    pm_view: PortfolioManagerView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    cross_constraint_impact: CrossConstraintImpact,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
) -> str:
    """Render the PM's complete ``=== GUARDRAIL STATE ===`` header."""
    _validate_feature_flag_closure(
        risk_budget=pm_view.risk_budget,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    per_position_max_pct = _require_param_entry(
        pm_view.active_risk_parameters, _PER_POSITION_MAX_RULE_ID
    ).value
    available_pct = (
        (available_for_new_positions_usd / total_portfolio_value_usd) * 100.0
        if total_portfolio_value_usd
        else 0.0
    )
    per_position_max_usd = total_portfolio_value_usd * per_position_max_pct / 100.0
    regime_display = _REGIME_LABEL_DISPLAY[pm_view.active_risk_parameters.regime_label]
    sector_entries = _resolve_sector_entries(pm_view.risk_budget, active_sectors)
    sector_label_resolver = _make_sector_label_resolver(sector_label_display)
    blocks: list[str] = [
        "\n".join(
            [
                render_envelope_open(invocation_id, timestamp),
                render_regime_line(pm_view.active_risk_parameters),
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
            net_long=_require_budget_entry(pm_view.risk_budget, _NET_LONG_RULE_ID),
            net_short=(
                pm_view.risk_budget.entry_by_rule_id(_NET_SHORT_RULE_ID)
                if short_selling_enabled
                else None
            ),
            gross=_require_budget_entry(pm_view.risk_budget, _GROSS_RULE_ID),
        ),
    ]
    options_block = render_options_headroom_block(
        delta=pm_view.risk_budget.entry_by_rule_id(_OPTIONS_DELTA_RULE_ID),
        theta=pm_view.risk_budget.entry_by_rule_id(_OPTIONS_THETA_RULE_ID),
        vega=pm_view.risk_budget.entry_by_rule_id(_OPTIONS_VEGA_RULE_ID),
    )
    if options_block is not None:
        blocks.append(options_block)
    blocks.append(_render_position_proximity_block(pm_view.positions, per_position_max_pct))
    blocks.append(
        _render_sector_breakdown_block(
            positions=pm_view.positions,
            sector_entries=sector_entries,
            sector_label_resolver=sector_label_resolver,
            sector_resolver=sector_resolver,
        )
    )
    blocks.append(_render_cross_constraint_impact_block(cross_constraint_impact))
    blocks.append(_render_validation_tool_reminder_block())
    blocks.append(
        _render_drawdown_context_block(
            pm_view=pm_view,
            total_portfolio_value_usd=total_portfolio_value_usd,
        )
    )
    breaches_block = _render_regime_transition_breaches_block(
        breaches=regime_transition_breaches,
        regime_label_display=_REGIME_LABEL_DISPLAY[pm_view.active_risk_parameters.regime_label],
    )
    if breaches_block is not None:
        blocks.append(breaches_block)
    blocks.append(_render_recent_engine_actions_block(pm_view.intra_invocation_changelog))
    blocks.append(_render_active_regime_overrides_block(active_regime_overrides))
    correlation_block = _render_correlation_state_block(
        state=correlation_state,
        positions=pm_view.positions,
        config=config,
    )
    if correlation_block is not None:
        blocks.append(correlation_block)
    dependency_block = _render_dependency_risk_flag_block(
        flag=dependency_risk_flag,
        positions=pm_view.positions,
        config=config,
    )
    if dependency_block is not None:
        blocks.append(dependency_block)
    hard_blocks = render_hard_blocks_block(
        breaching_entries=pm_view.risk_budget.breaching_entries(),
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        header_label=_PM_HARD_BLOCKS_HEADER,
    )
    if hard_blocks is not None:
        blocks.append(hard_blocks)
    return "\n\n".join(blocks) + "\n" + render_envelope_close()


# ---------------------------------------------------------------------------
# Per-position proximity helper (PM-only; strategist-style)
# ---------------------------------------------------------------------------


_POSITION_PROXIMITY_HEADER = "Position-level constraint proximity:"


def _format_signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{format_pct(abs(value))}%"


def _render_position_proximity_block(
    positions: tuple[StrategistPositionView, ...],
    per_position_max_pct: float,
) -> str:
    if not positions:
        return f"{_POSITION_PROXIMITY_HEADER}\n  None"
    pid_width = max(len(p.position.position_id) for p in positions)
    rows = [_POSITION_PROXIMITY_HEADER]
    for view in positions:
        position = view.position
        pid_cell = f"{position.position_id}:".ljust(pid_width + 1)
        weight = format_pct(position.position_weight_pct)
        max_size = format_pct(per_position_max_pct)
        pnl = _format_signed_pct(position.unrealized_pnl_pct)
        rows.append(f"  {pid_cell} {weight}% of portfolio (max {max_size}%) — P/L: {pnl} of cost")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Per-sector breakdown helper (PM-only; strategist-style)
# ---------------------------------------------------------------------------


_SECTOR_BREAKDOWN_HEADER = "Sector exposure breakdown (per position):"


def _render_sector_breakdown_block(
    *,
    positions: tuple[StrategistPositionView, ...],
    sector_entries: tuple[RiskBudgetEntry, ...],
    sector_label_resolver: Callable[[str], str],
    sector_resolver: Callable[[PositionRecord], str | None],
) -> str:
    by_sector: dict[str, list[StrategistPositionView]] = {}
    for view in positions:
        sector_key = sector_resolver(view.position)
        if sector_key is None:
            continue
        by_sector.setdefault(sector_key, []).append(view)
    rows = [_SECTOR_BREAKDOWN_HEADER]
    for entry in sector_entries:
        sector_key = entry.rule_id.removeprefix(_SECTOR_RULE_PREFIX)
        sector_label = sector_label_resolver(entry.rule_id)
        current = format_pct(entry.current_value)
        limit = format_pct(entry.limit_value)
        rows.append(f"  {sector_label} ({current}% / {limit}%):")
        members = by_sector.get(sector_key, [])
        for view in members:
            weight = format_pct(view.position.position_weight_pct)
            rows.append(f"    {view.position.position_id}: {weight}% (delta-adj)")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Cross-constraint impact summary helper (PM-only)
# ---------------------------------------------------------------------------


_CROSS_CONSTRAINT_HEADER = "Cross-constraint impact summary:"
_CAPITAL_LABEL = "Capital"


def _render_cross_constraint_impact_block(impact: CrossConstraintImpact) -> str:
    if not impact.per_rule:
        return f"{_CROSS_CONSTRAINT_HEADER}\n  No pending proposals; no projected impact."
    label_width = max(
        max(len(rule.rule_label) for rule in impact.per_rule),
        len(_CAPITAL_LABEL),
    )
    rows = [
        _CROSS_CONSTRAINT_HEADER,
        "  If all pending proposals are approved as-sized:",
    ]
    for rule in impact.per_rule:
        label_cell = f"{rule.rule_label}:".ljust(label_width + 1)
        current = format_pct(rule.current)
        projected = format_pct(rule.projected_after)
        if rule.projected_after <= rule.limit:
            status = "(within limit)"
        else:
            overage = rule.projected_after - rule.limit
            status = f"(would breach by {format_pct(overage)}%)"
        rows.append(f"    {label_cell} {current}% → {projected}% {status}")
    capital_cell = f"{_CAPITAL_LABEL}:".ljust(label_width + 1)
    before = format_dollar(impact.available_capital_before_usd)
    after = format_dollar(impact.available_capital_after_usd)
    rows.append(f"    {capital_cell} {before} → {after}")
    if impact.flagged_rule_ids:
        flagged_labels = [
            rule.rule_label for rule in impact.per_rule if rule.rule_id in impact.flagged_rule_ids
        ]
        rows.append(f"    Flagged: {', '.join(flagged_labels)}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Validation tool reminder helper (PM-only, static)
# ---------------------------------------------------------------------------


_VALIDATION_TOOL_REMINDER_BLOCK = (
    "Guardrail validation tool available:\n"
    "  Call validate_guardrail(instrument, direction, size) "
    "to check any proposed modification.\n"
    "  Tool tracks cumulative impact across multiple checks within this invocation."
)


def _render_validation_tool_reminder_block() -> str:
    return _VALIDATION_TOOL_REMINDER_BLOCK


# ---------------------------------------------------------------------------
# Drawdown context helper (PM-only; signed daily P/L)
# ---------------------------------------------------------------------------


_DRAWDOWN_CONTEXT_HEADER = "Drawdown context:"
# Matches the design's verbatim alignment: "Daily limit:" + 3 spaces aligns with
# "Daily P/L:" + 5 spaces and "Cumulative:" + 4 spaces.
_DRAWDOWN_VALUE_COLUMN = len("Daily limit:") + 3


def _render_drawdown_context_block(
    *,
    pm_view: PortfolioManagerView,
    total_portfolio_value_usd: float,
) -> str:
    daily_pnl_pct = (
        (pm_view.portfolio_pnl.daily_total_pnl_usd / total_portfolio_value_usd) * 100.0
        if total_portfolio_value_usd
        else 0.0
    )
    daily_zone_tag = render_zone_tag(pm_view.drawdown.daily_zone)
    cumulative_zone_tag = render_zone_tag(pm_view.drawdown.cumulative_zone)
    daily_limit = _require_param_entry(pm_view.active_risk_parameters, _DAILY_DRAWDOWN_RULE_ID)
    daily_budget = _require_budget_entry(pm_view.risk_budget, _DAILY_DRAWDOWN_RULE_ID)
    daily_pnl_body = f"{_format_signed_pct(daily_pnl_pct)} ({daily_zone_tag})"
    rows = [
        _DRAWDOWN_CONTEXT_HEADER,
        _format_drawdown_row("Daily P/L", daily_pnl_body),
        _format_drawdown_row(
            "Daily limit",
            f"{format_pct(daily_limit.value)}% — headroom: {format_pct(daily_budget.headroom)}%",
        ),
        _format_drawdown_row(
            "Cumulative",
            f"{format_pct(pm_view.drawdown.current_drawdown_pct)}% from HWM "
            f"({cumulative_zone_tag})",
        ),
    ]
    if pm_view.drawdown.cumulative_tier is not None:
        rows.append(_format_cumulative_tier_row(pm_view.drawdown.cumulative_tier))
    return "\n".join(rows)


def _format_drawdown_row(label: str, body: str) -> str:
    label_cell = f"{label}:".ljust(_DRAWDOWN_VALUE_COLUMN)
    return f"  {label_cell}{body}"


def _format_cumulative_tier_row(tier: DrawdownTier) -> str:
    tier_label = DRAWDOWN_TIER_DISPLAY[tier]
    restrictions = DRAWDOWN_TIER_RESTRICTIONS[tier]
    return f"  Cumulative tier: {tier_label} — {restrictions}"


# ---------------------------------------------------------------------------
# Recent engine-originated actions helper (PM-only)
# ---------------------------------------------------------------------------


_RECENT_ENGINE_ACTIONS_HEADER = "Recent engine-originated actions (since last invocation):"
_ENGINE_ACTION_EVENT_TYPES = frozenset({EventType.POSITION_CLOSED, EventType.POSITION_REDUCED})
_ACTION_VERB_BY_EVENT: dict[EventType, str] = {
    EventType.POSITION_CLOSED: "closed",
    EventType.POSITION_REDUCED: "trimmed",
}


def _render_recent_engine_actions_block(
    changelog: tuple[ActivityLogEntry, ...],
) -> str:
    rows = [_RECENT_ENGINE_ACTIONS_HEADER]
    engine_entries = [
        entry
        for entry in changelog
        if entry.event_type in _ENGINE_ACTION_EVENT_TYPES
        and entry.source == EventSource.GUARDRAIL_LAYER
    ]
    if not engine_entries:
        rows.append("  None")
        return "\n".join(rows)
    for entry in sorted(engine_entries, key=lambda e: e.timestamp):
        rows.append(_format_engine_action_row(entry))
    return "\n".join(rows)


def _format_engine_action_row(entry: ActivityLogEntry) -> str:
    iso_ts = entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    verb = _ACTION_VERB_BY_EVENT[entry.event_type]
    target = entry.position_id or entry.order_id or ""
    reason = _engine_action_reason(entry)
    return f"  {iso_ts}: Engine {verb} {target} — reason: {reason}"


def _engine_action_reason(entry: ActivityLogEntry) -> str:
    detail = entry.detail
    if isinstance(detail, PositionClosedDetail):
        return detail.thesis_resolution_category
    if isinstance(detail, PositionReducedDetail):
        return detail.close_rationale_classification
    return ""


# ---------------------------------------------------------------------------
# Active regime overrides helper (PM-only)
# ---------------------------------------------------------------------------


_ACTIVE_REGIME_OVERRIDES_HEADER = "Active regime overrides:"


def _render_active_regime_overrides_block(
    overrides: tuple[RegimeOverride, ...],
) -> str:
    rows = [_ACTIVE_REGIME_OVERRIDES_HEADER]
    if not overrides:
        rows.append("  None")
        return "\n".join(rows)
    for override in overrides:
        line = f"  {override.description}"
        if override.expires_at is not None:
            iso = override.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            line = f"{line} (expires {iso})"
        rows.append(line)
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Correlation state helper (PM-only; threshold-gated)
# ---------------------------------------------------------------------------


_CORRELATION_STATE_HEADER = "Correlation state:"


def _render_correlation_state_block(
    *,
    state: CorrelationState | None,
    positions: tuple[StrategistPositionView, ...],
    config: StateDeliveryConfig,
) -> str | None:
    if state is None or len(positions) < config.correlation_state_min_position_count:
        return None
    zone_tag = render_zone_tag(state.zone)
    return "\n".join(
        [
            _CORRELATION_STATE_HEADER,
            f"  Portfolio weighted avg correlation: "
            f"{state.weighted_avg_correlation:.2f} / "
            f"{state.correlation_limit:.2f} [{zone_tag}]",
            f"  Highest pairwise: {state.highest_pairwise_position_a} ↔ "
            f"{state.highest_pairwise_position_b} = {state.highest_pairwise_value:.2f}",
        ]
    )


# ---------------------------------------------------------------------------
# Dependency risk flag helper (PM-only; threshold-gated)
# ---------------------------------------------------------------------------


_DEPENDENCY_RISK_FLAG_HEADER = "Dependency risk flag:"


def _render_dependency_risk_flag_block(
    *,
    flag: DependencyRiskFlag | None,
    positions: tuple[StrategistPositionView, ...],
    config: StateDeliveryConfig,
) -> str | None:
    if flag is None or len(positions) < config.dependency_risk_flag_min_position_count:
        return None
    zone_tag = render_zone_tag(flag.zone)
    pids = ", ".join(flag.worst_shared_catalyst_position_ids)
    return "\n".join(
        [
            _DEPENDENCY_RISK_FLAG_HEADER,
            f"  Max catalyst-failure exposure: "
            f"{format_pct(flag.max_catalyst_failure_exposure_pct)}% / "
            f"{format_pct(flag.catalyst_failure_limit_pct)}% [{zone_tag}]",
            f"  Effective independent thesis count: {flag.effective_independent_thesis_count}",
            f'  Worst shared catalyst: "{flag.worst_shared_catalyst_label}" — '
            f"positions: {{{pids}}}",
        ]
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_param_entry(active: ActiveRiskParameterSet, rule_id: str) -> ActiveRiskParameterEntry:
    for entry in active.entries:
        if entry.rule_id == rule_id:
            return entry
    msg = f"active_risk_parameters missing required entry for rule_id {rule_id!r}"
    raise ValueError(msg)


def _require_budget_entry(risk_budget: RiskBudgetConsumption, rule_id: str) -> RiskBudgetEntry:
    entry = risk_budget.entry_by_rule_id(rule_id)
    if entry is None:
        msg = f"risk_budget missing required entry for rule_id {rule_id!r}"
        raise ValueError(msg)
    return entry


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
