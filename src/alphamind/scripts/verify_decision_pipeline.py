"""Decision-pipeline end-to-end live-SDK verification — story ALP-404.

Operator entry point for the decision-layer composition runner. Builds a
fixture-backed :class:`RepositoryFixture` plus an in-process
:class:`CurrentPriceProvider`, loads ``config/agents.yaml`` via the
existing :class:`AgentsConfig` validator, and invokes
:func:`alphamind.pipeline.decision.run_decision_pipeline` once for the
``normal`` scenario against the real Claude Agent SDK.

The four decision-layer agents (analyst + strategist in parallel,
pre-processor, PM) all run end-to-end through their real harnesses; the
script validates the returned :class:`DecisionPipelineResult` against
the parent issue's invariants and writes the serialized result to
``<archive-root>/decision_pipeline/normal/result.json``.

Two reporting paths:

- The composition runner returns a :class:`DecisionPipelineResult` →
  validate against acceptance invariants. PASS prints the operator
  summary, exits 0; FAIL prints the validation-error block, exits 1.
- The composition runner raises a :class:`HarnessFailure` → render the
  failure block, exit 1.

A missing ``CLAUDE_CODE_OAUTH_TOKEN`` is surfaced as a clean failure
report with a clear setup-error message before any SDK call is made.

Scope boundaries (per parent issue ALP-310 decisions):

1. **Halt and emergency modes are out of scope.** Only ``--scenario
   normal`` is accepted. The per-agent verify scripts already cover modal
   shape; this script proves the wiring.
2. **Live synthesizer→decision chaining is out of scope.** The verify
   script supplies pre-recorded synthesizer text + a fixture
   :class:`RetrievalStore`; live composition is the trigger layer's job.
3. **DB-backed persistence is out of scope.** The PM engine-stub writes
   to in-memory state; SQL writeback ships separately under ALP-119.

The fixture-builder helpers and the result-validation predicate live in
this module so ``tests/scripts/test_verify_decision_pipeline.py`` can
exercise them without touching the Anthropic API. The thin shim at
``scripts/verify_decision_pipeline.py`` defers to :func:`main` here.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import (
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.decision.analyst.harness import HarnessFailure as AnalystHarnessFailure
from alphamind.decision.portfolio_manager.harness import (
    HarnessFailure as PMHarnessFailure,
)
from alphamind.decision.portfolio_manager.harness import SDKFailure
from alphamind.decision.strategist.harness import (
    HarnessFailure as StrategistHarnessFailure,
)
from alphamind.pipeline.decision import (
    DecisionPipelineResult,
    run_decision_pipeline,
)
from alphamind.portfolio_state import PortfolioStateConfig, load_portfolio_state_config
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
)
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
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
)
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.repository import (
    CurrentInvocationMetadata,
    PortfolioPnLInputs,
    PriorInvocationContext,
    RepositoryFixture,
    StubPortfolioStateRepository,
)
from alphamind.risk_guardrails.library_snapshot import LibrarySnapshot
from alphamind.scripts.verify_strategist import (
    build_fixture_library_config,
    build_fixture_market_inputs,
    build_fixture_state_delivery_config,
)

__all__ = [
    "Verdict",
    "build_fixture_price_provider",
    "build_fixture_repository",
    "build_fixture_retrieval_store",
    "build_fixture_synthesizer_text",
    "main",
    "serialize_pipeline_result",
    "validate_pipeline_result",
]


_PIPELINE_LABEL = "decision-pipeline"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_YAML_DEFAULT = _REPO_ROOT / "config" / "agents.yaml"
_PORTFOLIO_STATE_YAML_DEFAULT = _REPO_ROOT / "config" / "portfolio_state.yaml"


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    """Verdict label printed in the operator report and mapped to exit code.

    PASS exits 0; FAIL exits 1 (validation failure or any
    :class:`HarnessFailure` subclass from any of the four agents).
    """

    PASS = "PASS"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Fixture constants — mirror the per-agent verifiers' test universe
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_PORTFOLIO_VALUE = 100_000.0
_AVAILABLE_FOR_NEW_POSITIONS = 70_000.0

# Four-way risk taxonomy: one ticker per sector. Mirrors the per-agent
# verifiers' universe so the fixture environment composes cleanly with
# any existing fixture-builder helpers callers might want to reuse.
_TICKER_TO_SECTOR: dict[str, str] = {
    "AAPL": "tech",
    "NVDA": "semis",
    "JPM": "financials",
    "XOM": "energy",
}

_CURRENT_PRICES: dict[str, float] = {
    "AAPL": 175.0,
    "NVDA": 862.0,
    "JPM": 200.0,
    "XOM": 110.0,
}


# ---------------------------------------------------------------------------
# Sector + borrow-cost resolvers
# ---------------------------------------------------------------------------


def _sector_resolver(ticker: str) -> str:
    """Map a ticker to its sector key string.

    Tickers outside the small test universe map to ``"tech"`` — the
    runner's contract requires *some* sector for any ticker, and the
    fallback keeps unrecognized tickers in the active-sector set.
    """
    return _TICKER_TO_SECTOR.get(ticker, "tech")


# ---------------------------------------------------------------------------
# Repository-fixture builder — 4 positions across the 4-way risk taxonomy
# ---------------------------------------------------------------------------


def _make_equity_position(
    *,
    position_id: str,
    ticker: str,
    direction: Direction,
    share_count: float,
    avg_cost: float,
    age_hours: float = 24.0,
    bracket_id: str | None = None,
    thesis_id: str | None = None,
) -> PositionRecord:
    """Build an OPEN equity ``PositionRecord`` with one prior fill.

    The position carries one execution-history fill recorded at the
    average cost price, which is what the assembler requires to mark the
    position to current price without raising on a missing fill chain.
    """
    fill = PositionFill(
        fill_timestamp=_AS_OF - timedelta(hours=age_hours),
        fill_price=price(avg_cost),
        fill_quantity=share_count,
        slippage=signed_money(0.0),
        fees=money(1.0),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_AS_OF - timedelta(hours=age_hours),
        details=EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=share_count,
            average_cost_basis_per_share=avg_cost,
        ),
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_bracket(*, position_id: str, ticker: str) -> BracketRecord:
    """Build a minimal ACTIVE bracket with mechanical TP/PS legs."""
    spot = _CURRENT_PRICES.get(ticker, 100.0)
    legs = (
        BracketLeg(
            leg_id=f"LEG-{position_id}-TP",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=OrderId(f"ORD-{position_id}-TP"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol(ticker),
                threshold_usd=spot * 1.10,
                direction="GTE",
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        ),
        BracketLeg(
            leg_id=f"LEG-{position_id}-PS",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId(f"ORD-{position_id}-PS"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol(ticker),
                threshold_usd=spot * 0.95,
                direction="LTE",
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        ),
    )
    return BracketRecord(
        bracket_id=BracketId(f"BRK-{position_id}"),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(f"ORD-{position_id}-ENTRY"),
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_thesis(*, position_id: str, ticker: str) -> ThesisRecord:
    """Build one active ACTIVE thesis with the three required component types."""
    thesis_id = f"THESIS-{position_id}"
    components = (
        ThesisComponent(
            component_id=f"TC-{position_id}-1",
            thesis_id=ThesisId(thesis_id),
            component_type=ThesisComponentType.ENTRY_RATIONALE,
            linked_bracket_leg_type=None,
            instrument_reference=position_id,
            narrative=f"Entry on confirmed {ticker} sector momentum.",
            key_assumptions=(KeyAssumption(text="Sector momentum persists.", outcome=None),),
            generation_timestamp=_AS_OF - timedelta(hours=20),
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id=f"TC-{position_id}-2",
            thesis_id=ThesisId(thesis_id),
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            instrument_reference=position_id,
            narrative=f"{ticker} target at +10% from entry; clear resistance band.",
            key_assumptions=(KeyAssumption(text="Resistance band holds.", outcome=None),),
            generation_timestamp=_AS_OF - timedelta(hours=20),
            resolution_outcome=None,
            resolution_notes=None,
        ),
        ThesisComponent(
            component_id=f"TC-{position_id}-3",
            thesis_id=ThesisId(thesis_id),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            instrument_reference=position_id,
            narrative=f"Invalidation below {ticker} swing low erodes the entry premise.",
            key_assumptions=(KeyAssumption(text="Swing low intact.", outcome=None),),
            generation_timestamp=_AS_OF - timedelta(hours=20),
            resolution_outcome=None,
            resolution_notes=None,
        ),
    )
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary=f"Long {ticker} on sector momentum.",
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
        key_catalyst="Earnings catalyst window opens later this week.",
    )


def _make_cash_ledger() -> CashLedger:
    return CashLedger(
        current_cash_usd=_AVAILABLE_FOR_NEW_POSITIONS,
        settled_cash_usd=_AVAILABLE_FOR_NEW_POSITIONS,
        reserved_capital_usd=0.0,
        available_buying_power_usd=_AVAILABLE_FOR_NEW_POSITIONS,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=_PORTFOLIO_VALUE,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    """Risk-budget covering the four sectors plus net-long / gross."""

    def _entry(rule_id: str, rule_label: str) -> RiskBudgetEntry:
        return RiskBudgetEntry(
            rule_id=rule_id,
            rule_label=rule_label,
            current_value=5.0,
            limit_value=25.0,
            headroom=20.0,
            headroom_pct_of_limit=80.0,
            zone=RiskZone.NORMAL,
            unit="% of portfolio",
            cumulative_invocation_impact_value=0.0,
        )

    return RiskBudgetConsumption(
        entries=(
            _entry("sector_concentration_tech", "Tech sector concentration"),
            _entry("sector_concentration_semis", "Semis sector concentration"),
            _entry("sector_concentration_financials", "Financials sector concentration"),
            _entry("sector_concentration_energy", "Energy sector concentration"),
            _entry("net_long_pct", "Net long exposure"),
            _entry("gross_exposure_pct", "Gross exposure"),
            _entry("daily_drawdown_pct", "Daily drawdown limit"),
        )
    )


def _make_active_risk_parameters() -> ActiveRiskParameterSet:
    """Active risk-parameter set — must include ``position_max_size_pct``
    and ``gross_exposure_pct`` so the Phase 1 enforcement override applier
    has the two rule entries it edits (story ALP-433)."""
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
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                value=200.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=200.0,
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


def _make_thesis_quality_aggregates() -> ThesisQualityAggregate:
    return ThesisQualityAggregate(
        as_of_timestamp=_AS_OF,
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
        signal_hit_rates=(),
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


def _make_pnl_inputs() -> PortfolioPnLInputs:
    return PortfolioPnLInputs(
        daily_realized_pnl_usd=0.0,
        cumulative_realized_pnl_usd=0.0,
        rolling_realized_pnl={"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_invocation_metadata(invocation_id: str) -> CurrentInvocationMetadata:
    return CurrentInvocationMetadata(
        invocation_id=invocation_id,
        phase1_committed_at=_AS_OF - timedelta(seconds=5),
        pipeline_invocation_started_at=None,
    )


def _make_prior_context() -> PriorInvocationContext:
    return PriorInvocationContext(
        prior_invocation_id=None,
        prior_active_risk_parameters=None,
        prior_phase1_committed_at=None,
    )


def build_fixture_repository(
    *, invocation_id: str = "verify-decision-pipeline-normal"
) -> RepositoryFixture:
    """Build the canonical normal-mode ``RepositoryFixture``.

    Four open equity positions across the 4-way risk taxonomy (one each
    in tech, semis, financials, energy), modest exposures (well under
    the medium-profile limits — no breaches expected), one active thesis
    on the AAPL position, no abandoned actions, no recent PM decisions.
    """
    positions = (
        _make_equity_position(
            position_id=PositionId("POS-AAPL"),
            ticker=Symbol("AAPL"),
            direction=Direction.LONG,
            share_count=10.0,
            avg_cost=170.0,
            bracket_id=BracketId("BRK-POS-AAPL"),
            thesis_id=ThesisId("THESIS-POS-AAPL"),
        ),
        _make_equity_position(
            position_id=PositionId("POS-NVDA"),
            ticker=Symbol("NVDA"),
            direction=Direction.LONG,
            share_count=5.0,
            avg_cost=820.0,
            bracket_id=BracketId("BRK-POS-NVDA"),
        ),
        _make_equity_position(
            position_id=PositionId("POS-JPM"),
            ticker=Symbol("JPM"),
            direction=Direction.LONG,
            share_count=15.0,
            avg_cost=180.0,
            bracket_id=BracketId("BRK-POS-JPM"),
        ),
        _make_equity_position(
            position_id=PositionId("POS-XOM"),
            ticker=Symbol("XOM"),
            direction=Direction.LONG,
            share_count=20.0,
            avg_cost=105.0,
            bracket_id=BracketId("BRK-POS-XOM"),
        ),
    )
    brackets = tuple(
        _make_bracket(position_id=pid, ticker=tk)
        for pid, tk in (
            ("POS-AAPL", "AAPL"),
            ("POS-NVDA", "NVDA"),
            ("POS-JPM", "JPM"),
            ("POS-XOM", "XOM"),
        )
    )
    # One active thesis (per scope) — anchored to the AAPL position so the
    # PM has at least one thesis to read via ``get_thesis_components``.
    theses = (_make_thesis(position_id=PositionId("POS-AAPL"), ticker=Symbol("AAPL")),)
    return RepositoryFixture(
        open_positions=positions,
        pending_positions=(),
        drawdown_state=_make_drawdown_state(),
        portfolio_pnl_inputs=_make_pnl_inputs(),
        active_theses=theses,
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        risk_budget=_make_risk_budget(),
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        brackets=brackets,
        current_invocation_metadata=_make_invocation_metadata(invocation_id),
        prior_invocation_context=_make_prior_context(),
    )


# ---------------------------------------------------------------------------
# Current-price provider builder
# ---------------------------------------------------------------------------


def build_fixture_price_provider() -> dict[str, PriceQuote]:
    """Build the ticker → ``PriceQuote`` map the in-process provider closes over.

    Returns the bare dict so tests can assert on quote shape without
    instantiating the provider; the verify script wraps the dict in a
    :class:`StubCurrentPriceProvider` before passing to the runner.
    """
    return {
        ticker: PriceQuote(
            ticker=ticker,
            price_usd=price,
            as_of_timestamp=_AS_OF,
            source=PriceSource.INTRADAY_QUOTE,
            is_stale=False,
        )
        for ticker, price in _CURRENT_PRICES.items()
    }


# ---------------------------------------------------------------------------
# Synthesizer-text + retrieval-store fixtures (library / market / state-delivery
# helpers are imported from verify_strategist)
# ---------------------------------------------------------------------------


def build_fixture_synthesizer_text() -> str:
    """Pre-recorded synthesizer prose with brief references the agents can resolve.

    The text references two bundles by ID so the analyst and strategist
    have something to point at when they cite the brief. The companion
    :func:`build_fixture_retrieval_store` populates those references.
    """
    return (
        "Cross-asset framing [CR-1].\n"
        "  Tech-semis decoupled from broader equities through the session;\n"
        "  rates remained tightly bound to dollar.\n"
        "Qualitative narrative [QR-1].\n"
        "  Hawkish rates narrative re-asserted after FOMC minutes;\n"
        "  pressure on duration-heavy growth names.\n"
        "Outlook: hold-only stance across the four-position book is appropriate;\n"
        "  no high-conviction adds suggested.\n"
    )


def build_fixture_retrieval_store() -> RetrievalStore:
    """Build a small ``RetrievalStore`` covering the references in the brief."""
    return RetrievalStore(
        entries={
            "CR-1": (
                "[CR-1] Cross-asset framing.\n"
                "  Tech-semis decoupled from broader equities;\n"
                "  rates remained tightly bound to dollar.\n"
            ),
            "QR-1": (
                "[QR-1] Hawkish rates narrative re-asserted after FOMC minutes.\n"
                "  Pressure on duration-heavy growth names.\n"
            ),
        },
        freshness_by_source={},
    )


# ---------------------------------------------------------------------------
# AgentsConfig loader — mirrors the per-agent verifier pattern
# ---------------------------------------------------------------------------


def _load_decision_layer_agents(
    agents_yaml_path: Path | None = None,
) -> dict[AgentName, BaseAgentConfig]:
    """Read ``config/agents.yaml`` and return the three decision-layer slots.

    The composition runner consumes a mapping keyed by :class:`AgentName`;
    only the analyst, strategist, and PM slots are required for this
    pipeline.
    """
    path = agents_yaml_path or _AGENTS_YAML_DEFAULT
    with path.open() as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    return {
        AgentName.analyst: cfg.agents[AgentName.analyst],
        AgentName.strategist: cfg.agents[AgentName.strategist],
        AgentName.portfolio_manager: cfg.agents[AgentName.portfolio_manager],
    }


def _load_portfolio_state_config(path: Path | None = None) -> PortfolioStateConfig:
    return load_portfolio_state_config(path or _PORTFOLIO_STATE_YAML_DEFAULT)


# ---------------------------------------------------------------------------
# Result validation
# ---------------------------------------------------------------------------


def validate_pipeline_result(
    result: DecisionPipelineResult,
) -> tuple[Verdict, tuple[str, ...]]:
    """Apply the acceptance-criterion invariants to a ``DecisionPipelineResult``.

    Returns a ``(Verdict, errors)`` pair. PASS iff every invariant holds:

    - the analyst returns a structured output (recommendations may be
      empty if the analyst supplied a documented "no recommendations"
      reason via ``mode='watchlist'``);
    - the strategist's ``position_assessments`` covers every position
      (at least one assessment present);
    - the pre-processor bundle's ``aggregate_observations`` carries
      populated ``combined_set_impact`` / ``conviction_distribution`` /
      ``book_health_summary`` sub-blocks;
    - the PM's ``submission_log`` is non-empty (at least one envelope
      submitted);
    - ``pm_result.output.envelopes_submitted`` matches
      ``len(submission_log)``.
    """
    errors: list[str] = []

    # AnalystOutput's Pydantic validator already enforces the mode-conditional
    # invariant (mode=normal => recommendations is a tuple, mode=watchlist =>
    # watchlist is a tuple) — if the runner returned successfully, the output
    # is structurally well-formed. The verify script only runs the ``normal``
    # scenario, so both agents must echo that mode back; a mismatch indicates
    # a wiring drift the operator must triage before trusting the run.
    for label, mode in (
        ("analyst", result.analyst_result.output.mode),
        ("strategist", result.strategist_result.output.mode),
    ):
        if mode != "normal":
            errors.append(
                f"{label} output: mode={mode!r} does not match the requested 'normal' scenario"
            )

    # Strategist must have at least one position assessment for a non-empty book.
    if not result.strategist_result.output.position_assessments:
        errors.append(
            "strategist output: position_assessments is empty — "
            "expected one assessment per held position"
        )

    # Pre-processor aggregate observations must be populated.
    aggregate = result.pre_processor_bundle.aggregate_observations
    if aggregate.combined_set_impact is None:
        errors.append("pre-processor bundle: aggregate_observations.combined_set_impact missing")
    if aggregate.conviction_distribution is None:
        errors.append(
            "pre-processor bundle: aggregate_observations.conviction_distribution missing"
        )
    if aggregate.book_health_summary is None:
        errors.append("pre-processor bundle: aggregate_observations.book_health_summary missing")

    # PM submission log must be non-empty.
    submission_log = result.pm_result.submission_log
    if not submission_log:
        errors.append("PM submitted zero envelopes — pm_result.submission_log is empty")

    # PM envelopes_submitted must match the submission log length.
    output = result.pm_result.output
    if output.envelopes_submitted != len(submission_log):
        errors.append(
            f"PM output: envelopes_submitted={output.envelopes_submitted} "
            f"!= len(submission_log)={len(submission_log)}"
        )

    if errors:
        return Verdict.FAIL, tuple(errors)
    return Verdict.PASS, ()


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def serialize_pipeline_result(result: DecisionPipelineResult, target: Path) -> None:
    """Write the six ``DecisionPipelineResult`` fields to *target* as JSON.

    Pydantic models are dumped via ``model_dump(mode='json')``; the
    library snapshot is a frozen dataclass with ``mappingproxy`` fields
    that ``dataclasses.asdict`` cannot serialize directly, so we hand-roll
    the necessary dict shape. The ``submission_log`` entries are
    ``SubmissionLogEntry`` dataclasses with a Pydantic ``PMEnvelope`` and
    a tuple of Pydantic ``SubmissionResult`` instances. The target's
    parent directory is created if missing.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    analyst = result.analyst_result
    strategist = result.strategist_result
    pm = result.pm_result
    payload = {
        "pydantic_snapshot": _snapshot_to_dict(result.pydantic_snapshot),
        "library_snapshot": _library_snapshot_to_dict(result.library_snapshot),
        "analyst_result": {
            "output": analyst.output.model_dump(mode="json"),
            "retry_count": analyst.retry_count,
            "tokens_used": analyst.tokens_used.model_dump(),
            "tool_calls_used": analyst.tool_calls_used,
            "wall_clock_seconds": analyst.wall_clock_seconds,
            "stop_reason": analyst.stop_reason,
        },
        "strategist_result": {
            "output": strategist.output.model_dump(mode="json"),
            "validation_result": dataclasses.asdict(strategist.validation_result),
            "tokens_used": strategist.tokens_used.model_dump(),
            "metadata": strategist.metadata,
        },
        "pre_processor_bundle": result.pre_processor_bundle.model_dump(mode="json"),
        "pm_result": {
            "output": pm.output.model_dump(mode="json"),
            "submission_log": [
                {
                    "envelope": entry.envelope.model_dump(mode="json"),
                    "submission_results": [
                        sub.model_dump(mode="json") for sub in entry.submission_results
                    ],
                }
                for entry in pm.submission_log
            ],
            "retry_count": pm.retry_count,
            "tokens_used": pm.tokens_used.model_dump(),
            "tool_calls_used": pm.tool_calls_used,
            "wall_clock_seconds": pm.wall_clock_seconds,
            "stop_reason": pm.stop_reason,
        },
    }
    target.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _library_snapshot_to_dict(snapshot: LibrarySnapshot) -> dict[str, Any]:
    """Convert a frozen-dataclass library snapshot to a JSON-friendly dict.

    The library snapshot's ``Mapping`` fields (``sector_exposure_pct``,
    ``existing_positions``) carry ``mappingproxy`` instances that
    ``dataclasses.asdict`` cannot pickle — convert each to a plain dict.
    Existing positions are themselves frozen dataclasses; we delegate
    further coercion to ``json.dumps(default=str)`` for residual
    non-JSON-native values (enums, dataclass instances).
    """
    return {
        "portfolio_value_usd": snapshot.portfolio_value_usd,
        "cash_usd": snapshot.cash_usd,
        "reserved_for_pending_orders_usd": snapshot.reserved_for_pending_orders_usd,
        "sector_exposure_pct": dict(snapshot.sector_exposure_pct),
        "net_long_pct": snapshot.net_long_pct,
        "net_short_pct": snapshot.net_short_pct,
        "gross_pct": snapshot.gross_pct,
        "options_delta_pct": snapshot.options_delta_pct,
        "portfolio_theta_pct_per_day": snapshot.portfolio_theta_pct_per_day,
        "portfolio_vega_pct_per_iv_point": snapshot.portfolio_vega_pct_per_iv_point,
        "total_short_pct": snapshot.total_short_pct,
        "single_short_max_pct": snapshot.single_short_max_pct,
        "daily_borrow_cost_pct": snapshot.daily_borrow_cost_pct,
        "position_max_size_pct": snapshot.position_max_size_pct,
        "existing_positions": {pid: str(pos) for pid, pos in snapshot.existing_positions.items()},
    }


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    """Best-effort JSON-friendly view of the OMS portfolio-state snapshot.

    Replaces the prior Pydantic ``model_dump(mode="json")`` call on
    :class:`alphamind.portfolio_state.snapshot.PortfolioStateSnapshot`. The
    diagnostic archive only needs the field names present on the instance;
    tests sometimes use ``object.__new__`` to bypass init, so we walk the
    fields defensively and skip any that aren't actually set.
    """
    import dataclasses as _dc

    if not _dc.is_dataclass(snapshot):
        return {"repr": repr(snapshot)}
    out: dict[str, Any] = {}
    for field in _dc.fields(snapshot):
        try:
            value = getattr(snapshot, field.name)
        except AttributeError:
            continue
        # is_dataclass() returns True for both instances and the class itself;
        # narrow to instances so asdict() doesn't reject the class.
        if _dc.is_dataclass(value) and not isinstance(value, type):
            try:
                out[field.name] = _dc.asdict(value)
            except (AttributeError, TypeError):
                out[field.name] = repr(value)
        else:
            out[field.name] = value
    return out


# ---------------------------------------------------------------------------
# Operator-report rendering
# ---------------------------------------------------------------------------


_BANNER = "=== Decision pipeline live-SDK verification ==="


def _render_success_report(
    *,
    invocation_id: str,
    result: DecisionPipelineResult,
    verdict: Verdict,
    errors: tuple[str, ...],
    archive_path: Path,
) -> str:
    pm_output = result.pm_result.output
    lines = [
        _BANNER,
        f"invocation_id: {invocation_id}",
        "scenario: normal",
        "",
        "--- Stage results ---",
        f"analyst.recommendations: {len(result.analyst_result.output.recommendations or ())}",
        (
            "strategist.position_assessments: "
            f"{len(result.strategist_result.output.position_assessments)}"
        ),
        f"pm.envelopes_submitted: {pm_output.envelopes_submitted}",
        f"pm.submission_log: {len(result.pm_result.submission_log)} entry(s)",
        (
            f"pm.verdict_summary: approve={pm_output.verdict_summary.approve} "
            f"approve_with_modification={pm_output.verdict_summary.approve_with_modification} "
            f"reject={pm_output.verdict_summary.reject}"
        ),
        "",
        f"archive: {archive_path}",
    ]
    if errors:
        lines.extend(["", "--- Validation errors ---", *(f"  - {err}" for err in errors)])
    lines.extend(["", f"--- Verdict: {verdict.value} ---"])
    return "\n".join(lines) + "\n"


def _render_failure_report(
    *,
    invocation_id: str,
    failure: AnalystHarnessFailure | StrategistHarnessFailure | PMHarnessFailure | SDKFailure,
    verdict: Verdict,
) -> str:
    # All four caught HarnessFailure subclasses (analyst, strategist, PM,
    # plus the pre-flight SDKFailure) carry ``agent_name`` and ``invocation_id``
    # per the harness contract — read them directly without getattr fallbacks.
    return (
        "\n".join(
            [
                _BANNER,
                f"invocation_id: {invocation_id}",
                "scenario: normal",
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
    """Surface missing CLAUDE_CODE_OAUTH_TOKEN as a clean SDKFailure.

    The Claude Agent SDK authenticates via ``CLAUDE_CODE_OAUTH_TOKEN``
    (mirrors the per-agent verify scripts; the parent issue's reference
    to ``ANTHROPIC_API_KEY`` is a documentation drift — the SDK reads the
    OAuth token, not a raw API key).
    """
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise SDKFailure(
            "CLAUDE_CODE_OAUTH_TOKEN is not set in the environment. The "
            "real-SDK decision-pipeline verification cannot run without it. "
            "Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run.",
            # The pre-flight check runs before any agent dispatch, so the
            # which-agent-failed context is unknown — label as the pipeline.
            agent_name=_PIPELINE_LABEL,
            invocation_id=invocation_id,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _format_invocation_id(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-verify-decision-pipeline"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the decision-layer pipeline composition end-to-end against a "
            "real CLAUDE_CODE_OAUTH_TOKEN for a single normal-mode scenario. "
            "Builds a fixture-backed RepositoryFixture + in-process price "
            "provider, supplies a pre-recorded synthesizer text + retrieval "
            "store (live synth chaining is the trigger layer's job), invokes "
            "run_decision_pipeline, validates the returned "
            "DecisionPipelineResult, and writes the serialized result to "
            "<archive-root>/decision_pipeline/normal/result.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        required=True,
        help=(
            "Path to the verification-archive root. The script writes the "
            "serialized DecisionPipelineResult to "
            "<archive-root>/decision_pipeline/normal/result.json and threads "
            "the same root into the per-agent harness diagnostic archives."
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=("normal",),
        default="normal",
        help=(
            "Which scenario to run. Only 'normal' is supported in this "
            "story; halt and emergency are deferred (per parent decision G)."
        ),
    )
    return parser


async def _run_pipeline(
    *,
    invocation_id: str,
    archive_root: Path,
) -> DecisionPipelineResult:
    """Compose runner kwargs and invoke ``run_decision_pipeline``.

    Mirrors the orchestrator: assemble the snapshot once against the stub
    repository, then pass the pre-built :class:`AssembledSnapshot` plus
    the Phase 1 enforcement-composition inputs (repository, synthetic
    regime output, progressive tiers — story ALP-433) into the runner.
    """
    from alphamind.config.guardrails_helpers import (
        load_cumulative_drawdown_progressive_tiers,
    )
    from alphamind.portfolio_state.assembler import assemble_snapshot
    from alphamind.portfolio_state.consumers.synthesizer import adapt_ticker_sector_resolver

    repository = StubPortfolioStateRepository(build_fixture_repository(invocation_id=invocation_id))
    quotes = build_fixture_price_provider()
    price_provider = StubCurrentPriceProvider(quotes, _AS_OF)
    library_config = build_fixture_library_config()

    assembled = assemble_snapshot(
        repository=repository,
        price_provider=price_provider,
        sector_resolver=adapt_ticker_sector_resolver(_sector_resolver),
        config=_load_portfolio_state_config(),
        now=_AS_OF,
    )
    return await run_decision_pipeline(
        assembled_snapshot=assembled,
        repository=repository,
        regime_output=_build_verify_regime_output(invocation_id=invocation_id),
        progressive_tiers=load_cumulative_drawdown_progressive_tiers(),
        synthesizer_text=build_fixture_synthesizer_text(),
        retrieval_store=build_fixture_retrieval_store(),
        mode="normal",
        halt_state=None,
        agents_config=_load_decision_layer_agents(),
        agent_overrides={},
        sector_resolver=_sector_resolver,
        borrow_cost_resolver=None,
        library_config=library_config,
        library_market=build_fixture_market_inputs(),
        profile_feature_flags=library_config.feature_flags,
        state_delivery_config=build_fixture_state_delivery_config(),
        options_enabled=library_config.feature_flags.options_enabled,
        short_selling_enabled=library_config.feature_flags.short_selling_enabled,
        active_sectors=frozenset(library_config.active_sectors),
        invocation_id=invocation_id,
        timestamp=_AS_OF,
        archive_root=archive_root,
    )


def _build_verify_regime_output(*, invocation_id: str) -> Any:
    """Build a fixture ``RegimeAdaptationOutput`` for the verify-script run.

    Wraps :func:`_make_active_risk_parameters` (the same regime-resolved
    parameter set the stub repository returns) in a synthetic NORMAL-regime
    output so the pipeline's Phase 1 enforcement composition has a stable
    regime-side input. Drawdown is zero in the verify fixture, so the
    composition is a no-op and the parameter set passes through unchanged.
    """
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        RegimeAdaptationOutput,
        RegimeAdaptationState,
    )

    parameters = _make_active_risk_parameters()
    state = RegimeAdaptationState(
        as_of=_AS_OF.isoformat().replace("+00:00", "Z"),
        invocation_id=invocation_id,
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="normal",
        distillation_vix_level=18.0,
        regime_skip_emergency=False,
    )
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=state,
        audit_log_entries=(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS, 1 on FAIL."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    invocation_id = _format_invocation_id(now)

    try:
        _check_oauth_token_set(invocation_id)
    except SDKFailure as failure:
        print(
            _render_failure_report(
                invocation_id=invocation_id,
                failure=failure,
                verdict=Verdict.FAIL,
            )
        )
        return 1

    archive_root: Path = args.archive_root
    archive_root.mkdir(parents=True, exist_ok=True)
    result_path = archive_root / "decision_pipeline" / "normal" / "result.json"

    print(
        f"=== Decision pipeline live-SDK verification: invocation_id={invocation_id} ===\n"
        "Invoking SDK (analyst + strategist parallel, then pre-processor, then PM)...",
        flush=True,
    )

    try:
        result = asyncio.run(
            _run_pipeline(
                invocation_id=invocation_id,
                archive_root=archive_root,
            )
        )
    except (
        AnalystHarnessFailure,
        StrategistHarnessFailure,
        PMHarnessFailure,
    ) as failure:
        print(
            _render_failure_report(
                invocation_id=invocation_id,
                failure=failure,
                verdict=Verdict.FAIL,
            )
        )
        return 1

    # Serialize first so the archive lands even if validation raises; this
    # keeps FAIL debugging cheap by guaranteeing the operator has the full
    # result.json on disk before any validator-side surprise.
    serialize_pipeline_result(result, result_path)
    verdict, errors = validate_pipeline_result(result)
    print(
        _render_success_report(
            invocation_id=invocation_id,
            result=result,
            verdict=verdict,
            errors=errors,
            archive_path=result_path,
        )
    )
    return 0 if verdict is Verdict.PASS else 1


if __name__ == "__main__":
    sys.exit(main())
