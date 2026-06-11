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

from collections.abc import Mapping
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
from alphamind.state.repository.options_chain_read import TickerOptionsContext

__all__ = ["assemble_input_bundle_halt", "assemble_input_bundle_normal"]


# ---------------------------------------------------------------------------
# Section-header constants
# ---------------------------------------------------------------------------

_BRIEF_HEADER = "=== SYNTHESIZER BRIEF PREVIEW (full brief via retrieve_brief) ==="

_REFERENCE_PRICES_HEADER = "=== REFERENCE PRICES (authoritative bracket anchors) ==="

_REFERENCE_PRICES_GUIDANCE = (
    "Anchor each equity entry, target, and protective stop to its ticker's price "
    "below — the exact number your brackets are validated against."
)

_REFERENCE_PRICES_NONE = "  None (no reference prices available this invocation)"


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
    underlying_prices: Mapping[str, float],
    sector_label_display: dict[str, str] | None = None,
    options_context: Mapping[str, TickerOptionsContext] | None = None,
) -> str:
    """Compose the analyst's user-message text for a normal-mode invocation.

    Renders the guardrail header via :func:`render_analyst_header`, the
    authoritative per-ticker reference-price block (the exact ``underlying_prices``
    the ALP-742 validator enforces, so the analyst anchors brackets to the same
    number it is judged against — ALP-758), a brief tool-reminder section, then
    the synthesizer brief verbatim.

    ``options_context`` (ALP-948) decorates a ticker's reference-price line
    with its IV rank and liquid expirations when the name has a usable options
    surface; tickers absent from the map keep the bare price line.
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
    reference_prices = _render_reference_prices(underlying_prices, options_context or {})
    tool_reminder = _render_tool_reminder(tool_names, halt_mode=False)
    return (
        f"{header}\n\n{reference_prices}\n\n{tool_reminder}\n\n"
        f"{_BRIEF_HEADER}\n{synthesizer_brief_text}"
    )


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
    return f"{header}\n\n{tool_reminder}\n\n{_BRIEF_HEADER}\n{synthesizer_brief_text}"


# ---------------------------------------------------------------------------
# Reference-prices section (ALP-758)
# ---------------------------------------------------------------------------


def _render_reference_prices(
    underlying_prices: Mapping[str, float],
    options_context: Mapping[str, TickerOptionsContext],
) -> str:
    """Render the ``=== REFERENCE PRICES ===`` block.

    One ``  TICKER: price`` line per entry in ``underlying_prices`` — the exact
    map the ALP-742 bracket-coherence / staleness validator keys on
    (``underlying_prices.get(rec.underlying)``). Surfacing it inline lets the
    analyst anchor entry/target/stop to the same number it is judged against,
    closing the ALP-758 gap where the prompt asked it to price precisely against
    a reference it was never shown. Sorted by ticker for determinism; prices are
    rendered to two decimals (a percentage staleness tolerance dwarfs the
    rounding).

    A ticker present in ``options_context`` (ALP-948) extends its line to
    ``  TICKER: price | IVr NN | exp: MM-DD, MM-DD`` — the ATM-IV percentile
    rank (``n/a`` while the baseline is uncalibrated) and the expirations that
    survive the chain-slice liquidity filters. Contract detail stays behind
    the ``retrieve_options_chain`` tool.
    """
    lines: list[str] = [_REFERENCE_PRICES_HEADER, _REFERENCE_PRICES_GUIDANCE]
    if not underlying_prices:
        lines.append(_REFERENCE_PRICES_NONE)
        return "\n".join(lines)
    lines.extend(
        _render_reference_price_line(ticker, underlying_prices[ticker], options_context.get(ticker))
        for ticker in sorted(underlying_prices)
    )
    return "\n".join(lines)


def _render_reference_price_line(
    ticker: str, price: float, context: TickerOptionsContext | None
) -> str:
    """One reference-price line, options-decorated when context exists."""
    base = f"  {ticker}: {price:.2f}"
    if context is None:
        return base
    iv_rank = f"{context.iv_rank:.0f}" if context.iv_rank is not None else "n/a"
    expirations = ", ".join(d.strftime("%m-%d") for d in context.liquid_expirations)
    return f"{base} | IVr {iv_rank} | exp: {expirations}"


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
