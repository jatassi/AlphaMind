"""Halt-mode guardrail state header wrappers (story 05).

Three audience-specific wrappers compose the same primitives the normal
renderers use, with documented block-level substitutions: a banner block
prepended after the envelope-open line, plus per-audience block replacements
or annotations. The wrappers are invoked only when ``HaltState`` is active;
the upstream pipeline classifies the trigger and constructs the typed record.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import AnalystView
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import StrategistView
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RegimeLabel,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.orders import OrderRecord
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.analyst import (
    _make_sector_label_resolver as _analyst_sector_label_resolver,
)
from alphamind.risk_guardrails.state_delivery.analyst import (
    _render_abandoned_openings_block,
    _render_held_positions_block,
)
from alphamind.risk_guardrails.state_delivery.analyst import (
    _require_entry as _analyst_require_entry,
)
from alphamind.risk_guardrails.state_delivery.analyst import (
    _resolve_sector_entries as _analyst_resolve_sector_entries,
)
from alphamind.risk_guardrails.state_delivery.analyst import (
    _validate_feature_flag_closure as _analyst_validate_feature_flag_closure,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _PM_HARD_BLOCKS_HEADER,
    CorrelationState,
    CrossConstraintImpact,
    DependencyRiskFlag,
    RegimeOverride,
    _render_active_regime_overrides_block,
    _render_correlation_state_block,
    _render_dependency_risk_flag_block,
    _render_drawdown_context_block,
    _render_recent_engine_actions_block,
    _render_validation_tool_reminder_block,
    _require_budget_entry,
    _require_param_entry,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _make_sector_label_resolver as _pm_sector_label_resolver,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _render_position_proximity_block as _pm_position_proximity_block,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _render_regime_transition_breaches_block as _pm_regime_transition_breaches_block,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _render_sector_breakdown_block as _pm_sector_breakdown_block,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _resolve_sector_entries as _pm_resolve_sector_entries,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    _validate_feature_flag_closure as _pm_validate_feature_flag_closure,
)
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
)
from alphamind.risk_guardrails.state_delivery.strategist import render_strategist_header

# ---------------------------------------------------------------------------
# Mode-line constants per audience
# ---------------------------------------------------------------------------

_ANALYST_MODE_LINE = "Mode: WATCHLIST ONLY — do not generate trade proposals"
_STRATEGIST_MODE_LINE = "Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions"
_PM_AVAILABLE_ACTIONS_LINE = "Available actions: CLOSE, ADJUST, CANCEL only"
_PM_BLOCKED_ACTIONS_LINE = "Blocked actions: OPEN, ADD"
_PER_POSITION_MAX_RULE_ID = "position_max_size_pct"

_PENDING_ORDERS_REVIEW_HEADER = "Pending orders review:"
_PENDING_ORDERS_NONE_LINE = "  None"

_HALT_MODE_CROSS_CONSTRAINT_HEADER = "Cross-constraint impact summary:"
_HALT_MODE_CROSS_CONSTRAINT_SCOPED_LINE = (
    "  Scoped to risk-reducing actions only (CLOSE, ADJUST, CANCEL)."
)
_HALT_MODE_NO_PENDING_ACTIONS_LINE = "  No pending risk-reducing actions; no projected impact."

_HALT_MODE_OPEN_BLOCK_LINE = "  OPEN: BLOCKED (halt mode)"
_HALT_MODE_ADD_BLOCK_LINE = "  ADD: BLOCKED (halt mode)"

_HALT_MODE_CAPITAL_BLOCK = (
    "Capital:\n"
    "  New positions: BLOCKED (halt active)\n"
    "  Per-position max size: not applicable (halt mode)"
)

_REGIME_LABEL_DISPLAY: dict[RegimeLabel, str] = {
    RegimeLabel.LOW_VOL: "low-vol compression",
    RegimeLabel.NORMAL: "normal",
    RegimeLabel.ELEVATED: "elevated",
    RegimeLabel.CRISIS: "crisis",
}

_NET_LONG_RULE_ID = "net_long_pct"
_NET_SHORT_RULE_ID = "net_short_pct"
_GROSS_RULE_ID = "gross_exposure_pct"
_OPTIONS_DELTA_RULE_ID = "options_delta_pct"
_OPTIONS_THETA_RULE_ID = "portfolio_theta_pct_per_day"
_OPTIONS_VEGA_RULE_ID = "portfolio_vega_pct_per_iv_point"


# ---------------------------------------------------------------------------
# Shared banner helper
# ---------------------------------------------------------------------------


def _render_halt_mode_banner(halt_state: HaltState) -> str:
    """Render the shared ``** HALT MODE ACTIVE ... **`` line."""
    pct = format_pct(halt_state.daily_drawdown_pct)
    limit = format_pct(halt_state.daily_drawdown_limit_pct)
    return f"** HALT MODE ACTIVE — daily drawdown {pct}% / {limit}% **"


# ---------------------------------------------------------------------------
# Analyst — watchlist mode wrapper
# ---------------------------------------------------------------------------


def render_analyst_header_halt_mode(  # noqa: PLR0913 — mirrors render_analyst_header
    *,
    halt_state: HaltState,
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
    """Render the analyst's halt-mode (WATCHLIST) header.

    Composition mirrors :func:`render_analyst_header`'s block order with two
    documented substitutions: the banner+mode-line is prepended after the
    envelope-open, and the ``Capital:`` block is replaced with the halt-mode
    variant. All other blocks (regime, sector headroom, directional headroom,
    options headroom, held positions, abandoned openings, hard blocks) render
    verbatim from the same primitives the normal renderer uses.
    """
    del config
    _analyst_validate_feature_flag_closure(
        risk_budget=risk_budget,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    sector_entries = _analyst_resolve_sector_entries(risk_budget, active_sectors)
    sector_label_resolver = _analyst_sector_label_resolver(sector_label_display)

    blocks: list[str] = [
        "\n".join(
            [
                render_envelope_open(invocation_id, timestamp),
                _render_halt_mode_banner(halt_state),
                _ANALYST_MODE_LINE,
                render_regime_line(active_risk_parameters),
            ]
        ),
        _HALT_MODE_CAPITAL_BLOCK,
        render_sector_headroom_block(sector_entries, sector_label_resolver=sector_label_resolver),
        render_directional_headroom_block(
            net_long=_analyst_require_entry(risk_budget, _NET_LONG_RULE_ID),
            net_short=(
                risk_budget.entry_by_rule_id(_NET_SHORT_RULE_ID) if short_selling_enabled else None
            ),
            gross=_analyst_require_entry(risk_budget, _GROSS_RULE_ID),
        ),
    ]
    options_block = render_options_headroom_block(
        delta=risk_budget.entry_by_rule_id(_OPTIONS_DELTA_RULE_ID),
        theta=risk_budget.entry_by_rule_id(_OPTIONS_THETA_RULE_ID),
        vega=risk_budget.entry_by_rule_id(_OPTIONS_VEGA_RULE_ID),
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


def render_strategist_header_halt_mode(  # noqa: PLR0913 — mirrors render_strategist_header
    *,
    halt_state: HaltState,
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
    """Render the strategist's halt-mode (DEFENSIVE POSTURE) header.

    The strategist's halt-mode behavioral contract surfaces in the prompt and
    output schema, not in the header — the header's only documented change is
    the banner + mode-line prepended after the envelope-open. The wrapper
    delegates every block to :func:`render_strategist_header` and splices the
    banner lines at the structurally fixed position immediately after the
    envelope-open line.
    """
    normal_rendered = render_strategist_header(
        strategist_view=strategist_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=config,
        sector_resolver=sector_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
    )
    return _splice_banner_after_envelope_open(
        normal_rendered,
        banner_lines=(_render_halt_mode_banner(halt_state), _STRATEGIST_MODE_LINE),
    )


def _splice_banner_after_envelope_open(
    rendered: str,
    *,
    banner_lines: tuple[str, ...],
) -> str:
    """Splice *banner_lines* in immediately after the envelope-open line.

    The envelope-open line is anchored at position 0 of every renderer's
    output by the :func:`render_envelope_open` contract; insertion at
    position 1 is structural, not content-search-based.
    """
    lines = rendered.splitlines()
    return "\n".join([lines[0], *banner_lines, *lines[1:]])


# ---------------------------------------------------------------------------
# PM halt-mode helpers (block-level)
# ---------------------------------------------------------------------------


def _render_pending_orders_review_block(
    pending_orders: tuple[OrderRecord, ...],
    current_price_lookup: Callable[[str], float],
) -> str:
    """Render the PM's halt-mode ``Pending orders review:`` block."""
    rows: list[str] = [_PENDING_ORDERS_REVIEW_HEADER]
    if not pending_orders:
        rows.append(_PENDING_ORDERS_NONE_LINE)
        return "\n".join(rows)
    for order in pending_orders:
        rows.append(_render_pending_order_row(order, current_price_lookup))
    return "\n".join(rows)


def _render_pending_order_row(
    order: OrderRecord,
    current_price_lookup: Callable[[str], float],
) -> str:
    ticker = _resolve_order_ticker(order)
    try:
        current_price = current_price_lookup(ticker)
    except KeyError as exc:
        msg = f"current_price_lookup missing price for ticker {ticker!r}"
        raise ValueError(msg) from exc
    limit_price = order.price_parameters.limit_price
    if limit_price is None:
        msg = f"pending order {order.order_id!r} has no limit_price"
        raise ValueError(msg)
    distance_pct = ((current_price - limit_price) / limit_price) * 100.0
    distance_sign = "+" if distance_pct >= 0 else "-"
    return (
        f"  {order.order_id}: {order.direction.value} {ticker} @ ${limit_price:.2f} "
        f"— current distance: {distance_sign}{abs(distance_pct):.1f}% "
        f"(placed {order.age_hours:.1f}h ago)"
    )


def _resolve_order_ticker(order: OrderRecord) -> str:
    spec = order.instrument_spec
    if spec.ticker is not None:
        return spec.ticker
    if spec.underlying is not None:
        return spec.underlying
    msg = f"pending order {order.order_id!r} has no ticker or underlying"
    raise ValueError(msg)


def _render_halt_mode_cross_constraint_block(
    cross_constraint_impact: CrossConstraintImpact,
) -> str:
    """Render the PM's halt-mode scoped cross-constraint-impact block."""
    rows: list[str] = [
        _HALT_MODE_CROSS_CONSTRAINT_HEADER,
        _HALT_MODE_CROSS_CONSTRAINT_SCOPED_LINE,
    ]
    if not cross_constraint_impact.per_rule:
        rows.append(_HALT_MODE_NO_PENDING_ACTIONS_LINE)
        return "\n".join(rows)
    label_width = max(len(rule.rule_label) for rule in cross_constraint_impact.per_rule)
    for rule in cross_constraint_impact.per_rule:
        label_cell = f"{rule.rule_label}:".ljust(label_width + 1)
        current = format_pct(rule.current)
        projected = format_pct(rule.projected_after)
        if rule.projected_after <= rule.limit:
            status = "(within limit)"
        else:
            overage = rule.projected_after - rule.limit
            status = f"(would breach by {format_pct(overage)}%)"
        rows.append(f"  {label_cell} {current}% → {projected}% {status}")
    return "\n".join(rows)


def _inject_halt_mode_hard_block_lines(
    rendered_hard_blocks_block: str | None,
    halt_state: HaltState,
) -> str:
    """Inject ``OPEN: BLOCKED (halt mode)`` and ``ADD: BLOCKED (halt mode)`` lines.

    When the primitive returns ``None`` (no breaches, no disabled features), the
    helper synthesizes the block from scratch with just the halt-mode action lines.
    Otherwise the lines are appended to the rendered block.
    """
    del halt_state  # parameter kept for interface symmetry; halt-mode action lines are constant
    if rendered_hard_blocks_block is None:
        return "\n".join(
            [
                _PM_HARD_BLOCKS_HEADER,
                _HALT_MODE_OPEN_BLOCK_LINE,
                _HALT_MODE_ADD_BLOCK_LINE,
            ]
        )
    return "\n".join(
        [rendered_hard_blocks_block, _HALT_MODE_OPEN_BLOCK_LINE, _HALT_MODE_ADD_BLOCK_LINE]
    )


# ---------------------------------------------------------------------------
# PM — risk reduction mode wrapper
# ---------------------------------------------------------------------------


def render_pm_header_halt_mode(  # noqa: PLR0913 — mirrors render_pm_header
    *,
    halt_state: HaltState,
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
    pending_orders: tuple[OrderRecord, ...],
    current_price_lookup: Callable[[str], float],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
) -> str:
    """Render the PM's halt-mode (RISK REDUCTION) header.

    Composition mirrors :func:`render_pm_header`'s block order with three
    documented changes: the three-line banner block prepended after the
    envelope-open; a ``Pending orders review:`` block inserted between the
    banner and the rest of the header; the cross-constraint-impact block
    replaced with a scoped variant; and the hard-blocks block annotated with
    ``OPEN: BLOCKED (halt mode)`` and ``ADD: BLOCKED (halt mode)`` lines
    (synthesized from scratch when the primitive returns ``None``).
    """
    _pm_validate_feature_flag_closure(
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
    sector_entries = _pm_resolve_sector_entries(pm_view.risk_budget, active_sectors)
    sector_label_resolver = _pm_sector_label_resolver(sector_label_display)

    blocks: list[str] = [
        "\n".join(
            [
                render_envelope_open(invocation_id, timestamp),
                _render_halt_mode_banner(halt_state),
                _PM_AVAILABLE_ACTIONS_LINE,
                _PM_BLOCKED_ACTIONS_LINE,
                render_regime_line(pm_view.active_risk_parameters),
            ]
        ),
        _render_pending_orders_review_block(pending_orders, current_price_lookup),
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
    blocks.append(_pm_position_proximity_block(pm_view.positions, per_position_max_pct))
    blocks.append(
        _pm_sector_breakdown_block(
            positions=pm_view.positions,
            sector_entries=sector_entries,
            sector_label_resolver=sector_label_resolver,
            sector_resolver=sector_resolver,
        )
    )
    blocks.append(_render_halt_mode_cross_constraint_block(cross_constraint_impact))
    blocks.append(_render_validation_tool_reminder_block())
    blocks.append(
        _render_drawdown_context_block(
            pm_view=pm_view,
            total_portfolio_value_usd=total_portfolio_value_usd,
        )
    )
    if regime_transition_breaches:
        blocks.append(
            _pm_regime_transition_breaches_block(
                breaches=regime_transition_breaches,
                regime_label_display=regime_display,
            )
        )
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
    rendered_hard_blocks = render_hard_blocks_block(
        breaching_entries=pm_view.risk_budget.breaching_entries(),
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        header_label=_PM_HARD_BLOCKS_HEADER,
    )
    blocks.append(_inject_halt_mode_hard_block_lines(rendered_hard_blocks, halt_state))
    return "\n\n".join(blocks) + "\n" + render_envelope_close()
