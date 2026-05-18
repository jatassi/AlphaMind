"""Human-readable rendering for activity-log entries (ALP-552).

The strategist and PM input bundles share these helpers for the
``=== ACTIVITY LOG (intra-invocation) ===`` block and per-position
modification trails. ``render_activity_log_row`` formats the outer
``[ts] event_type: summary  (position=…, order=…)`` line;
``summarize_activity_detail`` produces the inner detail summary, with a
``dataclasses.fields()``-based fallback so any future detail class still
yields ``field=value, ...`` output instead of a bare class name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, is_dataclass
from typing import Any

from alphamind.portfolio_state.events.bracket import BracketModifiedDetail
from alphamind.portfolio_state.events.configuration import DistillationConfigChangeDetail
from alphamind.portfolio_state.events.order_lifecycle import OrderModifiedDetail
from alphamind.portfolio_state.events.reconciliation import ReconciliationAlertDetail
from alphamind.portfolio_state.events.risk_guardrail import (
    EmergencyInvocationRequestedDetail,
    GreeksRefreshFailedDetail,
    HaltActivatedDetail,
    HaltLiftedDetail,
)
from alphamind.portfolio_state.events.thesis import ThesisStatusChangedDetail
from alphamind.portfolio_state.events.types import ActivityLogEntry

__all__ = ["render_activity_log_row", "summarize_activity_detail"]


def render_activity_log_row(entry: ActivityLogEntry) -> str:
    """Render one ``[ts] event_type: summary  (position=…, order=…)`` line."""
    ts = entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = summarize_activity_detail(entry.detail)
    suffix_parts: list[str] = []
    if entry.position_id is not None:
        suffix_parts.append(f"position={entry.position_id}")
    if entry.order_id is not None:
        suffix_parts.append(f"order={entry.order_id}")
    suffix = f"  ({', '.join(suffix_parts)})" if suffix_parts else ""
    return f"  [{ts}] {entry.event_type.value}: {summary}{suffix}"


def summarize_activity_detail(detail: Any) -> str:
    """Render a single-line summary of an activity-log detail payload.

    No code path returns a bare class name. Detail types with bespoke
    phrasing have a private renderer in ``_RENDERERS``; other dataclass
    payloads fall back to ``field=value, ...`` so the line stays useful
    even when a new detail class hasn't been registered.
    """
    renderer = _RENDERERS.get(type(detail))
    if renderer is not None:
        return renderer(detail)
    if is_dataclass(detail):
        rendered = ", ".join(f"{f.name}={getattr(detail, f.name)!s}" for f in fields(detail))
        return rendered or repr(detail)
    return repr(detail)


def _render_field_change(
    field_changed: str, old: object, new: object, rationale: str | None
) -> str:
    rationale_str = f" ({rationale})" if rationale else ""
    return f"{field_changed}: {old} → {new}{rationale_str}"


def _render_bracket_modified(d: BracketModifiedDetail) -> str:
    return _render_field_change(d.field_changed, d.old_value, d.new_value, d.rationale)


def _render_order_modified(d: OrderModifiedDetail) -> str:
    return _render_field_change(d.field_changed, d.old_value, d.new_value, d.pm_rationale)


def _render_thesis_status_changed(d: ThesisStatusChangedDetail) -> str:
    return f"status: {d.old_status} → {d.new_status}"


def _render_halt_activated(d: HaltActivatedDetail) -> str:
    return (
        f"{d.halt_type} halt activated at "
        f"drawdown={d.current_drawdown_pct:.2%} "
        f"limit={d.limit_pct:.2%}"
    )


def _render_halt_lifted(d: HaltLiftedDetail) -> str:
    return f"{d.halt_type} halt lifted at drawdown={d.current_drawdown_pct:.2%}"


def _render_greeks_refresh_failed(d: GreeksRefreshFailedDetail) -> str:
    return f"greeks refresh failed: {d.occ_symbol} ({d.failure_reason})"


def _render_emergency_invocation_requested(d: EmergencyInvocationRequestedDetail) -> str:
    return f"emergency invocation requested: {d.trigger_type} — {d.trigger_reason}"


def _render_reconciliation_alert(d: ReconciliationAlertDetail) -> str:
    return (
        f"{d.domain} {d.field_name} mismatch: "
        f"local={d.local_value} vs alpaca={d.alpaca_value} "
        f"— {d.delta_description}"
    )


def _render_distillation_config_change(d: DistillationConfigChangeDetail) -> str:
    if not d.changes:
        return f"config reloaded with no key changes (new_hash={d.new_hash[:8]})"
    return "; ".join(
        _render_field_change(c.key_path, c.old_value, c.new_value, None) for c in d.changes
    )


_RENDERERS: dict[type, Callable[[Any], str]] = {
    BracketModifiedDetail: _render_bracket_modified,
    OrderModifiedDetail: _render_order_modified,
    ThesisStatusChangedDetail: _render_thesis_status_changed,
    HaltActivatedDetail: _render_halt_activated,
    HaltLiftedDetail: _render_halt_lifted,
    GreeksRefreshFailedDetail: _render_greeks_refresh_failed,
    EmergencyInvocationRequestedDetail: _render_emergency_invocation_requested,
    ReconciliationAlertDetail: _render_reconciliation_alert,
    DistillationConfigChangeDetail: _render_distillation_config_change,
}
