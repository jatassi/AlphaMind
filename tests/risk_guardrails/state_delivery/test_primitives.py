"""Tests for the shared rendering primitives (story 03)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetEntry
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.state_delivery.primitives import (
    format_dollar,
    format_pct,
    render_capital_block,
    render_directional_headroom_block,
    render_envelope_close,
    render_envelope_open,
    render_hard_blocks_block,
    render_options_headroom_block,
    render_position_proximity_block,
    render_regime_line,
    render_sector_headroom_block,
    render_zone_tag,
)


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


def _make_active_parameter_set(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    parameter_change_flag: bool = False,
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
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )


def test_format_dollar_renders_positive_with_comma_separator() -> None:
    assert format_dollar(1_500_000) == "$1,500,000"


def test_format_dollar_renders_zero() -> None:
    assert format_dollar(0) == "$0"


def test_format_dollar_renders_negative_with_leading_dash() -> None:
    assert format_dollar(-1_500) == "-$1,500"


def test_format_dollar_is_deterministic() -> None:
    assert format_dollar(42_000) == format_dollar(42_000)


def test_format_pct_renders_one_decimal_place() -> None:
    assert format_pct(60.0) == "60.0"


def test_format_pct_renders_zero() -> None:
    assert format_pct(0) == "0.0"


def test_format_pct_renders_negative_with_one_decimal_place() -> None:
    assert format_pct(-1.5) == "-1.5"


def test_format_pct_renders_large_value() -> None:
    assert format_pct(1234.5678) == "1234.6"


def test_format_pct_is_deterministic() -> None:
    assert format_pct(18.345) == format_pct(18.345)


def test_envelope_open_renders_iso8601_utc() -> None:
    ts = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
    rendered = render_envelope_open("inv-001", ts)
    assert rendered == "=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ==="


def test_envelope_open_passes_through_invocation_id_verbatim() -> None:
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    rendered = render_envelope_open("complex_id-with-suffix.42", ts)
    assert "complex_id-with-suffix.42" in rendered


def test_envelope_open_rejects_naive_datetime() -> None:
    naive = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError):
        render_envelope_open("inv-001", naive)


def test_envelope_open_rejects_non_utc_tzinfo() -> None:
    eastern = datetime(2026, 4, 28, 14, 32, 5, tzinfo=timezone(timedelta(hours=-5)))
    with pytest.raises(ValueError):
        render_envelope_open("inv-001", eastern)


def test_envelope_open_is_deterministic() -> None:
    ts = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
    assert render_envelope_open("inv-001", ts) == render_envelope_open("inv-001", ts)


def test_envelope_close_returns_literal() -> None:
    assert render_envelope_close() == "==="


def test_envelope_close_is_deterministic() -> None:
    assert render_envelope_close() == render_envelope_close()


@pytest.mark.parametrize(
    "zone,expected",
    [
        (RiskZone.NORMAL, "NORMAL"),
        (RiskZone.WARNING, "⚠ WARNING"),
        (RiskZone.CRITICAL, "\U0001f534 CRITICAL"),
        (RiskZone.BLOCKED, "BLOCKED"),
    ],
)
def test_zone_tag_renders_documented_token(zone: RiskZone, expected: str) -> None:
    assert render_zone_tag(zone) == expected


@pytest.mark.parametrize("zone", list(RiskZone))
def test_zone_tag_returns_non_empty_for_every_zone(zone: RiskZone) -> None:
    assert render_zone_tag(zone)


def test_zone_tag_emoji_codepoints_round_trip_utf8() -> None:
    warning = render_zone_tag(RiskZone.WARNING)
    critical = render_zone_tag(RiskZone.CRITICAL)
    assert warning.encode("utf-8").decode("utf-8") == warning
    assert critical.encode("utf-8").decode("utf-8") == critical
    assert "⚠" in warning
    assert "\U0001f534" in critical


def test_zone_tag_is_deterministic() -> None:
    assert render_zone_tag(RiskZone.WARNING) == render_zone_tag(RiskZone.WARNING)


@pytest.mark.parametrize(
    "regime_label,expected_label",
    [
        (RegimeLabel.LOW_VOL, "low-vol compression"),
        (RegimeLabel.NORMAL, "normal"),
        (RegimeLabel.ELEVATED, "elevated"),
        (RegimeLabel.CRISIS, "crisis"),
    ],
)
def test_regime_line_renders_unchanged(regime_label: RegimeLabel, expected_label: str) -> None:
    active = _make_active_parameter_set(regime_label=regime_label, parameter_change_flag=False)
    assert render_regime_line(active) == f"Regime: {expected_label} [unchanged]"


@pytest.mark.parametrize("regime_label", list(RegimeLabel))
def test_regime_line_renders_changed(regime_label: RegimeLabel) -> None:
    active = _make_active_parameter_set(regime_label=regime_label, parameter_change_flag=True)
    rendered = render_regime_line(active)
    assert rendered.startswith("Regime: ")
    assert rendered.endswith("[CHANGED since last invocation]")


@pytest.mark.parametrize("regime_label", list(RegimeLabel))
def test_regime_line_display_string_non_empty_for_every_label(
    regime_label: RegimeLabel,
) -> None:
    active = _make_active_parameter_set(regime_label=regime_label)
    rendered = render_regime_line(active)
    label_segment = rendered.removeprefix("Regime: ").split(" [", maxsplit=1)[0]
    assert label_segment


def test_regime_line_is_deterministic() -> None:
    active = _make_active_parameter_set(
        regime_label=RegimeLabel.ELEVATED, parameter_change_flag=True
    )
    assert render_regime_line(active) == render_regime_line(active)


def test_capital_block_renders_documented_three_lines() -> None:
    rendered = render_capital_block(
        available_for_new_positions_usd=300_000,
        available_for_new_positions_pct=60.0,
        per_position_max_usd=25_000,
        per_position_max_pct=5.0,
        regime_label_display="normal",
    )
    assert rendered.splitlines() == [
        "Capital:",
        "  Available for new positions: $300,000 (60.0% of portfolio)",
        "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)",
    ]


def test_capital_block_renders_large_dollars_with_comma_separator() -> None:
    rendered = render_capital_block(
        available_for_new_positions_usd=1_500_000,
        available_for_new_positions_pct=75.5,
        per_position_max_usd=120_000,
        per_position_max_pct=6.0,
        regime_label_display="low-vol compression",
    )
    assert "$1,500,000" in rendered
    assert "$120,000" in rendered
    assert "low-vol compression regime" in rendered


def test_capital_block_renders_zero_values_cleanly() -> None:
    rendered = render_capital_block(
        available_for_new_positions_usd=0,
        available_for_new_positions_pct=0,
        per_position_max_usd=0,
        per_position_max_pct=0,
        regime_label_display="crisis",
    )
    assert "$0 (0.0% of portfolio)" in rendered
    assert "$0 (0.0% of portfolio, crisis regime)" in rendered


def test_capital_block_is_deterministic() -> None:
    first = render_capital_block(
        available_for_new_positions_usd=300_000,
        available_for_new_positions_pct=60.0,
        per_position_max_usd=25_000,
        per_position_max_pct=5.0,
        regime_label_display="normal",
    )
    second = render_capital_block(
        available_for_new_positions_usd=300_000,
        available_for_new_positions_pct=60.0,
        per_position_max_usd=25_000,
        per_position_max_pct=5.0,
        regime_label_display="normal",
    )
    assert first == second


def test_directional_headroom_block_renders_all_three_rows() -> None:
    net_long = _make_budget_entry(
        rule_id="net_long_exposure",
        rule_label="Net long exposure",
        current_value=42.0,
        limit_value=60.0,
    )
    net_short = _make_budget_entry(
        rule_id="net_short_exposure",
        rule_label="Net short exposure",
        current_value=10.0,
        limit_value=30.0,
    )
    gross = _make_budget_entry(
        rule_id="gross_exposure",
        rule_label="Gross exposure",
        current_value=78.0,
        limit_value=120.0,
    )
    rendered = render_directional_headroom_block(
        net_long=net_long, net_short=net_short, gross=gross
    )
    assert rendered.splitlines() == [
        "Directional headroom:",
        "  Net long:  42.0% / 60.0% — room: 18.0%",
        "  Net short: 10.0% / 30.0% — room: 20.0%",
        "  Gross:     78.0% / 120.0% — room: 42.0%",
    ]


def test_directional_headroom_block_omits_net_short_when_none() -> None:
    net_long = _make_budget_entry(
        rule_id="net_long_exposure",
        rule_label="Net long exposure",
        current_value=42.0,
        limit_value=60.0,
    )
    gross = _make_budget_entry(
        rule_id="gross_exposure",
        rule_label="Gross exposure",
        current_value=78.0,
        limit_value=120.0,
    )
    rendered = render_directional_headroom_block(net_long=net_long, net_short=None, gross=gross)
    assert rendered.splitlines() == [
        "Directional headroom:",
        "  Net long:  42.0% / 60.0% — room: 18.0%",
        "  Gross:     78.0% / 120.0% — room: 42.0%",
    ]


def _sector_label(rule_id: str) -> str:
    return rule_id.removeprefix("sector_concentration_").capitalize()


def test_sector_headroom_block_renders_documented_rows() -> None:
    entries = (
        _make_budget_entry(
            rule_id="sector_concentration_tech",
            rule_label="Tech sector concentration",
            current_value=18.3,
            limit_value=25.0,
            zone=RiskZone.NORMAL,
        ),
        _make_budget_entry(
            rule_id="sector_concentration_semis",
            rule_label="Semis sector concentration",
            current_value=24.2,
            limit_value=25.0,
            zone=RiskZone.CRITICAL,
        ),
        _make_budget_entry(
            rule_id="sector_concentration_financials",
            rule_label="Financials sector concentration",
            current_value=12.5,
            limit_value=25.0,
            zone=RiskZone.NORMAL,
        ),
    )
    rendered = render_sector_headroom_block(entries, sector_label_resolver=_sector_label)
    assert rendered.splitlines() == [
        "Sector headroom (delta-adjusted):",
        "  Tech:       18.3% / 25.0% — room: 6.7% [NORMAL]",
        "  Semis:      24.2% / 25.0% — room: 0.8% [\U0001f534 CRITICAL]",
        "  Financials: 12.5% / 25.0% — room: 12.5% [NORMAL]",
    ]


def test_sector_headroom_block_renders_warning_zone_with_emoji() -> None:
    entries = (
        _make_budget_entry(
            rule_id="sector_concentration_tech",
            rule_label="Tech sector concentration",
            current_value=22.0,
            limit_value=25.0,
            zone=RiskZone.WARNING,
        ),
    )
    rendered = render_sector_headroom_block(entries, sector_label_resolver=_sector_label)
    assert "⚠ WARNING" in rendered


def test_sector_headroom_block_pads_label_to_longest_in_input() -> None:
    entries = (
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
    )
    rendered = render_sector_headroom_block(entries, sector_label_resolver=_sector_label)
    lines = rendered.splitlines()
    # Both rows have their values aligned: position of the first digit must match.
    line_tech = lines[1]
    line_semis = lines[2]
    assert line_tech.index("18.3") == line_semis.index("12.0")


def test_sector_headroom_block_emits_rows_in_input_order() -> None:
    entries = (
        _make_budget_entry(
            rule_id="sector_concentration_zeta",
            rule_label="Zeta",
            current_value=1.0,
            limit_value=10.0,
        ),
        _make_budget_entry(
            rule_id="sector_concentration_alpha",
            rule_label="Alpha",
            current_value=2.0,
            limit_value=10.0,
        ),
    )
    rendered = render_sector_headroom_block(entries, sector_label_resolver=_sector_label)
    lines = rendered.splitlines()
    assert "Zeta" in lines[1]
    assert "Alpha" in lines[2]


def test_sector_headroom_block_renders_none_line_on_empty() -> None:
    rendered = render_sector_headroom_block((), sector_label_resolver=_sector_label)
    assert rendered.splitlines() == [
        "Sector headroom (delta-adjusted):",
        "  (none)",
    ]


def test_sector_headroom_block_is_deterministic() -> None:
    entries = (
        _make_budget_entry(
            rule_id="sector_concentration_tech",
            rule_label="Tech sector concentration",
            current_value=18.3,
            limit_value=25.0,
        ),
    )
    first = render_sector_headroom_block(entries, sector_label_resolver=_sector_label)
    second = render_sector_headroom_block(entries, sector_label_resolver=_sector_label)
    assert first == second


def test_options_headroom_block_renders_three_rows_when_all_present() -> None:
    delta = _make_budget_entry(
        rule_id="options_delta_exposure",
        rule_label="Options delta exposure",
        current_value=22.0,
        limit_value=40.0,
    )
    theta = _make_budget_entry(
        rule_id="portfolio_theta",
        rule_label="Portfolio theta",
        current_value=0.10,
        limit_value=0.18,
        unit="% of portfolio per day",
    )
    vega = _make_budget_entry(
        rule_id="portfolio_vega",
        rule_label="Portfolio vega",
        current_value=0.6,
        limit_value=1.0,
        unit="% of portfolio per vol point",
    )
    rendered = render_options_headroom_block(delta=delta, theta=theta, vega=vega)
    assert rendered is not None
    assert rendered.splitlines() == [
        "Options headroom:",
        "  Delta exposure: 22.0% / 40.0% — room: 18.0%",
        "  Theta:          0.1% / 0.2%/day",
        "  Vega:           0.6% / 1.0%/pt",
    ]


def test_options_headroom_block_returns_none_when_all_disabled() -> None:
    assert render_options_headroom_block(delta=None, theta=None, vega=None) is None


def test_options_headroom_block_raises_on_partial_none() -> None:
    delta = _make_budget_entry(
        rule_id="options_delta_exposure",
        rule_label="Options delta exposure",
        current_value=22.0,
        limit_value=40.0,
    )
    theta = _make_budget_entry(
        rule_id="portfolio_theta",
        rule_label="Portfolio theta",
        current_value=0.10,
        limit_value=0.15,
    )
    with pytest.raises(ValueError):
        render_options_headroom_block(delta=delta, theta=theta, vega=None)
    with pytest.raises(ValueError):
        render_options_headroom_block(delta=None, theta=theta, vega=None)
    with pytest.raises(ValueError):
        render_options_headroom_block(delta=delta, theta=None, vega=None)


def test_options_headroom_block_is_deterministic() -> None:
    delta = _make_budget_entry(
        rule_id="options_delta_exposure",
        rule_label="Options delta exposure",
        current_value=22.0,
        limit_value=40.0,
    )
    theta = _make_budget_entry(
        rule_id="portfolio_theta",
        rule_label="Portfolio theta",
        current_value=0.10,
        limit_value=0.15,
    )
    vega = _make_budget_entry(
        rule_id="portfolio_vega",
        rule_label="Portfolio vega",
        current_value=0.6,
        limit_value=1.0,
    )
    first = render_options_headroom_block(delta=delta, theta=theta, vega=vega)
    second = render_options_headroom_block(delta=delta, theta=theta, vega=vega)
    assert first == second


def test_hard_blocks_returns_none_when_no_breaches_and_both_flags_enabled() -> None:
    assert (
        render_hard_blocks_block(
            breaching_entries=(),
            options_enabled=True,
            short_selling_enabled=True,
        )
        is None
    )


def test_hard_blocks_renders_breaching_entries_with_default_header() -> None:
    semis = _make_budget_entry(
        rule_id="sector_concentration_semis",
        rule_label="Semis sector",
        current_value=24.2,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    rendered = render_hard_blocks_block(
        breaching_entries=(semis,),
        options_enabled=True,
        short_selling_enabled=True,
    )
    assert rendered is not None
    assert rendered.splitlines() == [
        "Hard blocks (do NOT recommend):",
        "  Semis sector at 24.2% / 25.0% limit",
    ]


def test_hard_blocks_appends_per_rule_guidance_suffix() -> None:
    semis = _make_budget_entry(
        rule_id="sector_concentration_semis",
        rule_label="Semis sector",
        current_value=24.2,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    rendered = render_hard_blocks_block(
        breaching_entries=(semis,),
        options_enabled=True,
        short_selling_enabled=True,
        per_rule_guidance={"sector_concentration_semis": "no new semi longs"},
    )
    assert rendered is not None
    assert rendered.splitlines() == [
        "Hard blocks (do NOT recommend):",
        "  Semis sector at 24.2% / 25.0% limit — no new semi longs",
    ]


def test_hard_blocks_emits_disabled_options_line() -> None:
    rendered = render_hard_blocks_block(
        breaching_entries=(),
        options_enabled=False,
        short_selling_enabled=True,
    )
    assert rendered is not None
    assert rendered.splitlines() == [
        "Hard blocks (do NOT recommend):",
        "  Options: DISABLED for this portfolio",
    ]


def test_hard_blocks_emits_disabled_shorts_line() -> None:
    rendered = render_hard_blocks_block(
        breaching_entries=(),
        options_enabled=True,
        short_selling_enabled=False,
    )
    assert rendered is not None
    assert rendered.splitlines() == [
        "Hard blocks (do NOT recommend):",
        "  Short selling: DISABLED for this portfolio",
    ]


def test_hard_blocks_emits_per_rule_lines_in_input_order() -> None:
    first = _make_budget_entry(
        rule_id="sector_concentration_zeta",
        rule_label="Zeta sector",
        current_value=24.0,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    second = _make_budget_entry(
        rule_id="sector_concentration_alpha",
        rule_label="Alpha sector",
        current_value=24.5,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    rendered = render_hard_blocks_block(
        breaching_entries=(first, second),
        options_enabled=True,
        short_selling_enabled=True,
    )
    assert rendered is not None
    lines = rendered.splitlines()
    assert "Zeta sector" in lines[1]
    assert "Alpha sector" in lines[2]


def test_hard_blocks_uses_pm_header_label_when_overridden() -> None:
    semis = _make_budget_entry(
        rule_id="sector_concentration_semis",
        rule_label="Semis sector",
        current_value=24.2,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    rendered = render_hard_blocks_block(
        breaching_entries=(semis,),
        options_enabled=True,
        short_selling_enabled=True,
        header_label="Hard blocks (do NOT issue commands violating):",
    )
    assert rendered is not None
    assert rendered.splitlines()[0] == "Hard blocks (do NOT issue commands violating):"


def test_hard_blocks_combines_breaching_entries_and_disabled_lines() -> None:
    semis = _make_budget_entry(
        rule_id="sector_concentration_semis",
        rule_label="Semis sector",
        current_value=24.2,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    rendered = render_hard_blocks_block(
        breaching_entries=(semis,),
        options_enabled=False,
        short_selling_enabled=False,
    )
    assert rendered is not None
    assert rendered.splitlines() == [
        "Hard blocks (do NOT recommend):",
        "  Semis sector at 24.2% / 25.0% limit",
        "  Options: DISABLED for this portfolio",
        "  Short selling: DISABLED for this portfolio",
    ]


def test_hard_blocks_is_deterministic() -> None:
    semis = _make_budget_entry(
        rule_id="sector_concentration_semis",
        rule_label="Semis sector",
        current_value=24.2,
        limit_value=25.0,
        zone=RiskZone.BLOCKED,
    )
    first = render_hard_blocks_block(
        breaching_entries=(semis,),
        options_enabled=False,
        short_selling_enabled=True,
    )
    second = render_hard_blocks_block(
        breaching_entries=(semis,),
        options_enabled=False,
        short_selling_enabled=True,
    )
    assert first == second


def test_directional_headroom_block_is_deterministic() -> None:
    net_long = _make_budget_entry(
        rule_id="net_long_exposure",
        rule_label="Net long exposure",
        current_value=42.0,
        limit_value=60.0,
    )
    gross = _make_budget_entry(
        rule_id="gross_exposure",
        rule_label="Gross exposure",
        current_value=78.0,
        limit_value=120.0,
    )
    first = render_directional_headroom_block(net_long=net_long, net_short=None, gross=gross)
    second = render_directional_headroom_block(net_long=net_long, net_short=None, gross=gross)
    assert first == second


# ---------------------------------------------------------------------------
# Position proximity block — loss-zone signed comparison
# ---------------------------------------------------------------------------


def _make_strategist_position_view(
    *,
    position_id: str,
    position_weight_pct: float,
    unrealized_pnl_pct: float,
) -> StrategistPositionView:
    equity = EquityPositionDetails(
        ticker=Symbol("AAA"),
        share_count=100.0,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=None,
        locate_status=None,
        margin_held_usd=None,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 4, 1, 14, 0, 0, tzinfo=UTC),
        details=equity,
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 4, 1, 14, 0, 0, tzinfo=UTC),
                fill_price=price(100.0),
                fill_quantity=100.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    view = PositionView(
        record=record,
        current_market_value_usd=signed_money(10_000.0),
        unrealized_pnl_usd=signed_money(unrealized_pnl_pct * 100.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=position_weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(10_000.0),
        delta_adjusted_exposure_usd=signed_money(10_000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )
    return StrategistPositionView(
        position=view,
        thesis=None,
        bracket=None,
        pending_orders=(),
        modification_trail=(),
    )


def _make_active_parameters_for_proximity(
    *,
    per_position_max_pct: float = 5.0,
    max_loss_equity_pct: float = 80.0,
) -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Per-position max size",
                value=per_position_max_pct,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=per_position_max_pct,
            ),
            ActiveRiskParameterEntry(
                rule_id="position_max_loss_equity_pct",
                rule_label="Equity max loss",
                value=max_loss_equity_pct,
                unit="% of cost",
                regime_multiplier_applied=1.0,
                base_value=max_loss_equity_pct,
            ),
        ),
        active_overlays=(),
    )


def test_position_proximity_positive_pnl_exceeding_max_loss_magnitude_not_critical() -> None:
    """ALP-550 AC#1: P/L=+19900%, max_loss=-80%, small size → no zone tag."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=2.0,
        unrealized_pnl_pct=19_900.0,
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "CRITICAL" not in rendered
    assert "WARNING" not in rendered
    assert "BLOCKED" not in rendered


def test_position_proximity_loss_breach_flags_critical_or_blocked() -> None:
    """ALP-550 AC#2: P/L=-85%, max_loss=-80%, small size → loss-driven CRITICAL tag."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=2.0,
        unrealized_pnl_pct=-85.0,
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "[\U0001f534 CRITICAL: loss]" in rendered


def test_position_proximity_loss_approaching_max_loss_flags_warning() -> None:
    """Loss progress in [0.70, 0.85) of max_loss → WARNING (signed comparison)."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=2.0,
        unrealized_pnl_pct=-60.0,  # 60/80 = 0.75 of max_loss
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "[⚠ WARNING: loss]" in rendered
    assert "CRITICAL" not in rendered


def test_position_proximity_zero_pnl_with_max_loss_is_normal() -> None:
    """P/L=0, well-under-size position → no zone tag emitted."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=2.0,
        unrealized_pnl_pct=0.0,
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "CRITICAL" not in rendered
    assert "WARNING" not in rendered
    assert "BLOCKED" not in rendered


def test_position_proximity_size_proximity_still_drives_tag_when_loss_normal() -> None:
    """Existing size-based zone behavior preserved when loss zone is NORMAL."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=4.5,  # 0.90 of 5.0 size cap → CRITICAL by size
        unrealized_pnl_pct=10.0,
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "[\U0001f534 CRITICAL: size]" in rendered


def test_position_proximity_takes_max_severity_when_both_zones_fire() -> None:
    """When size zone WARNING and loss zone CRITICAL, displayed tag is CRITICAL."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=4.0,  # 0.80 of 5.0 → WARNING by size
        unrealized_pnl_pct=-72.0,  # 72/80 = 0.90 → CRITICAL by loss
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "[\U0001f534 CRITICAL: loss]" in rendered
    assert "WARNING" not in rendered


def test_position_proximity_debug_pos_07_critical_is_size_not_loss() -> None:
    """ALP-580 regression: debug-pos-07 (weight 4.5%, P/L +19900%, max_loss -80%).

    The CRITICAL flag must be attributed to size proximity (4.5% / 5.0% = 90%),
    NOT to the +19900% P/L — a large *gain* is the opposite of a max-loss breach
    and its loss zone is NORMAL under the PR #96 signed comparison.
    """
    pos = _make_strategist_position_view(
        position_id="debug-pos-07",
        position_weight_pct=4.5,
        unrealized_pnl_pct=19_900.0,
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "[\U0001f534 CRITICAL: size]" in rendered
    assert "[\U0001f534 CRITICAL: loss]" not in rendered
    assert "[\U0001f534 CRITICAL: size+loss]" not in rendered


def test_position_proximity_size_and_loss_both_critical_names_both_sources() -> None:
    """When size and loss zones are both CRITICAL, the tag names size+loss."""
    pos = _make_strategist_position_view(
        position_id="POS-001",
        position_weight_pct=4.5,  # 0.90 of 5.0 → CRITICAL by size
        unrealized_pnl_pct=-72.0,  # 72/80 = 0.90 → CRITICAL by loss
    )
    rendered = render_position_proximity_block(
        positions=(pos,),
        active_risk_parameters=_make_active_parameters_for_proximity(),
    )
    assert "[\U0001f534 CRITICAL: size+loss]" in rendered
