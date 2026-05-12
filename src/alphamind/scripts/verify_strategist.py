"""Strategist end-to-end live-SDK verification (ALP-309).

Operator entry point. Reads a recorded synthesizer text from a prior
``verify_synthesizer.py`` archive (per parent-issue ALP-116 decision C —
fixture chained, not live composed), constructs three in-code
:class:`StrategistView` fixtures (normal, defensive_posture, emergency),
and runs the strategist against the real Claude Agent SDK once per
scenario. The verdict rubric per scenario classifies each invocation as
PASS / WARN / FAIL; ``--save-fixtures`` writes the parsed
:class:`StrategistOutput` payloads to
``tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json``
so downstream feature trees (proposal pre-processor, PM verifiers) can
consume them as fixtures.

The verdict rubric, the synthesizer-archive reader, and the three
fixture builders live in this module so
``tests/scripts/test_verify_strategist.py`` can exercise them without
touching the Anthropic API. The thin shim at
``scripts/verify_strategist.py`` defers to :func:`main` here.
"""

from __future__ import annotations

import argparse
import asyncio
import enum
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.decision.strategist.harness import (
    HarnessFailure,
    SDKFailure,
)
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.decision.strategist.runner import (
    StrategistResult,
    load_strategist_agent_config,
    run_strategist,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.strategist import (
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    SectorExposureEntry,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.scripts._artifact_io import load_retrieval_store, stage_artifacts_dir

__all__ = [
    "Verdict",
    "build_fixture_active_risk_parameters",
    "build_fixture_defensive_posture_view",
    "build_fixture_emergency_view",
    "build_fixture_library_config",
    "build_fixture_market_inputs",
    "build_fixture_normal_view",
    "build_fixture_portfolio_state_snapshot",
    "build_fixture_regime_transition_breach",
    "build_fixture_risk_budget",
    "build_fixture_sector_resolver",
    "build_fixture_state_delivery_config",
    "main",
    "read_retrieval_store",
    "read_synthesizer_text",
    "save_fixtures",
    "verdict_for_failure",
    "verdict_for_success",
]


_AGENT_NAME = AgentName.strategist.value


# ---------------------------------------------------------------------------
# Verdict rubric
# ---------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    """Verdict label printed in the operator report and mapped to exit code.

    PASS exits 0; WARN exits 0 (the fixture is still written, the operator
    decides whether to act on it); FAIL exits 1 (any
    :class:`HarnessFailure` subclass).
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def verdict_for_success(*, result: StrategistResult) -> Verdict:
    """Apply the per-scenario success-path rubric.

    PASS iff the validator returned ``overall == "PASS"`` with no
    warnings. WARN otherwise (a Layer-2/3 failure surfacing post-retry,
    or any validator-emitted warning).
    """
    validation = result.validation_result
    if validation.overall != "PASS" or validation.warnings:
        return Verdict.WARN
    return Verdict.PASS


def verdict_for_failure(failure: HarnessFailure) -> Verdict:
    """Apply the failure-path rubric: any HarnessFailure → FAIL."""
    del failure
    return Verdict.FAIL


# ---------------------------------------------------------------------------
# Synthesizer-archive reader
# ---------------------------------------------------------------------------


def read_synthesizer_text(
    *,
    archive_root: Path,
    synthesizer_invocation_id: str,
) -> str:
    """Read the synthesizer's recorded ``response.md`` from a prior archive.

    Path: ``<archive_root>/invocations/<inv_id>/analysis/synthesizer/response.md``.
    """
    response_path = (
        archive_root
        / INVOCATIONS_DIRNAME
        / synthesizer_invocation_id
        / "analysis"
        / "synthesizer"
        / "response.md"
    )
    try:
        return response_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"synthesizer response.md not found at {response_path}; "
            f"run scripts/verify_synthesizer.py with --archive-root {archive_root} "
            f"--invocation-id {synthesizer_invocation_id} first"
        ) from exc


def read_retrieval_store(
    *,
    archive_root: Path,
    synthesizer_invocation_id: str,
) -> RetrievalStore:
    """Read the synthesizer's recorded ``retrieval_store.json`` from a prior archive."""
    stage_dir = stage_artifacts_dir(archive_root, synthesizer_invocation_id)
    return load_retrieval_store(stage_dir)


# ---------------------------------------------------------------------------
# Constants for fixture construction
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_PORTFOLIO_VALUE = 100_000.0
_RISK_FREE_RATE = 0.045
_SPOT_DEFAULT = 100.0
_IV_DEFAULT = 0.30
_DEFAULT_OPTION_EXPIRATION = date(2026, 5, 28)


# Ticker → sector map covering the test universe. Mirrors the analyst
# verifier's universe so the fixture environments compose cleanly.
_TICKER_TO_SECTOR: dict[str, str] = {
    "NVDA": "semis",
    "MSFT": "tech",
    "GOOGL": "tech",
    "AAPL": "tech",
    "JPM": "financials",
    "XOM": "energy",
}


def build_fixture_sector_resolver() -> Callable[[str], str]:
    """Return a fixture-backed ``ticker -> sector`` callable.

    Tickers outside the small test universe map to ``"tech"`` — the
    runner's contract requires *some* sector for any ticker, and the
    fallback keeps unrecognized tickers in the active-sector set.
    """

    def _resolver(ticker: str) -> str:
        return _TICKER_TO_SECTOR.get(ticker, "tech")

    return _resolver


def _current_price_lookup(ticker: str) -> float:
    """Per-ticker current price covering the test universe."""
    prices = {
        "NVDA": 862.0,
        "MSFT": 420.0,
        "GOOGL": 175.0,
        "AAPL": 175.0,
        "JPM": 200.0,
        "XOM": 110.0,
    }
    return prices.get(ticker, 100.0)


# ---------------------------------------------------------------------------
# Position + thesis builders
# ---------------------------------------------------------------------------


def _make_equity_position(
    *,
    position_id: str,
    ticker: str,
    direction: Direction,
    share_count: float,
    avg_cost: float,
    age_hours: float = 24.0,
    weight_pct: float = 5.0,
) -> PositionView:
    """Build an OPEN equity PositionView with one prior fill."""
    fill = PositionFill(
        fill_timestamp=_AS_OF - timedelta(hours=age_hours),
        fill_price=avg_cost,
        fill_quantity=share_count,
        slippage=0.0,
        fees=1.0,
    )
    notional = share_count * _current_price_lookup(ticker)
    record = PositionRecord(
        position_id=position_id,
        thesis_id=f"THESIS-{position_id}",
        bracket_id=f"BRK-{position_id}",
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_AS_OF - timedelta(hours=age_hours),
        details=EquityPositionDetails(
            ticker=ticker,
            share_count=share_count,
            average_cost_basis_per_share=avg_cost,
        ),
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=notional,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=weight_pct,
        position_age_hours=age_hours,
        notional_exposure_usd=notional,
        delta_adjusted_exposure_usd=notional,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_thesis_record(
    *,
    position_id: str,
    summary: str,
) -> ThesisRecord:
    """Build an ACTIVE thesis with the three required component types."""
    components = (
        ThesisComponent(
            component_id=f"TC-{position_id}-1",
            thesis_id=f"THESIS-{position_id}",
            component_type=ThesisComponentType.ENTRY_RATIONALE,
            linked_bracket_leg_type=None,
            instrument_reference=position_id,
            narrative="Entry on confirmed sector momentum and constructive macro tape.",
            key_assumptions=(KeyAssumption(text="Sector momentum persists.", outcome=None),),
            generation_timestamp=_AS_OF - timedelta(hours=20),
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id=f"TC-{position_id}-2",
            thesis_id=f"THESIS-{position_id}",
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            instrument_reference=position_id,
            narrative="Target set at next resistance band; clear technical context.",
            key_assumptions=(KeyAssumption(text="Resistance band holds.", outcome=None),),
            generation_timestamp=_AS_OF - timedelta(hours=20),
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id=f"TC-{position_id}-3",
            thesis_id=f"THESIS-{position_id}",
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            instrument_reference=position_id,
            narrative="Invalidation below the swing low erodes the entry premise.",
            key_assumptions=(KeyAssumption(text="Swing low intact.", outcome=None),),
            generation_timestamp=_AS_OF - timedelta(hours=20),
            resolution_outcome=None,
            resolution_notes=None,
        ),
    )
    return ThesisRecord(
        thesis_id=f"THESIS-{position_id}",
        position_id=position_id,
        summary=summary,
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_AS_OF - timedelta(hours=20),
        time_expectation_hours=48.0,
        age_hours=20.0,
        expected_resolution_at=_AS_OF + timedelta(hours=28),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="Earnings catalyst window opens late this week.",
    )


def _make_bracket(*, position_id: str) -> BracketRecord:
    """Build a minimal ACTIVE bracket with a mechanical TAKE_PROFIT backstop."""
    legs = (
        BracketLeg(
            leg_id=f"LEG-{position_id}-TP",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=f"ORD-{position_id}-TP",
            trigger=PriceTrigger(
                underlying_ticker="AAPL",
                threshold_usd=200.0,
                direction="GTE",
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        ),
        BracketLeg(
            leg_id=f"LEG-{position_id}-PS",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=f"ORD-{position_id}-PS",
            trigger=PriceTrigger(
                underlying_ticker="AAPL",
                threshold_usd=150.0,
                direction="LTE",
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        ),
    )
    return BracketRecord(
        bracket_id=f"BRK-{position_id}",
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id=f"ORD-{position_id}-ENTRY",
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_pending_order(*, position_id: str, ticker: str) -> OrderRecord:
    """Build one pending limit order for the normal-scenario position."""
    return OrderRecord(
        order_id=f"ORD-{position_id}-PEND",
        position_id=position_id,
        bracket_id=f"BRK-{position_id}",
        role=OrderRole.ADD_ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=ticker),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(limit_price=_current_price_lookup(ticker) * 0.99),
        quantity=2.0,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id="alp-001",
        alpaca_order_id_chain=("alp-001",),
        submission_timestamp=_AS_OF - timedelta(hours=2),
        last_update_timestamp=_AS_OF - timedelta(hours=2),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=2.0,
        modification_count=0,
        originating_thesis_id=f"THESIS-{position_id}",
        originating_pm_command_id="cmd-001",
        age_hours=2.0,
    )


def _make_position_view(  # noqa: PLR0913 — fans out to several sub-builders
    *,
    position_id: str,
    ticker: str,
    direction: Direction,
    share_count: float,
    avg_cost: float,
    summary: str,
    health_status: ThesisStatus,
    prior_health_status: ThesisStatus | None = None,
    weight_pct: float = 5.0,
    pending_order: bool = False,
) -> tuple[StrategistPositionView, ThesisHealthSnapshot]:
    """Compose a per-position strategist view paired with its prior health snapshot.

    The returned snapshot represents the prior invocation's classification —
    the strategist input bundle renders ``snapshot.health_status`` as the
    ``Prior status`` line. The verify fixtures hand-roll one snapshot per
    seeded position so the strategist sees history on the first run.
    """
    position = _make_equity_position(
        position_id=position_id,
        ticker=ticker,
        direction=direction,
        share_count=share_count,
        avg_cost=avg_cost,
        weight_pct=weight_pct,
    )
    thesis = _make_thesis_record(
        position_id=position_id,
        summary=summary,
    )
    bracket = _make_bracket(position_id=position_id)
    pending: tuple[OrderRecord, ...] = ()
    if pending_order:
        pending = (_make_pending_order(position_id=position_id, ticker=ticker),)
    view = StrategistPositionView(
        position=position,
        thesis=thesis,
        bracket=bracket,
        pending_orders=pending,
        modification_trail=(),
    )
    prior_snapshot = ThesisHealthSnapshot(
        thesis_id=thesis.thesis_id,
        invocation_id=f"prior-inv-{position_id}",
        snapshot_timestamp=_AS_OF - timedelta(hours=1),
        health_status=health_status,
        prior_health_status=prior_health_status,
        component_health=(),
    )
    return view, prior_snapshot


# ---------------------------------------------------------------------------
# Risk budget + active risk parameters + helpers
# ---------------------------------------------------------------------------


def _make_budget_entry(
    *, rule_id: str, rule_label: str, current_value: float = 5.0, limit_value: float = 25.0
) -> RiskBudgetEntry:
    headroom = limit_value - current_value
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=(headroom / limit_value) * 100.0,
        zone=RiskZone.NORMAL,
        unit="% of portfolio",
        cumulative_invocation_impact_value=0.0,
    )


def build_fixture_risk_budget() -> RiskBudgetConsumption:
    """Risk-budget covering the four-way taxonomy plus net-long / gross."""
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_financials",
                rule_label="Financials sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_energy",
                rule_label="Energy sector concentration",
            ),
            _make_budget_entry(rule_id="net_long_pct", rule_label="Net long exposure"),
            _make_budget_entry(rule_id="gross_exposure_pct", rule_label="Gross exposure"),
        )
    )


def build_fixture_active_risk_parameters() -> ActiveRiskParameterSet:
    """Active risk-parameter set with the position-size + drawdown limits."""
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Per-position max size",
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown limit",
                value=2.5,
                unit="% of equity",
                regime_multiplier_applied=1.0,
                base_value=2.5,
            ),
            ActiveRiskParameterEntry(
                rule_id="cumulative_drawdown_pct",
                rule_label="Cumulative drawdown",
                value=10.0,
                unit="% of equity",
                regime_multiplier_applied=1.0,
                base_value=10.0,
            ),
        ),
        active_overlays=(),
    )


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=0.0,
        total_unrealized_pnl_pct_of_portfolio=0.0,
        daily_realized_pnl_usd=0.0,
        daily_total_pnl_usd=0.0,
        cumulative_realized_pnl_usd=0.0,
        rolling_realized_pnl={"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
        win_rate_pct=0.0,
        average_win_size_usd=0.0,
        average_loss_size_usd=0.0,
        profit_factor=0.0,
    )


def _make_drawdown(
    *, daily_pct: float = 0.0, daily_zone: RiskZone = RiskZone.NORMAL
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=daily_pct,
        equity_high_water_mark_usd=_PORTFOLIO_VALUE,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=daily_pct,
        intraday_drawdown_pct=daily_pct,
        daily_zone=daily_zone,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=20_000.0,
        total_short_delta_adjusted_usd=0.0,
        net_directional_pct_of_portfolio=20.0,
        gross_pct_of_portfolio=20.0,
    )


# ---------------------------------------------------------------------------
# Three in-code StrategistView fixture builders
# ---------------------------------------------------------------------------


def build_fixture_normal_view() -> tuple[StrategistView, tuple[ThesisHealthSnapshot, ...]]:
    """Normal scenario — 4 positions across 3 sectors, full thesis, 1 pending order."""
    paired = (
        _make_position_view(
            position_id="POS-NVDA",
            ticker="NVDA",
            direction=Direction.LONG,
            share_count=5.0,
            avg_cost=820.0,
            summary="Long NVDA on AI capex acceleration.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=4.3,
            pending_order=True,
        ),
        _make_position_view(
            position_id="POS-JPM",
            ticker="JPM",
            direction=Direction.LONG,
            share_count=15.0,
            avg_cost=180.0,
            summary="Long JPM on financials sector rotation.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=3.0,
        ),
        _make_position_view(
            position_id="POS-XOM",
            ticker="XOM",
            direction=Direction.LONG,
            share_count=20.0,
            avg_cost=105.0,
            summary="Long XOM on energy capex normalization.",
            health_status=ThesisStatus.PARTIALLY_REALIZED,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=2.2,
        ),
        _make_position_view(
            position_id="POS-AAPL",
            ticker="AAPL",
            direction=Direction.LONG,
            share_count=10.0,
            avg_cost=170.0,
            summary="Long AAPL on services-margin expansion.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=1.75,
        ),
    )
    positions = tuple(view for view, _snap in paired)
    snapshots = tuple(snap for _view, snap in paired)
    sector_exposure = _make_sector_exposure(
        {"semis": 4.3, "financials": 3.0, "energy": 2.2, "tech": 1.75}
    )
    view = StrategistView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(),
        sector_exposure=sector_exposure,
        directional_exposure=_make_directional(),
        risk_budget=build_fixture_risk_budget(),
        active_risk_parameters=build_fixture_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
    )
    return view, snapshots


def build_fixture_defensive_posture_view() -> tuple[
    StrategistView, tuple[ThesisHealthSnapshot, ...]
]:
    """Defensive-posture scenario — 6 positions; daily drawdown at the halt threshold.

    Activity log includes a recent engine-originated CLOSE on a sector-correlated
    position so the strategist's ``engine_originated_closure_signal`` discipline
    is exercised.
    """
    paired = (
        _make_position_view(
            position_id="POS-NVDA",
            ticker="NVDA",
            direction=Direction.LONG,
            share_count=5.0,
            avg_cost=820.0,
            summary="Long NVDA on AI capex acceleration.",
            health_status=ThesisStatus.AT_RISK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=4.3,
        ),
        _make_position_view(
            position_id="POS-MSFT",
            ticker="MSFT",
            direction=Direction.LONG,
            share_count=8.0,
            avg_cost=410.0,
            summary="Long MSFT on cloud upside.",
            health_status=ThesisStatus.AT_RISK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=3.4,
        ),
        _make_position_view(
            position_id="POS-GOOGL",
            ticker="GOOGL",
            direction=Direction.LONG,
            share_count=12.0,
            avg_cost=170.0,
            summary="Long GOOGL on search resilience.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=2.1,
        ),
        _make_position_view(
            position_id="POS-JPM",
            ticker="JPM",
            direction=Direction.LONG,
            share_count=15.0,
            avg_cost=180.0,
            summary="Long JPM on financials sector rotation.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=3.0,
        ),
        _make_position_view(
            position_id="POS-XOM",
            ticker="XOM",
            direction=Direction.LONG,
            share_count=20.0,
            avg_cost=105.0,
            summary="Long XOM on energy capex normalization.",
            health_status=ThesisStatus.PARTIALLY_REALIZED,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=2.2,
        ),
        _make_position_view(
            position_id="POS-AAPL",
            ticker="AAPL",
            direction=Direction.LONG,
            share_count=10.0,
            avg_cost=170.0,
            summary="Long AAPL on services-margin expansion.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=1.75,
        ),
    )
    positions = tuple(view for view, _snap in paired)
    snapshots = tuple(snap for _view, snap in paired)
    sector_exposure = _make_sector_exposure(
        {"semis": 4.3, "tech": 7.25, "financials": 3.0, "energy": 2.2}
    )

    # An engine-originated CLOSE on a sector-correlated position. The
    # strategist's defensive-posture renderer surfaces the
    # ``engine_originated_closure_signal`` line when this entry is present.
    engine_close_entry = ActivityLogEntry(
        entry_id="LOG-CLOSE-1",
        invocation_id="inv-strategist-defensive",
        timestamp=_AS_OF - timedelta(minutes=15),
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id="POS-MSFT",
        order_id=None,
        thesis_id="THESIS-POS-MSFT",
        source=EventSource.GUARDRAIL_LAYER,
        detail=PositionClosedDetail(
            exit_method=PositionExitMethod.PM_DECISION,
            exit_price=400.0,
            realized_pnl_usd=-80.0,
            thesis_resolution_category="INVALIDATED_STOPPED_CORRECTLY",
        ),
    )
    view = StrategistView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(daily_pct=2.5, daily_zone=RiskZone.BLOCKED),
        sector_exposure=sector_exposure,
        directional_exposure=_make_directional(),
        risk_budget=build_fixture_risk_budget(),
        active_risk_parameters=build_fixture_active_risk_parameters(),
        intra_invocation_changelog=(engine_close_entry,),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
    )
    return view, snapshots


def build_fixture_emergency_view() -> tuple[StrategistView, tuple[ThesisHealthSnapshot, ...]]:
    """Emergency-invocation scenario — 4 positions, no halt; one position has
    a regime-transition breach (sized 4.5%, new regime limit 3.5%, overage 1.0%).

    The breach is exposed via :func:`build_fixture_regime_transition_breach`
    and threaded into ``run_strategist`` as ``regime_transition_breaches``.
    """
    paired = (
        _make_position_view(
            position_id="POS-NVDA",
            ticker="NVDA",
            direction=Direction.LONG,
            share_count=5.0,
            avg_cost=820.0,
            summary="Long NVDA on AI capex acceleration.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            # Sized at the breaching weight.
            weight_pct=4.5,
        ),
        _make_position_view(
            position_id="POS-JPM",
            ticker="JPM",
            direction=Direction.LONG,
            share_count=15.0,
            avg_cost=180.0,
            summary="Long JPM on financials sector rotation.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=3.0,
        ),
        _make_position_view(
            position_id="POS-XOM",
            ticker="XOM",
            direction=Direction.LONG,
            share_count=20.0,
            avg_cost=105.0,
            summary="Long XOM on energy capex normalization.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=2.2,
        ),
        _make_position_view(
            position_id="POS-AAPL",
            ticker="AAPL",
            direction=Direction.LONG,
            share_count=10.0,
            avg_cost=170.0,
            summary="Long AAPL on services-margin expansion.",
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=ThesisStatus.ON_TRACK,
            weight_pct=1.75,
        ),
    )
    positions = tuple(view for view, _snap in paired)
    snapshots = tuple(snap for _view, snap in paired)
    sector_exposure = _make_sector_exposure(
        {"semis": 4.5, "financials": 3.0, "energy": 2.2, "tech": 1.75}
    )
    view = StrategistView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(),
        sector_exposure=sector_exposure,
        directional_exposure=_make_directional(),
        risk_budget=build_fixture_risk_budget(),
        active_risk_parameters=build_fixture_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
    )
    return view, snapshots


def build_fixture_regime_transition_breach() -> RegimeTransitionBreach:
    """Single regime-transition breach for the emergency scenario.

    The NVDA position is sized 4.5% but the new regime tightens
    ``position_max_size_pct`` to 3.5%; overage is 1.0 percentage points.
    """
    return RegimeTransitionBreach(
        position_id="POS-NVDA",
        rule_id="position_max_size_pct",
        rule_label="Per-position max size",
        current_value=4.5,
        new_limit_value=3.5,
        overage=1.0,
        unit="% of portfolio",
    )


def _make_sector_exposure(weights_by_sector: dict[str, float]) -> tuple[SectorExposureEntry, ...]:
    return tuple(
        SectorExposureEntry(
            sector=sector,
            long_delta_adjusted_usd=pct * _PORTFOLIO_VALUE / 100.0,
            short_delta_adjusted_usd=0.0,
            long_pct_of_portfolio=pct,
            short_pct_of_portfolio=0.0,
            long_short_ratio=None,
        )
        for sector, pct in weights_by_sector.items()
    )


# ---------------------------------------------------------------------------
# Library / market / portfolio-state-snapshot fixtures (validation-state inputs)
# ---------------------------------------------------------------------------


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def build_fixture_library_config() -> LibraryConfig:
    """Library config matching ``config/profiles/medium.yaml`` shape."""
    effective_limits = {
        "position_max_size_pct": 5.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 30.0,
        "gross_exposure_pct": 120.0,
        "options_delta_pct": 40.0,
        "portfolio_theta_pct_per_day": 0.15,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 3.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 30.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def build_fixture_market_inputs() -> MarketInputs:
    """Market-inputs fixture covering the test universe."""
    underlyings = tuple(_TICKER_TO_SECTOR)
    surface = {
        ticker: IvSurfaceEntry(
            underlying=ticker,
            quotes=(
                IvQuote(
                    strike=_SPOT_DEFAULT,
                    expiration=_DEFAULT_OPTION_EXPIRATION,
                    contract_type=ContractType.CALL,
                    implied_volatility=_IV_DEFAULT,
                ),
                IvQuote(
                    strike=_SPOT_DEFAULT,
                    expiration=_DEFAULT_OPTION_EXPIRATION,
                    contract_type=ContractType.PUT,
                    implied_volatility=_IV_DEFAULT,
                ),
            ),
        )
        for ticker in underlyings
    }
    return MarketInputs(
        underlying_prices=MappingProxyType(
            {ticker: _current_price_lookup(ticker) for ticker in underlyings}
        ),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=FixtureIvProvider(surface=surface, realized_vol={}),
        as_of=_AS_OF,
    )


def build_fixture_portfolio_state_snapshot() -> PortfolioStateSnapshot:
    """Portfolio-state snapshot with low utilization across all sectors."""
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 8.0, "semis": 6.0, "financials": 3.0, "energy": 3.0}
        ),
        net_long_pct=20.0,
        net_short_pct=0.0,
        gross_pct=20.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def build_fixture_state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )


def build_fixture_halt_state() -> HaltState:
    """Halt state matching the defensive_posture scenario's drawdown."""
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.5,
        daily_drawdown_limit_pct=2.5,
    )


# ---------------------------------------------------------------------------
# Fixture saving
# ---------------------------------------------------------------------------


_SCENARIOS: tuple[str, ...] = ("normal", "defensive_posture", "emergency")


def save_fixtures(
    *,
    outputs: dict[str, StrategistOutput],
    fixtures_dir: Path,
) -> None:
    """Write each scenario's parsed StrategistOutput to ``<fixtures_dir>/<scenario>.json``."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    for scenario, output in outputs.items():
        (fixtures_dir / f"{scenario}.json").write_text(
            output.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Operator-report rendering
# ---------------------------------------------------------------------------


_BANNER = "=== Strategist live-SDK verification ==="


def _render_success_report(
    *,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    result: StrategistResult,
    verdict: Verdict,
    scenario: str,
) -> str:
    output = result.output
    validation = result.validation_result
    failure_render = (
        ", ".join(f"{f.field_path}::{f.rule}" for f in validation.failures)
        if validation.failures
        else "(none)"
    )
    return (
        "\n".join(
            [
                _BANNER,
                f"scenario: {scenario}",
                f"invocation_id: {invocation_id}",
                f"model: {agent_config.model.value}",
                (
                    f"tokens: input={result.tokens_used.input_tokens} "
                    f"output={result.tokens_used.output_tokens} "
                    f"cache_read={result.tokens_used.cache_read_tokens} "
                    f"cache_write={result.tokens_used.cache_write_tokens}"
                ),
                f"attempts: {result.metadata.get('attempts')}",
                f"mode: {output.mode}",
                f"position_assessments: {len(output.position_assessments)}",
                f"pending_order_assessments: {len(output.pending_order_assessments)}",
                f"validation_overall: {validation.overall}",
                f"validation_failures: {len(validation.failures)} ({failure_render})",
                f"--- Verdict: {verdict.value} ---",
            ]
        )
        + "\n"
    )


def _render_failure_report(
    *,
    scenario: str,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    failure: HarnessFailure,
    verdict: Verdict,
) -> str:
    return (
        "\n".join(
            [
                _BANNER,
                f"scenario: {scenario}",
                f"invocation_id: {invocation_id}",
                f"model: {agent_config.model.value}",
                "",
                "--- Harness failure ---",
                f"type: {type(failure).__name__}",
                f"agent_name: {failure.agent_name}",
                f"invocation_id: {failure.invocation_id}",
                f"message: {failure}",
                "",
                f"--- Verdict: {verdict.value} ---",
            ]
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------


def _check_oauth_token_set(invocation_id: str) -> None:
    """Surface missing CLAUDE_CODE_OAUTH_TOKEN as a clean SDKFailure."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise SDKFailure(
            "CLAUDE_CODE_OAUTH_TOKEN is not set in the environment. The "
            "real-SDK strategist verification cannot run without it. "
            "Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run.",
            agent_name=_AGENT_NAME,
            invocation_id=invocation_id,
        )


# ---------------------------------------------------------------------------
# CLI scenario dispatch
# ---------------------------------------------------------------------------


def _format_invocation_id(now: datetime, scenario: str) -> str:
    """``YYYYMMDDTHHMMSSZ-verify-strategist-<scenario>``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-verify-strategist-{scenario}"


_ScenarioMode = Literal["normal", "defensive_posture"]


def _scenario_mode(scenario: str) -> _ScenarioMode:
    """The runner mode for a given scenario name. ``emergency`` runs in ``normal`` mode."""
    if scenario == "defensive_posture":
        return "defensive_posture"
    return "normal"


def _scenario_view(
    scenario: str,
) -> tuple[StrategistView, tuple[ThesisHealthSnapshot, ...]]:
    """Dispatch to the right fixture builder for the scenario."""
    if scenario == "normal":
        return build_fixture_normal_view()
    if scenario == "defensive_posture":
        return build_fixture_defensive_posture_view()
    if scenario == "emergency":
        return build_fixture_emergency_view()
    raise ValueError(f"unknown scenario: {scenario!r}")


def _scenario_breaches(scenario: str) -> tuple[RegimeTransitionBreach, ...]:
    """Regime-transition breaches threaded into the runner per scenario."""
    if scenario == "emergency":
        return (build_fixture_regime_transition_breach(),)
    return ()


async def _invoke_scenario(
    *,
    scenario: str,
    invocation_id: str,
    timestamp: datetime,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    archive_root: Path | None,
    library_config: LibraryConfig,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None,
) -> StrategistResult:
    """Compose runner kwargs and call :func:`run_strategist` for *scenario*."""
    mode = _scenario_mode(scenario)
    halt_state = build_fixture_halt_state() if mode == "defensive_posture" else None
    strategist_view, prior_health_snapshots = _scenario_view(scenario)
    return await run_strategist(
        invocation_id=invocation_id,
        timestamp=timestamp,
        mode=mode,
        halt_state=halt_state,
        strategist_view=strategist_view,
        synthesizer_brief_text=synthesizer_text,
        retrieval_store=retrieval_store,
        options_enabled=library_config.feature_flags.options_enabled,
        short_selling_enabled=library_config.feature_flags.short_selling_enabled,
        active_sectors=library_config.active_sectors,
        state_delivery_config=build_fixture_state_delivery_config(),
        sector_resolver=build_fixture_sector_resolver(),
        total_portfolio_value_usd=_PORTFOLIO_VALUE,
        available_for_new_positions_usd=70_000.0,
        current_price_lookup=_current_price_lookup,
        profile_feature_flags=library_config.feature_flags,
        library_config=library_config,
        library_market=build_fixture_market_inputs(),
        starting_snapshot=build_fixture_portfolio_state_snapshot(),
        archive_root=archive_root,
        regime_transition_breaches=_scenario_breaches(scenario),
        prior_health_snapshots=prior_health_snapshots,
        sdk_query_fn=sdk_query_fn,
    )


def _run_one_scenario(
    *,
    scenario: str,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    archive_root: Path,
    library_config: LibraryConfig,
    agent_config: BaseAgentConfig,
    now: datetime,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None,
) -> tuple[Verdict, StrategistResult | None]:
    """Run a single scenario; return verdict and parsed result (or None on FAIL)."""
    invocation_id = _format_invocation_id(now, scenario)
    print(
        f"=== Strategist live-SDK verification: scenario={scenario}, "
        f"invocation_id={invocation_id}, model={agent_config.model.value} ===\n"
        "Invoking SDK...",
        flush=True,
    )

    try:
        result = asyncio.run(
            _invoke_scenario(
                scenario=scenario,
                invocation_id=invocation_id,
                timestamp=now,
                synthesizer_text=synthesizer_text,
                retrieval_store=retrieval_store,
                archive_root=archive_root,
                library_config=library_config,
                sdk_query_fn=sdk_query_fn,
            )
        )
    except HarnessFailure as failure:
        verdict = verdict_for_failure(failure)
        print(
            _render_failure_report(
                scenario=scenario,
                invocation_id=invocation_id,
                agent_config=agent_config,
                failure=failure,
                verdict=verdict,
            )
        )
        return verdict, None

    verdict = verdict_for_success(result=result)
    print(
        _render_success_report(
            scenario=scenario,
            invocation_id=invocation_id,
            agent_config=agent_config,
            result=result,
            verdict=verdict,
        )
    )
    return verdict, result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the strategist end-to-end against a real CLAUDE_CODE_OAUTH_TOKEN "
            "for three scenarios (normal, defensive_posture, emergency), reading "
            "the brief from a recorded synthesizer archive."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        required=True,
        help=(
            "Path to the invocation-archive root containing a recorded "
            "synthesizer run. The script reads "
            "<archive-root>/invocations/<synth-id>/analysis/synthesizer/response.md "
            "and writes its own diagnostic archive under the same root."
        ),
    )
    parser.add_argument(
        "--synthesizer-invocation-id",
        type=str,
        required=True,
        help=(
            "The synthesizer's invocation_id from a prior verify_synthesizer.py run. "
            "Used to locate the recorded brief text and retrieval store."
        ),
    )
    parser.add_argument(
        "--save-fixtures",
        action="store_true",
        default=False,
        help=(
            "After all scenarios pass, write the parsed StrategistOutput JSON to "
            "tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json "
            "so downstream feature trees can consume them as input fixtures."
        ),
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("tests/fixtures/decision/strategist"),
        help="Override the fixture-output directory (defaults to the in-tree path).",
    )
    parser.add_argument(
        "--scenario",
        choices=("normal", "defensive_posture", "emergency", "all"),
        default="all",
        help="Run only the named scenario (or 'all', the default).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> int:
    """CLI entry point. Returns 0 on all-PASS/WARN, 1 if any scenario FAILed.

    Runs the chosen scenario(s) sequentially. A FAIL on one scenario does
    NOT short-circuit the loop — every scenario runs so the operator gets
    a full picture before deciding next steps. ``--save-fixtures`` writes
    the fixture file for every scenario that returned a non-FAIL verdict
    (so a partial-success run still emits fixtures for the passing ones).

    The optional ``sdk_query_fn`` parameter is dependency injection for
    tests: when supplied, the runner uses it instead of the real SDK.
    Operator runs leave it ``None`` so the real Anthropic API is hit.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    agent_config = load_strategist_agent_config()
    library_config = build_fixture_library_config()

    pre_flight_id = _format_invocation_id(now, "preflight")
    if sdk_query_fn is None:
        try:
            _check_oauth_token_set(pre_flight_id)
        except HarnessFailure as failure:
            verdict = verdict_for_failure(failure)
            print(
                _render_failure_report(
                    scenario="preflight",
                    invocation_id=pre_flight_id,
                    agent_config=agent_config,
                    failure=failure,
                    verdict=verdict,
                )
            )
            return 1

    synthesizer_text = read_synthesizer_text(
        archive_root=args.archive_root,
        synthesizer_invocation_id=args.synthesizer_invocation_id,
    )
    retrieval_store = read_retrieval_store(
        archive_root=args.archive_root,
        synthesizer_invocation_id=args.synthesizer_invocation_id,
    )

    scenarios: tuple[str, ...] = _SCENARIOS if args.scenario == "all" else (args.scenario,)
    verdicts: dict[str, Verdict] = {}
    outputs: dict[str, StrategistOutput] = {}

    for scenario in scenarios:
        verdict, result = _run_one_scenario(
            scenario=scenario,
            synthesizer_text=synthesizer_text,
            retrieval_store=retrieval_store,
            archive_root=args.archive_root,
            library_config=library_config,
            agent_config=agent_config,
            now=now,
            sdk_query_fn=sdk_query_fn,
        )
        verdicts[scenario] = verdict
        if result is not None:
            outputs[scenario] = result.output

    if args.save_fixtures and outputs:
        save_fixtures(outputs=outputs, fixtures_dir=args.fixtures_dir)
        print(
            f"[verify_strategist] fixtures written to {args.fixtures_dir}: "
            + ", ".join(f"{s}.json" for s in outputs),
            flush=True,
        )

    summary = " | ".join(f"{s}: {v.value}" for s, v in verdicts.items())
    print(f"=== Summary: {summary} ===")
    return 1 if any(v is Verdict.FAIL for v in verdicts.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
