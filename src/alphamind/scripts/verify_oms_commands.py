"""OMS commands end-to-end verification (ALP-376).

Operator entry point (via thin shim at ``scripts/verify_oms_commands.py``).
Exercises the OMS commands surface end-to-end against a freshly-migrated
SQLite DB across five phases:

* Phase 1 — canonical Pydantic round-trip across all five command variants
  (OPEN / CLOSE / ADJUST / CANCEL / ADD) plus :class:`EngineEnvelope`.
* Phase 2 — command-ID utility (PM derivation + engine derivation + round-trip
  parse + ``attempt_seq`` computation).
* Phase 3 — PM envelope path against a fresh DB (canonical ``PMEnvelope`` →
  ``submit_envelope`` MCP → Phase 2 writeback → activity log).
* Phase 4 — engine envelope path against the same DB (canonical
  ``EngineEnvelope`` → ``submit_engine_envelope`` → Phase 2 writeback →
  activity log with ``engine_guardrail`` provenance).
* Phase 5 — print one ``[PASS]`` / ``[FAIL]`` line per criterion + final
  ``RESULT: PASS`` / ``RESULT: FAIL`` summary.

No SDK invocation; no live broker contact. Sub-minute runtime against a fresh
on-disk DB.

Usage::

    uv run python scripts/verify_oms_commands.py [--db-path PATH] [--output FORMAT]

See ``scripts/RUNBOOK_oms_commands.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Side-effect import: pre-load ``portfolio_manager.models`` before the lazy
# ``alphamind.execution.oms.__getattr__`` triggers loading of
# ``submit_envelope_mcp`` (which imports the PM models). Mirrors the pattern in
# ``tests/execution/oms/test_submit_engine_envelope.py``; without this the
# transitive harness-side imports race with the lazy submodule load.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind.execution.oms.command_ids import (
    compute_attempt_seq,
    derive_engine_command_id,
    derive_pm_command_id,
    parse_engine_command_id,
    parse_pm_command_id,
)
from alphamind.execution.oms.command_models import (
    AddCommand,
    AdjustCommand,
    BracketAdjustment,
    BracketOrderParameters,
    CancelCommand,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    NewEventInvalidation,
    NewStopLevel,
    NewTargetLevel,
    OMSCommand,
    OpenCommand,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.execution.oms.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
)
from alphamind.persistence.session import make_engine
from alphamind.portfolio_state.events.activity_log import EventType

__all__ = [
    "PhaseResult",
    "main",
    "run_phase_1_canonical_round_trip",
    "run_phase_2_command_id_utility",
    "run_phase_3_pm_envelope_path",
    "run_phase_4_engine_envelope_path",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseResult:
    """One phase's outcome — pass/fail flag + named label + diagnostic detail."""

    label: str
    ok: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Phase 1 — canonical Pydantic round-trip
# ---------------------------------------------------------------------------


_PHASE_1_LABEL = "Phase 1 — canonical model round-trip"

_OMS_COMMAND_ADAPTER: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
_ENGINE_ENVELOPE_ADAPTER: TypeAdapter[EngineEnvelope] = TypeAdapter(EngineEnvelope)

_TICKER = "NVDA"
_NOW = datetime(2026, 5, 9, 14, 30, 0, tzinfo=UTC)


def _open_command_variant() -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=_TICKER, direction="long"),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=10.0, dollar_value=10_000.0),
        target=Target(
            target_type="absolute_price",
            price=950.0,
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=_TICKER,
                    comparator="<=",
                    trigger_price=750.0,
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary=f"Long {_TICKER}.",
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
    )


def _close_command_variants() -> tuple[CloseCommand, ...]:
    """One CloseCommand per ``close_rationale_type`` enum value."""
    return (
        CloseCommand(
            command_type="close",
            position_id="POS-NVDA-INVALIDATED",
            quantity="all",
            order_type="market",
            limit_price=None,
            close_rationale_type="thesis_invalidated",
            invalidation_reason="Thesis broken: catalyst missed.",
            risk_management_subtype=None,
        ),
        CloseCommand(
            command_type="close",
            position_id="POS-NVDA-TARGET",
            quantity="all",
            order_type="limit",
            limit_price=950.0,
            close_rationale_type="target_reached",
            invalidation_reason=None,
            risk_management_subtype=None,
        ),
        CloseCommand(
            command_type="close",
            position_id="POS-NVDA-CONVICTION",
            quantity=5.0,
            order_type="market",
            limit_price=None,
            close_rationale_type="conviction_reduced",
            invalidation_reason=None,
            risk_management_subtype=None,
        ),
        CloseCommand(
            command_type="close",
            position_id="POS-NVDA-RISK",
            quantity="all",
            order_type="market",
            limit_price=None,
            close_rationale_type="risk_management",
            invalidation_reason=None,
            risk_management_subtype="engine_guardrail",
        ),
    )


def _adjust_command_variants() -> tuple[AdjustCommand, ...]:
    """One AdjustCommand per change-field variant (stop / target / time / event / thesis)."""
    return (
        AdjustCommand(
            command_type="adjust",
            position_id="POS-NVDA-A1",
            adjustment_rationale="Tighten stop after favorable move.",
            new_stop_level=NewStopLevel(
                trigger_price=820.0,
                order_type="stop",
                limit_price=None,
            ),
        ),
        AdjustCommand(
            command_type="adjust",
            position_id="POS-NVDA-A2",
            adjustment_rationale="Lift target after upgrade.",
            new_target_level=NewTargetLevel(
                target_type="absolute_price",
                price=1000.0,
                pl_percentage=None,
                pl_dollar=None,
                order_type="limit",
            ),
        ),
        AdjustCommand(
            command_type="adjust",
            position_id="POS-NVDA-A3",
            adjustment_rationale="Extend time horizon.",
            new_time_expiration=datetime(2026, 6, 1, tzinfo=UTC),
        ),
        AdjustCommand(
            command_type="adjust",
            position_id="POS-NVDA-A4",
            adjustment_rationale="Add event invalidation leg.",
            new_event_invalidation=NewEventInvalidation(
                event_description="Earnings guidance cut.",
            ),
        ),
        AdjustCommand(
            command_type="adjust",
            position_id="POS-NVDA-A5",
            adjustment_rationale="Refine entry rationale.",
            thesis_component_updates=(
                ThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference=_TICKER,
                    narrative="Updated catalyst — revenue beat.",
                    key_assumptions=("Beat sustains.",),
                ),
            ),
        ),
    )


def _cancel_command_variant() -> CancelCommand:
    return CancelCommand(
        command_type="cancel",
        order_id="ORD-NVDA-PENDING",
        cancel_reason="Pre-fill cancellation: thesis stale.",
    )


def _add_command_variant() -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id="POS-NVDA-EXISTING",
        additional_quantity=5.0,
        additional_dollar_value=5_000.0,
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="add",
            instrument_reference=_TICKER,
            narrative="Add to existing — momentum intact.",
            key_assumptions=("Momentum intact.",),
        ),
        bracket_adjustment=BracketAdjustment(
            new_stop_level=NewStopLevel(
                trigger_price=800.0,
                order_type="stop",
                limit_price=None,
            ),
        ),
    )


def _engine_envelope_variant() -> EngineEnvelope:
    trigger_ts = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
    return EngineEnvelope(
        envelope_id="MON.session-verify.42",
        invocation_id=None,
        trigger_timestamp=trigger_ts,
        source_provenance="engine_guardrail",
        guardrail_trigger_record=GuardrailTriggerRecord(
            rule_breached="per_position_max_loss",
            trigger_timestamp=trigger_ts,
            breach_details=BreachDetails(
                current_value=12_500.0,
                limit_value=10_000.0,
                overage=2_500.0,
                unit="usd",
                regime_at_breach="risk_off",
            ),
            position_selection_rationale="position triggering the per-position max loss limit",
            cascade_id=None,
            secondary_breach_check_result=None,
        ),
        commands=(
            CloseCommand(
                command_type="close",
                position_id="POS-NVDA-001",
                quantity="all",
                order_type="market",
                limit_price=None,
                close_rationale_type="risk_management",
                invalidation_reason=None,
                risk_management_subtype="engine_guardrail",
            ),
        ),
    )


def run_phase_1_canonical_round_trip() -> PhaseResult:
    """Construct one of every command variant + an :class:`EngineEnvelope`,
    round-trip each via the canonical ``TypeAdapter``, surface the first
    validation error if any."""
    commands: list[OMSCommand] = [
        _open_command_variant(),
        *_close_command_variants(),
        *_adjust_command_variants(),
        _cancel_command_variant(),
        _add_command_variant(),
    ]
    try:
        for cmd in commands:
            payload = _OMS_COMMAND_ADAPTER.dump_python(cmd, mode="json")
            _OMS_COMMAND_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        return PhaseResult(
            label=_PHASE_1_LABEL,
            ok=False,
            detail=f"OMSCommand round-trip failed: {exc}",
        )

    try:
        env = _engine_envelope_variant()
        payload = _ENGINE_ENVELOPE_ADAPTER.dump_python(env, mode="json")
        _ENGINE_ENVELOPE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        return PhaseResult(
            label=_PHASE_1_LABEL,
            ok=False,
            detail=f"EngineEnvelope round-trip failed: {exc}",
        )

    return PhaseResult(label=_PHASE_1_LABEL, ok=True)


# ---------------------------------------------------------------------------
# Phase 2 — command-ID utility
# ---------------------------------------------------------------------------


_PHASE_2_LABEL = "Phase 2 — command-ID utility"

# Worked example from docs/design/oms-command-ids.md § Worked example.
_WORKED_INVOCATION_ID = "inv-2026-04-23T14-30Z"
_WORKED_ENVELOPE_ID = "ENV-REC-2"
_WORKED_EXPECTED_FIRST = "inv-2026-04-23T14-30Z.ENV-REC-2.0.0"
_WORKED_EXPECTED_RETRY = "inv-2026-04-23T14-30Z.ENV-REC-2.0.1"


def run_phase_2_command_id_utility() -> PhaseResult:
    """Cover PM derivation + engine derivation + parse round-trip + attempt_seq.

    Each per-check helper returns a failure-detail string or ``None``; the
    orchestrator collects them in order and surfaces the first violation.
    """
    checks = (
        _phase_2_check_pm_derivation,
        _phase_2_check_pm_parse,
        _phase_2_check_engine_derivation,
        _phase_2_check_engine_parse,
        _phase_2_check_attempt_seq,
    )
    for check in checks:
        failure = check()
        if failure is not None:
            return PhaseResult(label=_PHASE_2_LABEL, ok=False, detail=failure)
    return PhaseResult(label=_PHASE_2_LABEL, ok=True)


def _phase_2_check_pm_derivation() -> str | None:
    """Confirm both worked-example IDs (first + post-rejection retry) match
    the canonical format from ``oms-command-ids.md``."""
    first_id = derive_pm_command_id(
        invocation_id=_WORKED_INVOCATION_ID,
        envelope_id=_WORKED_ENVELOPE_ID,
        command_ordinal=0,
        attempt_seq=0,
    )
    if first_id != _WORKED_EXPECTED_FIRST:
        return (
            f"derive_pm_command_id worked example mismatch: "
            f"got {first_id!r}, expected {_WORKED_EXPECTED_FIRST!r}"
        )
    retry_id = derive_pm_command_id(
        invocation_id=_WORKED_INVOCATION_ID,
        envelope_id=_WORKED_ENVELOPE_ID,
        command_ordinal=0,
        attempt_seq=1,
    )
    if retry_id != _WORKED_EXPECTED_RETRY:
        return (
            f"derive_pm_command_id retry example mismatch: "
            f"got {retry_id!r}, expected {_WORKED_EXPECTED_RETRY!r}"
        )
    return None


def _phase_2_check_pm_parse() -> str | None:
    """Confirm ``parse_pm_command_id`` is the inverse of ``derive_pm_command_id``."""
    components = parse_pm_command_id(_WORKED_EXPECTED_FIRST)
    if (
        components.invocation_id != _WORKED_INVOCATION_ID.removeprefix("inv-")
        or components.envelope_id != _WORKED_ENVELOPE_ID
        or components.command_ordinal != 0
        or components.attempt_seq != 0
    ):
        return f"parse_pm_command_id round-trip mismatch: {components!r}"
    return None


def _phase_2_check_engine_derivation() -> str | None:
    """Confirm engine derivation matches the ``MON.{session}.{trigger}.{ord}`` shape."""
    engine_id = derive_engine_command_id(
        monitor_session_id="session-verify",
        trigger_id=42,
        command_ordinal=0,
    )
    if engine_id != "MON.session-verify.42.0":
        return f"derive_engine_command_id shape mismatch: got {engine_id!r}"
    return None


def _phase_2_check_engine_parse() -> str | None:
    """Confirm ``parse_engine_command_id`` is the inverse of ``derive_engine_command_id``."""
    components = parse_engine_command_id("MON.session-verify.42.0")
    if (
        components.monitor_session_id != "session-verify"
        or components.trigger_id != 42
        or components.command_ordinal != 0
    ):
        return f"parse_engine_command_id round-trip mismatch: {components!r}"
    return None


def _phase_2_check_attempt_seq() -> str | None:
    """Synthesize a PMEnvelope with one pre + two post modifications and assert
    ``compute_attempt_seq`` returns 2 (counts only ``post_rejection``)."""
    from alphamind.decision.portfolio_manager.models import (
        CriterionAssessment,
        ModificationRecord,
        PMAnalystEnvelope,
        ThesisQualityEvaluation,
    )

    pre_modification = ModificationRecord(
        phase="pre_submission",
        field_changed="position_size.dollar_value",
        original_value=4_000.0,
        approved_value=3_000.0,
        adjustment_category="conviction_disagreement",
        rationale="Lower conviction than analyst proposed.",
        triggering_rule=None,
    )
    post_modification_a = ModificationRecord(
        phase="post_rejection",
        field_changed="position_size.dollar_value",
        original_value=3_000.0,
        approved_value=1_500.0,
        adjustment_category="guardrail_rejection_response",
        rationale="Guardrail rejected — reduced size.",
        triggering_rule="sector_concentration_pct",
    )
    post_modification_b = ModificationRecord(
        phase="post_rejection",
        field_changed="position_size.dollar_value",
        original_value=1_500.0,
        approved_value=750.0,
        adjustment_category="guardrail_rejection_response",
        rationale="Second rejection — reduced again.",
        triggering_rule="sector_concentration_pct",
    )
    pass_assessment = CriterionAssessment(status="pass", note=None)
    envelope = PMAnalystEnvelope(
        envelope_id=_WORKED_ENVELOPE_ID,
        invocation_id=_WORKED_INVOCATION_ID,
        source_provenance="pm_analyst",
        source_recommendation_id="REC-2",
        recommendation_type="new_entry",
        verdict="approve_with_modification",
        evaluation=ThesisQualityEvaluation(
            falsifiability=pass_assessment,
            sizing_proportionality=pass_assessment,
            portfolio_coherence=pass_assessment,
            timing_plausibility=pass_assessment,
            counterargument_consideration=pass_assessment,
        ),
        modifications=(pre_modification, post_modification_a, post_modification_b),
        concerns=(),
        rationale_narrative="Sized down twice after guardrail rejections.",
        anti_patterns_identified=None,
        commands=(_open_command_variant(),),
    )

    attempt_seq = compute_attempt_seq(envelope)
    if attempt_seq != 2:
        return f"compute_attempt_seq returned {attempt_seq}, expected 2 (one pre + two post)"
    return None


# ---------------------------------------------------------------------------
# Phase 3 — PM envelope path against a fresh DB
# ---------------------------------------------------------------------------


_PHASE_3_LABEL = "Phase 3 — PM envelope path"

_PHASE_3_INVOCATION_ID = "inv-verify-oms-commands-phase3"
_PHASE_3_PROCESS_ID = "verify-oms-commands-proc"
_PHASE_3_ENVELOPE_ID = "ENV-REC-1"
_PHASE_3_RECOMMENDATION_ID = "REC-1"
_PHASE_3_TICKER = "NVDA"
# Use a non-stub dollar value so the assertion catches a regression to the
# old $1k stub constant (story 03 / ALP-374 retired ``_STUB_DOLLAR_VALUE``).
_PHASE_3_DOLLAR_VALUE = 5_000.0
_PHASE_3_QUANTITY = 5.0


def _state_persistence_config() -> Any:
    from alphamind.execution.state_persistence.config import StatePersistenceConfig

    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/verify-oms-commands",
            "invocation_provenance_root": "/tmp/verify-oms-commands",
        }
    )


def _open_async_factory(
    db_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
    )

    engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(engine)
    return engine, factory


def _phase_3_open_command() -> OpenCommand:
    """Single OPEN command — sized $5,000 so the dollar_value assertion catches
    a regression to the retired $1k stub."""
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(
            asset_type="equity",
            ticker=_PHASE_3_TICKER,
            direction="long",
        ),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(
            quantity=_PHASE_3_QUANTITY,
            dollar_value=_PHASE_3_DOLLAR_VALUE,
        ),
        target=Target(
            target_type="absolute_price",
            price=950.0,
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=_PHASE_3_TICKER,
                    comparator="<=",
                    trigger_price=750.0,
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary=f"Long {_PHASE_3_TICKER}.",
            components=(
                ThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference=_PHASE_3_TICKER,
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
    )


def _phase_3_envelope() -> Any:
    from alphamind.decision.portfolio_manager.models import (
        CriterionAssessment,
        PMAnalystEnvelope,
        ThesisQualityEvaluation,
    )

    pass_assessment = CriterionAssessment(status="pass", note=None)
    return PMAnalystEnvelope(
        envelope_id=_PHASE_3_ENVELOPE_ID,
        invocation_id=_PHASE_3_INVOCATION_ID,
        source_provenance="pm_analyst",
        source_recommendation_id=_PHASE_3_RECOMMENDATION_ID,
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
        rationale_narrative="Verify-script PM-envelope OPEN fixture.",
        anti_patterns_identified=None,
        commands=(_phase_3_open_command(),),
    )


def _phase_3_validation_state() -> Any:
    """Build the cumulative ValidationToolState the engine-stub re-runs guardrail
    checks against. Sized so the OPEN command's $5,000 stays well under all
    per-rule headroom on a $100k portfolio."""
    from alphamind.portfolio_state.records.capital import (
        ActiveRiskParameterSet,
        RegimeLabel,
        RegimeTransitionState,
        RiskBudgetConsumption,
    )
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
        portfolio_value_usd=100_000.0,
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
            _PHASE_3_TICKER: IvSurfaceEntry(
                underlying=_PHASE_3_TICKER,
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
    market = MarketInputs(
        underlying_prices=MappingProxyType({_PHASE_3_TICKER: 800.0}),
        risk_free_rate=0.045,
        iv_provider=iv_provider,
        as_of=_NOW,
    )
    return (
        ValidationToolState(
            invocation_id=_PHASE_3_INVOCATION_ID,
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
            sector_resolver=lambda _t: "semis",
            accumulated_deltas=(),
        ),
        library_config,
        market,
    )


def _phase_3_pre_processor_bundle() -> Any:
    """Build a minimal bundle whose recommendations contain the source_recommendation_id."""
    from alphamind.decision.analyst.models import Recommendation
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
    rec = Recommendation.model_construct(recommendation_id=_PHASE_3_RECOMMENDATION_ID)
    wrapped = WrappedRecommendation.model_construct(
        recommendation=rec,
        pre_processor_annotations=AnalystSideAnnotations(conflicts=()),
    )
    return ProposalPreProcessorBundle.model_construct(
        invocation_id=_PHASE_3_INVOCATION_ID,
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


def _phase_3_pm_view() -> Any:
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

    return PortfolioManagerView.model_construct(
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


def _phase_3_retrieval_store() -> Any:
    from alphamind.analysis.synthesizer.retrieval import RetrievalStore

    return RetrievalStore(entries={}, freshness_by_source={})


async def _seed_phase_3_substrate(factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed process_lifetime + cash_ledger so the engine-stub's writeback can
    reserve capital and the activity-log writes have a valid invocation FK."""
    from alphamind.execution.state_persistence.invocation_context.records import (
        ProcessLifetimeRecord,
        process_lifetime_record_to_row,
    )
    from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
        cash_ledger_record_to_row,
    )
    from alphamind.portfolio_state.records.cash import CashLedger

    proc = ProcessLifetimeRecord(
        process_lifetime_id=_PHASE_3_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="verify-oms-commands",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/verify-oms-commands/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="verify-script",
    )
    cash = CashLedger.model_validate(
        {
            "current_cash_usd": 100_000.0,
            "settled_cash_usd": 100_000.0,
            "reserved_capital_usd": 0.0,
            "available_buying_power_usd": 100_000.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(proc))
        await sess.flush()
        sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
        await sess.commit()


def _phase_3_invocation_record() -> Any:
    from alphamind.execution.state_persistence.invocation_context.records import (
        InvocationRecord,
    )

    return InvocationRecord(
        invocation_id=_PHASE_3_INVOCATION_ID,
        process_lifetime_id=_PHASE_3_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="verify_oms_commands.py",
        trigger_reason="operator-triggered verification",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/verify-oms-commands/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/verify-oms-commands/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _invoke_submit_envelope_tool(
    server: Any, envelope_payload: dict[str, Any]
) -> tuple[str, bool]:
    """Drive the in-process MCP server's tools/call handler.

    Mirrors the helper in ``tests/execution/oms/test_submit_envelope_mcp.py``;
    encodes the same plumbing so the verify script honours the spec's
    "build an MCP server via build_submit_envelope_mcp_server, call the tool"
    requirement.
    """
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
    if not content_blocks:
        msg = "submit_envelope tool returned no content blocks"
        raise RuntimeError(msg)
    text_block = content_blocks[0]
    return str(text_block.text), bool(result.root.isError)


async def run_phase_3_pm_envelope_path(db_path: Path) -> PhaseResult:
    """Construct a PMEnvelope with one OPEN, drive it through the engine-stub
    MCP server bound to a real ``InvocationContext`` against *db_path*, and
    assert (a) the per-command result is ``accepted`` with a PM-pattern
    command_id, (b) the ``capital_reserved`` activity-log entry's
    ``amount_usd`` equals the command's real ``dollar_value`` (proves the
    retired $1k stub is gone), (c) the bracket carries one leg per
    ``invalidation_legs`` entry, (d) the thesis carries one component per
    ``thesis.components`` entry, (e) the activity log carries
    ``order_submitted`` + ``thesis_created`` + ``capital_reserved`` +
    ``pm_decision``."""
    from alphamind.execution.oms import build_submit_envelope_mcp_server
    from alphamind.execution.oms.submit_envelope_mcp import (
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.state_persistence.invocation_context.context import (
        InvocationContext,
    )

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_phase_3_substrate(factory)

        validation_state, library_config, library_market = _phase_3_validation_state()
        envelope = _phase_3_envelope()

        # Bind state cell + MCP server to the open invocation handle.
        ctx = InvocationContext(
            session_factory=factory,
            record=_phase_3_invocation_record(),
        )
        handle = await ctx.__aenter__()
        try:
            state = build_initial_submit_envelope_state(
                invocation_id=_PHASE_3_INVOCATION_ID,
                starting_validation_state=validation_state,
            )
            mcp_servers, _allowed = build_submit_envelope_mcp_server(
                state,
                retrieval_store=_phase_3_retrieval_store(),
                pre_processor_bundle=_phase_3_pre_processor_bundle(),
                pm_view=_phase_3_pm_view(),
                active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
                halt_mode=False,
                sector_resolver=lambda _t: "semis",
                library_config=library_config,
                library_market=library_market,
                invocation_handle=handle,
                state_persistence_config=_state_persistence_config(),
            )
            server = mcp_servers["alphamind_execution_oms_submit"]["instance"]

            response_text, is_error = await _invoke_submit_envelope_tool(
                server, envelope.model_dump(mode="json")
            )
            if is_error:
                return PhaseResult(
                    label=_PHASE_3_LABEL,
                    ok=False,
                    detail=f"submit_envelope tool returned error: {response_text}",
                )

            shape_failure = _phase_3_check_response_shape(response_text)
            if shape_failure is not None:
                await ctx.__aexit__(None, None, None)
                return PhaseResult(label=_PHASE_3_LABEL, ok=False, detail=shape_failure)
        except BaseException:
            await ctx.__aexit__(None, None, None)
            raise
        else:
            await ctx.__aexit__(None, None, None)

        validation_failure = await _phase_3_validate_state(factory, envelope)
        if validation_failure is not None:
            return PhaseResult(label=_PHASE_3_LABEL, ok=False, detail=validation_failure)

        return PhaseResult(label=_PHASE_3_LABEL, ok=True)
    finally:
        await engine.dispose()


_PM_COMMAND_ID_REGEX = r"^inv-[^.]+\.ENV-(REC|SA|SA-ORD)-[0-9]+\.[0-9]+\.[0-9]+$"


def _phase_3_check_response_shape(response_text: str) -> str | None:
    """Confirm the per-command result is accepted with a PM-originated command_id."""
    import re

    payload = json.loads(response_text)
    submission_results = payload.get("submission_results", [])
    if len(submission_results) != 1:
        return f"expected exactly one submission_result, got {len(submission_results)}"
    sr = submission_results[0]
    if sr.get("status") != "accepted":
        return f"submission_result.status={sr.get('status')!r}, expected 'accepted'"
    command_id = sr.get("command_id", "")
    if not re.match(_PM_COMMAND_ID_REGEX, command_id):
        return f"command_id {command_id!r} does not match PM-originated regex"
    return None


_PHASE_3_REQUIRED_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.ORDER_SUBMITTED,
        EventType.THESIS_CREATED,
        EventType.CAPITAL_RESERVED,
        EventType.PM_DECISION,
    }
)


async def _phase_3_validate_state(
    factory: async_sessionmaker[AsyncSession], envelope: Any
) -> str | None:
    """Open one session and run the per-check chain against it; return the
    first failure-detail string or ``None`` when every check passes."""
    open_command = envelope.commands[0]
    expected_dollar = open_command.position_size.dollar_value
    expected_bracket_legs = len(open_command.invalidation_legs) + 1
    min_thesis_components = len(open_command.thesis.components)

    async with factory() as sess:
        return await _phase_3_run_check_chain(
            sess,
            expected_dollar=expected_dollar,
            expected_bracket_legs=expected_bracket_legs,
            min_thesis_components=min_thesis_components,
        )


async def _phase_3_run_check_chain(
    sess: AsyncSession,
    *,
    expected_dollar: float,
    expected_bracket_legs: int,
    min_thesis_components: int,
) -> str | None:
    """Execute the Phase 3 check chain in dependency order against *sess*."""
    log_rows = await _phase_3_load_activity_log(sess)

    log_failure = _phase_3_check_activity_log(log_rows, expected_dollar)
    if log_failure is not None:
        return log_failure

    cash_failure = await _phase_3_check_cash_ledger(sess, expected_dollar)
    if cash_failure is not None:
        return cash_failure

    thesis_id, position_id = _phase_3_resolve_thesis_position(log_rows)
    if thesis_id is None or position_id is None:
        return "thesis_created entry missing thesis_id or position_id"

    thesis_failure = await _phase_3_check_thesis(sess, thesis_id, min_thesis_components)
    if thesis_failure is not None:
        return thesis_failure

    return await _phase_3_check_bracket(sess, position_id, expected_bracket_legs)


async def _phase_3_load_activity_log(sess: AsyncSession) -> Sequence[Any]:
    """Return every ``activity_log`` row scoped to the Phase 3 invocation."""
    from alphamind.execution.state_persistence.tables.activity_log import (
        ActivityLogRow,
    )

    return (
        (
            await sess.execute(
                select(ActivityLogRow).where(ActivityLogRow.invocation_id == _PHASE_3_INVOCATION_ID)
            )
        )
        .scalars()
        .all()
    )


def _phase_3_check_activity_log(log_rows: Sequence[Any], expected_dollar: float) -> str | None:
    """Confirm every required event lands AND the ``capital_reserved`` row's
    ``amount_usd`` matches the canonical command's ``dollar_value`` (the
    regression-to-$1k-stub guard)."""
    observed_events = {EventType(r.event_type) for r in log_rows}
    missing = _PHASE_3_REQUIRED_EVENTS - observed_events
    if missing:
        return "activity log missing required events: " + ", ".join(
            sorted(e.value for e in missing)
        )
    return _phase_3_check_capital_reserved(log_rows, expected_dollar)


def _phase_3_check_capital_reserved(log_rows: Sequence[Any], expected_dollar: float) -> str | None:
    """Confirm exactly one ``capital_reserved`` row whose ``amount_usd`` matches
    the canonical command's ``dollar_value`` — the regression-to-$1k-stub guard."""
    rows = [r for r in log_rows if r.event_type == EventType.CAPITAL_RESERVED.value]
    if len(rows) != 1:
        return f"expected exactly 1 capital_reserved entry, got {len(rows)}"
    detail = json.loads(rows[0].detail_json)
    amount_usd = detail.get("amount_usd")
    if amount_usd != expected_dollar:
        return (
            f"capital_reserved.amount_usd={amount_usd!r}, expected "
            f"{expected_dollar!r} (regression to retired $1k stub?)"
        )
    return None


async def _phase_3_check_cash_ledger(sess: AsyncSession, expected_dollar: float) -> str | None:
    """Confirm the cash_ledger singleton's ``reserved_capital_usd`` matches the
    canonical command's ``dollar_value``."""
    from alphamind.execution.state_persistence.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )

    cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if cash_row is None:
        return "cash_ledger singleton row missing after Phase 2 writeback"
    if cash_row.reserved_capital_usd != expected_dollar:
        return (
            f"cash_ledger.reserved_capital_usd={cash_row.reserved_capital_usd!r}, "
            f"expected {expected_dollar!r}"
        )
    return None


def _phase_3_resolve_thesis_position(
    log_rows: Sequence[Any],
) -> tuple[str | None, str | None]:
    """Return the ``(thesis_id, position_id)`` from the first ``thesis_created`` row."""
    rows = [r for r in log_rows if r.event_type == EventType.THESIS_CREATED.value]
    if not rows:
        return None, None
    return rows[0].thesis_id, rows[0].position_id


async def _phase_3_check_thesis(
    sess: AsyncSession, thesis_id: str, min_components: int
) -> str | None:
    """Confirm one persisted thesis row + ≥ ``min_components`` component rows.

    The writeback's coverage backfill guarantees one component per
    ``ThesisComponentType``; ``>=`` lets the verify tolerate the backfill while
    still catching a regression that drops wire-side components.
    """
    from alphamind.execution.state_persistence.tables.theses import ThesisRow
    from alphamind.execution.state_persistence.tables.thesis_components import (
        ThesisComponentRow,
    )

    thesis_rows = (
        (await sess.execute(select(ThesisRow).where(ThesisRow.thesis_id == thesis_id)))
        .scalars()
        .all()
    )
    if len(thesis_rows) != 1:
        return f"expected exactly 1 thesis row for {thesis_id!r}, got {len(thesis_rows)}"
    component_rows = (
        (
            await sess.execute(
                select(ThesisComponentRow).where(ThesisComponentRow.thesis_id == thesis_id)
            )
        )
        .scalars()
        .all()
    )
    if len(component_rows) < min_components:
        return f"expected at least {min_components} thesis components, got {len(component_rows)}"
    return None


async def _phase_3_check_bracket(
    sess: AsyncSession, position_id: str, expected_legs: int
) -> str | None:
    """Confirm one bracket row for the position + the expected leg count
    (one TAKE_PROFIT + one per ``command.invalidation_legs``)."""
    from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
    from alphamind.execution.state_persistence.tables.brackets import BracketRow

    bracket_rows = (
        (await sess.execute(select(BracketRow).where(BracketRow.position_id == position_id)))
        .scalars()
        .all()
    )
    if len(bracket_rows) != 1:
        return f"expected exactly 1 bracket for position {position_id!r}, got {len(bracket_rows)}"
    bracket_id = bracket_rows[0].bracket_id
    leg_rows = (
        (await sess.execute(select(BracketLegRow).where(BracketLegRow.bracket_id == bracket_id)))
        .scalars()
        .all()
    )
    if len(leg_rows) != expected_legs:
        return (
            f"expected {expected_legs} bracket legs (one TAKE_PROFIT + one per "
            f"invalidation_leg), got {len(leg_rows)}"
        )
    return None


# ---------------------------------------------------------------------------
# Phase 4 — engine envelope path against the same DB
# ---------------------------------------------------------------------------


_PHASE_4_LABEL = "Phase 4 — engine envelope path"

_PHASE_4_INVOCATION_ID = "inv-verify-oms-commands-phase4"
_PHASE_4_MONITOR_SESSION = "session-verify-oms"
_PHASE_4_TRIGGER_ID = 7
_PHASE_4_RATIONALE = "smallest position triggering per-position max loss"
_PHASE_4_RULE_BREACHED = "per_position_max_loss"


def _phase_4_invocation_record() -> Any:
    from alphamind.execution.state_persistence.invocation_context.records import (
        InvocationRecord,
    )

    return InvocationRecord(
        invocation_id=_PHASE_4_INVOCATION_ID,
        process_lifetime_id=_PHASE_3_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="verify_oms_commands.py",
        trigger_reason="phase 4 engine envelope path",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/verify-oms-commands/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/verify-oms-commands/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _phase_4_resolve_position_id(
    factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Look up the position_id Phase 3 wrote (the NVDA OPEN-target position).

    Returns ``None`` if nothing matches — the verify orchestrator catches that
    and surfaces a useful diagnostic (Phase 3 must run first)."""
    from alphamind.execution.state_persistence.tables.positions import PositionRow

    async with factory() as sess:
        row = (
            (await sess.execute(select(PositionRow).where(PositionRow.thesis_id.is_not(None))))
            .scalars()
            .first()
        )
        return row.position_id if row is not None else None


async def run_phase_4_engine_envelope_path(db_path: Path) -> PhaseResult:
    """Construct an :class:`EngineEnvelope` carrying one CLOSE for the
    Phase-3-opened position, drive it through ``submit_engine_envelope``,
    and assert (a) ``SubmissionResult.status == "accepted"`` with an
    engine-pattern command_id, (b) one ``order_submitted`` activity-log
    entry exists whose detail carries
    ``risk_management_subtype="engine_guardrail"`` and the trigger record's
    ``position_selection_rationale`` + ``rule_breached``."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope
    from alphamind.execution.state_persistence.invocation_context.context import (
        InvocationContext,
    )

    engine, factory = _open_async_factory(db_path)
    try:
        position_id = await _phase_4_resolve_position_id(factory)
        if position_id is None:
            return PhaseResult(
                label=_PHASE_4_LABEL,
                ok=False,
                detail="no position found to close (run Phase 3 first)",
            )

        envelope = _phase_4_engine_envelope(position_id=position_id)
        state = build_initial_submit_engine_envelope_state(
            monitor_session_id=_PHASE_4_MONITOR_SESSION,
        )

        ctx = InvocationContext(
            session_factory=factory,
            record=_phase_4_invocation_record(),
        )
        handle = await ctx.__aenter__()
        try:
            result = await submit_engine_envelope(
                envelope,
                handle=handle,
                state=state,
                config=_state_persistence_config(),
            )
        except BaseException:
            await ctx.__aexit__(None, None, None)
            raise
        else:
            await ctx.__aexit__(None, None, None)

        result_failure = _phase_4_check_result_shape(result)
        if result_failure is not None:
            return PhaseResult(label=_PHASE_4_LABEL, ok=False, detail=result_failure)

        log_failure = await _phase_4_validate_activity_log(factory)
        if log_failure is not None:
            return PhaseResult(label=_PHASE_4_LABEL, ok=False, detail=log_failure)

        return PhaseResult(label=_PHASE_4_LABEL, ok=True)
    finally:
        await engine.dispose()


def _phase_4_engine_envelope(*, position_id: str) -> EngineEnvelope:
    trigger_ts = _NOW
    return EngineEnvelope(
        envelope_id=f"MON.{_PHASE_4_MONITOR_SESSION}.{_PHASE_4_TRIGGER_ID}",
        invocation_id=None,
        trigger_timestamp=trigger_ts,
        source_provenance="engine_guardrail",
        guardrail_trigger_record=GuardrailTriggerRecord(
            rule_breached=_PHASE_4_RULE_BREACHED,
            trigger_timestamp=trigger_ts,
            breach_details=BreachDetails(
                current_value=12_500.0,
                limit_value=10_000.0,
                overage=2_500.0,
                unit="usd",
                regime_at_breach="risk_off",
            ),
            position_selection_rationale=_PHASE_4_RATIONALE,
            cascade_id=None,
            secondary_breach_check_result=None,
        ),
        commands=(
            CloseCommand(
                command_type="close",
                position_id=position_id,
                quantity="all",
                order_type="market",
                limit_price=None,
                close_rationale_type="risk_management",
                invalidation_reason=None,
                risk_management_subtype="engine_guardrail",
            ),
        ),
    )


_ENGINE_COMMAND_ID_REGEX = r"^MON\.[^.]+\.[0-9]+\.[0-9]+$"


def _phase_4_check_result_shape(result: Any) -> str | None:
    import re

    if result.status != "accepted":
        return f"submit_engine_envelope returned status={result.status!r}, expected 'accepted'"
    if not re.match(_ENGINE_COMMAND_ID_REGEX, result.command_id):
        return f"command_id {result.command_id!r} does not match engine-originated regex"
    return None


async def _phase_4_validate_activity_log(
    factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Confirm one ``order_submitted`` row landed for the Phase-4 invocation,
    whose detail carries the engine-guardrail provenance + the trigger
    record's ``position_selection_rationale``."""
    from alphamind.execution.state_persistence.tables.activity_log import (
        ActivityLogRow,
    )
    from alphamind.portfolio_state.events.activity_log import EventType

    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.invocation_id == _PHASE_4_INVOCATION_ID,
                        ActivityLogRow.event_type == EventType.ORDER_SUBMITTED.value,
                    )
                )
            )
            .scalars()
            .all()
        )

    if len(rows) != 1:
        return f"expected exactly 1 order_submitted row, got {len(rows)}"
    detail = json.loads(rows[0].detail_json)
    params = detail.get("order_parameters_json", {})
    if params.get("risk_management_subtype") != "engine_guardrail":
        return (
            "order_submitted detail missing risk_management_subtype='engine_guardrail' "
            "(engine-guardrail provenance regression)"
        )
    if params.get("position_selection_rationale") != _PHASE_4_RATIONALE:
        return (
            f"order_submitted detail position_selection_rationale="
            f"{params.get('position_selection_rationale')!r}, expected {_PHASE_4_RATIONALE!r}"
        )
    if params.get("rule_breached") != _PHASE_4_RULE_BREACHED:
        return (
            f"order_submitted detail rule_breached="
            f"{params.get('rule_breached')!r}, expected {_PHASE_4_RULE_BREACHED!r}"
        )
    return None


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _render_text(results: Sequence[PhaseResult]) -> str:
    lines = ["=" * 70, "AlphaMind OMS Commands Verification", "=" * 70]
    pass_count = sum(1 for r in results if r.ok)
    for r in results:
        marker = "[PASS]" if r.ok else "[FAIL]"
        lines.append(f"  {marker} {r.label}")
        if r.detail:
            lines.append(f"      {r.detail}")
    lines.append("=" * 70)
    if pass_count == len(results):
        lines.append(f"RESULT: PASS ({pass_count}/{len(results)} criteria)")
    else:
        failed = [r.label for r in results if not r.ok]
        lines.append(
            f"RESULT: FAIL ({pass_count}/{len(results)} criteria, failing: {', '.join(failed)})"
        )
    lines.append("=" * 70)
    return "\n".join(lines)


def _render_json(results: Sequence[PhaseResult]) -> str:
    import json

    payload: dict[str, Any] = {
        "all_pass": all(r.ok for r in results),
        "phases": [{"label": r.label, "ok": r.ok, "detail": r.detail} for r in results],
    }
    return json.dumps(payload, indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the OMS commands surface end-to-end against a freshly-"
            "migrated SQLite DB across five phases (canonical model round-trip, "
            "command-ID utility, PM envelope path, engine envelope path, summary)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to a freshly-migrated SQLite DB (overrides DATABASE_PATH and main.yaml).",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser


async def _run_all_phases(db_path: Path) -> list[PhaseResult]:
    results = [
        run_phase_1_canonical_round_trip(),
        run_phase_2_command_id_utility(),
        await run_phase_3_pm_envelope_path(db_path),
    ]
    # Phase 4 closes the position Phase 3 opened; only run if Phase 3 passed.
    if results[-1].ok:
        results.append(await run_phase_4_engine_envelope_path(db_path))
    else:
        results.append(
            PhaseResult(
                label=_PHASE_4_LABEL,
                ok=False,
                detail="skipped — Phase 3 did not pass",
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on all-PASS, 1 if any phase FAILed."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = args.db_path
    if db_path is None:
        engine = make_engine(None)
        db_path = Path(str(engine.url.database or ""))
        engine.dispose()

    results = asyncio.run(_run_all_phases(db_path))

    if args.output == "json":
        print(_render_json(results))
    else:
        print(_render_text(results))

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
