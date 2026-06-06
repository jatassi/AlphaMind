"""Initial-greeks persistence regression test (ALP-397).

Verifies the architecture's ``Computed greeks are returned in the validation
response and persisted as the position's initial greeks`` promise from
``docs/design/05-execution-layer/architecture.md`` § 3 — Greek computation for
options validation.

Drives an OPEN-options command end-to-end through the in-process
``submit_envelope`` MCP server bound to a fresh on-disk SQLite DB. The
expected flow is:

1. ``submit_envelope`` runs Layer-2/3 validation, computes greeks via the IV
   surface + Black-Scholes core, and returns an :class:`Acknowledgment`
   carrying ``validation_metadata.greeks``.
2. Phase-2 writeback (``persist_envelope_outcome``) lands a
   :class:`PositionRecord` whose ``details`` field is an
   :class:`OptionsPositionDetails` with a ``greeks`` field equal — field by
   field (delta / gamma / theta / vega) — to the Acknowledgment's
   ``validation_metadata.greeks``.

Per ALP-397 (extended scope per operator decision), the chain closing this
test asserts is now implemented end-to-end. Three gaps were closed:

1. ``submit_envelope_mcp._build_constructive_request_from_open`` propagates
   ``strike`` / ``expiration`` / ``contract_type`` from ``OptionInstrument``
   to ``ValidationInstrument`` so OPTIONS validation does not raise on the
   missing fields before an Acknowledgment is built.
2. ``phase2._build_pending_position`` dispatches on the OPEN command's
   instrument variant — equity OPENs land an ``EquityPositionDetails``;
   options OPENs land an ``OptionsPositionDetails``.
3. ``phase2._writeback_open`` reads
   ``SubmissionResult.acknowledgment.validation_metadata.greeks`` and threads
   it through to ``OptionsPositionDetails.greeks`` so the four greek values
   round-trip identically.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Side-effect import: pre-load ``portfolio_manager.models`` before the lazy
# ``alphamind.execution.oms.__getattr__`` triggers loading of
# ``submit_envelope_mcp``. Mirrors the pattern in
# ``tests/execution/oms/test_submit_envelope_mcp.py``.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind._kernel.ids import (
    EnvelopeId,
    InvocationId,
    RecommendationId,
)
from alphamind._kernel.money import money, price
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.command_models import (
    BracketOrderParameters,
    CapitalProtectionFloor,
    EntryOrder,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.decision.portfolio_manager.models import (
    CriterionAssessment,
    OpenCommand,
    PMAnalystEnvelope,
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
    WrappedRecommendation,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.positions import OptionsPositionDetails
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
from alphamind.state.invocation_context.context import (
    InvocationContext,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

_NOW = datetime(2026, 5, 9, 14, 30, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-09-options-greeks"
_PROCESS_ID = "proc-options-greeks"
_TICKER = "AAPL"
_STRIKE = 100.0
_EXPIRATION_DATE = date(2026, 6, 19)
_EXPIRATION_STR = _EXPIRATION_DATE.isoformat()
_PORTFOLIO_VALUE = 100_000.0


# ---------------------------------------------------------------------------
# DB fixture (mirrors tests/execution/state_persistence/test_phase2_write_path.py)
# ---------------------------------------------------------------------------


# Bypass-init helpers — replace Pydantic ``model_construct``. The dataclass __init__
# enforces all fields; these helpers skip validation so tests can inject sparse fixtures.


def _bypass_init_PortfolioManagerView(**kwargs: object) -> PortfolioManagerView:  # noqa: N802
    obj = object.__new__(PortfolioManagerView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Substrate seeding
# ---------------------------------------------------------------------------


def _make_process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="test-initial-greeks-persistence",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/test-options-greeks/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="test-fixture",
    )


def _make_invocation_record(invocation_id: str = _INV_ID) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="test_initial_greeks_persistence.py",
        trigger_reason="ALP-397 regression",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/test-options-greeks/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/test-options-greeks/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _seed_substrate(factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed process_lifetime + cash_ledger so Phase-2 writeback can reserve capital
    and the activity-log writes have a valid invocation FK. The invocation row
    itself is written by the surrounding ``InvocationContext.__aenter__``."""
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        cash = CashLedger(
            current_cash_usd=_PORTFOLIO_VALUE,
            settled_cash_usd=_PORTFOLIO_VALUE,
            reserved_capital_usd=0.0,
            available_buying_power_usd=_PORTFOLIO_VALUE,
            margin_held_usd=0.0,
            unsettled_proceeds=(),
            cash_pct_of_portfolio=0.0,
            true_deployable_capital_usd=0.0,
            regt_excess_trailing_30d_usd=0.0,
            regt_excess_trailing_90d_usd=0.0,
            regt_excess_lifetime_usd=0.0,
        )
        sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
        await sess.commit()


# ---------------------------------------------------------------------------
# Validation-state + MCP server fixture builders
# ---------------------------------------------------------------------------


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/test-options-greeks",
            "invocation_provenance_root": "/tmp/test-options-greeks",
        }
    )


def _validation_state() -> tuple[ValidationToolState, LibraryConfig, MarketInputs]:
    """Build a Validation tool state sized so the OPEN-options command passes
    every per-rule headroom check on a $100k portfolio."""
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
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
    zones = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
    library_config = LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: zones for k in effective_limits}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )
    snapshot = PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 0.0, "semis": 0.0, "financials": 0.0, "energy": 0.0}
        ),
        net_long_pct=0.0,
        net_short_pct=0.0,
        gross_pct=0.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=0.0,
        existing_positions=MappingProxyType({}),
    )
    iv_provider = FixtureIvProvider(
        surface={
            _TICKER: IvSurfaceEntry(
                underlying=_TICKER,
                quotes=(
                    IvQuote(
                        strike=_STRIKE,
                        expiration=_EXPIRATION_DATE,
                        contract_type=ContractType.CALL,
                        implied_volatility=0.30,
                    ),
                ),
            ),
        },
        realized_vol={},
    )
    market = MarketInputs(
        underlying_prices=MappingProxyType({_TICKER: _STRIKE}),
        risk_free_rate=0.045,
        iv_provider=iv_provider,
        as_of=_NOW,
    )
    state = ValidationToolState(
        invocation_id=_INV_ID,
        starting_snapshot=snapshot,
        starting_risk_budget=RiskBudgetConsumption(entries=()),
        starting_active_risk_parameters=ActiveRiskParameterSet(
            regime_label=RegimeLabel.NORMAL,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=(),
            active_overlays=(),
        ),
        profile_feature_flags=library_config.feature_flags,
        library_config=library_config,
        library_market=market,
        sector_resolver=lambda _t: "tech",
        accumulated_deltas=(),
    )
    return state, library_config, market


# ---------------------------------------------------------------------------
# OPEN-options command + envelope
# ---------------------------------------------------------------------------


def _open_options_command() -> OpenCommand:
    """Build a canonical full-shape OPEN command on a single-leg call option."""
    return OpenCommand(
        command_type="open",
        instrument=OptionInstrument(
            asset_type="option",
            underlying=_TICKER,
            strike=price(_STRIKE),
            expiration=_EXPIRATION_STR,
            contract_type="call",
            direction="long",
        ),
        # ALP-866: an options OPEN entry must rest (limit / stop_limit).
        entry_order=EntryOrder(type="limit", limit_price=price(5.0), stop_price=None),
        position_size=PositionSize(quantity=1.0, dollar_value=money(1_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(120.0),
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
                    underlying_trigger=_TICKER,
                    comparator="<=",
                    trigger_price=price(80.0),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary=f"Long {_TICKER} call.",
            nature="directional",
            components=(
                ThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference=_TICKER,
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
        capital_protection_floor=CapitalProtectionFloor(max_loss=money(500.0)),
    )


def _envelope() -> PMAnalystEnvelope:
    pass_assessment = CriterionAssessment(status="pass", note=None)
    return PMAnalystEnvelope(
        envelope_id=EnvelopeId("ENV-REC-1"),
        invocation_id=InvocationId(_INV_ID),
        source_provenance="pm_analyst",
        source_recommendation_id=RecommendationId("REC-1"),
        recommendation_type="new_entry",
        verdict="approve",
        evaluation=ThesisQualityEvaluation(
            falsifiability=pass_assessment,
            sizing_proportionality=pass_assessment,
            portfolio_coherence=pass_assessment,
            timing_plausibility=pass_assessment,
            counterargument_consideration=pass_assessment,
        ),
        modifications=(),
        concerns=(),
        rationale_narrative="ALP-397 fixture envelope.",
        anti_patterns_identified=None,
        commands=(_open_options_command(),),
    )


def _retrieval_store() -> RetrievalStore:
    return RetrievalStore(entries={}, freshness_by_source={})


def _pre_processor_bundle() -> ProposalPreProcessorBundle:
    """Minimal bundle whose recommendations carry the source_recommendation_id."""
    from alphamind.decision.analyst.models import Recommendation

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
    aggregate = AggregateObservations(
        combined_set_impact=CombinedSetImpact(
            basis=BasisSection(
                analyst_proposal_ids=(),
                strategist_action_ids=(),
                strategist_holds_excluded_count=0,
                snapshot_timestamp=_NOW,
            ),
            per_rule=(),
            breaches=(),
        ),
        conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
        book_health_summary=BookHealthSummary(
            by_thesis_status=by_thesis,
            by_recommended_action=by_action,
            remedy_flagged_count=0,
            total=0,
        ),
    )
    rec = Recommendation.model_construct(recommendation_id="REC-1")
    wrapped = WrappedRecommendation.model_construct(
        recommendation=rec,
        pre_processor_annotations=AnalystSideAnnotations(conflicts=()),
    )
    return ProposalPreProcessorBundle.model_construct(
        invocation_id=_INV_ID,
        timestamp=_NOW,
        aggregate_observations=aggregate,
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="normal", recommendations=(wrapped,), watchlist=None
        ),
    )


def _pm_view() -> PortfolioManagerView:
    return _bypass_init_PortfolioManagerView(
        positions=(),
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


# ---------------------------------------------------------------------------
# In-process MCP-server invocation helper
# ---------------------------------------------------------------------------


async def _invoke_submit_envelope(
    server: Any, envelope_payload: dict[str, Any]
) -> tuple[str, bool]:
    """Drive the in-process MCP server's tools/call handler."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="submit_envelope",
            arguments=envelope_payload,
        ),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    content_blocks = result.root.content
    assert content_blocks, "submit_envelope returned no content blocks"
    text_block = content_blocks[0]
    return str(text_block.text), bool(result.root.isError)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_options_acknowledgment_greeks_match_persisted_position_greeks(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-397 regression: validation-time greeks land on the persisted PositionRecord.

    Submits a PMEnvelope containing one OPEN command on a single-leg call
    option through the in-process ``submit_envelope`` MCP server bound to a
    fresh on-disk SQLite DB. After Phase-2 writeback completes, the persisted
    ``PositionRecord.details`` (cast to ``OptionsPositionDetails``) must
    expose a ``greeks`` field equal field-by-field (delta / gamma / theta /
    vega) to the Acknowledgment's ``validation_metadata.greeks``.
    """
    from alphamind.decision.portfolio_manager.submit_envelope import (
        build_initial_submit_envelope_state,
        build_submit_envelope_mcp_server,
    )

    _, factory = db
    await _seed_substrate(factory)

    validation_state, library_config, library_market = _validation_state()
    envelope = _envelope()

    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(),
    )
    handle = await ctx.__aenter__()
    try:
        state = build_initial_submit_envelope_state(
            invocation_id=_INV_ID,
            starting_validation_state=validation_state,
        )
        mcp_servers, _allowed, _get_state = build_submit_envelope_mcp_server(
            state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=_pre_processor_bundle(),
            pm_view=_pm_view(),
            active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
            halt_mode=False,
            sector_resolver=lambda _t: "tech",
            library_config=library_config,
            library_market=library_market,
            invocation_handle=handle,
            state_persistence_config=_state_persistence_config(),
        )
        server = mcp_servers["alphamind_execution_oms_submit"]["instance"]

        response_text, is_error = await _invoke_submit_envelope(
            server, envelope.model_dump(mode="json")
        )
        assert not is_error, f"submit_envelope returned error: {response_text}"

        # Parse the response and grab the Acknowledgment's greeks.
        import json

        payload = json.loads(response_text)
        results = payload["submission_results"]
        assert len(results) == 1
        result = results[0]
        assert result["status"] == "accepted", result
        ack = result["acknowledgment"]
        validation_metadata = ack["validation_metadata"]
        ack_greeks = validation_metadata["greeks"]
        assert ack_greeks is not None, (
            "OPEN-options Acknowledgment must carry computed greeks per "
            "docs/design/05-execution-layer/architecture.md § 3"
        )
        ack_iv = validation_metadata["implied_volatility"]
        assert ack_iv is not None, (
            "OPEN-options Acknowledgment must carry the validation-time "
            "implied_volatility (ALP-399) so persistence can populate "
            "OptionGreeks.iv_used"
        )
    except BaseException:
        await ctx.__aexit__(None, None, None)
        raise
    else:
        await ctx.__aexit__(None, None, None)

    # Read the persisted PositionRecord and assert OptionsPositionDetails.greeks
    # matches validation_metadata.greeks field by field.
    async with factory() as sess:
        rows = (await sess.execute(select(PositionRow))).scalars().all()
    assert len(rows) == 1, f"expected exactly 1 persisted position, got {len(rows)}"
    record = position_row_to_record(rows[0])
    assert isinstance(record.details, OptionsPositionDetails), (
        f"expected OptionsPositionDetails, got {type(record.details).__name__}"
    )
    persisted_greeks = record.details.greeks
    assert persisted_greeks.delta == pytest.approx(ack_greeks["delta"])
    assert persisted_greeks.gamma == pytest.approx(ack_greeks["gamma"])
    assert persisted_greeks.theta == pytest.approx(ack_greeks["theta"])
    assert persisted_greeks.vega == pytest.approx(ack_greeks["vega"])
    assert persisted_greeks.iv_used is not None, (
        "ALP-399: OPEN-options writeback must thread the validation-time IV to OptionGreeks.iv_used"
    )
    assert persisted_greeks.iv_used == pytest.approx(ack_iv)
