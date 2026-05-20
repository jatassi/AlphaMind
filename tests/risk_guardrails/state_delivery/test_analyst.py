"""Tests for the analyst guardrail state header renderer (story 04a)."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
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
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystAvailableCapital,
    AnalystHeldPosition,
    AnalystView,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.state_delivery import render_analyst_header
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

# ---------------------------------------------------------------------------
# Fixture builders
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


def _make_active_parameters(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    parameter_change_flag: bool = False,
    per_position_max_pct: float = 5.0,
) -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=parameter_change_flag,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="per_position_max_size",
                rule_label="Per-position max size",
                value=per_position_max_pct,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=per_position_max_pct,
            ),
        ),
        active_overlays=(),
    )


def _make_state_delivery_config(*, abandoned_lookback: int = 1) -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=abandoned_lookback,
    )


def _make_available_capital(
    *,
    available_usd: float = 300_000.0,
    available_pct: float = 60.0,
    per_position_usd: float = 25_000.0,
    per_position_pct: float = 5.0,
) -> AnalystAvailableCapital:
    return AnalystAvailableCapital(
        available_for_new_positions_usd=available_usd,
        available_for_new_positions_pct=available_pct,
        per_position_max_size_usd=per_position_usd,
        per_position_max_size_pct=per_position_pct,
    )


def _make_held_position(
    *,
    position_id: str,
    ticker: str,
    direction: Direction | None = Direction.LONG,
    sector: str = "tech",
    size_pct: float = 4.2,
    instrument_type: InstrumentType = InstrumentType.EQUITY,
    strategy_type_label: str | None = None,
) -> AnalystHeldPosition:
    return AnalystHeldPosition(
        position_id=position_id,
        ticker=ticker,
        direction=direction,
        sector=sector,
        size_pct=size_pct,
        instrument_type=instrument_type,
        strategy_type_label=strategy_type_label,
    )


def _make_abandoned_opening(
    *,
    envelope_id: str = "ENV-REC-1",
    direction: Direction = Direction.LONG,
    ticker: str = "MSFT",
    instrument_type: InstrumentType = InstrumentType.EQUITY,
    size_pct: float = 3.0,
    abandoned_at: datetime | None = None,
    failure_reason: str = "broker rejected: stale price",
) -> AnalystAbandonedOpening:
    if abandoned_at is None:
        abandoned_at = datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC)
    return AnalystAbandonedOpening(
        envelope_id=envelope_id,
        direction=direction,
        ticker=ticker,
        instrument_type=instrument_type,
        size_pct=size_pct,
        abandoned_at=abandoned_at,
        failure_reason=failure_reason,
    )


def _make_analyst_view(
    *,
    held_positions: tuple[AnalystHeldPosition, ...] = (),
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
    available_capital: AnalystAvailableCapital | None = None,
) -> AnalystView:
    return AnalystView(
        held_positions=held_positions,
        active_thesis_summaries=(),
        available_capital=available_capital or _make_available_capital(),
        pending_orders=(),
        abandoned_openings=abandoned_openings,
    )


def _micro_risk_budget(
    *,
    tech_zone: RiskZone = RiskZone.NORMAL,
    semis_zone: RiskZone = RiskZone.CRITICAL,
) -> RiskBudgetConsumption:
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=18.3,
                limit_value=25.0,
                zone=tech_zone,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=24.2,
                limit_value=25.0,
                zone=semis_zone,
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


def _full_system_risk_budget() -> RiskBudgetConsumption:
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
                current_value=12.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_financials",
                rule_label="Financials sector concentration",
                current_value=8.5,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_energy",
                rule_label="Energy sector concentration",
                current_value=4.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=42.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="net_short_pct",
                rule_label="Net short exposure",
                current_value=10.0,
                limit_value=30.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=78.0,
                limit_value=120.0,
            ),
            _make_budget_entry(
                rule_id="options_delta_pct",
                rule_label="Options delta exposure",
                current_value=22.0,
                limit_value=40.0,
            ),
            _make_budget_entry(
                rule_id="portfolio_theta_pct_per_day",
                rule_label="Portfolio theta",
                current_value=0.10,
                limit_value=0.18,
                unit="% of portfolio per day",
            ),
            _make_budget_entry(
                rule_id="portfolio_vega_pct_per_iv_point",
                rule_label="Portfolio vega",
                current_value=0.6,
                limit_value=1.0,
                unit="% of portfolio per vol point",
            ),
        )
    )


_MICRO_SECTOR_LABELS = {"tech": "Tech", "semis": "Semis"}
_FULL_SECTOR_LABELS = {
    "tech": "Tech",
    "semis": "Semis",
    "financials": "Financials",
    "energy": "Energy",
}


# ---------------------------------------------------------------------------
# Tracer bullet
# ---------------------------------------------------------------------------


def test_render_analyst_header_returns_string_starting_with_envelope_open() -> None:
    view = _make_analyst_view()
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert isinstance(rendered, str)
    assert rendered.startswith("=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===")
    assert rendered.endswith("===")


def test_render_analyst_header_renders_regime_line_after_envelope() -> None:
    view = _make_analyst_view()
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(regime_label=RegimeLabel.NORMAL),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    lines = rendered.splitlines()
    assert lines[1] == "Regime: normal [unchanged]"


def test_render_analyst_header_micro_profile_full_fixture() -> None:
    held_positions = (
        _make_held_position(
            position_id=PositionId("POS-NVDA-001"),
            ticker=Symbol("NVDA"),
            sector="tech",
            size_pct=4.2,
        ),
        _make_held_position(
            position_id=PositionId("POS-MU-002"), ticker=Symbol("MU"), sector="semis", size_pct=2.5
        ),
        _make_held_position(
            position_id=PositionId("POS-AMD-003"), ticker=Symbol("AMD"), sector="tech", size_pct=2.1
        ),
        _make_held_position(
            position_id=PositionId("POS-AVGO-004"),
            ticker=Symbol("AVGO"),
            sector="semis",
            size_pct=3.0,
        ),
        _make_held_position(
            position_id=PositionId("POS-AAPL-005"),
            ticker=Symbol("AAPL"),
            sector="tech",
            size_pct=3.5,
        ),
    )
    abandoned = (
        _make_abandoned_opening(
            envelope_id="ENV-REC-1",
            direction=Direction.LONG,
            ticker=Symbol("MSFT"),
            instrument_type=InstrumentType.EQUITY,
            size_pct=3.0,
            abandoned_at=datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC),
            failure_reason="broker rejected: stale price",
        ),
    )
    view = _make_analyst_view(
        held_positions=held_positions,
        abandoned_openings=abandoned,
    )
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===",
            "Regime: normal [unchanged]",
            "",
            "Capital:",
            "  Available for new positions: $300,000 (60.0% of portfolio)",
            "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:  18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis: 24.2% / 25.0% — room: 0.8% [\U0001f534 CRITICAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Held positions (dedup — skip same underlying + direction; "
            "strategist owns hold/add/reduce):",
            "  NVDA  long    4.2%  Tech",
            "  MU    long    2.5%  Semis",
            "  AMD   long    2.1%  Tech",
            "  AVGO  long    3.0%  Semis",
            "  AAPL  long    3.5%  Tech",
            "",
            "Abandoned openings from prior invocation "
            "(decide on current grounds whether to re-propose):",
            "  ENV-REC-1  long MSFT equity  3.0%  — "
            "abandoned at 2026-04-28T13:30:00Z (broker rejected: stale price)",
            "",
            "Hard blocks (do NOT recommend):",
            "  Semis sector concentration at 24.2% / 25.0% limit",
            "  Options: DISABLED for this portfolio",
            "  Short selling: DISABLED for this portfolio",
            "===",
        ]
    )
    assert rendered == expected


def _full_system_held_positions() -> tuple[AnalystHeldPosition, ...]:
    spec = (
        ("POS-NVDA-001", "NVDA", Direction.LONG, "tech", 4.2),
        ("POS-AAPL-002", "AAPL", Direction.LONG, "tech", 3.5),
        ("POS-AMD-003", "AMD", Direction.SHORT, "tech", 1.5),
        ("POS-AVGO-004", "AVGO", Direction.LONG, "semis", 3.0),
        ("POS-MU-005", "MU", Direction.LONG, "semis", 2.5),
        ("POS-TSM-006", "TSM", Direction.SHORT, "semis", 1.0),
        ("POS-JPM-007", "JPM", Direction.LONG, "financials", 3.8),
        ("POS-GS-008", "GS", Direction.LONG, "financials", 2.2),
        ("POS-BAC-009", "BAC", Direction.SHORT, "financials", 1.5),
        ("POS-XOM-010", "XOM", Direction.LONG, "energy", 2.5),
        ("POS-CVX-011", "CVX", Direction.LONG, "energy", 2.0),
        ("POS-SLB-012", "SLB", Direction.SHORT, "energy", 0.8),
    )
    return tuple(
        _make_held_position(
            position_id=position_id,
            ticker=ticker,
            direction=direction,
            sector=sector,
            size_pct=size_pct,
        )
        for position_id, ticker, direction, sector, size_pct in spec
    )


def test_render_analyst_header_full_system_profile_full_fixture() -> None:
    view = _make_analyst_view(
        held_positions=_full_system_held_positions(),
        abandoned_openings=(),
    )
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_full_system_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-002, 2026-04-28T14:32:05Z) ===",
            "Regime: normal [unchanged]",
            "",
            "Capital:",
            "  Available for new positions: $300,000 (60.0% of portfolio)",
            "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:       18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis:      12.0% / 25.0% — room: 13.0% [NORMAL]",
            "  Financials: 8.5% / 25.0% — room: 16.5% [NORMAL]",
            "  Energy:     4.0% / 25.0% — room: 21.0% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Net short: 10.0% / 30.0% — room: 20.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Options headroom:",
            "  Delta exposure: 22.0% / 40.0% — room: 18.0%",
            "  Theta:          0.1% / 0.2%/day",
            "  Vega:           0.6% / 1.0%/pt",
            "",
            "Held positions (dedup — skip same underlying + direction; "
            "strategist owns hold/add/reduce):",
            "  NVDA  long    4.2%  Tech",
            "  AAPL  long    3.5%  Tech",
            "  AMD   short   1.5%  Tech",
            "  AVGO  long    3.0%  Semis",
            "  MU    long    2.5%  Semis",
            "  TSM   short   1.0%  Semis",
            "  JPM   long    3.8%  Financials",
            "  GS    long    2.2%  Financials",
            "  BAC   short   1.5%  Financials",
            "  XOM   long    2.5%  Energy",
            "  CVX   long    2.0%  Energy",
            "  SLB   short   0.8%  Energy",
            "",
            "Abandoned openings from prior invocation "
            "(decide on current grounds whether to re-propose):",
            "  None",
            "===",
        ]
    )
    assert rendered == expected


def test_render_analyst_header_omits_hard_blocks_block_when_no_breaches_and_features_enabled() -> (
    None
):
    view = _make_analyst_view(held_positions=_full_system_held_positions())
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_full_system_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
    )
    assert "Hard blocks" not in rendered
    lines = rendered.splitlines()
    # Closing === must be immediately preceded by a non-blank line.
    assert lines[-1] == "==="
    assert lines[-2] != ""


def test_render_analyst_header_has_no_double_blank_lines() -> None:
    view = _make_analyst_view(
        held_positions=_full_system_held_positions(),
        abandoned_openings=(),
    )
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_full_system_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
    )
    lines = rendered.splitlines()
    for prev_line, next_line in pairwise(lines):
        assert not (prev_line == "" and next_line == ""), "double blank line found"


def test_render_analyst_header_has_no_trailing_blank_line() -> None:
    view = _make_analyst_view()
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert not rendered.endswith("\n")
    assert rendered.splitlines()[-1] == "==="


def test_render_analyst_header_is_deterministic() -> None:
    view = _make_analyst_view(
        held_positions=_full_system_held_positions(),
        abandoned_openings=(),
    )
    risk_budget = _full_system_risk_budget()
    active = _make_active_parameters()
    timestamp = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
    config = _make_state_delivery_config()
    first = render_analyst_header(
        analyst_view=view,
        risk_budget=risk_budget,
        active_risk_parameters=active,
        invocation_id="inv-002",
        timestamp=timestamp,
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=config,
        sector_label_display=_FULL_SECTOR_LABELS,
    )
    second = render_analyst_header(
        analyst_view=view,
        risk_budget=risk_budget,
        active_risk_parameters=active,
        invocation_id="inv-002",
        timestamp=timestamp,
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=config,
        sector_label_display=_FULL_SECTOR_LABELS,
    )
    assert first == second


def test_render_analyst_header_renders_none_for_empty_held_positions() -> None:
    view = _make_analyst_view(held_positions=())
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(tech_zone=RiskZone.NORMAL, semis_zone=RiskZone.NORMAL),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    lines = rendered.splitlines()
    held_header_idx = lines.index(
        "Held positions (dedup — skip same underlying + direction; "
        "strategist owns hold/add/reduce):"
    )
    assert lines[held_header_idx + 1] == "  None"


def test_render_analyst_header_raises_when_options_disabled_but_options_rule_present() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=(
            *_micro_risk_budget().entries,
            _make_budget_entry(
                rule_id="options_delta_pct",
                rule_label="Options delta exposure",
                current_value=22.0,
                limit_value=40.0,
            ),
        )
    )
    view = _make_analyst_view()
    with pytest.raises(ValueError, match="options_delta_pct"):
        render_analyst_header(
            analyst_view=view,
            risk_budget=risk_budget,
            active_risk_parameters=_make_active_parameters(),
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
        )


def test_render_analyst_header_raises_when_short_selling_disabled_but_net_short_present() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=(
            *_micro_risk_budget().entries,
            _make_budget_entry(
                rule_id="net_short_pct",
                rule_label="Net short exposure",
                current_value=10.0,
                limit_value=30.0,
            ),
        )
    )
    view = _make_analyst_view()
    with pytest.raises(ValueError, match="net_short_pct"):
        render_analyst_header(
            analyst_view=view,
            risk_budget=risk_budget,
            active_risk_parameters=_make_active_parameters(),
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
        )


def test_render_analyst_header_raises_when_net_long_pct_missing() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=tuple(
            entry for entry in _micro_risk_budget().entries if entry.rule_id != "net_long_pct"
        )
    )
    view = _make_analyst_view()
    with pytest.raises(ValueError, match="net_long_pct"):
        render_analyst_header(
            analyst_view=view,
            risk_budget=risk_budget,
            active_risk_parameters=_make_active_parameters(),
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
        )


def test_render_analyst_header_raises_when_gross_exposure_pct_missing() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=tuple(
            entry for entry in _micro_risk_budget().entries if entry.rule_id != "gross_exposure_pct"
        )
    )
    view = _make_analyst_view()
    with pytest.raises(ValueError, match="gross_exposure_pct"):
        render_analyst_header(
            analyst_view=view,
            risk_budget=risk_budget,
            active_risk_parameters=_make_active_parameters(),
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
        )


def test_render_analyst_header_raises_when_active_sector_missing_from_risk_budget() -> None:
    view = _make_analyst_view()
    with pytest.raises(ValueError, match="sector_concentration_financials"):
        render_analyst_header(
            analyst_view=view,
            risk_budget=_micro_risk_budget(),
            active_risk_parameters=_make_active_parameters(),
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis", "financials"),
            config=_make_state_delivery_config(),
            sector_label_display={"tech": "Tech", "semis": "Semis", "financials": "Financials"},
        )


def test_render_analyst_header_emits_sector_rows_in_active_sectors_order() -> None:
    # active_sectors order is (semis, tech) — opposite of risk_budget.entries order
    # and not alphabetical.
    view = _make_analyst_view()
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(tech_zone=RiskZone.NORMAL, semis_zone=RiskZone.NORMAL),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("semis", "tech"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    lines = rendered.splitlines()
    sector_header_idx = lines.index("Sector headroom (delta-adjusted):")
    assert lines[sector_header_idx + 1].lstrip().startswith("Semis:")
    assert lines[sector_header_idx + 2].lstrip().startswith("Tech:")


def test_render_analyst_header_renders_none_for_empty_abandoned_openings() -> None:
    view = _make_analyst_view(abandoned_openings=())
    rendered = render_analyst_header(
        analyst_view=view,
        risk_budget=_micro_risk_budget(tech_zone=RiskZone.NORMAL, semis_zone=RiskZone.NORMAL),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    lines = rendered.splitlines()
    abandoned_header_idx = lines.index(
        "Abandoned openings from prior invocation "
        "(decide on current grounds whether to re-propose):"
    )
    assert lines[abandoned_header_idx + 1] == "  None"
