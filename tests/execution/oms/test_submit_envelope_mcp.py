"""Tests for the submit_envelope MCP tool wrapper — story 06c (ALP-328).

Drives the engine-stub wrapper that accepts one PM envelope per call, runs
Layer-2/3 validation (story 06b), then re-runs validate_guardrail per embedded
command against cumulative state. Mirrors the analyst-side
test_validation_tool_mcp.py shape.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
    from alphamind.portfolio_state.records.positions import PositionRecord

import pytest

from alphamind._kernel.ids import (
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.command_models import (
    BracketOrderParameters,
    CapitalProtectionFloor,
    EntryOrder,
    EquityInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
)
from alphamind.commands.command_models import (
    ThesisComponent as OMSThesisComponent,
)
from alphamind.decision.portfolio_manager.models import (
    AddCommand,
    CancelCommand,
    CloseCommand,
    CriterionAssessment,
    OpenCommand,
    PMAnalystEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
)
from alphamind.decision.proposal_pre_processor.models import (
    AggregateObservations,
    AnalystSection,
    AnalystSideAnnotations,
    BasisSection,
    BookHealthSummary,
    ByRecommendedAction,
    ByThesisStatus,
    CombinedSetImpact,
    ConvictionDistribution,
    ConvictionHistogram,
    ProposalPreProcessorBundle,
    StrategistSection,
    StrategistSideAnnotations,
    WrappedPendingOrderAssessment,
    WrappedPositionAssessment,
    WrappedRecommendation,
)
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
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
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    ValidationToolState,
)
from alphamind.state.config import StatePersistenceConfig

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_PORTFOLIO_VALUE = 100_000.0
_DEFAULT_ACTIVE_SECTORS = frozenset({"tech", "semis", "financials", "energy"})


# ---------------------------------------------------------------------------
# Fixture builders — guardrail-side primitives
# ---------------------------------------------------------------------------


# Bypass-init helpers — replace Pydantic ``model_construct``. The dataclass __init__
# enforces all fields; these helpers skip validation so tests can inject sparse fixtures.


def _bypass_init_PortfolioManagerView(**kwargs: object) -> PortfolioManagerView:  # noqa: N802
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

    obj = object.__new__(PortfolioManagerView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _bypass_init_StrategistPositionView(**kwargs: object) -> StrategistPositionView:  # noqa: N802
    from alphamind.portfolio_state.consumers.strategist import StrategistPositionView

    obj = object.__new__(StrategistPositionView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _bypass_init_PositionRecord(**kwargs: object) -> PositionRecord:  # noqa: N802
    from alphamind.portfolio_state.records.positions import PositionRecord

    obj = object.__new__(PositionRecord)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _bypass_init_PositionView(**kwargs: object) -> Any:  # noqa: N802
    from alphamind.portfolio_state.views.positions import PositionView

    obj = object.__new__(PositionView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _bypass_init_OrderRecord(**kwargs: object) -> Any:  # noqa: N802
    from alphamind.portfolio_state.records.orders import OrderRecord

    obj = object.__new__(OrderRecord)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/test-submit-envelope-mcp",
            "invocation_provenance_root": "/tmp/test-submit-envelope-mcp",
        }
    )


def _config(
    *,
    sector_concentration_pct: float = 25.0,
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
) -> LibraryConfig:
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": sector_concentration_pct,
        "net_long_pct": 60.0,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=active_sectors,
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    sector_exposure_pct: Mapping[str, float] | None = None,
    cash_usd: float = 70_000.0,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {
            "tech": 18.3,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        }
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=30.0,
        net_short_pct=0.0,
        gross_pct=78.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def _atm_provider() -> FixtureIvProvider:
    return FixtureIvProvider(
        surface={
            "AAPL": IvSurfaceEntry(
                underlying=Symbol("AAPL"),
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_NOW.date(),
                        contract_type=ContractType.CALL,
                        implied_volatility=0.30,
                    ),
                ),
            ),
        },
        realized_vol={},
    )


def _market(underlyings: Sequence[str] = ("AAPL", "NVDA", "ABC", "XOM")) -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType({u: 100.0 for u in underlyings}),
        risk_free_rate=0.045,
        iv_provider=_atm_provider(),
        as_of=_NOW,
    )


def _risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _active_risk_parameters() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),
        active_overlays=(),
    )


_TICKER_TO_SECTOR: Mapping[str, str] = {
    "AAPL": "tech",
    "NVDA": "semis",
    "ABC": "tech",
    "XOM": "energy",
    "JPM": "financials",
}


def _sector_resolver(ticker: str) -> str:
    return _TICKER_TO_SECTOR.get(ticker, "tech")


def _make_validation_state(
    *,
    config: LibraryConfig | None = None,
    snapshot: PortfolioStateSnapshot | None = None,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
) -> ValidationToolState:
    cfg = config or _config()
    return ValidationToolState(
        invocation_id="inv-2026-05-05",
        starting_snapshot=snapshot or _snapshot(),
        starting_risk_budget=_risk_budget(),
        starting_active_risk_parameters=_active_risk_parameters(),
        profile_feature_flags=cfg.feature_flags,
        library_config=cfg,
        library_market=_market(),
        sector_resolver=_sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
        accumulated_deltas=(),
    )


# ---------------------------------------------------------------------------
# Fixture builders — PM-envelope-side primitives
# ---------------------------------------------------------------------------


def _pass(note: str | None = None) -> CriterionAssessment:
    return CriterionAssessment(status="pass", note=note)


def _thesis_eval_all_pass() -> ThesisQualityEvaluation:
    return ThesisQualityEvaluation(
        falsifiability=_pass(),
        sizing_proportionality=_pass(),
        portfolio_coherence=_pass(),
        timing_plausibility=_pass(),
        counterargument_consideration=_pass(),
    )


def _position_eval_all_pass() -> PositionActionEvaluation:
    return PositionActionEvaluation(
        status_classification_warrant=_pass(),
        action_status_alignment=_pass(),
        action_specific_justification=_pass(),
        portfolio_coherence=_pass(),
    )


def _open_command(
    underlying: str = "NVDA",
    *,
    dollar_value: float = 1_000.0,
    quantity: float = 1.0,
    entry_order: EntryOrder | None = None,
) -> OpenCommand:
    """Build a canonical full-shape OPEN command.

    Default sizing is small enough (1% of $100k portfolio at the fixture's
    limits) that the projected exposure stays under every per-rule headroom;
    callers exercising larger sizing pass ``dollar_value`` / ``quantity``
    explicitly. ``entry_order`` defaults to a market entry; pass a limit entry
    to exercise the capital-reservation path (a market entry reserves nothing —
    ALP-741).
    """
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=underlying, direction="long"),
        entry_order=entry_order or EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=quantity, dollar_value=money(dollar_value)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                trigger_signal="underlying_price",
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator="<=",
                    trigger_price=price(750.0),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary=f"Long {underlying}.",
            nature="directional",
            components=(
                OMSThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference=underlying,
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
    )


def _add_command(position_id: str = "POS-NVDA-001") -> AddCommand:
    """Build a canonical ADD command (no embedded instrument)."""
    return AddCommand(
        command_type="add",
        position_id=PositionId(position_id),
        additional_quantity=5.0,
        additional_dollar_value=money(5_000.0),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        thesis_addition_component=OMSThesisComponent(
            component_type="entry_rationale",
            linked_leg="add",
            instrument_reference="NVDA",
            narrative="Add to NVDA.",
            key_assumptions=("Setup intact.",),
        ),
        bracket_adjustment=None,
    )


def _close_command(
    *,
    position_id: str = "POS-NVDA-001",
    rationale_type: str = "thesis_invalidated",
) -> CloseCommand:
    return CloseCommand.model_validate(
        {
            "command_type": "close",
            "position_id": position_id,
            "quantity": "all",
            "order_type": "market",
            "limit_price": None,
            "close_rationale_type": rationale_type,
            "invalidation_reason": (
                "Thesis broken." if rationale_type == "thesis_invalidated" else None
            ),
            "risk_management_subtype": None,
        }
    )


def _cancel_command(order_id: str = "ORD-1") -> CancelCommand:
    return CancelCommand(command_type="cancel", order_id=OrderId(order_id), cancel_reason="stale")


def _make_analyst_envelope(**overrides: Any) -> PMAnalystEnvelope:
    defaults: dict[str, Any] = {
        "envelope_id": "ENV-REC-1",
        "invocation_id": "inv-2026-05-05",
        "source_provenance": "pm_analyst",
        "source_recommendation_id": "REC-1",
        "recommendation_type": "new_entry",
        "verdict": "approve",
        "evaluation": _thesis_eval_all_pass(),
        "modifications": (),
        "concerns": (),
        "rationale_narrative": "Analyst proposal aligns with the book.",
        "anti_patterns_identified": None,
        "commands": (_open_command(),),
    }
    return PMAnalystEnvelope(**(defaults | overrides))


def _make_strategist_envelope(**overrides: Any) -> PMStrategistEnvelope:
    defaults: dict[str, Any] = {
        "envelope_id": "ENV-SA-1",
        "invocation_id": "inv-2026-05-05",
        "source_provenance": "pm_strategist",
        "source_recommendation_id": "SA-1",
        "recommendation_type": "position_assessment",
        "position_id": "POS-NVDA-001",
        "verdict": "approve",
        "evaluation": _position_eval_all_pass(),
        "modifications": (),
        "concerns": (),
        "rationale_narrative": "Strategist position assessment is well-grounded.",
        "anti_patterns_identified": None,
        "commands": (_close_command(),),
    }
    return PMStrategistEnvelope(**(defaults | overrides))


# ---------------------------------------------------------------------------
# Fixture builders — pre-processor bundle / pm_view / retrieval store
# ---------------------------------------------------------------------------


def _retrieval_store(*ref_ids: str) -> RetrievalStore:
    entries = {ref_id: f"section text for {ref_id}" for ref_id in ref_ids}
    freshness = {BriefSource.SA_TECH: _NOW}
    return RetrievalStore(entries=entries, freshness_by_source=freshness)


def _empty_combined_set_impact() -> CombinedSetImpact:
    return CombinedSetImpact(
        basis=BasisSection(
            analyst_proposal_ids=(),
            strategist_action_ids=(),
            strategist_holds_excluded_count=0,
            snapshot_timestamp=_NOW,
        ),
        per_rule=(),
        breaches=(),
    )


def _empty_aggregate_observations() -> AggregateObservations:
    histogram = ConvictionHistogram.model_validate({"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
    by_thesis = ByThesisStatus.model_validate(
        {
            "on-track": 0,
            "partially-realized": 0,
            "at-risk": 0,
            "stale": 0,
            "invalidated": 0,
        }
    )
    by_action = ByRecommendedAction.model_validate(
        {"hold": 0, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
    )
    return AggregateObservations(
        combined_set_impact=_empty_combined_set_impact(),
        conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
        book_health_summary=BookHealthSummary(
            by_thesis_status=by_thesis,
            by_recommended_action=by_action,
            remedy_flagged_count=0,
            total=0,
        ),
    )


def _recommendation_stub(recommendation_id: str) -> WrappedRecommendation:
    from alphamind.decision.analyst.models import Recommendation

    rec = Recommendation.model_construct(recommendation_id=recommendation_id)
    return WrappedRecommendation.model_construct(
        recommendation=rec,
        pre_processor_annotations=AnalystSideAnnotations(conflicts=()),
    )


def _position_assessment_stub(assessment_id: str) -> WrappedPositionAssessment:
    from alphamind.decision.strategist.models import PositionAssessment

    pa = PositionAssessment.model_construct(assessment_id=assessment_id)
    return WrappedPositionAssessment.model_construct(
        assessment=pa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )


def _pending_order_assessment_stub(
    pending_order_assessment_id: str,
) -> WrappedPendingOrderAssessment:
    from alphamind.decision.strategist.models import PendingOrderAssessment

    poa = PendingOrderAssessment.model_construct(
        pending_order_assessment_id=pending_order_assessment_id
    )
    return WrappedPendingOrderAssessment.model_construct(
        pending_order_assessment=poa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )


def _make_bundle(
    *,
    recommendations: tuple[WrappedRecommendation, ...] = (),
    position_assessments: tuple[WrappedPositionAssessment, ...] = (),
    pending_order_assessments: tuple[WrappedPendingOrderAssessment, ...] = (),
) -> ProposalPreProcessorBundle:
    return ProposalPreProcessorBundle.model_construct(
        invocation_id="inv-2026-05-05",
        timestamp=_NOW,
        aggregate_observations=_empty_aggregate_observations(),
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=position_assessments,
            pending_order_assessments=pending_order_assessments,
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="normal", recommendations=recommendations, watchlist=None
        ),
    )


def _make_pm_view(positions: tuple[Any, ...] = ()) -> PortfolioManagerView:
    return _bypass_init_PortfolioManagerView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=None,
        drawdown=None,
        sector_exposure=(),
        directional_exposure=None,
        risk_budget=None,
        active_risk_parameters=None,
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=None,
        position_modification_trail={},
    )


_FIXTURE_THESIS_ID = "THE-NVDA-0123456789abcdef0123456789abcdef"
"""Default originating thesis FK the fixture positions / pending orders carry so
CLOSE / ADD / ADJUST / CANCEL resolve a real broker-carried link (ALP-844)."""


def _pending_order_for_cancel(order_id: str = "ORD-1") -> Any:
    """A minimal pending :class:`OrderRecord` the CANCEL fixture targets.

    Only the fields the order→thesis lookup reads are populated; the rest are
    left unset (the lookup never touches them)."""
    return _bypass_init_OrderRecord(
        order_id=order_id,
        originating_thesis_id=_FIXTURE_THESIS_ID,
    )


def _position_view(position_id: str, *, record: PositionRecord | None = None) -> Any:
    """Build a sparse :class:`StrategistPositionView` referencing *position_id*.

    The default record carries a ``thesis_id`` and a pending order (``ORD-1``)
    with an ``originating_thesis_id`` so CLOSE / ADD / ADJUST / CANCEL commands
    can resolve their broker-carried thesis FK (ALP-844). Callers needing a
    typed record (e.g., AddCommand instrument projections) pass one in.
    """
    inner_record = record or _bypass_init_PositionRecord(
        position_id=position_id, thesis_id=_FIXTURE_THESIS_ID
    )
    inner_view = _bypass_init_PositionView(record=inner_record)
    return _bypass_init_StrategistPositionView(
        position=inner_view,
        thesis=None,
        bracket=None,
        pending_orders=(_pending_order_for_cancel(),),
        modification_trail=(),
    )


# ---------------------------------------------------------------------------
# In-process MCP-server handler invocation helper
# ---------------------------------------------------------------------------


async def _invoke_mcp_tool(server: Any, tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Drive the in-process MCP server's tools/call handler."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=tool_name, arguments=args),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    content_blocks = result.root.content
    assert content_blocks, "tool returned no content blocks"
    block = content_blocks[0]
    text = block.text
    assert isinstance(text, str)
    return text, bool(result.root.isError)


# ---------------------------------------------------------------------------
# Helper: build the engine-stub state + factory output ergonomically
# ---------------------------------------------------------------------------


def _build_state_and_server(
    *,
    envelope_for_routing: PMAnalystEnvelope | PMStrategistEnvelope | None = None,
    config: LibraryConfig | None = None,
    snapshot: PortfolioStateSnapshot | None = None,
    halt_mode: bool = False,
    active_sectors: frozenset[str] = _DEFAULT_ACTIVE_SECTORS,
    extra_recommendations: tuple[WrappedRecommendation, ...] = (),
    extra_position_assessments: tuple[WrappedPositionAssessment, ...] = (),
    extra_pending_order_assessments: tuple[WrappedPendingOrderAssessment, ...] = (),
    extra_positions: tuple[Any, ...] = (),
    extra_ref_ids: tuple[str, ...] = (),
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
) -> tuple[Any, Any, Any]:
    """Construct a SubmitEnvelopeState + factory output for tests.

    Returns ``(get_state, mcp_server_instance, allowed_tools)``. Post-ALP-476
    the state cell is frozen; the closure rebinds the captured cell on each
    invocation, so callers must read the latest state via ``get_state()``
    rather than retaining a reference to the initial instance.

    Routes the bundle / pm_view based on the supplied envelope. ``ENV-REC-N``
    auto-populates an analyst recommendation; ``ENV-SA-N`` an analyst-side
    position assessment; ``ENV-SA-ORD-N`` a pending-order assessment.
    """
    from alphamind.decision.portfolio_manager.submit_envelope import (
        build_initial_submit_envelope_state,
        build_submit_envelope_mcp_server,
    )

    cfg = config or _config()
    validation_state = _make_validation_state(
        config=cfg,
        snapshot=snapshot,
        borrow_cost_resolver=borrow_cost_resolver,
    )

    # Default pre-processor records.
    recs: list[WrappedRecommendation] = list(extra_recommendations)
    poss: list[WrappedPositionAssessment] = list(extra_position_assessments)
    pendings: list[WrappedPendingOrderAssessment] = list(extra_pending_order_assessments)
    if envelope_for_routing is not None:
        sid = envelope_for_routing.source_recommendation_id
        if isinstance(envelope_for_routing, PMAnalystEnvelope):
            if not any(r.recommendation.recommendation_id == sid for r in recs):
                recs.append(_recommendation_stub(sid))
        else:
            if envelope_for_routing.recommendation_type == "position_assessment":
                if not any(p.assessment.assessment_id == sid for p in poss):
                    poss.append(_position_assessment_stub(sid))
            else:
                if not any(
                    p.pending_order_assessment.pending_order_assessment_id == sid for p in pendings
                ):
                    pendings.append(_pending_order_assessment_stub(sid))

    bundle = _make_bundle(
        recommendations=tuple(recs),
        position_assessments=tuple(poss),
        pending_order_assessments=tuple(pendings),
    )

    positions = list(extra_positions)
    if (
        envelope_for_routing is not None
        and envelope_for_routing.position_id is not None
        and not any(
            getattr(p.position, "position_id", None) == envelope_for_routing.position_id
            for p in positions
        )
    ):
        positions.append(_position_view(envelope_for_routing.position_id))

    pm_view = _make_pm_view(positions=tuple(positions))

    retrieval = _retrieval_store(*extra_ref_ids)

    state = build_initial_submit_envelope_state(
        invocation_id=validation_state.invocation_id,
        starting_validation_state=validation_state,
    )

    mcp_servers, allowed_tools, get_state = build_submit_envelope_mcp_server(
        state,
        retrieval_store=retrieval,
        pre_processor_bundle=bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=_sector_resolver,
        library_config=cfg,
        library_market=_market(),
        state_persistence_config=_state_persistence_config(),
    )
    server = mcp_servers["alphamind_execution_oms_submit"]["instance"]
    return get_state, server, allowed_tools


# ===========================================================================
# Tests
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Factory shape — returns (mcp_servers, allowed_tools)
# ---------------------------------------------------------------------------


def test_factory_returns_mcp_server_and_allowed_tools() -> None:
    """The factory returns a ``(mcp_servers, allowed_tools, get_state)`` triple
    with the canonical server name and a single registered tool."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        build_initial_submit_envelope_state,
        build_submit_envelope_mcp_server,
    )

    cfg = _config()
    validation_state = _make_validation_state(config=cfg)
    state = build_initial_submit_envelope_state(
        invocation_id="inv-2026-05-05",
        starting_validation_state=validation_state,
    )
    mcp_servers, allowed_tools, _get_state = build_submit_envelope_mcp_server(
        state,
        retrieval_store=_retrieval_store(),
        pre_processor_bundle=_make_bundle(),
        pm_view=_make_pm_view(),
        active_sectors=_DEFAULT_ACTIVE_SECTORS,
        halt_mode=False,
        sector_resolver=_sector_resolver,
        library_config=cfg,
        library_market=_market(),
        state_persistence_config=_state_persistence_config(),
    )

    assert "alphamind_execution_oms_submit" in mcp_servers
    server_config = mcp_servers["alphamind_execution_oms_submit"]
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_execution_oms_submit"

    assert tuple(allowed_tools) == ("mcp__alphamind_execution_oms_submit__submit_envelope",)


# ---------------------------------------------------------------------------
# 2. Accept a well-formed pm_analyst envelope with one valid OPEN command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepts_well_formed_envelope() -> None:
    """A well-formed pm_analyst envelope with one valid OPEN command is
    accepted; the response carries one submission_result with status=accepted
    and the cumulative-state cell is updated to count the proposal."""
    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-1"
    assert len(payload["submission_results"]) == 1
    result = payload["submission_results"][0]
    assert result["command_ordinal"] == 0
    assert result["status"] == "accepted"
    assert result["acknowledgment"] is not None
    assert result["rejection_payload"] is None

    # Cumulative state advanced — the next call would see proposal #2.
    assert len(get_state().validation_state.accumulated_deltas) == 1


# ---------------------------------------------------------------------------
# 3. Reject envelope with Layer-2 invariant violation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_envelope_with_layer_2_violation() -> None:
    """An envelope whose envelope_id integer doesn't match
    source_recommendation_id (Layer-2 bijection) is rejected; the response
    carries a single submission_result with rule=schema_invariant; the
    cumulative-state cell is NOT advanced; and the submission log records
    the rejection."""
    # envelope_id ENV-REC-1 vs source_recommendation_id REC-2 — bijection violation.
    envelope = _make_analyst_envelope(
        envelope_id="ENV-REC-1",
        source_recommendation_id="REC-2",
    )
    get_state, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-1"
    assert len(payload["submission_results"]) == 1
    result = payload["submission_results"][0]
    assert result["status"] == "rejected"
    assert result["acknowledgment"] is None
    rejection = result["rejection_payload"]
    assert rejection["rules_breached"][0]["rule"] == "schema_invariant"

    # State cell unchanged.
    assert len(get_state().validation_state.accumulated_deltas) == 0
    # Submission log captures the call.
    assert len(get_state().submission_log) == 1


# ---------------------------------------------------------------------------
# 4. Reject command breaching sector concentration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_command_breaching_sector_concentration() -> None:
    """An OPEN command that would breach sector concentration is rejected;
    the rejection_payload names ``sector_concentration_pct`` with current /
    limit / overage populated; the cumulative-state cell is NOT updated."""
    # Squeeze the sector_concentration limit so the token $1k OPEN tips it.
    cfg = _config(sector_concentration_pct=6.0)
    snapshot = _snapshot(
        sector_exposure_pct={
            "tech": 5.5,
            "semis": 5.5,  # OPEN of NVDA (semis) adds 1% → 6.5% > 6.0% limit.
            "financials": 0.0,
            "energy": 0.0,
        }
    )
    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(
        envelope_for_routing=envelope,
        config=cfg,
        snapshot=snapshot,
    )

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    assert len(payload["submission_results"]) == 1
    result = payload["submission_results"][0]
    assert result["status"] == "rejected"
    rejection = result["rejection_payload"]
    rule_names = {b["rule"] for b in rejection["rules_breached"]}
    assert "sector_concentration_semis" in rule_names
    breach = next(
        b for b in rejection["rules_breached"] if b["rule"] == "sector_concentration_semis"
    )
    assert breach["current"] == pytest.approx(5.5, abs=0.01)
    assert breach["limit"] == pytest.approx(6.0, abs=0.01)
    assert breach["overage"] > 0

    # State cell unchanged.
    assert len(get_state().validation_state.accumulated_deltas) == 0


@pytest.mark.asyncio
async def test_rejection_payload_surfaces_cumulative_projected_after() -> None:
    """ALP-743 (AC#3): a breached rule's ``projected_after`` carries the
    cumulative basis the overage was measured against — distinct from this
    command's single-command ``delta_adjusted_exposure``.

    Two $4k NVDA shorts (4% each) on a $100k book: the first PASSes
    (single_short_max cap 5%) and credits 4% to cumulative state; the second
    projects 8% and FAILs. Its rejection reports ``projected_after == 8.0``
    (cumulative) while ``delta_adjusted_exposure == -4000`` (single), so the PM
    can read the overage as stacked exposure rather than inventing a broker
    margin multiplier to reconcile the two figures.
    """
    cmd1 = _open_command(underlying=Symbol("NVDA"), dollar_value=4_000.0, quantity=20)
    cmd2 = _open_command(underlying=Symbol("NVDA"), dollar_value=4_000.0, quantity=20)
    envelope = _make_strategist_envelope(verdict="approve", commands=(cmd1, cmd2))
    raw = envelope.model_dump(mode="json")
    for command in raw["commands"]:
        command["instrument"]["direction"] = "short"

    _get_state, server, _ = _build_state_and_server(
        envelope_for_routing=envelope,
        borrow_cost_resolver=lambda _ticker: 0.5,
    )

    text, is_error = await _invoke_mcp_tool(server, "submit_envelope", raw)
    assert not is_error, text

    results = json.loads(text)["submission_results"]
    assert results[0]["status"] == "accepted"
    assert results[1]["status"] == "rejected"
    rejection = results[1]["rejection_payload"]
    breach = next(b for b in rejection["rules_breached"] if b["rule"] == "single_short_max_pct")
    assert breach["projected_after"] == pytest.approx(8.0, abs=0.01)
    assert breach["overage"] == pytest.approx(3.0, abs=0.01)
    # The single-command delta is half the cumulative basis — the figure that,
    # absent projected_after, looked inconsistent with the overage.
    assert rejection["delta_adjusted_exposure"] == pytest.approx(-4_000.0, abs=0.01)


# ---------------------------------------------------------------------------
# 5. Multiple commands with partial rejection — middle command rejects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_processes_multiple_commands_with_partial_rejection() -> None:
    """A strategist envelope with three commands where command 1 passes,
    command 2 rejects (sector breach), and command 3 sees only command 1's
    cumulative impact in the validation tool's accumulated_deltas tracking."""
    cfg = _config(sector_concentration_pct=8.0)
    snapshot = _snapshot(
        sector_exposure_pct={
            "tech": 6.5,
            "semis": 0.0,
            "financials": 0.0,
            "energy": 0.0,
        }
    )
    # Command 1: tech OPEN — projected after = 6.5 + 1.0 = 7.5%, PASS.
    # Command 2: tech OPEN — would push to 8.5%, FAIL on sector_concentration.
    # Command 3: energy OPEN — XOM → energy sector at 0% baseline → PASS.
    cmd1 = _open_command(underlying=Symbol("ABC"))
    cmd2 = _open_command(underlying=Symbol("ABC"))
    cmd3 = _open_command(underlying=Symbol("XOM"))

    envelope = _make_strategist_envelope(
        verdict="approve",
        commands=(cmd1, cmd2, cmd3),
    )
    get_state, server, _ = _build_state_and_server(
        envelope_for_routing=envelope,
        config=cfg,
        snapshot=snapshot,
    )

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    results = payload["submission_results"]
    assert len(results) == 3
    assert [r["command_ordinal"] for r in results] == [0, 1, 2]
    assert results[0]["status"] == "accepted"
    assert results[1]["status"] == "rejected"
    assert results[2]["status"] == "accepted"

    # State cell advanced exactly twice — once per accepted constructive
    # command (cmd1 + cmd3).
    assert len(get_state().validation_state.accumulated_deltas) == 2


# ---------------------------------------------------------------------------
# 6. CLOSE / CANCEL commands skip rule projection but pass through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_commands_skip_validation_pass_through() -> None:
    """A strategist envelope carrying a CLOSE and a CANCEL is accepted —
    neither command produces a projected exposure delta, so they pass through
    to ``accepted`` without advancing the cumulative-state cell."""
    cfg = _config(sector_concentration_pct=6.0)
    snapshot = _snapshot(
        sector_exposure_pct={
            # Already over the limit, so any constructive proposal would FAIL.
            # CLOSE / CANCEL pass through regardless.
            "tech": 7.0,
            "semis": 7.0,
            "financials": 0.0,
            "energy": 0.0,
        }
    )
    envelope = _make_strategist_envelope(
        verdict="approve",
        commands=(_close_command(), _cancel_command()),
    )
    get_state, server, _ = _build_state_and_server(
        envelope_for_routing=envelope,
        config=cfg,
        snapshot=snapshot,
    )

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    results = payload["submission_results"]
    assert len(results) == 2
    assert all(r["status"] == "accepted" for r in results)
    # No cumulative-state advancement (CLOSE/CANCEL produce no projected delta).
    assert len(get_state().validation_state.accumulated_deltas) == 0


# ---------------------------------------------------------------------------
# 7. Halt mode rejects an envelope with an OPEN command (Layer-2 invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halt_mode_rejects_open_command() -> None:
    """With ``halt_mode=True``, an envelope carrying an OPEN command violates
    the halt-mode invariant in :func:`validate_pm_envelope`; the response is
    a single envelope-level rejection with rule=schema_invariant."""
    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(
        envelope_for_routing=envelope,
        halt_mode=True,
    )

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    results = payload["submission_results"]
    assert len(results) == 1
    assert results[0]["status"] == "rejected"
    assert results[0]["rejection_payload"]["rules_breached"][0]["rule"] == "schema_invariant"
    assert "halt_mode" in results[0]["rejection_payload"]["suggested_modification"].lower()
    # No state-cell advancement.
    assert len(get_state().validation_state.accumulated_deltas) == 0


# ---------------------------------------------------------------------------
# 8. Submission log captures every call (accepts and rejects)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submission_log_captures_every_call() -> None:
    """Calling submit_envelope twice — once accepted, once rejected — appends
    two entries to ``state.submission_log`` in call order."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_submission_log

    cfg = _config()

    # Build state once and reuse for two calls.
    get_state, server, _ = _build_state_and_server(
        config=cfg,
        # Pre-route both envelopes' provenance.
        extra_recommendations=(
            _recommendation_stub("REC-1"),
            _recommendation_stub("REC-2"),
        ),
    )
    accepted_env = _make_analyst_envelope(envelope_id="ENV-REC-1", source_recommendation_id="REC-1")
    rejected_env = _make_analyst_envelope(
        envelope_id="ENV-REC-2",
        source_recommendation_id="REC-3",  # mismatched: REC-3 vs ENV-REC-2.
    )

    text1, _ = await _invoke_mcp_tool(
        server, "submit_envelope", accepted_env.model_dump(mode="json")
    )
    payload1 = json.loads(text1)
    assert payload1["submission_results"][0]["status"] == "accepted"

    text2, _ = await _invoke_mcp_tool(
        server, "submit_envelope", rejected_env.model_dump(mode="json")
    )
    payload2 = json.loads(text2)
    assert payload2["submission_results"][0]["status"] == "rejected"

    log = get_submission_log(get_state())
    assert len(log) == 2
    assert log[0].envelope.envelope_id == "ENV-REC-1"
    assert log[1].envelope.envelope_id == "ENV-REC-2"


# ---------------------------------------------------------------------------
# 9. State cell isolation — two distinct states do not leak across
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_cell_isolation() -> None:
    """Two distinct SubmitEnvelopeState instances + two distinct factories
    must keep their cumulative state independent — submitting to one does
    not affect the other."""
    envelope = _make_analyst_envelope()

    get_state_a, server_a, _ = _build_state_and_server(envelope_for_routing=envelope)
    get_state_b, _server_b, _ = _build_state_and_server(envelope_for_routing=envelope)

    text, _ = await _invoke_mcp_tool(server_a, "submit_envelope", envelope.model_dump(mode="json"))
    assert json.loads(text)["submission_results"][0]["status"] == "accepted"

    # State A advanced; state B untouched.
    assert len(get_state_a().validation_state.accumulated_deltas) == 1
    assert len(get_state_b().validation_state.accumulated_deltas) == 0
    assert len(get_state_a().submission_log) == 1
    assert len(get_state_b().submission_log) == 0


# ---------------------------------------------------------------------------
# 10. Synthetic command_id format matches OMS regex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_command_id_format() -> None:
    """An accepted command's command_id is a structurally-valid PM-originated
    id carrying the broker-carried thesis FK (ALP-844) — it parses cleanly and
    its thesis round-trips to the OPEN's deterministic ``THE-NVDA-{suffix}``."""
    from alphamind.execution.oms.command_ids import (
        is_pm_originated,
        parse_pm_command_id,
        synthesize_id_suffix,
    )

    envelope = _make_analyst_envelope()
    _, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    text, _ = await _invoke_mcp_tool(server, "submit_envelope", envelope.model_dump(mode="json"))
    payload = json.loads(text)
    cmd_id = payload["submission_results"][0]["command_id"]

    assert is_pm_originated(cmd_id), f"command_id {cmd_id!r} is not a valid PM-originated id"
    components = parse_pm_command_id(cmd_id)
    assert components.envelope_id == "ENV-REC-1"
    assert components.thesis_id == f"THE-NVDA-{synthesize_id_suffix(cmd_id)}"


# ---------------------------------------------------------------------------
# 11. Layer-1 (Pydantic) parse failure captured in failed_submission_log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer_1_parse_failure_captured_in_failed_submission_log() -> None:
    """A payload that fails Pydantic discriminated-union parsing (Layer-1) is
    rejected at the envelope level AND captured in
    ``state.failed_submission_log`` — raw args, the formatted Pydantic error,
    and the synthetic command_id are all preserved for forensics. The parsed
    ``submission_log`` remains empty since no PMEnvelope was produced."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_failed_submission_log

    get_state, server, _ = _build_state_and_server()

    # Missing the source_provenance discriminator — the discriminated-union
    # adapter cannot route the payload to either PMAnalystEnvelope or
    # PMStrategistEnvelope, so Layer-1 parsing raises ValidationError before
    # any semantic validation runs.
    bogus_args: dict[str, Any] = {
        "envelope_id": "ENV-REC-99",
        "garbage": "value",
    }

    text, is_error = await _invoke_mcp_tool(server, "submit_envelope", bogus_args)
    assert not is_error, text

    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-99"
    assert len(payload["submission_results"]) == 1
    result = payload["submission_results"][0]
    assert result["status"] == "rejected"
    assert result["rejection_payload"]["rules_breached"][0]["rule"] == "schema_invariant"

    failed_log = get_failed_submission_log(get_state())
    assert len(failed_log) == 1
    entry = failed_log[0]
    assert entry.raw_args == bogus_args
    assert "source_provenance" in entry.validation_error_repr
    # A rejected / malformed envelope is not a real order and must NOT route
    # through the thesis-required order-derivation contract (ALP-844). The
    # forensic id uses its own ``ENV-REC-INVALID.…`` synthetic scheme that is
    # deliberately NOT a parseable PM/engine command id.
    from alphamind.execution.oms.command_ids import is_engine_originated, is_pm_originated

    pattern = re.compile(r"^ENV-REC-INVALID\.inv-[A-Za-z0-9_\-:]+\.ENV-REC-99\.0\.0$")
    assert pattern.match(entry.command_id), (
        f"command_id {entry.command_id!r} does not match expected forensic format"
    )
    assert not is_pm_originated(entry.command_id)
    assert not is_engine_originated(entry.command_id)
    # Parsed submission_log untouched — Layer-1 failures don't reach there.
    assert len(get_state().submission_log) == 0
    # State cell unchanged.
    assert len(get_state().validation_state.accumulated_deltas) == 0


@pytest.mark.asyncio
async def test_layer_1_no_longer_rejects_short_equity_open_command() -> None:
    """ALP-717: a SHORT equity OPEN command no longer trips Layer-1 schema
    validation. The OMS-boundary guard from ALP-644 was retired once Phase 1
    grew direction-aware fill integration (Story 02) and the four short-only
    fields on ``EquityPositionDetails`` (Story 01). The envelope parses
    cleanly through Layer-1 and reaches Layer-2/3 — when routing + risk
    invariants are satisfied (as in this fixture, which wires the analyst
    envelope into the pre-processor bundle), the command is accepted; the
    cumulative state advances; and neither the failed_submission_log
    (Layer-1 forensics) nor a Layer-1 short-equity rejection appears."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_failed_submission_log

    # Build a fully-shaped LONG-equity envelope, then flip the embedded OPEN
    # command's instrument direction to SHORT — exercises Layer-1 parsing
    # from the wire-format edge. Wire the envelope into the routing fixture
    # so it reaches Layer-2/3 cleanly (no fabricated routing rejection); wire
    # a borrow_cost_resolver so the SHORT-equity guardrail (ALP-712) has the
    # data it needs to validate.
    envelope = _make_analyst_envelope()
    envelope_dict = envelope.model_dump(mode="json")
    envelope_dict["commands"][0]["instrument"]["direction"] = "short"

    get_state, server, _ = _build_state_and_server(
        envelope_for_routing=envelope,
        borrow_cost_resolver=lambda _ticker: 0.50,
    )

    text, is_error = await _invoke_mcp_tool(server, "submit_envelope", envelope_dict)
    assert not is_error, text

    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-1"
    assert len(payload["submission_results"]) == 1
    result = payload["submission_results"][0]

    # Unconditional: the new contract says SHORT EQUITY must reach Layer-2/3
    # cleanly. With routing wired, the command is accepted end-to-end.
    assert result["status"] == "accepted", (
        f"Expected SHORT EQUITY OPEN to be accepted post-ALP-717; "
        f"got status={result['status']!r}, rejection={result.get('rejection_payload')!r}"
    )
    assert result["acknowledgment"] is not None
    assert result["rejection_payload"] is None

    # Layer-1 forensics log MUST be empty — no Layer-1 short-equity rejection
    # surfaces (the guard was retired). This is the load-bearing invariant
    # the original ALP-644 guard would have violated.
    failed_log = get_failed_submission_log(get_state())
    assert len(failed_log) == 0, (
        f"Expected zero Layer-1 failures; got {[e.validation_error_repr for e in failed_log]!r}"
    )

    # Parsed submission_log records the call (one accepted submission).
    assert len(get_state().submission_log) == 1
    # Cumulative state advances — the SHORT proposal reached the
    # validation cell, exactly as a LONG would.
    assert len(get_state().validation_state.accumulated_deltas) == 1


@pytest.mark.asyncio
async def test_layer_1_failure_uses_fallback_envelope_id_when_missing() -> None:
    """When the raw payload omits ``envelope_id`` entirely, the rejection and
    the failed_submission_log both fall back to ``ENV-REC-INVALID`` so the
    synthetic command_id is still well-formed for downstream tooling."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_failed_submission_log

    get_state, server, _ = _build_state_and_server()

    bogus_args: dict[str, Any] = {"garbage": "value"}

    text, _ = await _invoke_mcp_tool(server, "submit_envelope", bogus_args)
    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-INVALID"

    failed_log = get_failed_submission_log(get_state())
    assert len(failed_log) == 1
    assert failed_log[0].raw_args == bogus_args
    assert failed_log[0].command_id.endswith(".ENV-REC-INVALID.0.0")


# ---------------------------------------------------------------------------
# 11b. Layer-0 tolerant envelope-wrapper unwrap (ALP-700)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer_0_unwraps_envelope_wrapper_key() -> None:
    """ALP-700: when the LLM wraps the envelope under a single top-level
    ``envelope`` key (e.g. ``submit_envelope({"envelope": {...}})``) instead
    of inlining the envelope fields, the wrapper transparently unwraps so
    the discriminated-union parse succeeds — the PM agent's contract
    self-discovery loop (~3 wasted tool calls per invocation) collapses
    to a single accepted submission."""
    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    wrapped_args: dict[str, Any] = {"envelope": envelope.model_dump(mode="json")}

    text, is_error = await _invoke_mcp_tool(server, "submit_envelope", wrapped_args)
    assert not is_error, text

    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-1"
    assert len(payload["submission_results"]) == 1
    result = payload["submission_results"][0]
    assert result["status"] == "accepted"
    assert result["acknowledgment"] is not None

    # Cumulative state advanced exactly as if the envelope had been submitted
    # inlined — the unwrap is a Layer-0 step, not a parallel code path.
    assert len(get_state().validation_state.accumulated_deltas) == 1


@pytest.mark.asyncio
async def test_layer_0_unwrap_preserves_inner_envelope_id_on_invalid_payload() -> None:
    """ALP-700: when the wrapped form's inner envelope still fails Pydantic
    parsing (e.g. an internal field has the wrong shape), the rejection's
    synthetic command_id carries the inner ``envelope_id`` rather than the
    legacy ``ENV-REC-INVALID`` fallback — AND the failure log preserves
    the literal pre-unwrap payload so an operator can still tell that the
    LLM mistakenly wrapped its call. Verifies the unwrap path threads the
    inner dict through validation while keeping forensics honest."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_failed_submission_log

    get_state, server, _ = _build_state_and_server()

    # Inner envelope shape carries a recognizable envelope_id but is missing
    # source_provenance — the discriminated-union parse still fails, but the
    # failure log should now surface the inner envelope_id rather than
    # falling back to "ENV-REC-INVALID".
    wrapped_bogus: dict[str, Any] = {
        "envelope": {
            "envelope_id": "ENV-REC-7",
            "garbage": "value",
        },
    }

    text, _ = await _invoke_mcp_tool(server, "submit_envelope", wrapped_bogus)
    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-7"

    failed_log = get_failed_submission_log(get_state())
    assert len(failed_log) == 1
    assert failed_log[0].command_id.endswith(".ENV-REC-7.0.0")
    # Forensic fidelity — raw_args records the wrapped form the LLM actually
    # sent, not the post-unwrap inner dict.
    assert failed_log[0].raw_args == wrapped_bogus


@pytest.mark.asyncio
async def test_layer_0_unwrap_skipped_with_extra_top_level_keys() -> None:
    """ALP-700: the unwrap fires only when ``envelope`` is the sole top-level
    key. A payload with both an ``envelope`` wrapper AND sibling fields is
    not a recognized LLM shape; it falls through to the normal Layer-1
    failure path so the structural error is surfaced rather than masked."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_failed_submission_log

    get_state, server, _ = _build_state_and_server()

    envelope = _make_analyst_envelope()
    extra_keyed: dict[str, Any] = {
        "envelope": envelope.model_dump(mode="json"),
        "stray": "value",
    }

    text, _ = await _invoke_mcp_tool(server, "submit_envelope", extra_keyed)
    payload = json.loads(text)
    # No unwrap: top-level lacks source_provenance, so the discriminated-union
    # parse fails at Layer-1 and the rejection surfaces with the fallback
    # envelope_id (no top-level envelope_id available either).
    assert payload["envelope_id"] == "ENV-REC-INVALID"
    failed_log = get_failed_submission_log(get_state())
    assert len(failed_log) == 1
    assert failed_log[0].raw_args == extra_keyed


@pytest.mark.asyncio
async def test_layer_0_unwrap_skipped_when_envelope_value_not_dict() -> None:
    """ALP-700: a top-level ``envelope`` whose value is not a dict (e.g. a
    string) cannot plausibly contain a discriminator field; the unwrap is
    skipped and the input falls through to the normal Layer-1 failure path
    so the original error message is preserved."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_failed_submission_log

    get_state, server, _ = _build_state_and_server()

    non_dict_envelope: dict[str, Any] = {"envelope": "not-a-dict"}

    text, _ = await _invoke_mcp_tool(server, "submit_envelope", non_dict_envelope)
    payload = json.loads(text)
    assert payload["envelope_id"] == "ENV-REC-INVALID"
    failed_log = get_failed_submission_log(get_state())
    assert len(failed_log) == 1
    assert failed_log[0].raw_args == non_dict_envelope


# ---------------------------------------------------------------------------
# 11c. Layer-0.5 analyst-only-field strip (ALP-736)
# ---------------------------------------------------------------------------


def _inject_analyst_only_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Mutate a dumped analyst envelope's OPEN command to carry the analyst-only
    fields the PM copies verbatim from the analyst Recommendation — the real MRVL
    ``inv-20260528T080000Z`` failure shape (``delta_adjusted_exposure`` +
    ``pct_of_portfolio`` + per-leg ``leg_id`` + an ``entry_window`` block) plus
    the analyst ``Target.dollar_pl_target`` that has no command slot."""
    command = raw["commands"][0]
    command["position_size"]["delta_adjusted_exposure"] = 2000
    command["position_size"]["pct_of_portfolio"] = 1.0
    command["target"]["dollar_pl_target"] = 500
    for leg_index, leg in enumerate(command["invalidation_legs"]):
        leg["leg_id"] = f"INV-{leg_index + 1}"
    command["entry_window"] = {
        "deadline": "2026-05-28T19:55:00Z",
        "decay_type": "gradual",
        "rationale": "Edge attenuates with time per [SA-TECH-TC-1].",
    }
    return raw


@pytest.mark.asyncio
async def test_layer_0_5_strips_analyst_only_fields_so_approved_open_is_accepted() -> None:
    """ALP-736: an OPEN command carrying analyst-only ``delta_adjusted_exposure``
    and ``leg_id`` fields — which previously rejected with ``extra_forbidden``
    and silently lost the PM-approved trade — is now normalized at the parse
    boundary and accepted. The MRVL 2026-05-28 loss no longer reproduces."""
    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    raw_args = _inject_analyst_only_fields(envelope.model_dump(mode="json"))

    text, is_error = await _invoke_mcp_tool(server, "submit_envelope", raw_args)
    assert not is_error, text

    payload = json.loads(text)
    assert payload["submission_results"][0]["status"] == "accepted"
    # The recommendation reached the broker path (cumulative state advanced),
    # not the failed_submission_log.
    assert len(get_state().validation_state.accumulated_deltas) == 1


@pytest.mark.asyncio
async def test_layer_0_5_strip_threads_entry_window_through_to_the_command() -> None:
    """ALP-736 / ALP-737: ``entry_window`` is preserved (not stripped) through
    normalization, so the parsed command carries the analyst's deadline into the
    submission log on its way to the bracket's ``entry_window_deadline``."""
    from alphamind.decision.portfolio_manager.submit_envelope import get_submission_log

    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    raw_args = _inject_analyst_only_fields(envelope.model_dump(mode="json"))

    _text, is_error = await _invoke_mcp_tool(server, "submit_envelope", raw_args)
    assert not is_error

    logged = get_submission_log(get_state())[-1].envelope
    open_command = logged.commands[0]
    assert isinstance(open_command, OpenCommand)
    assert open_command.entry_window is not None
    assert open_command.entry_window.deadline == datetime(2026, 5, 28, 19, 55, tzinfo=UTC)
    assert open_command.entry_window.decay_type == "gradual"


@pytest.mark.asyncio
async def test_layer_1_parse_failure_is_logged_for_operator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ALP-736: a residual Layer-1 parse failure (one normalization can't
    repair) is surfaced to the operator log as a WARNING — not buried in
    ``failed_submission_log.json``. The production scheduler path defers the
    activity-log writeback, so this WARNING is the signal that reaches
    ``collector.log`` regardless of the writeback gate."""
    _get_state, server, _ = _build_state_and_server()

    bogus_args: dict[str, Any] = {"garbage": "value"}

    with caplog.at_level(
        logging.WARNING,
        logger="alphamind.decision.portfolio_manager.submit_envelope.server",
    ):
        _text, _is_error = await _invoke_mcp_tool(server, "submit_envelope", bogus_args)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Layer-1 parse failure" in r.getMessage() for r in warnings), [
        r.getMessage() for r in warnings
    ]


# ---------------------------------------------------------------------------
# 12. SQL writeback — opt-in via injected InvocationHandle (ALP-366)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_submit_envelope_persists_layer1_failure_via_command_execution(
    tmp_path: Any,
) -> None:
    """When ``invocation_handle`` is supplied AND Layer-1 fails, the
    in-memory failed_submission_log AND an envelope_parse_failed activity log
    entry are both written. Mirrors the command_execution write-path test surface so
    an OMS-tree change touching the wrapper trips here too."""
    from sqlalchemy import select as _select

    import alphamind.state.tables  # noqa: F401
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.persistence.models import Base
    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
        make_engine,
    )
    from alphamind.portfolio_state.events.activity_log import EventType
    from alphamind.state.invocation_context.context import (
        InvocationContext,
    )
    from alphamind.state.invocation_context.records import (
        InvocationRecord,
        ProcessLifetimeRecord,
        process_lifetime_record_to_row,
    )
    from alphamind.state.tables.activity_log import (
        ActivityLogRow,
    )

    db_path = tmp_path / "test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        # Seed substrate (process + invocation row).
        proc = ProcessLifetimeRecord(
            process_lifetime_id="proc-1",
            process_role="pipeline",
            process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
            process_pid=123,
            hostname="host",
            git_sha="a" * 40,
            git_branch="main",
            git_dirty=False,
            python_version="3.13.1",
            pip_freeze_hash="0" * 64,
            pip_freeze_snapshot_path="/tmp/p.txt",
            anthropic_sdk_version="0.40.0",
            claude_agent_sdk_version="0.1.69",
            os_release="Linux-6.5.0",
        )
        async with factory() as sess:
            sess.add(process_lifetime_record_to_row(proc))
            await sess.commit()

        # Open an InvocationContext so the handle has a transaction.
        inv_record = InvocationRecord(
            invocation_id="inv-alp366-fail",
            process_lifetime_id="proc-1",
            start_at=_NOW.isoformat().replace("+00:00", "Z"),
            fill_collection_completed_at=None,
            command_execution_completed_at=None,
            trigger_type="scheduled",
            trigger_source="cron",
            trigger_reason="0 9 * * 1-5",
            git_sha_at_invocation="a" * 40,
            active_profile="medium",
            active_regime="normal",
            active_mode="normal",
            active_overlays_json="[]",
            resolved_config_hash="0" * 64,
            resolved_config_snapshot_path="/tmp/r.json",
            feature_flags_snapshot_json="{}",
            data_calibration_state_snapshot_path="/tmp/c.json",
            data_source_freshness_json="{}",
            fill_collection_summary_json=None,
            command_execution_summary_json=None,
            staleness_flag=None,
            snapshot_metadata_json=None,
        )
        ctx = InvocationContext(session_factory=factory, record=inv_record)
        handle = await ctx.__aenter__()

        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id="inv-alp366-fail",
            starting_validation_state=validation_state,
        )

        bogus_args: dict[str, Any] = {"envelope_id": "ENV-REC-99", "garbage": "value"}
        _response, state = await _handle_submit_envelope(
            bogus_args,
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=_make_bundle(),
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_state_persistence_config(),
            invocation_handle=handle,
        )
        await ctx.__aexit__(None, None, None)

        assert len(state.failed_submission_log) == 1
        async with factory() as sess:
            log_rows = (
                (
                    await sess.execute(
                        _select(ActivityLogRow).where(
                            ActivityLogRow.invocation_id == "inv-alp366-fail"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert any(r.event_type == EventType.ENVELOPE_PARSE_FAILED.value for r in log_rows)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_handle_submit_envelope_persists_accepted_envelope_via_command_execution(
    tmp_path: Any,
) -> None:
    """When ``invocation_handle`` is supplied AND the envelope is accepted,
    the command_execution writeback runs alongside the in-memory state-cell advance."""
    from sqlalchemy import select as _select

    import alphamind.state.tables  # noqa: F401
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.persistence.models import Base
    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
        make_engine,
    )
    from alphamind.portfolio_state.events.activity_log import EventType
    from alphamind.portfolio_state.records.cash import CashLedger
    from alphamind.state.invocation_context.context import (
        InvocationContext,
    )
    from alphamind.state.invocation_context.records import (
        InvocationRecord,
        ProcessLifetimeRecord,
        process_lifetime_record_to_row,
    )
    from alphamind.state.tables.activity_log import (
        ActivityLogRow,
    )
    from alphamind.state.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )
    from alphamind.state.tables.cash_ledger_codec import (
        cash_ledger_record_to_row,
    )

    db_path = tmp_path / "test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        # Seed process lifetime + cash ledger.
        proc = ProcessLifetimeRecord(
            process_lifetime_id="proc-1",
            process_role="pipeline",
            process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
            process_pid=123,
            hostname="host",
            git_sha="a" * 40,
            git_branch="main",
            git_dirty=False,
            python_version="3.13.1",
            pip_freeze_hash="0" * 64,
            pip_freeze_snapshot_path="/tmp/p.txt",
            anthropic_sdk_version="0.40.0",
            claude_agent_sdk_version="0.1.69",
            os_release="Linux-6.5.0",
        )
        cash = CashLedger(
            current_cash_usd=100_000.0,
            settled_cash_usd=100_000.0,
            reserved_capital_usd=0.0,
            available_buying_power_usd=100_000.0,
            margin_held_usd=0.0,
            unsettled_proceeds=(),
            cash_pct_of_portfolio=0.0,
            true_deployable_capital_usd=0.0,
            regt_excess_trailing_30d_usd=0.0,
            regt_excess_trailing_90d_usd=0.0,
            regt_excess_lifetime_usd=0.0,
        )
        async with factory() as sess:
            sess.add(process_lifetime_record_to_row(proc))
            await sess.flush()
            sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
            await sess.commit()

        inv_record = InvocationRecord(
            invocation_id="inv-alp366-ok",
            process_lifetime_id="proc-1",
            start_at=_NOW.isoformat().replace("+00:00", "Z"),
            fill_collection_completed_at=None,
            command_execution_completed_at=None,
            trigger_type="scheduled",
            trigger_source="cron",
            trigger_reason="0 9 * * 1-5",
            git_sha_at_invocation="a" * 40,
            active_profile="medium",
            active_regime="normal",
            active_mode="normal",
            active_overlays_json="[]",
            resolved_config_hash="0" * 64,
            resolved_config_snapshot_path="/tmp/r.json",
            feature_flags_snapshot_json="{}",
            data_calibration_state_snapshot_path="/tmp/c.json",
            data_source_freshness_json="{}",
            fill_collection_summary_json=None,
            command_execution_summary_json=None,
            staleness_flag=None,
            snapshot_metadata_json=None,
        )
        ctx = InvocationContext(session_factory=factory, record=inv_record)
        handle = await ctx.__aenter__()

        # A non-marketable LIMIT entry reserves capital ($1000 * 1 = $1000);
        # a market entry would reserve nothing under the notional basis (ALP-741).
        envelope = _make_analyst_envelope(
            commands=(
                _open_command(
                    entry_order=EntryOrder(type="limit", limit_price=price(1000.0), stop_price=None)
                ),
            )
        )
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-1"),))

        _response, _state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=bundle,
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_state_persistence_config(),
            invocation_handle=handle,
        )
        await ctx.__aexit__(None, None, None)

        async with factory() as sess:
            log_rows = (
                (
                    await sess.execute(
                        _select(ActivityLogRow).where(
                            ActivityLogRow.invocation_id == "inv-alp366-ok"
                        )
                    )
                )
                .scalars()
                .all()
            )
            cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        types = {r.event_type for r in log_rows}
        assert EventType.PM_DECISION.value in types
        assert EventType.ORDER_SUBMITTED.value in types
        assert EventType.THESIS_CREATED.value in types
        assert EventType.CAPITAL_RESERVED.value in types
        assert cash_row is not None
        assert cash_row.reserved_capital_usd > 0.0
    finally:
        await async_engine.dispose()


# ---------------------------------------------------------------------------
# 14. SubmitEnvelopeState rejects empty invocation_id
# ---------------------------------------------------------------------------


def test_submit_envelope_state_rejects_empty_invocation_id() -> None:
    """An empty ``invocation_id`` would propagate into ``derive_pm_command_id``
    and produce malformed IDs like ``inv-.{envelope_id}.0.0``; the constructor
    must reject it so the constraint is enforced at the type system."""
    from alphamind.decision.portfolio_manager.submit_envelope import SubmitEnvelopeState

    cfg = _config()
    validation_state = _make_validation_state(config=cfg)

    with pytest.raises(ValueError, match="invocation_id"):
        SubmitEnvelopeState(validation_state=validation_state, invocation_id="")


def test_build_initial_submit_envelope_state_rejects_empty_invocation_id() -> None:
    """The public assembler must propagate the empty-string rejection so
    composition pipelines that forget to thread the invocation id fail loudly
    at construction rather than silently producing malformed command IDs."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        build_initial_submit_envelope_state,
    )

    cfg = _config()
    validation_state = _make_validation_state(config=cfg)

    with pytest.raises(ValueError, match="invocation_id"):
        build_initial_submit_envelope_state(
            invocation_id="",
            starting_validation_state=validation_state,
        )


# ---------------------------------------------------------------------------
# 15. ALP-476 — frozen-dataclass invariants on the state cell + log entries
# ---------------------------------------------------------------------------


def test_submit_envelope_state_is_frozen() -> None:
    """``SubmitEnvelopeState`` is a frozen dataclass; attribute assignment raises."""
    import dataclasses

    from alphamind.decision.portfolio_manager.submit_envelope import SubmitEnvelopeState

    cfg = _config()
    validation_state = _make_validation_state(config=cfg)
    state = SubmitEnvelopeState(validation_state=validation_state, invocation_id="inv-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.submission_log = ()  # type: ignore[misc]


def test_submit_envelope_state_replace_produces_new_instance_with_field_changed() -> None:
    """``dataclasses.replace(state, submission_log=new_log)`` yields a new
    instance with the updated field; the input state is unchanged."""
    import dataclasses

    from alphamind.decision.portfolio_manager.submit_envelope import (
        SubmissionLogEntry,
        SubmitEnvelopeState,
    )

    cfg = _config()
    validation_state = _make_validation_state(config=cfg)
    state = SubmitEnvelopeState(validation_state=validation_state, invocation_id="inv-1")

    envelope = _make_analyst_envelope()
    entry = SubmissionLogEntry(envelope=envelope, submission_results=())
    new_state = dataclasses.replace(state, submission_log=(entry,))

    assert new_state is not state
    assert new_state.submission_log == (entry,)
    assert state.submission_log == ()
    # Other fields preserved.
    assert new_state.invocation_id == state.invocation_id
    assert new_state.validation_state is state.validation_state


def test_submission_log_entry_is_frozen() -> None:
    """``SubmissionLogEntry`` is a frozen, slotted dataclass."""
    import dataclasses

    from alphamind.decision.portfolio_manager.submit_envelope import SubmissionLogEntry

    envelope = _make_analyst_envelope()
    entry = SubmissionLogEntry(envelope=envelope, submission_results=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.submission_results = ()  # type: ignore[misc]


def test_failed_submission_entry_is_frozen() -> None:
    """``FailedSubmissionEntry`` is a frozen, slotted dataclass."""
    import dataclasses

    from alphamind.decision.portfolio_manager.submit_envelope import FailedSubmissionEntry

    entry = FailedSubmissionEntry(
        raw_args={"k": "v"},
        validation_error_repr="err",
        command_id="inv-1.ENV-REC-1.0.0",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.command_id = "new"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ALP-514 — Real position_id / order_id on OPEN acknowledgments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_acknowledgment_carries_deterministic_position_and_order_ids() -> None:
    """OpenCommand acknowledgments carry deterministic ``POS-{ticker}-{suffix}``
    / ``ORD-{ticker}-entry-{suffix}`` identifiers matching Phase 2's scheme,
    not the legacy ``-stub`` placeholders."""
    from alphamind.execution.oms.command_ids import (
        derive_pm_command_id,
        synthesize_id_suffix,
    )

    envelope = _make_analyst_envelope()
    get_state, server, _ = _build_state_and_server(envelope_for_routing=envelope)

    text, is_error = await _invoke_mcp_tool(
        server, "submit_envelope", envelope.model_dump(mode="json")
    )
    assert not is_error, text

    payload = json.loads(text)
    ack = payload["submission_results"][0]["acknowledgment"]
    state = get_state()

    # The Phase-2 minted ids hash the *base* command id; the broker-carried
    # link (ALP-844) is stripped by ``synthesize_id_suffix`` so the suffix is
    # identical regardless of which thesis FK the id carries. We pass the
    # OPEN's deterministic thesis here so the call is well-formed.
    expected_command_id = derive_pm_command_id(
        invocation_id=state.invocation_id,
        envelope_id="ENV-REC-1",
        command_ordinal=0,
        attempt_seq=0,
        thesis_id=ThesisId("THE-NVDA-0123456789abcdef0123456789abcdef"),
    )
    expected_suffix = synthesize_id_suffix(expected_command_id)
    ticker = "NVDA"

    assert ack["position_id"] == f"POS-{ticker}-{expected_suffix}"
    assert ack["order_id"] == f"ORD-{ticker}-entry-{expected_suffix}"
    assert "stub" not in ack["position_id"]
    assert "stub" not in ack["order_id"]


# ---------------------------------------------------------------------------
# ALP-514 — Real ValidationRequest for AddCommand (no __PLACEHOLDER__)
# ---------------------------------------------------------------------------


def test_add_command_builds_real_validation_request_from_position_lookup() -> None:
    """``_command_to_validation_request`` resolves AddCommand's ``position_id``
    through *position_lookup* and projects the underlying's instrument into a
    real :class:`ValidationRequest` — no ``__PLACEHOLDER__`` ticker."""
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _command_to_validation_request,
    )
    from alphamind.portfolio_state.records.positions import (
        Direction as PSDirection,
    )
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
    )
    from alphamind.risk_guardrails.state_delivery.validation_tool import (
        ValidationAction,
    )

    add_position_id = "POS-NVDA-001"
    record = _bypass_init_PositionRecord(
        position_id=add_position_id,
        direction=PSDirection.LONG,
        details=EquityPositionDetails(
            ticker="NVDA",  # type: ignore[arg-type]
            share_count=100.0,
            average_cost_basis_per_share=750.0,
        ),
    )
    lookup = {add_position_id: record}.get

    request = _command_to_validation_request(
        _add_command(position_id=add_position_id), position_lookup=lookup
    )
    assert request.action is ValidationAction.ADD
    assert request.instrument.ticker == "NVDA"
    assert "PLACEHOLDER" not in request.instrument.ticker
    assert request.size.quantity > 0


def test_add_command_equity_carries_position_direction() -> None:
    """ADD against an equity position projects its position-level direction.

    ALP-609 — the equity ``ValidationInstrument`` direction is sourced via
    ``position_direction()``; a SHORT equity position must project
    ``Direction.SHORT`` (no fabricated LONG).
    """
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _command_to_validation_request,
    )
    from alphamind.portfolio_state.records.positions import (
        Direction as PSDirection,
    )
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
        LocateStatus,
    )

    add_position_id = "POS-NVDA-SHORT-001"
    record = _bypass_init_PositionRecord(
        position_id=add_position_id,
        direction=PSDirection.SHORT,
        details=EquityPositionDetails(
            ticker="NVDA",  # type: ignore[arg-type]
            share_count=100.0,
            average_cost_basis_per_share=750.0,
            borrow_rate_pct=3.0,
            accrued_borrow_cost_usd=0.0,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=10_000.0,
        ),
    )
    lookup = {add_position_id: record}.get

    request = _command_to_validation_request(
        _add_command(position_id=add_position_id), position_lookup=lookup
    )
    assert request.instrument.direction is PSDirection.SHORT


def test_add_command_raises_when_position_lookup_returns_none() -> None:
    """An AddCommand whose ``position_id`` doesn't resolve in the lookup
    surfaces as a :class:`ValueError` naming the missing id — better than
    silently using a placeholder."""
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _command_to_validation_request,
    )

    with pytest.raises(ValueError, match="POS-NVDA-MISSING"):
        _command_to_validation_request(
            _add_command(position_id="POS-NVDA-MISSING"),
            position_lookup=lambda _id: None,
        )


# ---------------------------------------------------------------------------
# ALP-844 — broker-carried link: originating thesis FK in the command id
# ---------------------------------------------------------------------------


def _equity_position_with_thesis(*, position_id: str, thesis_id: str | None) -> PositionRecord:
    from alphamind.portfolio_state.records.positions import (
        Direction as PSDirection,
    )
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
    )

    return _bypass_init_PositionRecord(
        position_id=position_id,
        thesis_id=thesis_id,
        direction=PSDirection.LONG,
        details=EquityPositionDetails(
            ticker="NVDA",  # type: ignore[arg-type]
            share_count=100.0,
            average_cost_basis_per_share=750.0,
        ),
    )


def test_resolve_thesis_open_matches_command_execution_minted_thesis_id() -> None:
    """An OPEN's broker-carried thesis FK equals the ``THE-{ticker}-{suffix}``
    thesis_id command_execution mints from the same base command id — so a fill
    self-attributes to the very thesis row the OPEN creates (ALP-844)."""
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _resolve_originating_thesis,
    )
    from alphamind.execution.oms.command_ids import synthesize_id_suffix

    base = "inv-2026-05-05.ENV-REC-1.0.0"
    resolved = _resolve_originating_thesis(
        _open_command(underlying="NVDA"),
        base_command_id=base,
        position_lookup=lambda _id: None,
        order_thesis_lookup=lambda _id: None,
    )
    assert resolved == f"THE-NVDA-{synthesize_id_suffix(base)}"


def test_resolve_thesis_close_reads_position_thesis() -> None:
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _resolve_originating_thesis,
    )

    thesis = "THE-NVDA-0123456789abcdef0123456789abcdef"
    record = _equity_position_with_thesis(position_id="POS-NVDA-001", thesis_id=thesis)
    resolved = _resolve_originating_thesis(
        _close_command(position_id="POS-NVDA-001"),
        base_command_id="inv-X.ENV-SA-1.0.0",
        position_lookup={"POS-NVDA-001": record}.get,
        order_thesis_lookup=lambda _id: None,
    )
    assert resolved == thesis


def test_resolve_thesis_close_raises_when_position_thesis_is_none() -> None:
    """A monitor/PM-managed position always carries a thesis; a CLOSE against a
    thesis-less position must raise rather than mint a thesis-less id — the
    sentinel path the prior attempt introduced is gone."""
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _resolve_originating_thesis,
    )

    record = _equity_position_with_thesis(position_id="POS-NVDA-001", thesis_id=None)
    with pytest.raises(ValueError, match="originating thesis"):
        _resolve_originating_thesis(
            _close_command(position_id="POS-NVDA-001"),
            base_command_id="inv-X.ENV-SA-1.0.0",
            position_lookup={"POS-NVDA-001": record}.get,
            order_thesis_lookup=lambda _id: None,
        )


def test_resolve_thesis_close_raises_when_position_absent() -> None:
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _resolve_originating_thesis,
    )

    with pytest.raises(ValueError, match="absent from the PM view"):
        _resolve_originating_thesis(
            _close_command(position_id="POS-GONE"),
            base_command_id="inv-X.ENV-SA-1.0.0",
            position_lookup=lambda _id: None,
            order_thesis_lookup=lambda _id: None,
        )


def test_resolve_thesis_cancel_reads_order_thesis() -> None:
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _resolve_originating_thesis,
    )

    thesis = ThesisId("THE-NVDA-0123456789abcdef0123456789abcdef")
    order_thesis: dict[str, ThesisId] = {"ORD-1": thesis}
    resolved = _resolve_originating_thesis(
        _cancel_command(order_id="ORD-1"),
        base_command_id="inv-X.ENV-SA-1.0.0",
        position_lookup=lambda _id: None,
        order_thesis_lookup=order_thesis.get,
    )
    assert resolved == thesis


def test_resolve_thesis_cancel_raises_when_order_thesis_unresolvable() -> None:
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _resolve_originating_thesis,
    )

    with pytest.raises(ValueError, match="ORD-1"):
        _resolve_originating_thesis(
            _cancel_command(order_id="ORD-1"),
            base_command_id="inv-X.ENV-SA-1.0.0",
            position_lookup=lambda _id: None,
            order_thesis_lookup=lambda _id: None,
        )


def test_add_command_projects_options_position_into_validation_request() -> None:
    """ADD against an :class:`OptionsPositionDetails` position projects
    strike / expiration / contract_type onto the
    :class:`ValidationInstrument`."""
    from datetime import date

    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _command_to_validation_request,
    )
    from alphamind.portfolio_state.records.positions import (
        Direction as PSDirection,
    )
    from alphamind.portfolio_state.records.positions import (
        InstrumentType,
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
    )

    add_position_id = "POS-NVDA-OPT-001"
    record = _bypass_init_PositionRecord(
        position_id=add_position_id,
        direction=PSDirection.LONG,
        details=OptionsPositionDetails(
            underlying_ticker="NVDA",  # type: ignore[arg-type]
            strike_price=800.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=10.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=12.5,
            greeks=OptionGreeks(delta=0.4, gamma=0.01, theta=-0.05, vega=0.2),
        ),
    )
    lookup = {add_position_id: record}.get

    request = _command_to_validation_request(
        _add_command(position_id=add_position_id), position_lookup=lookup
    )
    assert request.instrument.asset_type is InstrumentType.OPTIONS
    assert request.instrument.ticker == "NVDA"
    assert request.instrument.strike == 800.0
    assert request.instrument.contract_type == "call"
    assert request.instrument.expiration is not None
    assert request.instrument.expiration.tzinfo is not None


def test_add_command_projects_strategy_position_with_per_leg_directions() -> None:
    """ADD against a :class:`StrategyPositionDetails` position projects each
    persisted leg's direction onto the :class:`ValidationStrategyLeg` tuple
    and emits ``direction=None`` — a strategy has no position-level direction
    (ALP-603)."""
    from datetime import date

    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _command_to_validation_request,
    )
    from alphamind.portfolio_state.records.positions import (
        Direction as PSDirection,
    )
    from alphamind.portfolio_state.records.positions import (
        InstrumentType,
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )
    from alphamind.portfolio_state.records.positions import (
        StrategyLeg as PSStrategyLeg,
    )

    def _leg(leg_id: str, direction: PSDirection, strike: float) -> PSStrategyLeg:
        return PSStrategyLeg(
            leg_id=leg_id,
            direction=direction,
            options=OptionsPositionDetails(
                underlying_ticker="NVDA",  # type: ignore[arg-type]
                strike_price=strike,
                expiration_date=date(2026, 6, 19),
                contract_type=OptionContractType.CALL,
                contract_count=1.0,
                contract_multiplier=100.0,
                premium_paid_per_contract=10.0,
                greeks=OptionGreeks(delta=0.4, gamma=0.01, theta=-0.05, vega=0.2),
            ),
        )

    add_position_id = "POS-NVDA-STRAT-001"
    record = _bypass_init_PositionRecord(
        position_id=add_position_id,
        # A strategy has no position-level direction (ALP-591) — the persisted
        # record carries ``None`` — and the validation request likewise emits
        # ``direction=None`` for a strategy.
        direction=None,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_spread",
            legs=(
                _leg("L1", PSDirection.LONG, 800.0),
                _leg("L2", PSDirection.SHORT, 810.0),
            ),
            net_premium_usd=1.0,
            max_profit_usd=10.0,
            max_loss_usd=10.0,
            breakeven_levels=(805.0,),
            strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        ),
    )
    lookup = {add_position_id: record}.get

    request = _command_to_validation_request(
        _add_command(position_id=add_position_id), position_lookup=lookup
    )
    assert request.instrument.asset_type is InstrumentType.STRATEGY
    assert request.instrument.direction is None
    assert request.instrument.legs is not None
    assert tuple(leg.direction for leg in request.instrument.legs) == (
        PSDirection.LONG,
        PSDirection.SHORT,
    )


def test_add_command_raises_on_strategy_leg_without_direction() -> None:
    """A persisted strategy leg with no direction recorded surfaces as a
    :class:`ValueError` — better than silently dropping the leg or assuming
    a default."""
    from datetime import date

    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _command_to_validation_request,
    )
    from alphamind.portfolio_state.records.positions import (
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )
    from alphamind.portfolio_state.records.positions import (
        StrategyLeg as PSStrategyLeg,
    )

    add_position_id = "POS-NVDA-STRAT-BAD"
    record = _bypass_init_PositionRecord(
        position_id=add_position_id,
        direction=None,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_spread",
            legs=(
                PSStrategyLeg(
                    leg_id="L1",
                    direction=None,
                    options=OptionsPositionDetails(
                        underlying_ticker="NVDA",  # type: ignore[arg-type]
                        strike_price=800.0,
                        expiration_date=date(2026, 6, 19),
                        contract_type=OptionContractType.CALL,
                        contract_count=1.0,
                        contract_multiplier=100.0,
                        premium_paid_per_contract=10.0,
                        greeks=OptionGreeks(delta=0.4, gamma=0.01, theta=-0.05, vega=0.2),
                    ),
                ),
            ),
            net_premium_usd=1.0,
            max_profit_usd=10.0,
            max_loss_usd=10.0,
            breakeven_levels=(800.0,),
            strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        ),
    )
    lookup = {add_position_id: record}.get

    with pytest.raises(ValueError, match="L1"):
        _command_to_validation_request(
            _add_command(position_id=add_position_id), position_lookup=lookup
        )


# ---------------------------------------------------------------------------
# ALP-514 — Strategy direction projection consumes per-leg directions
# ---------------------------------------------------------------------------


def test_strategy_open_validation_request_carries_per_leg_directions() -> None:
    """OPEN of a StrategyInstrument projects each leg's direction into the
    :class:`ValidationStrategyLeg` tuple — the position-level direction is
    ``None`` (a strategy has no position-level direction — ALP-603)."""
    from alphamind.commands.command_models import StrategyInstrument as OMSStrategyInstrument
    from alphamind.commands.command_models import StrategyLeg as OMSStrategyLeg
    from alphamind.decision.portfolio_manager.submit_envelope.process import (
        _build_constructive_request_from_open,
    )
    from alphamind.portfolio_state.records.positions import (
        Direction as PSDirection,
    )
    from alphamind.portfolio_state.records.positions import (
        InstrumentType,
    )

    strategy = OMSStrategyInstrument(
        asset_type="strategy",
        strategy_type="vertical_spread",
        underlying="NVDA",
        legs=(
            OMSStrategyLeg(
                strike=price(800.0),
                expiration="2026-06-19T00:00:00+00:00",
                contract_type="call",
                direction="long",
                quantity_ratio=1,
            ),
            OMSStrategyLeg(
                strike=price(810.0),
                expiration="2026-06-19T00:00:00+00:00",
                contract_type="call",
                direction="short",
                quantity_ratio=1,
            ),
        ),
    )
    command = OpenCommand(
        command_type="open",
        instrument=strategy,
        # ALP-866: a strategy OPEN entry must rest (limit / stop_limit).
        entry_order=EntryOrder(type="limit", limit_price=price(5.0), stop_price=None),
        position_size=PositionSize(quantity=1.0, dollar_value=money(1_000.0), premium_at_risk=None),
        # A strategy take-profit must be pl_percentage (ALP-611).
        target=Target(
            target_type="pl_percentage", pl_percentage=80.0, price=price(830.0), order_type="limit"
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                trigger_signal="net_mark",
                condition=PriceCondition(
                    underlying_trigger="NVDA",
                    comparator="<=",
                    trigger_price=price(750.0),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary="Spread.",
            nature="non_directional",
            components=(
                OMSThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference="NVDA",
                    narrative="Bullish.",
                    key_assumptions=("Trend intact.",),
                ),
            ),
        ),
        capital_protection_floor=CapitalProtectionFloor(max_loss=money(500.0)),
    )
    request = _build_constructive_request_from_open(command)
    assert request.instrument.asset_type is InstrumentType.STRATEGY
    # A strategy has no position-level direction (ALP-603) — only the per-leg
    # directions projected below.
    assert request.instrument.direction is None
    assert request.instrument.legs is not None
    assert len(request.instrument.legs) == 2
    # Per-leg directions are preserved from the wire-format StrategyInstrument.
    assert request.instrument.legs[0].direction is PSDirection.LONG
    assert request.instrument.legs[1].direction is PSDirection.SHORT
