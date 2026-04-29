"""Pure rendering primitives for the guardrail state header."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RegimeLabel,
    RiskBudgetEntry,
    RiskZone,
)

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

_DIRECTIONAL_LABEL_WIDTH = len("Net short")
_OPTIONS_LABEL_WIDTH = len("Delta exposure")
_DEFAULT_HARD_BLOCKS_HEADER = "Hard blocks (do NOT recommend):"


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

    *timestamp* must be a tz-aware UTC datetime; it is rendered in ISO-8601 with
    seconds precision and a trailing ``Z``.
    """
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(None):
        msg = "timestamp must be a tz-aware UTC datetime"
        raise ValueError(msg)
    iso = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"=== GUARDRAIL STATE (invocation {invocation_id}, {iso}) ==="


def render_envelope_close() -> str:
    """Render the closing line of the ``=== GUARDRAIL STATE ===`` envelope."""
    return "==="


def render_zone_tag(zone: RiskZone) -> str:
    """Render the trailing-bracket zone display token used in headroom rows."""
    return _ZONE_TAG_DISPLAY[zone]


def render_regime_line(active: ActiveRiskParameterSet) -> str:
    """Render the ``Regime:`` line driven by *active*'s label and change flag."""
    label = _REGIME_LABEL_DISPLAY[active.regime_label]
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
