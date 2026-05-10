"""Tests for the analyst input-bundle assembler — story 06 (ALP-297)."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.decision.analyst.input_bundle import (
    assemble_input_bundle_halt,
    assemble_input_bundle_normal,
)
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystAvailableCapital,
    AnalystHeldPosition,
    AnalystView,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskZone,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

# ---------------------------------------------------------------------------
# Constants — canonical analyst tool names (story 04 + synthesizer story 06a)
# ---------------------------------------------------------------------------

_VALIDATE_GUARDRAIL_TOOL = "mcp__alphamind_decision_validation__validate_guardrail"
_RETRIEVE_BRIEF_TOOL = "mcp__alphamind_synthesizer_retrieval__retrieve_brief"
_TOOL_NAMES: tuple[str, ...] = (_VALIDATE_GUARDRAIL_TOOL, _RETRIEVE_BRIEF_TOOL)

_SYNTHESIZER_BRIEF = (
    "## Cross-domain market snapshot\n\n"
    "Tech tape mixed [SA-TECH-3]; financials grinding higher [SA-FIN-1]; "
    "energy quiet ahead of inventory print [SA-ENERGY-2]."
)

_TIMESTAMP = datetime(2026, 5, 4, 14, 32, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture builders (lifted/adapted from tests/risk_guardrails/state_delivery)
# ---------------------------------------------------------------------------


def _make_budget_entry(
    *,
    rule_id: str,
    rule_label: str,
    current_value: float,
    limit_value: float,
    zone: RiskZone = RiskZone.NORMAL,
    unit: str = "% of portfolio",
) -> RiskBudgetEntry:
    headroom = limit_value - current_value
    headroom_pct = max(0.0, min(100.0, (headroom / limit_value) * 100.0)) if limit_value else 0.0
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=headroom_pct,
        zone=zone,
        unit=unit,
        cumulative_invocation_impact_value=0.0,
    )


def _make_active_parameters() -> ActiveRiskParameterSet:
    from tests.decision.conftest import compose_active_risk_parameters_via_orchestrator

    return compose_active_risk_parameters_via_orchestrator(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="per_position_max_size",
                rule_label="Per-position max size",
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )


def _make_state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )


def _make_available_capital() -> AnalystAvailableCapital:
    return AnalystAvailableCapital(
        available_for_new_positions_usd=300_000.0,
        available_for_new_positions_pct=60.0,
        per_position_max_size_usd=25_000.0,
        per_position_max_size_pct=5.0,
    )


def _make_held_position(
    *,
    position_id: str = "POS-NVDA-001",
    ticker: str = "NVDA",
    direction: Direction = Direction.LONG,
    sector: str = "tech",
    size_pct: float = 4.2,
) -> AnalystHeldPosition:
    return AnalystHeldPosition(
        position_id=position_id,
        ticker=ticker,
        direction=direction,
        sector=sector,
        size_pct=size_pct,
        instrument_type=InstrumentType.EQUITY,
    )


def _make_abandoned_opening() -> AnalystAbandonedOpening:
    return AnalystAbandonedOpening(
        envelope_id="ENV-REC-1",
        direction=Direction.LONG,
        ticker="MSFT",
        instrument_type=InstrumentType.EQUITY,
        size_pct=3.0,
        abandoned_at=datetime(2026, 5, 3, 13, 30, 0, tzinfo=UTC),
        failure_reason="broker rejected: stale price",
    )


def _make_analyst_view(
    *,
    held_positions: tuple[AnalystHeldPosition, ...] = (),
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
) -> AnalystView:
    return AnalystView(
        held_positions=held_positions,
        active_thesis_summaries=(),
        available_capital=_make_available_capital(),
        pending_orders=(),
        abandoned_openings=abandoned_openings,
    )


def _micro_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=18.3,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=12.1,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=42.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=78.0,
                limit_value=120.0,
            ),
        )
    )


_MICRO_SECTOR_LABELS = {
    "sector_concentration_tech": "Tech",
    "sector_concentration_semis": "Semis",
}


def _make_halt_state() -> HaltState:
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.5,
        daily_drawdown_limit_pct=2.5,
    )


def _normal_kwargs(
    *,
    held_positions: tuple[AnalystHeldPosition, ...] = (),
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
) -> dict[str, object]:
    return {
        "analyst_view": _make_analyst_view(
            held_positions=held_positions,
            abandoned_openings=abandoned_openings,
        ),
        "risk_budget": _micro_risk_budget(),
        "active_risk_parameters": _make_active_parameters(),
        "invocation_id": "inv-001",
        "timestamp": _TIMESTAMP,
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": ("tech", "semis"),
        "state_delivery_config": _make_state_delivery_config(),
        "synthesizer_brief_text": _SYNTHESIZER_BRIEF,
        "tool_names": _TOOL_NAMES,
    }


# ---------------------------------------------------------------------------
# Tracer bullet — normal-mode bundle starts with envelope and ends with brief
# ---------------------------------------------------------------------------


def test_normal_mode_bundle_starts_with_envelope_and_ends_with_brief() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert out.startswith("=== GUARDRAIL STATE (invocation inv-001, 2026-05-04T14:32:05Z) ===")
    assert out.rstrip().endswith(_SYNTHESIZER_BRIEF.rstrip())


# ---------------------------------------------------------------------------
# Section ordering — header → tools → brief, matching prompt <inputs> contract
# ---------------------------------------------------------------------------


def test_normal_mode_section_ordering() -> None:
    """Header marker comes first, AVAILABLE TOOLS in the middle, SYNTHESIZER BRIEF last."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    envelope_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF ===")
    assert envelope_idx < tools_idx < brief_idx


# ---------------------------------------------------------------------------
# Tool-name interpolation — caller-supplied tool_names render as bullets
# ---------------------------------------------------------------------------


def test_normal_mode_tool_names_render_one_bullet_each() -> None:
    """Every name in ``tool_names`` appears as a ``- <name>`` bullet — verbatim."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    for name in _TOOL_NAMES:
        assert f"- {name}" in out


def test_normal_mode_tool_names_arbitrary_set() -> None:
    """Tool-reminder section reflects whatever names the caller supplies — no rewriting."""
    custom = ("mcp__server_a__tool_x", "mcp__server_b__tool_y", "mcp__server_c__tool_z")
    kwargs = _normal_kwargs()
    kwargs["tool_names"] = custom
    out = assemble_input_bundle_normal(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    for name in custom:
        assert f"- {name}" in out


# ---------------------------------------------------------------------------
# Held-positions and abandoned-openings — empty + populated
# ---------------------------------------------------------------------------


def test_normal_mode_empty_held_positions_renders_none_line() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(held_positions=()),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert "Held positions" in out
    held_idx = out.index("Held positions")
    # The "  None" placeholder appears immediately after the held-positions header.
    after_header = out[held_idx:]
    assert "\n  None" in after_header.split("Abandoned openings")[0]


def test_normal_mode_populated_held_positions_appear() -> None:
    held = (
        _make_held_position(position_id="POS-NVDA-001", ticker="NVDA"),
        _make_held_position(
            position_id="POS-MU-002",
            ticker="MU",
            sector="semis",
            size_pct=2.5,
        ),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(held_positions=held),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert "NVDA" in out
    assert "MU" in out
    # The "  None" placeholder for held positions is absent (positions exist).
    held_block = out.split("Held positions")[1].split("Abandoned openings")[0]
    assert "  None" not in held_block


def test_normal_mode_empty_abandoned_openings_renders_none_line() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(abandoned_openings=()),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    abandoned_idx = out.index("Abandoned openings from prior invocation")
    after_header = out[abandoned_idx:]
    # Section is followed by "  None" before the next double-newline-separated block.
    section = after_header.split("\n\n")[0]
    assert "  None" in section


def test_normal_mode_populated_abandoned_openings_appear() -> None:
    abandoned = (_make_abandoned_opening(),)
    out = assemble_input_bundle_normal(
        **_normal_kwargs(abandoned_openings=abandoned),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert "ENV-REC-1" in out
    assert "MSFT" in out
    assert "broker rejected: stale price" in out


# ---------------------------------------------------------------------------
# Brief is appended verbatim
# ---------------------------------------------------------------------------


def test_synthesizer_brief_appears_verbatim() -> None:
    """The brief text is included in full, byte-for-byte."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert _SYNTHESIZER_BRIEF in out


# ---------------------------------------------------------------------------
# Determinism — identical inputs produce identical output
# ---------------------------------------------------------------------------


def test_normal_mode_deterministic() -> None:
    kwargs = _normal_kwargs()
    out_a = assemble_input_bundle_normal(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    out_b = assemble_input_bundle_normal(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert out_a == out_b


# ---------------------------------------------------------------------------
# Halt-mode round-trip — banner present, watchlist note in tool block,
# brief still appended
# ---------------------------------------------------------------------------


def _halt_kwargs(
    *,
    held_positions: tuple[AnalystHeldPosition, ...] = (),
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
) -> dict[str, object]:
    kwargs = _normal_kwargs(
        held_positions=held_positions,
        abandoned_openings=abandoned_openings,
    )
    kwargs["halt_state"] = _make_halt_state()
    return kwargs


def test_halt_mode_bundle_includes_envelope_and_banner() -> None:
    out = assemble_input_bundle_halt(
        **_halt_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert out.startswith("=== GUARDRAIL STATE (invocation inv-001, 2026-05-04T14:32:05Z) ===")
    assert "** HALT MODE ACTIVE — daily drawdown 2.5% / 2.5% **" in out
    assert "Mode: WATCHLIST ONLY — do not generate trade proposals" in out


def test_halt_mode_bundle_notes_watchlist_in_tool_section() -> None:
    """The tool-reminder section names the canonical tool list and tells the agent
    that ``validate_guardrail`` is not used in watchlist mode."""
    out = assemble_input_bundle_halt(
        **_halt_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF ===")
    tool_section = out[tools_idx:brief_idx]
    assert "watchlist" in tool_section.lower()
    assert "validate_guardrail" in tool_section


def test_halt_mode_bundle_appends_brief() -> None:
    out = assemble_input_bundle_halt(
        **_halt_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert out.rstrip().endswith(_SYNTHESIZER_BRIEF.rstrip())


def test_halt_mode_section_ordering() -> None:
    out = assemble_input_bundle_halt(
        **_halt_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    envelope_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF ===")
    assert envelope_idx < tools_idx < brief_idx


def test_halt_mode_empty_held_positions_renders_none_line() -> None:
    out = assemble_input_bundle_halt(
        **_halt_kwargs(held_positions=()),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    held_idx = out.index("Held positions")
    after_header = out[held_idx:]
    assert "\n  None" in after_header.split("Abandoned openings")[0]


def test_halt_mode_populated_abandoned_openings_appear() -> None:
    out = assemble_input_bundle_halt(
        **_halt_kwargs(abandoned_openings=(_make_abandoned_opening(),)),  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert "ENV-REC-1" in out
    assert "MSFT" in out


def test_halt_mode_deterministic() -> None:
    kwargs = _halt_kwargs()
    out_a = assemble_input_bundle_halt(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    out_b = assemble_input_bundle_halt(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert out_a == out_b
