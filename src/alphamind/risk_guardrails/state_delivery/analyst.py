"""Analyst guardrail state header renderer (story 04a)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystHeldPosition,
    AnalystView,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RegimeLabel,
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
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

_SECTOR_RULE_PREFIX = "sector_concentration_"
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

_DIRECTION_DISPLAY: dict[Direction, str] = {
    Direction.LONG: "long",
    Direction.SHORT: "short",
}

_INSTRUMENT_TYPE_DISPLAY: dict[InstrumentType, str] = {
    InstrumentType.EQUITY: "equity",
    InstrumentType.OPTIONS: "option",
    InstrumentType.STRATEGY: "strategy",
}

_HELD_POSITIONS_HEADER = (
    "Held positions (dedup — skip same underlying + direction; strategist owns hold/add/reduce):"
)
_ABANDONED_OPENINGS_HEADER = (
    "Abandoned openings from prior invocation (decide on current grounds whether to re-propose):"
)
_NONE_LINE = "  None"
_DIRECTION_COLUMN_WIDTH = len("short")


def render_analyst_header(  # noqa: PLR0913 — signature dictated by story 04a AC #2
    *,
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
    """Render the analyst's complete ``=== GUARDRAIL STATE ===`` header.

    The ``config`` argument carries operator-tunable knobs the projection layer
    enforces upstream (the abandoned-window lookback filter is applied to
    ``analyst_view.abandoned_openings`` before reaching this renderer).
    """
    del config
    _validate_feature_flag_closure(
        risk_budget=risk_budget,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    sector_entries = _resolve_sector_entries(risk_budget, active_sectors)
    sector_label_resolver = _make_sector_label_resolver(sector_label_display)
    capital = analyst_view.available_capital
    regime_display = _REGIME_LABEL_DISPLAY[active_risk_parameters.regime_label]

    blocks: list[str] = [
        "\n".join(
            [
                render_envelope_open(invocation_id, timestamp),
                render_regime_line(active_risk_parameters),
            ]
        ),
        render_capital_block(
            available_for_new_positions_usd=capital.available_for_new_positions_usd,
            available_for_new_positions_pct=capital.available_for_new_positions_pct,
            per_position_max_usd=capital.per_position_max_size_usd,
            per_position_max_pct=capital.per_position_max_size_pct,
            regime_label_display=regime_display,
        ),
        render_sector_headroom_block(sector_entries, sector_label_resolver=sector_label_resolver),
        render_directional_headroom_block(
            net_long=_require_entry(risk_budget, _NET_LONG_RULE_ID),
            net_short=(
                risk_budget.entry_by_rule_id(_NET_SHORT_RULE_ID) if short_selling_enabled else None
            ),
            gross=_require_entry(risk_budget, _GROSS_RULE_ID),
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


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


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


def _require_entry(risk_budget: RiskBudgetConsumption, rule_id: str) -> RiskBudgetEntry:
    entry = risk_budget.entry_by_rule_id(rule_id)
    if entry is None:
        msg = f"risk_budget missing required entry for rule_id {rule_id!r}"
        raise ValueError(msg)
    return entry


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


# ---------------------------------------------------------------------------
# Held-positions and abandoned-openings helpers (analyst-only, stay private)
# ---------------------------------------------------------------------------


def _render_held_positions_block(
    positions: tuple[AnalystHeldPosition, ...],
    sector_label_resolver: Callable[[str], str],
) -> str:
    rows: list[str] = [_HELD_POSITIONS_HEADER]
    if not positions:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    ticker_width = max(len(p.ticker) for p in positions)
    for position in positions:
        ticker = position.ticker.ljust(ticker_width)
        direction = _DIRECTION_DISPLAY[position.direction].ljust(_DIRECTION_COLUMN_WIDTH)
        size = f"{format_pct(position.size_pct)}%".rjust(5)
        sector_label = sector_label_resolver(f"{_SECTOR_RULE_PREFIX}{position.sector}")
        rows.append(f"  {ticker}  {direction}  {size}  {sector_label}")
    return "\n".join(rows)


def _render_abandoned_openings_block(
    entries: tuple[AnalystAbandonedOpening, ...],
) -> str:
    rows: list[str] = [_ABANDONED_OPENINGS_HEADER]
    if not entries:
        rows.append(_NONE_LINE)
        return "\n".join(rows)
    for entry in entries:
        ts = entry.abandoned_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        size = f"{format_pct(entry.size_pct)}%"
        direction = _DIRECTION_DISPLAY[entry.direction]
        asset_type = _INSTRUMENT_TYPE_DISPLAY[entry.instrument_type]
        rows.append(
            f"  {entry.envelope_id}  {direction} {entry.ticker} "
            f"{asset_type}  {size}  — abandoned at {ts} ({entry.failure_reason})"
        )
    return "\n".join(rows)
