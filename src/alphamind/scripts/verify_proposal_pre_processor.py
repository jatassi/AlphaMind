"""Proposal pre-processor E2E verification — ALP-319.

Operator entry point (via thin shim at ``scripts/verify_proposal_pre_processor.py``).
Reads analyst and strategist fixture JSON files, constructs matching portfolio
snapshots + library configs + market inputs in code, calls
:func:`alphamind.decision.proposal_pre_processor.runner.run_proposal_pre_processor`,
validates each produced bundle against ``BUNDLE_OUTPUT_SCHEMA``, and writes the
bundles to ``tests/fixtures/decision/proposal_pre_processor/<scenario>.json``.

No SDK invocation. Sub-second runtime per scenario.

Usage::

    uv run python scripts/verify_proposal_pre_processor.py [--scenario SCENARIO]

where SCENARIO is one of ``normal``, ``halt``, ``emergency``,
``normal_with_breach``, or ``all`` (default).

Design decisions documented in
``docs/design/04-decision-layer/proposal-pre-processor.md``.

If the required upstream analyst or strategist fixture JSON is missing, the
script prints a clear error message pointing to the upstream verify script and
exits 1.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import jsonschema

from alphamind._kernel.ids import (
    InvocationId,
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.proposal_pre_processor.models import (
    BUNDLE_OUTPUT_SCHEMA,
    ProposalPreProcessorBundle,
)
from alphamind.decision.proposal_pre_processor.runner import run_proposal_pre_processor
from alphamind.decision.strategist.models import (
    DefensivePostureSummary,
    PortfolioLevelObservations,
    PositionAssessment,
    ReductionPriorityEntry,
    RegimeTransitionAddressedBreach,
    RegimeTransitionSummary,
    StrategistOutput,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    AssetType,
    ContractType,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.scripts._stdio import configure_utf8_stdio

__all__ = [
    "Verdict",
    "build_fixture_analyst_output",
    "build_fixture_emergency_strategist_output",
    "build_fixture_halt_analyst_output",
    "build_fixture_halt_strategist_output",
    "build_fixture_library_config",
    "build_fixture_market_inputs",
    "build_fixture_normal_analyst_output",
    "build_fixture_normal_strategist_output",
    "build_fixture_portfolio_state_snapshot",
    "build_fixture_portfolio_state_snapshot_near_limit",
    "main",
    "run_all_scenarios",
    "run_scenario",
]

# ---------------------------------------------------------------------------
# Constants — mirror verify_strategist.py so fixtures compose cleanly
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_PORTFOLIO_VALUE = 100_000.0
_RISK_FREE_RATE = 0.045
_SPOT_DEFAULT = 100.0
_IV_DEFAULT = 0.30
_DEFAULT_OPTION_EXPIRATION = date(2026, 5, 28)

_TICKER_TO_SECTOR: dict[str, str] = {
    "NVDA": "semis",
    "MSFT": "tech",
    "GOOGL": "tech",
    "AAPL": "tech",
    "JPM": "financials",
    "XOM": "energy",
}

_CURRENT_PRICES: dict[str, float] = {
    "NVDA": 862.0,
    "MSFT": 420.0,
    "GOOGL": 175.0,
    "AAPL": 175.0,
    "JPM": 200.0,
    "XOM": 110.0,
}

_SCENARIOS: tuple[str, ...] = ("normal", "halt", "emergency", "normal_with_breach")

_ANALYST_FIXTURE_DIR = (
    Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "decision" / "analyst"
)
_OUTPUT_FIXTURE_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "tests"
    / "fixtures"
    / "decision"
    / "proposal_pre_processor"
)

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class Verdict:
    PASS = "PASS"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Analyst output loaders — read from checked-in fixture JSON
# ---------------------------------------------------------------------------


def _load_analyst_fixture(path: Path, *, scenario_label: str) -> AnalystOutput:
    """Load an AnalystOutput from a fixture JSON file.

    Raises ``FileNotFoundError`` with a clear message if the file is missing.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Analyst fixture not found: {path}\n"
            f"  Run ``uv run python scripts/verify_analyst.py "
            f"--archive-root <DIR> --synthesizer-invocation-id <INV> --save-fixtures`` "
            f"to produce the analyst fixtures first.\n"
            f"  Scenario '{scenario_label}' requires: {path}"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    return AnalystOutput.model_validate(raw)


def build_fixture_normal_analyst_output(*, fixtures_dir: Path | None = None) -> AnalystOutput:
    """Load the normal-mode analyst output from the checked-in fixture."""
    fixtures_dir = fixtures_dir or _ANALYST_FIXTURE_DIR
    return _load_analyst_fixture(fixtures_dir / "normal.json", scenario_label="normal")


def build_fixture_halt_analyst_output(*, fixtures_dir: Path | None = None) -> AnalystOutput:
    """Load the halt/watchlist-mode analyst output from the checked-in fixture."""
    fixtures_dir = fixtures_dir or _ANALYST_FIXTURE_DIR
    return _load_analyst_fixture(fixtures_dir / "halt.json", scenario_label="halt")


# ---------------------------------------------------------------------------
# In-code StrategistOutput constructors
# (Strategist fixture JSONs not yet emitted — constructed in code per ALP-319)
# ---------------------------------------------------------------------------


def _make_normal_plo() -> PortfolioLevelObservations:
    """Normal-mode portfolio-level observations."""
    return PortfolioLevelObservations(
        aggregate_thesis_health="All four positions on-track; thesis narratives intact.",
        sector_balance_shifts="No material sector shifts this invocation.",
        thesis_dependency_warnings="No correlated breakdowns detected.",
        capital_allocation_observations="Book utilisation at ~11%; ample headroom.",
    )


def _make_halt_plo() -> PortfolioLevelObservations:
    """Defensive-posture portfolio-level observations."""
    return PortfolioLevelObservations(
        aggregate_thesis_health="Positions in protective mode; daily drawdown at halt threshold.",
        sector_balance_shifts="No new exposures; concentration unchanged.",
        thesis_dependency_warnings="Engine-originated MSFT closure noted; correlated risk reduced.",
        capital_allocation_observations="Capital preservation prioritised; no add actions.",
        defensive_posture_summary=DefensivePostureSummary(
            reduction_priority=(
                ReductionPriorityEntry(
                    position_id=PositionId("POS-NVDA"),
                    priority_rationale=(
                        "Highest risk-adjusted weight in a correlated sector cluster."
                    ),
                ),
            ),
            capital_preservation_notes="Maintain stops; no new entries until halt clears.",
        ),
    )


def _make_emergency_plo() -> PortfolioLevelObservations:
    """Emergency-invocation portfolio-level observations with regime-transition summary."""
    return PortfolioLevelObservations(
        aggregate_thesis_health="Normal mode; NVDA position sized over new regime limit.",
        sector_balance_shifts="Regime tightened position-size limit from 5% to 3.5%.",
        thesis_dependency_warnings="NVDA overage 1.0 pp; remedy action issued.",
        capital_allocation_observations="Reduction required on NVDA to comply with new limit.",
        regime_transition_summary=RegimeTransitionSummary(
            addressed_breaches=(
                RegimeTransitionAddressedBreach(
                    breach_id="breach-pos-nvda-size",
                    remedy_assessment_ids=("SA-1",),
                ),
            ),
            uncured_breaches=(),
        ),
    )


def build_fixture_analyst_output(*, invocation_id: str) -> AnalystOutput:
    """Build a minimal normal-mode AnalystOutput in code with a given invocation_id.

    Used when the analyst fixture is loaded from disk but needs an
    invocation_id override, or when no recommendations are needed (empty set).
    """
    return AnalystOutput.model_validate(
        {
            "invocation_id": invocation_id,
            "timestamp": _AS_OF.isoformat(),
            "mode": "normal",
            "recommendations": [],
            "watchlist": None,
        }
    )


def build_fixture_normal_strategist_output(*, invocation_id: str) -> StrategistOutput:
    """Normal scenario — in-code StrategistOutput matching the normal StrategistView.

    Four positions across three sectors (NVDA-semis, JPM-financials, XOM-energy, AAPL-tech)
    all assessed as hold. Produces zero breach contributions for the combined-set check.
    """
    assessments = (
        PositionAssessment(
            assessment_id=RecommendationId("SA-1"),
            position_id=PositionId("POS-NVDA"),
            thesis_id=ThesisId("THESIS-POS-NVDA"),
            underlying=Symbol("NVDA"),
            sector="semis",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="AI capex momentum intact; no new information.",
            action_rationale="Hold; thesis on-track.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-2"),
            position_id=PositionId("POS-JPM"),
            thesis_id=ThesisId("THESIS-POS-JPM"),
            underlying=Symbol("JPM"),
            sector="financials",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Financials sector rotation ongoing; position healthy.",
            action_rationale="Hold; thesis on-track.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-3"),
            position_id=PositionId("POS-XOM"),
            thesis_id=ThesisId("THESIS-POS-XOM"),
            underlying=Symbol("XOM"),
            sector="energy",
            thesis_status="partially-realized",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Energy capex normalisation thesis partially realised.",
            action_rationale="Hold; target not yet reached.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-4"),
            position_id=PositionId("POS-AAPL"),
            thesis_id=ThesisId("THESIS-POS-AAPL"),
            underlying=Symbol("AAPL"),
            sector="tech",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Services-margin expansion thesis intact.",
            action_rationale="Hold; thesis on-track.",
        ),
    )
    return StrategistOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_AS_OF,
        mode="normal",
        position_assessments=assessments,
        pending_order_assessments=(),
        portfolio_level_observations=_make_normal_plo(),
    )


def build_fixture_halt_strategist_output(*, invocation_id: str) -> StrategistOutput:
    """Halt/defensive-posture scenario — in-code StrategistOutput.

    Mirrors the defensive-posture StrategistView: six positions, all hold/reduce,
    no add actions. Matches analyst watchlist mode.
    """
    assessments = (
        PositionAssessment(
            assessment_id=RecommendationId("SA-1"),
            position_id=PositionId("POS-NVDA"),
            thesis_id=ThesisId("THESIS-POS-NVDA"),
            underlying=Symbol("NVDA"),
            sector="semis",
            thesis_status="at-risk",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Position at-risk; defensive posture active.",
            action_rationale="Hold pending halt resolution.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-2"),
            position_id=PositionId("POS-MSFT"),
            thesis_id=ThesisId("THESIS-POS-MSFT"),
            underlying=Symbol("MSFT"),
            sector="tech",
            thesis_status="at-risk",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Cloud upside thesis at-risk in drawdown environment.",
            action_rationale="Hold; engine closure already executed.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-3"),
            position_id=PositionId("POS-GOOGL"),
            thesis_id=ThesisId("THESIS-POS-GOOGL"),
            underlying=Symbol("GOOGL"),
            sector="tech",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Search resilience intact.",
            action_rationale="Hold.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-4"),
            position_id=PositionId("POS-JPM"),
            thesis_id=ThesisId("THESIS-POS-JPM"),
            underlying=Symbol("JPM"),
            sector="financials",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Financials rotation thesis intact.",
            action_rationale="Hold.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-5"),
            position_id=PositionId("POS-XOM"),
            thesis_id=ThesisId("THESIS-POS-XOM"),
            underlying=Symbol("XOM"),
            sector="energy",
            thesis_status="partially-realized",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Energy capex thesis partially realised.",
            action_rationale="Hold.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-6"),
            position_id=PositionId("POS-AAPL"),
            thesis_id=ThesisId("THESIS-POS-AAPL"),
            underlying=Symbol("AAPL"),
            sector="tech",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Services-margin expansion intact.",
            action_rationale="Hold.",
        ),
    )
    return StrategistOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_AS_OF,
        mode="defensive_posture",
        position_assessments=assessments,
        pending_order_assessments=(),
        portfolio_level_observations=_make_halt_plo(),
    )


def build_fixture_emergency_strategist_output(*, invocation_id: str) -> StrategistOutput:
    """Emergency scenario — in-code StrategistOutput with remedy_flag on NVDA.

    Four positions (normal mode), with NVDA position carrying a ``remedy_flag``
    indicating the regime-transition breach must be remedied.
    """
    assessments = (
        PositionAssessment(
            assessment_id=RecommendationId("SA-1"),
            position_id=PositionId("POS-NVDA"),
            thesis_id=ThesisId("THESIS-POS-NVDA"),
            underlying=Symbol("NVDA"),
            sector="semis",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag="regime_transition_breach:position_max_size_pct",
            status_rationale=(
                "AI capex intact; position sized over new regime limit (4.5% vs 3.5%)."
            ),
            action_rationale="Hold pending operator review of regime-transition breach.",
            remedy_rationale=(
                "New regime tightened position_max_size_pct to 3.5%; "
                "current position at 4.5%; overage 1.0 pp. "
                "Operator must decide whether to reduce or accept the overage."
            ),
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-2"),
            position_id=PositionId("POS-JPM"),
            thesis_id=ThesisId("THESIS-POS-JPM"),
            underlying=Symbol("JPM"),
            sector="financials",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Financials rotation thesis intact.",
            action_rationale="Hold.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-3"),
            position_id=PositionId("POS-XOM"),
            thesis_id=ThesisId("THESIS-POS-XOM"),
            underlying=Symbol("XOM"),
            sector="energy",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Energy capex normalisation on-track.",
            action_rationale="Hold.",
        ),
        PositionAssessment(
            assessment_id=RecommendationId("SA-4"),
            position_id=PositionId("POS-AAPL"),
            thesis_id=ThesisId("THESIS-POS-AAPL"),
            underlying=Symbol("AAPL"),
            sector="tech",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="Services-margin expansion intact.",
            action_rationale="Hold.",
        ),
    )
    return StrategistOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_AS_OF,
        mode="normal",
        position_assessments=assessments,
        pending_order_assessments=(),
        portfolio_level_observations=_make_emergency_plo(),
    )


# ---------------------------------------------------------------------------
# Portfolio snapshot constructors
# ---------------------------------------------------------------------------


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def build_fixture_library_config() -> LibraryConfig:
    """Library config matching ``config/profiles/medium.yaml`` shape."""
    effective_limits: dict[str, float] = {
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
    """Market-inputs fixture covering the test ticker universe."""
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
            {ticker: _CURRENT_PRICES.get(ticker, 100.0) for ticker in underlyings}
        ),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=FixtureIvProvider(surface=surface, realized_vol={}),
        as_of=_AS_OF,
    )


def build_fixture_portfolio_state_snapshot() -> PortfolioStateSnapshot:
    """Portfolio snapshot with low net_long utilisation (~11% of 60% limit).

    Mirrors the verify_strategist normal-scenario snapshot so combined-set
    checks on hold-only strategist outputs produce no breaches.

    ``position_max_size_pct`` is set to 4.3 (the NVDA weight in the normal
    strategist view) — below the 5.0 limit — so the rule does not pre-breach.
    """
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
        position_max_size_pct=4.3,
        existing_positions=MappingProxyType({}),
    )


def build_fixture_portfolio_state_snapshot_near_limit() -> PortfolioStateSnapshot:
    """Portfolio snapshot with net_long_pct=49.5 — close to the 60% limit.

    Used for normal_with_breach: analyst recommendations adding ~12%+ notional
    push net_long_pct past the 60% limit.

    The ``existing_positions`` map is seeded with the four positions the
    normal strategist output references (NVDA, JPM, XOM, AAPL) so the
    conflict-detection resolver does not raise KeyError when the analyst
    recommends NVDA (hold direction resolution needs the existing position).
    All existing positions are LONG so recommendations on the same underlying
    produce an ``entry_vs_hold`` conflict annotation (harmless, does not
    prevent the combined-set check).
    """
    # Four LONG equity positions representing ~49.5% total net_long
    _existing = {
        "POS-NVDA": ExistingPosition(
            position_id=PositionId("POS-NVDA"),
            underlying=Symbol("NVDA"),
            sector="semis",
            direction=Direction.LONG,
            asset_type=AssetType.EQUITY,
            notional_usd=15_000.0,
            delta_adjusted_exposure_usd=15_000.0,
            current_greeks=None,
            daily_borrow_cost_usd=None,
            reserves_capital_usd=0.0,
            quantity=17.4,
        ),
        "POS-JPM": ExistingPosition(
            position_id=PositionId("POS-JPM"),
            underlying=Symbol("JPM"),
            sector="financials",
            direction=Direction.LONG,
            asset_type=AssetType.EQUITY,
            notional_usd=10_000.0,
            delta_adjusted_exposure_usd=10_000.0,
            current_greeks=None,
            daily_borrow_cost_usd=None,
            reserves_capital_usd=0.0,
            quantity=50.0,
        ),
        "POS-XOM": ExistingPosition(
            position_id=PositionId("POS-XOM"),
            underlying=Symbol("XOM"),
            sector="energy",
            direction=Direction.LONG,
            asset_type=AssetType.EQUITY,
            notional_usd=4_500.0,
            delta_adjusted_exposure_usd=4_500.0,
            current_greeks=None,
            daily_borrow_cost_usd=None,
            reserves_capital_usd=0.0,
            quantity=40.9,
        ),
        "POS-AAPL": ExistingPosition(
            position_id=PositionId("POS-AAPL"),
            underlying=Symbol("AAPL"),
            sector="tech",
            direction=Direction.LONG,
            asset_type=AssetType.EQUITY,
            notional_usd=20_000.0,
            delta_adjusted_exposure_usd=20_000.0,
            current_greeks=None,
            daily_borrow_cost_usd=None,
            reserves_capital_usd=0.0,
            quantity=114.3,
        ),
    }
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=50_500.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 20.0, "semis": 15.0, "financials": 10.0, "energy": 4.5}
        ),
        net_long_pct=49.5,
        net_short_pct=0.0,
        gross_pct=49.5,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=4.3,
        existing_positions=MappingProxyType(_existing),
    )


# ---------------------------------------------------------------------------
# In-code AnalystOutput for normal_with_breach scenario
# ---------------------------------------------------------------------------


def _build_normal_with_breach_analyst_output(*, invocation_id: str) -> AnalystOutput:
    """Build an AnalystOutput with a long recommendation that breaches net_long_pct.

    The snapshot starts at net_long_pct=49.5. Adding a 5% MSFT long recommendation
    pushes net_long to ~54.5 — below the 60% limit. To ensure breach we add
    recommendations that collectively add >10.5% notional net long exposure.

    We add two LONG equity recommendations at 4% and 5% of portfolio (= $4k + $5k =
    $9k delta notional on a $100k portfolio = 9pp net long), pushing 49.5 + 9 = 58.5pp
    — still under 60. We need >10.5pp, so use three recs at ~4pp each = 12pp total,
    pushing to 61.5pp which breaches 60%.
    """
    # Three equity LONG recommendations, each 4% of portfolio ($4,000 notional).
    # 3x 4% = 12% net long addition; 49.5 + 12 = 61.5 > 60 limit -> breach.
    _deadline = (_AS_OF + timedelta(hours=48)).isoformat()
    _checked_at = _AS_OF.isoformat()

    def _make_rec(
        rec_id: str,
        ticker: str,
        sector: str,
        price: float,
        qty: float,
        dollar_value: float,
    ) -> dict[str, Any]:
        inv_leg_id = f"INV-{rec_id.split('-')[1]}"
        return {
            "recommendation_id": rec_id,
            "instrument": {"asset_type": "equity", "ticker": ticker, "direction": "long"},
            "underlying": ticker,
            "sector": sector,
            "conviction_level": 3,
            "entry_order": {"type": "market"},
            "position_size": {
                "quantity": qty,
                "dollar_value": dollar_value,
                "pct_of_portfolio": dollar_value / _PORTFOLIO_VALUE * 100.0,
                "delta_adjusted_exposure": dollar_value,
            },
            "target": {
                "target_type": "absolute_price",
                "price": price * 1.10,
                "dollar_pl_target": dollar_value * 0.10,
            },
            "invalidation_legs": [
                {
                    "leg_id": inv_leg_id,
                    "type": "time",
                    "is_hard": True,
                    "condition": {"deadline": _deadline},
                    "order_parameters": {"order_type": "market"},
                }
            ],
            "time_expectation_hours": 48.0,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [],
                "checked_at": _checked_at,
            },
            "thesis_narrative": f"Long {ticker} on momentum.",
            "target_rationale": "10% upside target.",
            "invalidation_rationale": [{"leg_id": inv_leg_id, "rationale": "48-hour time stop."}],
            "position_size_rationale": "4% position size, within limits.",
            "counterarguments_acknowledged": "Macro downside risk acknowledged.",
        }

    recs = [
        _make_rec("REC-1", "MSFT", "tech", 420.0, 9.52, 4_000.0),
        _make_rec("REC-2", "GOOGL", "tech", 175.0, 22.86, 4_000.0),
        _make_rec("REC-3", "NVDA", "semis", 862.0, 4.64, 4_000.0),
    ]

    return AnalystOutput.model_validate(
        {
            "invocation_id": invocation_id,
            "timestamp": _AS_OF.isoformat(),
            "mode": "normal",
            "recommendations": recs,
            "watchlist": None,
        }
    )


# ---------------------------------------------------------------------------
# Per-scenario runners
# ---------------------------------------------------------------------------


def _assert_invariants(bundle: ProposalPreProcessorBundle) -> list[str]:
    """Check cross-field invariants from the design doc.

    Returns a list of violation messages (empty if all invariants hold).
    """
    violations: list[str] = []

    # Mirror-symmetry: invocation_id matches across analyst_section and strategist_section
    # (bundle.invocation_id is the shared ID set by run_proposal_pre_processor).

    # Basis consistency: analyst_proposal_ids entries must appear in analyst_section
    basis = bundle.aggregate_observations.combined_set_impact.basis
    if bundle.analyst_section.mode == "normal" and bundle.analyst_section.recommendations:
        actual_rec_ids = {
            wr.recommendation.recommendation_id for wr in bundle.analyst_section.recommendations
        }
        for pid in basis.analyst_proposal_ids:
            if pid not in actual_rec_ids:
                violations.append(
                    f"basis.analyst_proposal_ids contains {pid!r} "
                    f"not present in analyst_section.recommendations"
                )

    # strategist_action_ids must come from non-hold position assessments
    if bundle.strategist_section.position_assessments:
        non_hold_ids = {
            wpa.assessment.assessment_id
            for wpa in bundle.strategist_section.position_assessments
            if wpa.assessment.recommended_action != "hold"
        }
        for sid in basis.strategist_action_ids:
            if sid not in non_hold_ids:
                violations.append(
                    f"basis.strategist_action_ids contains {sid!r} "
                    f"not present in non-hold position assessments"
                )

    # Total identity: conviction_distribution.total == len(analyst recs) in normal mode
    if bundle.analyst_section.mode == "normal":
        expected_rec_count = (
            len(bundle.analyst_section.recommendations)
            if bundle.analyst_section.recommendations
            else 0
        )
        actual_total = bundle.aggregate_observations.conviction_distribution.total
        if actual_total != expected_rec_count:
            violations.append(
                f"conviction_distribution.total={actual_total} != "
                f"len(analyst_section.recommendations)={expected_rec_count}"
            )

    # book_health_summary.total == len(strategist position_assessments)
    expected_book_total = len(bundle.strategist_section.position_assessments)
    actual_book_total = bundle.aggregate_observations.book_health_summary.total
    if actual_book_total != expected_book_total:
        violations.append(
            f"book_health_summary.total={actual_book_total} != "
            f"len(strategist_section.position_assessments)={expected_book_total}"
        )

    return violations


ScenarioResult = tuple[str, list[str]]  # (verdict, error_messages)


def run_scenario(
    scenario: str,
    *,
    analyst_fixtures_dir: Path | None = None,
    output_dir: Path | None = None,
    timestamp: datetime | None = None,
) -> ScenarioResult:
    """Run one scenario end-to-end and return (verdict, error_messages).

    Writes the produced bundle to ``<output_dir>/<scenario>.json`` on success.
    On failure, returns (Verdict.FAIL, [error_message]).
    """
    output_dir = output_dir or _OUTPUT_FIXTURE_DIR
    timestamp = timestamp or _AS_OF

    errors: list[str] = []

    try:
        analyst_out, strategist_out, snapshot = _build_scenario_inputs(
            scenario, analyst_fixtures_dir=analyst_fixtures_dir
        )
    except FileNotFoundError as exc:
        return Verdict.FAIL, [str(exc)]

    library_config = build_fixture_library_config()
    market = build_fixture_market_inputs()

    try:
        bundle = run_proposal_pre_processor(
            analyst_output=analyst_out,
            strategist_output=strategist_out,
            snapshot=snapshot,
            library_config=library_config,
            market=market,
            snapshot_timestamp=_AS_OF,
            timestamp=timestamp,
        )
    except Exception as exc:
        # Verify-script per-check supervisor per runtime §G1: any failure
        # from the pre-processor is a FAIL verdict; the exception's type +
        # message names the offending layer for triage. ``BaseException``
        # (``KeyboardInterrupt``) propagates.
        return Verdict.FAIL, [f"run_proposal_pre_processor raised {type(exc).__name__}: {exc}"]

    # Schema validation
    bundle_dict = bundle.model_dump(by_alias=True, mode="json")
    try:
        jsonschema.validate(bundle_dict, BUNDLE_OUTPUT_SCHEMA)
    except jsonschema.ValidationError as exc:
        errors.append(f"schema validation FAIL: {exc.message}")

    # Scenario-specific assertions
    scenario_errors = _check_scenario_assertions(scenario, bundle)
    errors.extend(scenario_errors)

    # Cross-field invariants
    invariant_errors = _assert_invariants(bundle)
    errors.extend(invariant_errors)

    if errors:
        return Verdict.FAIL, errors

    # Write output fixture
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{scenario}.json").write_text(
        json.dumps(bundle_dict, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return Verdict.PASS, []


def _build_scenario_inputs(
    scenario: str,
    *,
    analyst_fixtures_dir: Path | None,
) -> tuple[AnalystOutput, StrategistOutput, PortfolioStateSnapshot]:
    """Construct the (analyst_output, strategist_output, snapshot) triple for a scenario."""
    if scenario == "normal":
        analyst_out = build_fixture_normal_analyst_output(fixtures_dir=analyst_fixtures_dir)
        inv_id = analyst_out.invocation_id
        strategist_out = build_fixture_normal_strategist_output(invocation_id=inv_id)
        snapshot = build_fixture_portfolio_state_snapshot()

    elif scenario == "halt":
        analyst_out = build_fixture_halt_analyst_output(fixtures_dir=analyst_fixtures_dir)
        inv_id = analyst_out.invocation_id
        strategist_out = build_fixture_halt_strategist_output(invocation_id=inv_id)
        snapshot = build_fixture_portfolio_state_snapshot()

    elif scenario == "emergency":
        analyst_out = build_fixture_normal_analyst_output(fixtures_dir=analyst_fixtures_dir)
        inv_id = analyst_out.invocation_id
        strategist_out = build_fixture_emergency_strategist_output(invocation_id=inv_id)
        snapshot = build_fixture_portfolio_state_snapshot()

    elif scenario == "normal_with_breach":
        # Use a synthetic invocation_id (not from fixture) — both outputs share it.
        inv_id = InvocationId("20260504T143000Z-verify-pre-processor-breach")
        analyst_out = _build_normal_with_breach_analyst_output(invocation_id=inv_id)
        strategist_out = build_fixture_normal_strategist_output(invocation_id=inv_id)
        snapshot = build_fixture_portfolio_state_snapshot_near_limit()

    else:
        raise ValueError(f"unknown scenario: {scenario!r}")

    return analyst_out, strategist_out, snapshot


def _check_normal_assertions(bundle: ProposalPreProcessorBundle) -> list[str]:
    """normal scenario: no breaches expected."""
    breaches = bundle.aggregate_observations.combined_set_impact.breaches
    if breaches:
        return [
            f"normal scenario: expected no breaches, got {len(breaches)}: "
            + ", ".join(b.rule for b in breaches)
        ]
    return []


def _check_halt_assertions(bundle: ProposalPreProcessorBundle) -> list[str]:
    """halt scenario: mode-conditional shape checks."""
    errors: list[str] = []
    analyst_sec = bundle.analyst_section
    if analyst_sec.mode != "watchlist":
        errors.append(f"halt: analyst_section.mode={analyst_sec.mode!r} != 'watchlist'")
    strat_sec = bundle.strategist_section
    if strat_sec.mode != "defensive_posture":
        errors.append(f"halt: strategist_section.mode={strat_sec.mode!r} != 'defensive_posture'")
    basis = bundle.aggregate_observations.combined_set_impact.basis
    if basis.analyst_proposal_ids:
        errors.append(
            f"halt: basis.analyst_proposal_ids should be empty, got {basis.analyst_proposal_ids!r}"
        )
    return errors


def _check_emergency_assertions(bundle: ProposalPreProcessorBundle) -> list[str]:
    """emergency scenario: at least one position assessment must have remedy_flag."""
    remedy_flagged = [
        wpa for wpa in bundle.strategist_section.position_assessments if wpa.assessment.remedy_flag
    ]
    if not remedy_flagged:
        return [
            "emergency: no position assessment has remedy_flag populated; expected at least one"
        ]
    return []


def _check_breach_contributors(bundle: ProposalPreProcessorBundle) -> list[str]:
    """normal_with_breach scenario: net_long breach with positive REC-N contributors."""
    errors: list[str] = []
    breaches = bundle.aggregate_observations.combined_set_impact.breaches
    if not breaches:
        return ["normal_with_breach: expected at least one breach, got none"]
    net_long_breach = next((b for b in breaches if "net_long" in b.rule.lower()), None)
    if net_long_breach is None:
        return [
            f"normal_with_breach: expected a net_long_exposure breach, "
            f"got rules: {[b.rule for b in breaches]!r}"
        ]
    if not net_long_breach.contributors:
        return ["normal_with_breach: breach has no contributors"]
    positive_recs = [
        c
        for c in net_long_breach.contributors
        if c.proposal_id.startswith("REC-") and c.contribution > 0
    ]
    if not positive_recs:
        contrib_summary = [(c.proposal_id, c.contribution) for c in net_long_breach.contributors]
        errors.append(
            "normal_with_breach: no positive-contribution REC-N contributor "
            f"found in breach {net_long_breach.rule!r}; contributors: {contrib_summary!r}"
        )
    return errors


def _check_scenario_assertions(scenario: str, bundle: ProposalPreProcessorBundle) -> list[str]:
    """Dispatch to per-scenario assertion helpers. Returns error messages (empty = pass)."""
    if scenario == "normal":
        return _check_normal_assertions(bundle)
    if scenario == "halt":
        return _check_halt_assertions(bundle)
    if scenario == "emergency":
        return _check_emergency_assertions(bundle)
    if scenario == "normal_with_breach":
        return _check_breach_contributors(bundle)
    return []


# ---------------------------------------------------------------------------
# run_all_scenarios
# ---------------------------------------------------------------------------


def run_all_scenarios(
    *,
    analyst_fixtures_dir: Path | None = None,
    output_dir: Path | None = None,
    timestamp: datetime | None = None,
) -> dict[str, ScenarioResult]:
    """Run all four scenarios and return a mapping of scenario → (verdict, errors)."""
    results: dict[str, ScenarioResult] = {}
    for scenario in _SCENARIOS:
        results[scenario] = run_scenario(
            scenario,
            analyst_fixtures_dir=analyst_fixtures_dir,
            output_dir=output_dir,
            timestamp=timestamp,
        )
    return results


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def _render_verdict_line(scenario: str, verdict: str, errors: list[str]) -> str:
    icon = "✓" if verdict == Verdict.PASS else "✗"
    if verdict == Verdict.PASS:
        return f"  {icon} {scenario}: PASS"
    reason = errors[0] if errors else "(unknown)"
    return f"  {icon} {scenario}: FAIL ({reason})"


def _render_summary_table(results: dict[str, ScenarioResult]) -> str:
    lines = ["", "=== Proposal pre-processor verification ===", ""]
    for scenario in _SCENARIOS:
        if scenario in results:
            verdict, errors = results[scenario]
            lines.append(_render_verdict_line(scenario, verdict, errors))
    lines.append("")
    all_pass = all(v == Verdict.PASS for v, _ in results.values())
    status = "ALL PASS" if all_pass else "FAIL"
    lines.append(f"Result: {status}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main() — CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code (0=all pass, 1=any fail)."""
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description=(
            "Run proposal pre-processor verification against analyst and strategist "
            "fixture JSON files. No SDK calls. Sub-second runtime."
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=[*_SCENARIOS, "all"],
        default="all",
        help="Which scenario(s) to run (default: all).",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=None,
        help=(
            "Override the output fixtures directory "
            "(default: tests/fixtures/decision/proposal_pre_processor)."
        ),
    )
    parser.add_argument(
        "--analyst-fixtures-dir",
        type=Path,
        default=None,
        help=(
            "Override the analyst input fixtures directory "
            "(default: tests/fixtures/decision/analyst)."
        ),
    )
    args = parser.parse_args(argv)

    scenario_arg: str = args.scenario
    output_dir: Path | None = args.fixtures_dir
    analyst_fixtures_dir: Path | None = args.analyst_fixtures_dir

    if scenario_arg == "all":
        results = run_all_scenarios(
            analyst_fixtures_dir=analyst_fixtures_dir,
            output_dir=output_dir,
        )
    else:
        verdict, errors = run_scenario(
            scenario_arg,
            analyst_fixtures_dir=analyst_fixtures_dir,
            output_dir=output_dir,
        )
        results = {scenario_arg: (verdict, errors)}

    print(_render_summary_table(results))

    if any(v == Verdict.FAIL for v, _ in results.values()):
        for scenario, (verdict, errors) in results.items():
            if verdict == Verdict.FAIL:
                print(f"Errors in {scenario!r}:")
                for err in errors:
                    print(f"  - {err}")
        return 1
    return 0
