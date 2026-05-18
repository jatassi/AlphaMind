"""Pure rendering primitives for the guardrail state header."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from alphamind._kernel.regime import (
    DrawdownTier,
    RegimeLabel,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
from alphamind.portfolio_state.records.positions import (
    InstrumentType,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach

# ---------------------------------------------------------------------------
# Display dictionaries (module-private; helpers below access them)
# ---------------------------------------------------------------------------

_ZONE_TAG_DISPLAY: dict[RiskZone, str] = {
    RiskZone.NORMAL: "NORMAL",
    RiskZone.WARNING: "⚠ WARNING",
    RiskZone.CRITICAL: "\U0001f534 CRITICAL",
    RiskZone.BLOCKED: "BLOCKED",
}

_REGIME_LABEL_DISPLAY: dict[RegimeLabel, str] = {
    RegimeLabel.LOW_VOL: "low-vol compression",
    RegimeLabel.NORMAL: "normal",
    RegimeLabel.ELEVATED: "elevated",
    RegimeLabel.CRISIS: "crisis",
}

DRAWDOWN_TIER_DISPLAY: dict[DrawdownTier, str] = {
    DrawdownTier.CONSTRAINED: "constrained",
    DrawdownTier.HEAVILY_CONSTRAINED: "heavily constrained",
    DrawdownTier.FULL_HALT: "full halt",
}

DRAWDOWN_TIER_RESTRICTIONS: dict[DrawdownTier, str] = {
    DrawdownTier.CONSTRAINED: (
        "max position size 3%, max gross 80%, positions w/ unrealized loss > 10% flagged"
    ),
    DrawdownTier.HEAVILY_CONSTRAINED: (
        "max position size 2%, max gross 60%, positions w/ unrealized loss > 15% flagged"
    ),
    DrawdownTier.FULL_HALT: "no new positions; orderly reductions only",
}

# ---------------------------------------------------------------------------
# Shared rule-id constants (used by validation helpers and headroom blocks)
# ---------------------------------------------------------------------------

SECTOR_RULE_PREFIX = "sector_concentration_"
NET_LONG_RULE_ID = "net_long_pct"
NET_SHORT_RULE_ID = "net_short_pct"
GROSS_RULE_ID = "gross_exposure_pct"
OPTIONS_DELTA_RULE_ID = "options_delta_pct"
OPTIONS_THETA_RULE_ID = "portfolio_theta_pct_per_day"
OPTIONS_VEGA_RULE_ID = "portfolio_vega_pct_per_iv_point"
POSITION_MAX_SIZE_RULE_ID = "position_max_size_pct"
POSITION_MAX_LOSS_EQUITY_RULE_ID = "position_max_loss_equity_pct"
POSITION_MAX_LOSS_OPTIONS_RULE_ID = "position_max_loss_options_pct"
OPTIONS_RULE_IDS: tuple[str, ...] = (
    OPTIONS_DELTA_RULE_ID,
    OPTIONS_THETA_RULE_ID,
    OPTIONS_VEGA_RULE_ID,
)

_DIRECTIONAL_LABEL_WIDTH = len("Net short")
_OPTIONS_LABEL_WIDTH = len("Delta exposure")
_DEFAULT_HARD_BLOCKS_HEADER = "Hard blocks (do NOT recommend):"

# Block-header constants for the proximity / sector-breakdown blocks.
_POSITION_PROXIMITY_HEADER = "Position-level constraint proximity:"
_SECTOR_BREAKDOWN_HEADER = "Sector exposure breakdown (per position):"
_REGIME_TRANSITION_BREACHES_HEADER = "Regime-transition breaches (if any):"
_NONE_LINE = "  None"
_UNCLASSIFIED_GROUP_LABEL = "Unclassified"

# Position-zone classification thresholds (ratio of current_value / limit).
_ZONE_WARNING_THRESHOLD = 0.70
_ZONE_CRITICAL_THRESHOLD = 0.85
_ZONE_BLOCKED_THRESHOLD = 0.95


def format_dollar(value: float) -> str:
    """Render *value* as a dollar amount with comma separators and zero decimals."""
    if value < 0:
        return f"-${-value:,.0f}"
    return f"${value:,.0f}"


def format_pct(value: float) -> str:
    """Render *value* as a percentage scalar with one decimal place (no trailing ``%``)."""
    return f"{value:.1f}"


def render_envelope_open(invocation_id: str, timestamp: datetime) -> str:
    """Render the opening line of the ``=== GUARDRAIL STATE ===`` envelope.

    *timestamp* must carry a UTC offset of zero (``UTC``, ``timezone.utc``, or
    any equivalent tzinfo); rendered in ISO-8601 with seconds precision and a
    trailing ``Z``.
    """
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(None):
        msg = "timestamp must have a UTC offset of zero"
        raise ValueError(msg)
    iso = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"=== GUARDRAIL STATE (invocation {invocation_id}, {iso}) ==="


def render_envelope_close() -> str:
    """Render the closing line of the ``=== GUARDRAIL STATE ===`` envelope."""
    return "==="


def render_zone_tag(zone: RiskZone) -> str:
    """Render the trailing-bracket zone display token used in headroom rows."""
    return _ZONE_TAG_DISPLAY[zone]


def regime_label_display(label: RegimeLabel) -> str:
    """Return the human-readable display string for *label* (e.g. ``"normal"``).

    Centralizes the regime-label display string used by the ``Capital:`` block
    and the regime-transition-breach line; sibling renderers must not redefine
    this mapping.
    """
    return _REGIME_LABEL_DISPLAY[label]


def render_regime_line(active: ActiveRiskParameterSet) -> str:
    """Render the ``Regime:`` line driven by *active*'s label and change flag."""
    label = regime_label_display(active.regime_label)
    flag = "[CHANGED since last invocation]" if active.parameter_change_flag else "[unchanged]"
    return f"Regime: {label} {flag}"


def render_capital_block(
    *,
    available_for_new_positions_usd: float,
    available_for_new_positions_pct: float,
    per_position_max_usd: float,
    per_position_max_pct: float,
    regime_label_display: str,
) -> str:
    """Render the three-line ``Capital:`` block."""
    available_dollars = format_dollar(available_for_new_positions_usd)
    available_pct = format_pct(available_for_new_positions_pct)
    max_dollars = format_dollar(per_position_max_usd)
    max_pct = format_pct(per_position_max_pct)
    return (
        "Capital:\n"
        f"  Available for new positions: {available_dollars} ({available_pct}% of portfolio)\n"
        f"  Per-position max size: {max_dollars} "
        f"({max_pct}% of portfolio, {regime_label_display} regime)"
    )


def _format_headroom_row(
    label: str,
    entry: RiskBudgetEntry,
    *,
    label_width: int,
    zone_tag: str | None = None,
) -> str:
    padded = f"{label}:".ljust(label_width + 1)
    current = format_pct(entry.current_value)
    limit = format_pct(entry.limit_value)
    remaining = format_pct(entry.headroom)
    suffix = f" [{zone_tag}]" if zone_tag is not None else ""
    return f"  {padded} {current}% / {limit}% — room: {remaining}%{suffix}"


def render_sector_headroom_block(
    entries: tuple[RiskBudgetEntry, ...],
    *,
    sector_label_resolver: Callable[[str], str],
) -> str:
    """Render the ``Sector headroom (delta-adjusted):`` block.

    Each row carries the per-sector zone tag from ``render_zone_tag(entry.zone)``.
    Empty *entries* renders only the header followed by an indented ``(none)``.
    """
    if not entries:
        return "Sector headroom (delta-adjusted):\n  (none)"
    labels = [sector_label_resolver(entry.rule_id) for entry in entries]
    label_width = max(len(label) for label in labels)
    rows = ["Sector headroom (delta-adjusted):"]
    rows.extend(
        _format_headroom_row(
            label, entry, label_width=label_width, zone_tag=render_zone_tag(entry.zone)
        )
        for entry, label in zip(entries, labels, strict=True)
    )
    return "\n".join(rows)


def render_directional_headroom_block(
    net_long: RiskBudgetEntry,
    net_short: RiskBudgetEntry | None,
    gross: RiskBudgetEntry,
) -> str:
    """Render the ``Directional headroom:`` block.

    The ``Net short:`` row is omitted when *net_short* is ``None`` (primary portfolio).
    Column alignment is consistent across both layouts.
    """
    rows = [
        "Directional headroom:",
        _format_headroom_row("Net long", net_long, label_width=_DIRECTIONAL_LABEL_WIDTH),
    ]
    if net_short is not None:
        rows.append(
            _format_headroom_row("Net short", net_short, label_width=_DIRECTIONAL_LABEL_WIDTH)
        )
    rows.append(_format_headroom_row("Gross", gross, label_width=_DIRECTIONAL_LABEL_WIDTH))
    return "\n".join(rows)


def render_options_headroom_block(
    delta: RiskBudgetEntry | None,
    theta: RiskBudgetEntry | None,
    vega: RiskBudgetEntry | None,
) -> str | None:
    """Render the ``Options headroom:`` block, or return ``None`` when options are disabled.

    All three arguments must be ``None`` (block omitted) or all three must be present.
    Mixed inputs raise :class:`ValueError`, surfacing upstream config drift.
    """
    if delta is None and theta is None and vega is None:
        return None
    if delta is None or theta is None or vega is None:
        msg = "options block requires all three of delta, theta, vega or none"
        raise ValueError(msg)
    delta_row = _format_headroom_row("Delta exposure", delta, label_width=_OPTIONS_LABEL_WIDTH)
    theta_label = "Theta:".ljust(_OPTIONS_LABEL_WIDTH + 1)
    vega_label = "Vega:".ljust(_OPTIONS_LABEL_WIDTH + 1)
    return "\n".join(
        [
            "Options headroom:",
            delta_row,
            f"  {theta_label} {format_pct(theta.current_value)}% / "
            f"{format_pct(theta.limit_value)}%/day",
            f"  {vega_label} {format_pct(vega.current_value)}% / "
            f"{format_pct(vega.limit_value)}%/pt",
        ]
    )


def render_hard_blocks_block(
    *,
    breaching_entries: tuple[RiskBudgetEntry, ...],
    options_enabled: bool,
    short_selling_enabled: bool,
    header_label: str = _DEFAULT_HARD_BLOCKS_HEADER,
    per_rule_guidance: dict[str, str] | None = None,
) -> str | None:
    """Render the ``Hard blocks`` block.

    Returns ``None`` when *breaching_entries* is empty and both feature flags are ``True``.
    Per-rule lines emit in input order; *per_rule_guidance* maps ``rule_id`` to a trailing
    clause that is appended after `` — ``.
    """
    if not breaching_entries and options_enabled and short_selling_enabled:
        return None
    guidance = per_rule_guidance or {}
    rows = [header_label]
    for entry in breaching_entries:
        line = (
            f"  {entry.rule_label} at "
            f"{format_pct(entry.current_value)}% / {format_pct(entry.limit_value)}% limit"
        )
        suffix = guidance.get(entry.rule_id)
        if suffix:
            line = f"{line} — {suffix}"
        rows.append(line)
    if not options_enabled:
        rows.append("  Options: DISABLED for this portfolio")
    if not short_selling_enabled:
        rows.append("  Short selling: DISABLED for this portfolio")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Shared validation helpers (consumed by every audience renderer + halt-mode wrapper)
# ---------------------------------------------------------------------------


def validate_feature_flag_closure(
    *,
    risk_budget: RiskBudgetConsumption,
    options_enabled: bool,
    short_selling_enabled: bool,
) -> None:
    """Raise if *risk_budget* contains rules incompatible with the feature flags.

    The renderers refuse to silently elide options or short-selling rule entries
    when the corresponding flag is off; the upstream pipeline must drop those
    entries before the projection reaches the renderer.
    """
    if not options_enabled:
        for rule_id in OPTIONS_RULE_IDS:
            if risk_budget.entry_by_rule_id(rule_id) is not None:
                msg = f"options_enabled is False but risk_budget contains options rule {rule_id!r}"
                raise ValueError(msg)
    if not short_selling_enabled and risk_budget.entry_by_rule_id(NET_SHORT_RULE_ID) is not None:
        msg = f"short_selling_enabled is False but risk_budget contains {NET_SHORT_RULE_ID!r} entry"
        raise ValueError(msg)


def require_budget_entry(risk_budget: RiskBudgetConsumption, rule_id: str) -> RiskBudgetEntry:
    """Return the budget entry for *rule_id* or raise if it is absent."""
    entry = risk_budget.entry_by_rule_id(rule_id)
    if entry is None:
        msg = f"risk_budget missing required entry for rule_id {rule_id!r}"
        raise ValueError(msg)
    return entry


def require_param_entry(active: ActiveRiskParameterSet, rule_id: str) -> ActiveRiskParameterEntry:
    """Return the active-parameter entry for *rule_id* or raise if it is absent."""
    for entry in active.entries:
        if entry.rule_id == rule_id:
            return entry
    msg = f"active_risk_parameters missing required entry for rule_id {rule_id!r}"
    raise ValueError(msg)


def resolve_sector_entries(
    risk_budget: RiskBudgetConsumption,
    active_sectors: tuple[str, ...],
) -> tuple[RiskBudgetEntry, ...]:
    """Return budget entries for ``sector_concentration_<key>`` rules in *active_sectors* order."""
    entries: list[RiskBudgetEntry] = []
    for sector_key in active_sectors:
        rule_id = f"{SECTOR_RULE_PREFIX}{sector_key}"
        entry = risk_budget.entry_by_rule_id(rule_id)
        if entry is None:
            msg = f"risk_budget missing required entry for rule_id {rule_id!r}"
            raise ValueError(msg)
        entries.append(entry)
    return tuple(entries)


def make_sector_label_resolver(
    sector_label_display: dict[str, str] | None,
) -> Callable[[str], str]:
    """Return a resolver mapping ``sector_concentration_<key>`` rule_ids to display labels.

    Falls back to the capitalized sector key when no display mapping is provided
    or when the key is not found in *sector_label_display*.
    """

    def _resolver(rule_id: str) -> str:
        sector_key = rule_id.removeprefix(SECTOR_RULE_PREFIX)
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


def _classify_loss_zone(pnl_pct: float, max_loss_pct: float | None) -> RiskZone:
    """Zone of a position's signed P/L vs. its max-loss floor (``-max_loss_pct``).

    Positive P/L yields a non-positive ratio against the negative floor and
    stays NORMAL. CRITICAL is the worst loss-zone level — BLOCKED is reserved
    for size-cap breaches where the operator action is "no more sizing", not
    "close the position".
    """
    if max_loss_pct is None or max_loss_pct <= 0:
        return RiskZone.NORMAL
    loss_progress = -pnl_pct / max_loss_pct
    if loss_progress >= _ZONE_CRITICAL_THRESHOLD:
        return RiskZone.CRITICAL
    if loss_progress >= _ZONE_WARNING_THRESHOLD:
        return RiskZone.WARNING
    return RiskZone.NORMAL


_ZONE_ORDER: tuple[RiskZone, ...] = (
    RiskZone.NORMAL,
    RiskZone.WARNING,
    RiskZone.CRITICAL,
    RiskZone.BLOCKED,
)


def _max_severity_zone(*zones: RiskZone) -> RiskZone:
    return max(zones, key=_ZONE_ORDER.index)


def _max_loss_for(active: ActiveRiskParameterSet, rule_id: str) -> float | None:
    for entry in active.entries:
        if entry.rule_id == rule_id:
            return entry.value
    return None


def _max_loss_for_position(
    pos: PositionView,
    max_loss_equity: float | None,
    max_loss_options: float | None,
) -> float | None:
    if pos.instrument_type == InstrumentType.EQUITY:
        return max_loss_equity
    if pos.instrument_type in (InstrumentType.OPTIONS, InstrumentType.STRATEGY):
        return max_loss_options
    return None


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
    size_zone = _classify_position_zone(pos.position_weight_pct, per_position_max_pct)
    loss_zone = _classify_loss_zone(pos.unrealized_pnl_pct, max_loss)
    zone = _max_severity_zone(size_zone, loss_zone)
    zone_tag = f" [{render_zone_tag(zone)}]" if zone != RiskZone.NORMAL else ""
    return (
        f"  {padded_id} {weight}% of portfolio (max {max_pct}%) "
        f"— P/L: {pnl}% of cost{suffix}{zone_tag}"
    )


def render_position_proximity_block(
    *,
    positions: tuple[StrategistPositionView, ...],
    active_risk_parameters: ActiveRiskParameterSet,
) -> str:
    """Render the per-position constraint proximity block.

    Each row shows position weight against the per-position size limit, the
    unrealized P/L of cost, the per-instrument-type max-loss annotation when
    the corresponding ``position_max_loss_*`` parameter is present, and a
    zone tag (``[⚠ WARNING]`` / ``[CRITICAL]`` / ``[BLOCKED]``) whose severity
    is the max of the size-proximity and loss-proximity zones.
    """
    rows: list[str] = [_POSITION_PROXIMITY_HEADER]
    if not positions:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    per_position_max_pct = require_param_entry(
        active_risk_parameters, POSITION_MAX_SIZE_RULE_ID
    ).value
    max_loss_equity = _max_loss_for(active_risk_parameters, POSITION_MAX_LOSS_EQUITY_RULE_ID)
    max_loss_options = _max_loss_for(active_risk_parameters, POSITION_MAX_LOSS_OPTIONS_RULE_ID)
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


# ---------------------------------------------------------------------------
# Sector exposure breakdown (block 5 in the design)
# ---------------------------------------------------------------------------


def _render_sector_row(view: StrategistPositionView) -> str:
    pos = view.position
    base = f"    {pos.position_id}: {format_pct(pos.position_weight_pct)}% (delta-adj)"
    details = pos.details
    if isinstance(details, OptionsPositionDetails):
        return f"{base} [options, delta {details.greeks.delta:.2f}]"
    if isinstance(details, StrategyPositionDetails):
        return f"{base} [strategy, delta {details.strategy_greeks.delta:.2f}]"
    return base


def render_sector_breakdown_block(
    *,
    positions: tuple[StrategistPositionView, ...],
    active_sectors: tuple[str, ...],
    sector_label_resolver: Callable[[str], str],
    sector_resolver: SectorResolver,
    risk_budget: RiskBudgetConsumption,
) -> str:
    """Render the per-sector position breakdown block.

    Each ``active_sectors`` group renders with its current/limit headroom on
    the header line and indented per-position rows beneath. Positions whose
    sector resolver returns ``None`` (or a key not in *active_sectors*) are
    grouped under ``Unclassified:`` at the end.
    """
    rows: list[str] = [_SECTOR_BREAKDOWN_HEADER]
    grouped: dict[str, list[StrategistPositionView]] = {sector: [] for sector in active_sectors}
    unclassified: list[StrategistPositionView] = []
    for view in positions:
        sector_key = sector_resolver(view.position.record)
        if sector_key is None:
            unclassified.append(view)
        elif sector_key in grouped:
            grouped[sector_key].append(view)
        else:
            unclassified.append(view)
    for sector_key in active_sectors:
        sector_entry = require_budget_entry(risk_budget, f"{SECTOR_RULE_PREFIX}{sector_key}")
        label = sector_label_resolver(f"{SECTOR_RULE_PREFIX}{sector_key}")
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


# ---------------------------------------------------------------------------
# Regime-transition breaches (block 7 in the design)
# ---------------------------------------------------------------------------


def render_regime_transition_breaches_block(
    *,
    breaches: tuple[RegimeTransitionBreach, ...],
    regime_label_display: str,
) -> str | None:
    """Render the ``Regime-transition breaches (if any):`` block.

    Returns ``None`` when *breaches* is empty so the caller can omit the block.
    Each breach renders as one row keyed on the position id (when present) or
    the rule label, with a ``[<rule_label>]`` suffix added for non-position-max
    rules so the rule context is preserved when the row is keyed by position.
    """
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
        if breach.position_id is not None and breach.rule_id != POSITION_MAX_SIZE_RULE_ID:
            suffix = f" [{breach.rule_label}]"
        rows.append(
            f"  {prefix}: {breach.current_value:.1f}% exceeds "
            f"{regime_label_display} regime limit of {breach.new_limit_value:.1f}% "
            f"— overage {breach.overage:.1f}%{suffix}"
        )
    return "\n".join(rows)
