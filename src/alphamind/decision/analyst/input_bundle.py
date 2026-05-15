"""Input-bundle assembler for the analyst agent — story 06 (ALP-297).

Composes the user-message text the analyst harness (story 07) sends to the
LLM: the ``=== GUARDRAIL STATE ===`` header (rendered via the state-delivery
layer's normal-mode or halt-mode renderer), a brief tool-reminder block, and
the synthesizer brief verbatim. Pure function — no I/O, no logging,
deterministic.

The user-turn order matches the analyst prompt's ``<inputs>`` block: header
first, then brief.

See ``docs/design/04-decision-layer/analyst.md`` § Inputs and
``prompts/decision/analyst.md`` for the source-order contract.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.consumers.analyst import AnalystView
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.state_delivery import (
    render_analyst_header,
    render_analyst_header_halt_mode,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

__all__ = ["assemble_input_bundle_halt", "assemble_input_bundle_normal"]


def assemble_input_bundle_normal(  # noqa: PLR0913 — mirrors render_analyst_header's signature
    *,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
) -> str:
    """Compose the analyst's user-message text for a normal-mode invocation.

    Renders the guardrail header via :func:`render_analyst_header`, then a
    brief tool-reminder section, then the synthesizer brief verbatim.
    """
    header = render_analyst_header(
        analyst_view=analyst_view,
        risk_budget=risk_budget,
        active_risk_parameters=active_risk_parameters,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=state_delivery_config,
        sector_label_display=sector_label_display,
    )
    tool_reminder = _render_tool_reminder(tool_names, halt_mode=False)
    return f"{header}\n\n{tool_reminder}\n\n=== SYNTHESIZER BRIEF ===\n{synthesizer_brief_text}"


def assemble_input_bundle_halt(  # noqa: PLR0913 — mirrors render_analyst_header_halt_mode's signature
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
    state_delivery_config: StateDeliveryConfig,
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
) -> str:
    """Compose the analyst's user-message text for a halt-mode invocation.

    Renders the halt-mode guardrail header via
    :func:`render_analyst_header_halt_mode`, notes that watchlist mode is in
    effect (no ``validate_guardrail`` use), then the synthesizer brief
    verbatim.
    """
    header = render_analyst_header_halt_mode(
        halt_state=halt_state,
        analyst_view=analyst_view,
        risk_budget=risk_budget,
        active_risk_parameters=active_risk_parameters,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        config=state_delivery_config,
        sector_label_display=sector_label_display,
    )
    tool_reminder = _render_tool_reminder(tool_names, halt_mode=True)
    return f"{header}\n\n{tool_reminder}\n\n=== SYNTHESIZER BRIEF ===\n{synthesizer_brief_text}"


# ---------------------------------------------------------------------------
# Tool-reminder section
# ---------------------------------------------------------------------------


_HALT_MODE_TOOL_NOTE = (
    "Watchlist mode active — do not call validate_guardrail "
    "(no proposals are emitted in halt mode)."
)


def _render_tool_reminder(tool_names: tuple[str, ...], *, halt_mode: bool) -> str:
    """Render the ``=== AVAILABLE TOOLS ===`` block.

    One bullet per tool name. Tool names already include the
    ``mcp__<server>__<name>`` prefix; the assembler does not strip or rewrite
    them. In halt mode the same tool list is rendered with a watchlist-mode
    note appended so the agent sees the canonical surface but is reminded
    that ``validate_guardrail`` is not used.
    """
    lines: list[str] = ["=== AVAILABLE TOOLS ==="]
    lines.extend(f"- {name}" for name in tool_names)
    if halt_mode:
        lines.append(_HALT_MODE_TOOL_NOTE)
    return "\n".join(lines)
