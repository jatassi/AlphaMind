"""Standalone end-to-end verify for the feedback-loop spine (ALP-896 / story 10).

The feedback loop runs **out-of-pipeline** (operator CLIs + a per-invocation
resolver step), so ``scripts/verify/verify_debug_e2e.py`` does not cover it. This script
is the deterministic operator check after a code change or a migration that
touches the feedback-loop tables: it runs ``alembic upgrade head`` on a fresh
scratch DB, seeds one controlled scenario (a CLOSED position + its linked ACTIVE
thesis, a ``thesis_pnl_ledger`` row, a ``POSITION_CLOSED`` exit entry, ``agent_calls``
telemetry, PM-decision envelopes, and a pre-registered validation), then drives the
spine end-to-end:

1. **resolution** — runs the thesis resolver (story 04e) so the closed-position
   ACTIVE thesis is authored ``ACTIVE → RESOLVED`` with a category + per-component
   outcomes;
2. **telemetry + outcome read** — loads the metrics ``WindowDataset`` over the
   resolution window and confirms the resolved thesis flows into ``outcomes`` and
   the in-window ``agent_calls``;
3. **metric** — computes a representative outcome metric (win-rate) over the
   resolved thesis and confirms a non-empty value (the ALP-834 regression: the
   resolved thesis is both returned by ``get_recent_thesis_resolutions`` and
   computes a metric value);
4. **digest + snapshot** — generates a :class:`WeeklyDigest` and snapshots it
   idempotently into ``weekly_digest_snapshots``;
5. **validation register + evaluate** — registers a validation, then evaluates it
   to a written ``validation_outcomes`` record;
6. **supersession** — runs the mid-window supersession detector;
7. **retrospective** — ingests the Phase-1 retrospective data set and saves a
   report.

The **seed is the ground truth**: :func:`build_test_seed` inserts the documented
rows and returns a :class:`FeedbackLoopSeed` carrying the identifiers + the watched
metric id the per-stage assertions read. The pure ``assert_*`` helpers take a
loaded stage result (and the seed) and return a failure message or ``None``, so each
is unit-testable in isolation (``tests/scripts/test_verify_feedback_loop.py``). The
full end-to-end run is exercised by invoking this script — it is the
verify-of-the-verify and is not embedded in pytest.

**Mock boundaries.** The database is the sole sanctioned data mock (a throwaway
on-disk scratch DB). The resolver's LLM-fallback evaluator fires for the qualitative
``ENTRY_RATIONALE`` component (always ``INCONCLUSIVE`` programmatically); its Claude
Agent SDK call is satisfied by an injected ``sdk_query_fn`` stub — the one sanctioned
LLM-boundary fake, so no run touches the Anthropic API.

Usage::

    uv run python scripts/verify/verify_feedback_loop.py [--db-path PATH]

Exit codes: ``0`` on full PASS, ``1`` on any FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Side-effect import: register the state-persistence tables on ``Base.metadata``
# so the codecs + reads resolve the mappers.
import alphamind.state.tables  # noqa: F401
from alphamind._kernel.ids import EnvelopeId, PositionId, ReplayId, ThesisId
from alphamind._kernel.money import money, signed_money
from alphamind.analysis.thesis_resolution.resolver import (
    ResolvedThesis,
    resolve_closed_position_theses,
)
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.feedback import FeedbackLoopConfig
from alphamind.feedback_loop.dataset import WindowDataset, load_window
from alphamind.feedback_loop.digest.generator import WeeklyDigest, generate_digest
from alphamind.feedback_loop.digest.snapshot import SnapshotOutcome, snapshot_week
from alphamind.feedback_loop.digest.windows import load_week_inputs, trailing_weeks
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.outcomes import METRIC_WIN_RATE
from alphamind.feedback_loop.metrics.pm_accuracy import METRIC_MODIFICATION_EFFECTIVENESS
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricResult
from alphamind.feedback_loop.retrospective.ingestion import RetrospectiveIngestion, ingest_window
from alphamind.feedback_loop.retrospective.report import save_report
from alphamind.feedback_loop.validation.evaluate import (
    EvaluationJudgments,
    EvaluationResult,
    evaluate_validation,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    OutcomeId,
    ValidationId,
)
from alphamind.feedback_loop.validation.register import RegistrationRequest, register_validation
from alphamind.feedback_loop.validation.supersession import detect_supersessions
from alphamind.persistence.alembic_upgrade import upgrade_to_head
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import EventGroup, PMVerdict
from alphamind.portfolio_state.records.positions import InstrumentType
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    RecentThesisResolution,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)
from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
    build_active_risk_parameters,
)
from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
    make_repository_providers,
)
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.repository import build_sql_portfolio_state_repository
from alphamind.state.repository.counterfactual_replays import insert_counterfactual_replay
from alphamind.state.tables.agent_calls import AgentCallsRow
from alphamind.state.tables.counterfactual_replays import (
    Confidence,
    CounterfactualReplayRecord,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
)
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow
from alphamind.state.tables.theses_codec import record_to_rows
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow

__all__ = [
    "FeedbackLoopSeed",
    "StageResults",
    "assert_digest_generated",
    "assert_outcome_metric_nonempty",
    "assert_recent_resolutions_regression",
    "assert_replay_join_computes",
    "assert_retrospective_saved",
    "assert_snapshot_written",
    "assert_supersession_detected",
    "assert_thesis_resolved",
    "assert_validation_evaluated",
    "assert_window_loaded",
    "build_test_seed",
    "main",
    "run_verification",
]

# ---------------------------------------------------------------------------
# Seed clock + controlled identifiers
# ---------------------------------------------------------------------------
#: The repo root (``…/scripts/verify/verify_feedback_loop.py`` → repo root): the alembic.ini
#: + config dir anchor. Resolved once at import (a sync context) so the I/O-driven
#: async runner never stats the filesystem itself.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The whole scenario lands inside a single ISO week so one ``[start, end)`` window
# covers it. ``_RESOLVE_NOW`` is the resolver's ``now`` — it stamps the thesis's
# ``resolution_timestamp``, which ``read_resolved_theses_in_window`` filters on, so
# it MUST fall inside ``[_WINDOW_START, _WINDOW_END)`` or the resolved thesis would
# not flow into the outcomes bundle (the single most likely silent failure).
_WEEK_MONDAY = datetime(2026, 5, 4, tzinfo=UTC).date()  # a Monday
_WINDOW_START = datetime(2026, 5, 4, 0, 0, 0, tzinfo=UTC)
_WINDOW_END = _WINDOW_START + timedelta(days=7)
_GENERATION = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_RESOLVE_NOW = datetime(2026, 5, 6, 14, 0, 0, tzinfo=UTC)
_INVOCATION_START = datetime(2026, 5, 5, 9, 30, 0, tzinfo=UTC)

_PLT_ID = "plt-fbl-verify"
_INVOCATION_ID = "inv-2026-05-05T09:30:00Z-fbl"
_POSITION_ID = "pos-fbl-verify"
_THESIS_ID = "thesis-fbl-verify"
_VALIDATION_ID = "val-fbl-verify"
_OUTCOME_ID = "outcome-fbl-verify"
_MODEL_ID = AllowedModel.sonnet_4_6.value
_ENTRY_TICKER = "NVDA"
_ENTRY_COST_BASIS = 150.0
# Positive realized P/L so the win-rate predicate yields value=1.0 (an unambiguous
# non-empty outcome reading); the exit method partitions the resolution category.
_REALIZED_PNL_USD = 500.0

#: The PM-decision envelope id — the join anchor between the PM_DECISION activity-log
#: entry and the counterfactual replay that descends from it.
_ENVELOPE_ID = "env-fbl-verify"
#: One canonical anti-pattern tag (``commands.pm_envelope.AntiPattern``) the PM
#: flagged on the verdict — the ``anti_patterns_json`` the detector metrics read.
_ANTI_PATTERN = "conviction_inflation"
#: The counterfactual original-form P/L for the modification replay. Strictly below
#: ``_REALIZED_PNL_USD`` so ``_modification_helped`` reads True (the actual modified
#: form beat the simulated original form) and the modification metric computes a value.
_COUNTERFACTUAL_PNL_USD = 100.0
#: The replay's engine version — the loader filters to the single latest version
#: present, so one fresh constant suffices for this single-replay scenario.
_REPLAY_ENGINE_VERSION = "fbl-verify-v1"
#: The counterfactual replay id.
_REPLAY_ID = "replay-fbl-verify"

#: Trailing weeks the digest + snapshot load. One suffices for this single-week
#: scenario (the trajectory degrades to one point); the read CLI default is 12.
_TRAJECTORY_WEEKS = 1

#: The prompt the LLM-fallback evaluator config points at (the committed 04d seam).
_EVALUATOR_PROMPT = "prompts/analysis/thesis_component_evaluator.md"


# ---------------------------------------------------------------------------
# Ground-truth seed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedbackLoopSeed:
    """The ground truth a :func:`build_test_seed` run establishes.

    Carries the identifiers + the watched metric id the per-stage assertions read.
    ``window_start`` / ``window_end`` bound the metrics window the resolved thesis
    must land in; ``resolve_now`` is the resolution timestamp.
    """

    invocation_id: str
    position_id: str
    thesis_id: str
    validation_id: str
    outcome_id: str
    watched_metric_id: MetricId
    window_start: datetime
    window_end: datetime
    week_monday: date
    resolve_now: datetime
    expected_resolution_category: ThesisResolutionCategory


def _process_lifetime_row() -> ProcessLifetimeRow:
    return ProcessLifetimeRow(
        process_lifetime_id=_PLT_ID,
        process_role="monitor",
        process_start_at=_INVOCATION_START.isoformat(),
        process_pid=1,
        hostname="verify-host",
        git_sha="0" * 40,
        git_branch="main",
        git_dirty=0,
        python_version="3.13.0",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="snapshot/path",
        anthropic_sdk_version="0.0.0",
        claude_agent_sdk_version="0.0.0",
        os_release="verify-os",
    )


def _invocation_row() -> InvocationRow:
    return InvocationRow(
        invocation_id=_INVOCATION_ID,
        process_lifetime_id=_PLT_ID,
        start_at=_INVOCATION_START.isoformat(),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="market_open",
        trigger_reason="verify-fixture",
        git_sha_at_invocation="0" * 40,
        active_profile="default",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="snapshot/path",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="snapshot/path",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=0,
        snapshot_metadata_json=None,
    )


def _agent_call_row() -> AgentCallsRow:
    """One in-window ``agent_calls`` telemetry row (the prod-runtime behavior).

    Establishes both the window's non-empty ``agent_calls`` bundle and the model-id
    provenance ``register_validation`` snapshots from the registering invocation.
    """
    return AgentCallsRow(
        agent_call_id="ac-fbl-verify-1",
        invocation_id=_INVOCATION_ID,
        agent_name="synthesizer",
        attempt_number=1,
        model_id=_MODEL_ID,
        prompt_path="prompts/analysis/synthesizer.md",
        prompt_git_sha="0" * 40,
        prompt_content_hash="0" * 64,
        sampling_params_json="{}",
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=500,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


def _active_thesis() -> ThesisRecord:
    """An ACTIVE thesis with all three mandatory component types.

    ``ENTRY_RATIONALE`` is always ``INCONCLUSIVE`` programmatically, so the
    resolver's LLM fallback fires for it (the injected SDK stub supplies the
    verdict); ``TARGET_RATIONALE`` / ``INVALIDATION_RATIONALE`` resolve
    programmatically from the exit method.
    """
    components = tuple(
        ThesisComponent(
            component_id=f"{_THESIS_ID}-{ct.value}",
            thesis_id=ThesisId(_THESIS_ID),
            component_type=ct,
            linked_bracket_leg_type=None,
            instrument_reference=_ENTRY_TICKER,
            narrative=f"{ct.value} narrative.",
            key_assumptions=(KeyAssumption(text="A falsifiable claim.", outcome=None),),
            generation_timestamp=_GENERATION,
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    return ThesisRecord(
        thesis_id=ThesisId(_THESIS_ID),
        position_id=PositionId(_POSITION_ID),
        summary="Feedback-loop verify thesis.",
        key_catalyst="Earnings catalyst.",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_GENERATION,
        time_expectation_hours=24.0,
        age_hours=24.0,
        expected_resolution_at=_GENERATION + timedelta(hours=24.0),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _closed_position_row() -> PositionRow:
    """A CLOSED equity ``PositionRow`` carrying a real entry reference.

    Built by hand (not via ``stub_position_row``, which lives under ``tests/``) so
    the script is self-contained. The CLOSED invariants — non-NULL realized P/L,
    entry timestamp, non-empty execution history — must hold so ``row_to_record``
    rehydrates it for the resolver's entry-reference read.
    """
    return PositionRow(
        position_id=_POSITION_ID,
        thesis_id=_THESIS_ID,
        bracket_id=None,
        status="CLOSED",
        direction="LONG",
        entry_timestamp=_GENERATION.isoformat(),
        instrument_type=InstrumentType.EQUITY.value,
        details_json=(
            f'{{"instrument_type":"{InstrumentType.EQUITY.value}",'
            f'"ticker":"{_ENTRY_TICKER}","share_count":10,'
            f'"average_cost_basis_per_share":{_ENTRY_COST_BASIS}}}'
        ),
        execution_history_json=(
            '[{"fill_timestamp":"' + _GENERATION.isoformat() + '",'
            '"fill_price":150.0,"fill_quantity":10.0,"slippage":0.0,'
            '"fees":0.0,"live_execution_estimate":null}]'
        ),
        realized_pnl_to_date_usd=_REALIZED_PNL_USD,
        corporate_action_adjustment_needed=0,
        parent_position_id=None,
        origin=None,
    )


def _ledger_row() -> ThesisPnlLedgerRow:
    return ThesisPnlLedgerRow(
        thesis_id=_THESIS_ID,
        realized_pnl_usd=Decimal(str(_REALIZED_PNL_USD)),
        cost_basis_usd=Decimal("1500.0"),
        provenance_json="{}",
        derived_from_invocation_id=_INVOCATION_ID,
        updated_at=_RESOLVE_NOW.isoformat().replace("+00:00", "Z"),
        last_derived_event_seq=None,
    )


def _position_closed_entry() -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=f"{_INVOCATION_ID}-POSITION_CLOSED-{uuid.uuid4().hex}",
        invocation_id=_INVOCATION_ID,
        timestamp=_GENERATION + timedelta(hours=6),
        event_type=EventType.POSITION_CLOSED,
        event_group=EVENT_TYPE_TO_GROUP[EventType.POSITION_CLOSED],
        position_id=_POSITION_ID,
        order_id=None,
        thesis_id=_THESIS_ID,
        source=EventSource.FILL_PROCESSOR,
        detail=PositionClosedDetail(
            exit_method=PositionExitMethod.TARGET_REACHED,
            exit_price=money(160.0),
            realized_pnl_usd=signed_money(_REALIZED_PNL_USD),
            thesis_resolution_category="",
        ),
    )


def _pm_decision_entry() -> ActivityLogEntry:
    """One PM_DECISION envelope — the ``pm_decision_log`` + retrospective surface.

    Carries the ``position_id`` (under ``source_provenance_json``, mirroring
    production ``_emit_pm_decision``) so the PM-accuracy replay join keys the
    ``modification_original_form`` replay to the resolved thesis on this position,
    and one canonical anti-pattern tag (the ``anti_patterns_json`` the detector
    metrics read).
    """
    detail = PMDecisionDetail(
        envelope_id=_ENVELOPE_ID,
        source_provenance_json={
            "source_provenance": "pm_analyst",
            "position_id": _POSITION_ID,
        },
        evaluation_json={"thesis_quality": "strong"},
        modifications_json=[],
        resulting_command_ids=("cmd-fbl-1",),
        verdict=PMVerdict.APPROVE,
        originating_proposal_json={"recommendation_id": "REC-FBL"},
        anti_patterns_json=[_ANTI_PATTERN],
    )
    return ActivityLogEntry(
        entry_id="e-fbl-pm-decision",
        invocation_id=_INVOCATION_ID,
        timestamp=_GENERATION,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=_POSITION_ID,
        order_id=None,
        thesis_id=_THESIS_ID,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def _modification_replay_record() -> CounterfactualReplayRecord:
    """One EVALUATED ``MODIFICATION_ORIGINAL_FORM`` counterfactual replay.

    Joined to the seeded PM envelope (``_ENVELOPE_ID``) so the PM-accuracy loader
    flows it through ``_load_replays``: its ``realized_pl`` is the counterfactual
    *original-form* P/L the engine simulated, set strictly below the resolved
    thesis's ``_REALIZED_PNL_USD`` (the actual modified-form leg the join reads via
    ``read_resolved_thesis_pnl_by_position``) so the modified form is scored as
    having helped and ``pm_modification_effectiveness`` computes a real value.
    Medium confidence so it clears the aggregation eligibility rule.
    """
    return CounterfactualReplayRecord(
        replay_id=ReplayId(_REPLAY_ID),
        pm_decision_envelope_id=EnvelopeId(_ENVELOPE_ID),
        replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=True,
        entry_price=money(Decimal(str(_ENTRY_COST_BASIS))),
        entry_timestamp=_GENERATION,
        entry_slippage=money("0.0"),
        entry_fees=money("0.0"),
        exit_leg=ExitLeg.TARGET_HIT,
        exit_price=money("160.0"),
        exit_timestamp=_RESOLVE_NOW,
        exit_slippage=money("0.0"),
        exit_fees=money("0.0"),
        realized_pl=signed_money(_COUNTERFACTUAL_PNL_USD),
        confidence=Confidence.MEDIUM,
        replay_timestamp=_RESOLVE_NOW,
        replay_data_window_start=_GENERATION,
        replay_data_window_end=_RESOLVE_NOW,
        replay_engine_version=_REPLAY_ENGINE_VERSION,
    )


async def build_test_seed(factory: async_sessionmaker[AsyncSession]) -> FeedbackLoopSeed:
    """Insert the documented scenario and return the :class:`FeedbackLoopSeed`.

    One transaction seeds the FK substrate (process-lifetime + invocation), the
    ``agent_calls`` telemetry row, the CLOSED position + ACTIVE thesis + its
    components, the ``thesis_pnl_ledger`` row, the ``POSITION_CLOSED`` exit entry,
    the PM-decision envelope, and one EVALUATED ``MODIFICATION_ORIGINAL_FORM``
    counterfactual replay joined to that envelope (so the PM-accuracy modification
    metric computes a real value end-to-end). The validation is registered in its
    own stage so the script exercises ``register_validation`` rather than seeding
    the row.
    """
    thesis = _active_thesis()
    thesis_row, component_rows = record_to_rows(thesis)
    async with factory() as session:
        session.add(_process_lifetime_row())
        await session.flush()
        session.add(_invocation_row())
        await session.flush()
        session.add(_agent_call_row())
        session.add(_closed_position_row())
        session.add(thesis_row)
        for crow in component_rows:
            session.add(crow)
        await session.flush()
        session.add(_ledger_row())
        session.add(activity_log_entry_to_row(_position_closed_entry()))
        session.add(activity_log_entry_to_row(_pm_decision_entry()))
        await session.run_sync(
            lambda sync_session: insert_counterfactual_replay(
                sync_session, _modification_replay_record()
            )
        )
        await session.commit()

    return FeedbackLoopSeed(
        invocation_id=_INVOCATION_ID,
        position_id=_POSITION_ID,
        thesis_id=_THESIS_ID,
        validation_id=_VALIDATION_ID,
        outcome_id=_OUTCOME_ID,
        watched_metric_id=METRIC_WIN_RATE,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        week_monday=_WEEK_MONDAY,
        resolve_now=_RESOLVE_NOW,
        # TARGET_HIT + positive P/L → VALIDATED (the thesis played out).
        expected_resolution_category=ThesisResolutionCategory.VALIDATED,
    )


# ---------------------------------------------------------------------------
# LLM-boundary stub (the one sanctioned fake)
# ---------------------------------------------------------------------------


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _make_sdk_stub(outcome: str = "VALIDATED") -> Any:
    """An ``sdk_query_fn`` stub returning a structured component verdict.

    Mirrors the resolver tests' stub: yields an ``AssistantMessage`` then a
    ``ResultMessage`` carrying ``structured_output`` the LLM evaluator parses. The
    ENTRY_RATIONALE component falls back to the LLM, so the stub fires exactly once.
    """

    async def _stub(**_kwargs: Any) -> AsyncIterator[Any]:
        from claude_agent_sdk import AssistantMessage, ResultMessage

        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        assistant = AssistantMessage(
            content=[], model="claude-sonnet-4-6", stop_reason="end_turn", usage=usage
        )
        result = ResultMessage(
            subtype="result",
            duration_ms=500,
            duration_api_ms=450,
            is_error=False,
            num_turns=1,
            session_id="sess-fbl",
            stop_reason="end_turn",
            usage=usage,
            structured_output={"outcome": outcome, "notes": "Qualitative read."},
        )
        async for msg in _async_iter([assistant, result]):
            yield msg

    return _stub


def _evaluator_config() -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=_EVALUATOR_PROMPT,
        latency_budget_seconds=120,
        context_token_budget=4000,
        output_token_budget=2000,
        tools=[],
    )


# ---------------------------------------------------------------------------
# Stage results — the loaded shape each pure assert reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageResults:
    """Every stage's loaded output, gathered for the pure assertions.

    The runner fills this as it drives the spine; each field is the result the
    matching ``assert_*`` helper reads (no helper touches a session).
    """

    resolved: tuple[ResolvedThesis, ...]
    recent_resolutions: tuple[RecentThesisResolution, ...]
    window: WindowDataset
    metric_result: MetricResult
    digest: WeeklyDigest
    snapshot: SnapshotOutcome
    evaluation: EvaluationResult
    superseded_count: int
    ingestion: RetrospectiveIngestion
    report_file_ref: str


# ---------------------------------------------------------------------------
# Pure assertion helpers — return a failure message or ``None``
# ---------------------------------------------------------------------------


def assert_thesis_resolved(
    resolved: tuple[ResolvedThesis, ...], seed: FeedbackLoopSeed
) -> str | None:
    """The resolver moved the seeded ACTIVE thesis to RESOLVED with the category."""
    if len(resolved) != 1:
        return f"expected exactly 1 resolved thesis, got {len(resolved)}"
    record = resolved[0].record
    if str(record.thesis_id) != seed.thesis_id:
        return f"expected resolved thesis {seed.thesis_id}, got {record.thesis_id}"
    if record.status is not ThesisRecordStatus.RESOLVED:
        return f"expected status RESOLVED, got {record.status.value}"
    if record.resolution_category is not seed.expected_resolution_category:
        got = record.resolution_category.value if record.resolution_category else None
        return f"expected category {seed.expected_resolution_category.value}, got {got}"
    missing = [c.component_id for c in record.components if c.resolution_outcome is None]
    if missing:
        return f"components without a resolution_outcome: {missing}"
    return None


def assert_recent_resolutions_regression(
    recent: tuple[RecentThesisResolution, ...],
    metric_result: MetricResult,
    seed: FeedbackLoopSeed,
) -> str | None:
    """The ALP-834 regression: the resolved thesis is queryable *and* metric-valued.

    ``get_recent_thesis_resolutions`` must return the resolved thesis, and the
    outcome metric over it must compute a non-empty ``value`` (its small-sample
    ``insufficient_sample`` flag is expected and not asserted on).
    """
    ids = {str(r.thesis_id) for r in recent}
    if seed.thesis_id not in ids:
        return f"resolved thesis {seed.thesis_id} not in get_recent_thesis_resolutions ({ids})"
    if metric_result.value is None:
        return "outcome metric value is None — the resolved thesis computed no reading"
    return None


def assert_window_loaded(window: WindowDataset, seed: FeedbackLoopSeed) -> str | None:
    """The window carries the resolved thesis outcome + the in-window agent_calls."""
    thesis_ids = {str(o.thesis_id) for o in window.outcomes.theses}
    if seed.thesis_id not in thesis_ids:
        return f"resolved thesis {seed.thesis_id} absent from outcomes bundle ({thesis_ids})"
    if not window.agent_calls:
        return "agent_calls bundle is empty — telemetry did not land in the window"
    return None


def assert_replay_join_computes(window: WindowDataset) -> str | None:
    """The PM-accuracy replay join is live end-to-end (ALP-930 (C)).

    The seeded ``MODIFICATION_ORIGINAL_FORM`` replay must flow through
    ``_load_replays`` into ``window.replays.replays`` (non-empty), and the
    ``pm_modification_effectiveness`` metric must compute a real (non-``None``)
    value over it — proving the loader joins the replay to the resolved thesis's
    realized P/L on the originating position and the metric scores it.
    """
    if not window.replays.replays:
        return "window.replays.replays is empty — the seeded replay did not flow through the join"
    metric = get_metric(METRIC_MODIFICATION_EFFECTIVENESS)
    if metric is None:
        return "pm_modification_effectiveness metric is not registered"
    result = metric.compute(window, UNCONDITIONED)
    if result.value is None:
        return "pm_modification_effectiveness value is None — the join computed no reading"
    return None


def assert_outcome_metric_nonempty(
    metric_result: MetricResult, seed: FeedbackLoopSeed
) -> str | None:
    """The representative outcome metric computed over the resolved thesis."""
    if metric_result.metric_id != seed.watched_metric_id:
        return f"expected metric {seed.watched_metric_id}, got {metric_result.metric_id}"
    if metric_result.value is None:
        return "outcome metric value is None over a non-empty window"
    if metric_result.sample_size < 1:
        return f"expected sample_size >= 1, got {metric_result.sample_size}"
    return None


def assert_digest_generated(digest: WeeklyDigest, seed: FeedbackLoopSeed) -> str | None:
    """The digest assembled its sections for the seeded week.

    Confirms the week label, the headline section, and one trajectory point per
    trailing week on the win-rate sparkline (the determinism + assembly contract).
    """
    if digest.week != seed.week_monday.isoformat():
        return f"expected digest week {seed.week_monday.isoformat()}, got {digest.week}"
    if digest.headline is None:
        return "digest headline section is absent"
    if len(digest.trajectory.win_rate.points) != _TRAJECTORY_WEEKS:
        return (
            f"expected {_TRAJECTORY_WEEKS} win-rate trajectory point(s), "
            f"got {len(digest.trajectory.win_rate.points)}"
        )
    return None


def assert_snapshot_written(snapshot: SnapshotOutcome, seed: FeedbackLoopSeed) -> str | None:
    """The snapshot writer inserted a row for the seeded week."""
    if snapshot.week_start != seed.week_monday:
        return f"expected snapshot week_start {seed.week_monday}, got {snapshot.week_start}"
    if not snapshot.written:
        return "snapshot reported written=False on a fresh DB (expected a new row)"
    if not snapshot.snapshot_id:
        return "snapshot did not carry a snapshot_id"
    return None


def assert_validation_evaluated(evaluation: EvaluationResult, seed: FeedbackLoopSeed) -> str | None:
    """The validation evaluated to a written outcome (not superseded)."""
    if evaluation.superseded:
        return f"validation unexpectedly superseded ({evaluation.superseded_reason})"
    if evaluation.outcome is None:
        return "validation evaluation wrote no outcome record"
    if str(evaluation.outcome.validation_id) != seed.validation_id:
        return f"outcome bound to {evaluation.outcome.validation_id}, expected {seed.validation_id}"
    return None


def assert_supersession_detected(superseded_count: int) -> str | None:
    """The supersession detector ran and reported a count (no shift seeded → 0).

    The scenario seeds no mid-window conditioning shift, so the active validation
    is *not* superseded — the detector's correct reading is a clean ``0``. The
    stage confirms the detector executes end-to-end and returns the count.
    """
    if superseded_count != 0:
        return f"expected 0 supersessions (no shift seeded), got {superseded_count}"
    return None


def assert_retrospective_saved(
    ingestion: RetrospectiveIngestion, report_file_ref: str, seed: FeedbackLoopSeed
) -> str | None:
    """The retrospective ingested the window and saved a report row + file."""
    thesis_ids = {str(o.thesis_id) for o in ingestion.window.outcomes.theses}
    if seed.thesis_id not in thesis_ids:
        return f"retrospective window missing resolved thesis {seed.thesis_id}"
    if not report_file_ref:
        return "save_report returned no report_file_ref"
    if "retrospective_reports" not in report_file_ref:
        return f"report_file_ref not rooted under retrospective_reports: {report_file_ref!r}"
    return None


# ---------------------------------------------------------------------------
# Runner — the imperative shell that drives the spine
# ---------------------------------------------------------------------------


def _feedback_config() -> FeedbackLoopConfig:
    return FeedbackLoopConfig(min_resolved_theses_monthly=30, min_resolved_theses_quarterly=60)


def _digest_config() -> DigestConfig:
    """The packaged digest config (notable-shift detector thresholds)."""
    return DigestConfig.model_validate(read_yaml_file(_REPO_ROOT / "config" / "digest.yaml"))


@asynccontextmanager
async def _provisioned_db(db_path: str | None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provision a fresh scratch DB at schema head and yield its async factory.

    ``alembic upgrade head`` needs an on-disk path (its transient engine cannot
    upgrade ``:memory:``), so an omitted *db_path* gets a throwaway temp file. The
    async engine is disposed on exit.
    """
    if db_path is None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = str(Path(tmp) / "alphamind-fbl-verify.db")
            async with _open_after_upgrade(scratch) as factory:
                yield factory
    else:
        async with _open_after_upgrade(db_path) as factory:
            yield factory


@asynccontextmanager
async def _open_after_upgrade(
    scratch: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Upgrade *scratch* to schema head, yield its async factory, dispose on exit.

    A proper async context manager (not a bare async generator) so the engine is
    disposed promptly when the body raises — otherwise the dispose is deferred to
    GC and, on Windows, the open connection's file lock can fail the enclosing
    ``TemporaryDirectory`` cleanup and shadow the original error.
    """
    upgrade_to_head(scratch, repo_root=_REPO_ROOT)
    engine = make_async_engine(scratch)
    try:
        yield make_async_session_factory(engine)
    finally:
        await engine.dispose()


async def _run_resolver(
    factory: async_sessionmaker[AsyncSession], seed: FeedbackLoopSeed
) -> tuple[ResolvedThesis, ...]:
    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=seed.invocation_id)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=_evaluator_config(),
            now=seed.resolve_now,
            sdk_query_fn=_make_sdk_stub("VALIDATED"),
        )
        await session.commit()
    return resolved


def _read_recent_resolutions(
    factory: async_sessionmaker[AsyncSession], seed: FeedbackLoopSeed
) -> tuple[RecentThesisResolution, ...]:
    """Read ``get_recent_thesis_resolutions`` via the production repository (ALP-834).

    Builds the real ``SqlPortfolioStateRepository`` through its composition-root
    factory and calls the production method — the ALP-834 regression must exercise
    the same read path the orchestrator's snapshot assembly does, not a re-derived
    query. The risk-parameter providers are required by the factory but unread by
    this method, so a trivially-composed parameter set (empty rule values) suffices.
    """
    from alphamind.config.models.regimes import Regime

    active_provider, prior_provider = make_repository_providers(
        build_active_risk_parameters(rule_values={}, regime=Regime.normal)
    )
    repository = build_sql_portfolio_state_repository(
        session_factory=factory,
        invocation_id=seed.invocation_id,
        active_risk_parameters_provider=active_provider,
        prior_active_risk_parameters_provider=prior_provider,
        config=_state_persistence_config(),
        thesis_quality_aggregates_trailing_windows_days=(5, 20),
    )
    return repository.get_recent_thesis_resolutions(lookback_trading_days=30)


def _state_persistence_config() -> StatePersistenceConfig:
    """A minimal valid ``StatePersistenceConfig`` for the read-only repository.

    The provenance roots are unused by ``get_recent_thesis_resolutions``; only the
    four required fields are supplied.
    """
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 50,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": tempfile.gettempdir(),
            "invocation_provenance_root": tempfile.gettempdir(),
        }
    )


async def _register_validation(
    factory: async_sessionmaker[AsyncSession], seed: FeedbackLoopSeed
) -> None:
    """Register the validation, snapshotting provenance from the seeded invocation."""
    async with factory() as session:
        await session.run_sync(
            lambda sync_session: register_validation(
                sync_session,
                RegistrationRequest(
                    validation_id=seed.validation_id,
                    registering_invocation_id=seed.invocation_id,
                    registered_at=seed.window_start + timedelta(days=1),
                    edited_artifact="prompts/analysis/synthesizer.md",
                    pre_edit_version="v1",
                    post_edit_version="v2",
                    watched_metric_ids=(seed.watched_metric_id,),
                    window_length_days=1,
                    expected_direction=ExpectedDirection.IMPROVED,
                    expected_magnitude="small",
                    success_criterion="win rate up",
                    failure_criterion="win rate down",
                ),
            )
        )
        await session.commit()


async def _evaluate_validation(
    factory: async_sessionmaker[AsyncSession], seed: FeedbackLoopSeed
) -> EvaluationResult:
    async with factory() as session:
        result = await evaluate_validation(
            session,
            validation_id=ValidationId(seed.validation_id),
            outcome_id=OutcomeId(seed.outcome_id),
            evaluated_at=seed.window_end,
            judgments=EvaluationJudgments(
                confounder_flagged=False,
                failure_criterion_crossed=False,
                confounder_notes=None,
            ),
            narrative="Verify-run evaluation.",
            config=_feedback_config(),
        )
        await session.commit()
    return result


async def _detect_supersessions(factory: async_sessionmaker[AsyncSession], repo_root: Path) -> int:
    async with factory() as session:
        marked = await session.run_sync(
            lambda sync_session: detect_supersessions(sync_session, repo_root=repo_root)
        )
        await session.commit()
    return marked


async def _ingest_and_save_retrospective(
    factory: async_sessionmaker[AsyncSession], seed: FeedbackLoopSeed, data_root: Path
) -> tuple[RetrospectiveIngestion, str]:
    async with factory() as session:
        ingestion = await ingest_window(
            session, seed.window_start, seed.window_end, _feedback_config()
        )
        record = await session.run_sync(
            lambda sync_session: save_report(
                sync_session,
                seed.window_start,
                seed.window_end,
                "# Verify retrospective\n\nA rendered report.",
                data_root=data_root,
            )
        )
        await session.commit()
    return ingestion, record.report_file_ref


async def _drive_stages(
    factory: async_sessionmaker[AsyncSession],
    seed: FeedbackLoopSeed,
    *,
    data_root: Path,
) -> StageResults:
    """Drive every stage against the seeded DB, gathering the loaded results."""
    feedback_config = _feedback_config()
    digest_config = _digest_config()

    resolved = await _run_resolver(factory, seed)
    recent = _read_recent_resolutions(factory, seed)

    async with factory() as session:
        window = await load_window(session, seed.window_start, seed.window_end, feedback_config)

    metric = get_metric(seed.watched_metric_id)
    assert metric is not None  # the win-rate metric is always registered
    metric_result = metric.compute(window, UNCONDITIONED)

    async with factory() as session:
        weeks = await load_week_inputs(
            session,
            trailing_weeks(seed.week_monday, _TRAJECTORY_WEEKS),
            feedback_config,
        )
    digest = generate_digest(weeks, digest_config)

    async with factory() as session:
        snapshot = await snapshot_week(
            session,
            seed.week_monday,
            digest_config=digest_config,
            feedback_config=feedback_config,
            trajectory_weeks=_TRAJECTORY_WEEKS,
        )

    await _register_validation(factory, seed)
    evaluation = await _evaluate_validation(factory, seed)
    superseded_count = await _detect_supersessions(factory, _REPO_ROOT)
    ingestion, report_file_ref = await _ingest_and_save_retrospective(factory, seed, data_root)

    return StageResults(
        resolved=resolved,
        recent_resolutions=recent,
        window=window,
        metric_result=metric_result,
        digest=digest,
        snapshot=snapshot,
        evaluation=evaluation,
        superseded_count=superseded_count,
        ingestion=ingestion,
        report_file_ref=report_file_ref,
    )


def _evaluate_assertions(results: StageResults, seed: FeedbackLoopSeed) -> list[tuple[bool, str]]:
    """Run every pure assertion, rendering ``(passed, line)`` per stage."""
    return [
        _check("resolution (ACTIVE → RESOLVED)", assert_thesis_resolved(results.resolved, seed)),
        _check(
            "ALP-834 regression (recent resolutions + metric value)",
            assert_recent_resolutions_regression(
                results.recent_resolutions, results.metric_result, seed
            ),
        ),
        _check("window load (outcomes + agent_calls)", assert_window_loaded(results.window, seed)),
        _check(
            "PM-accuracy replay join (modification effectiveness computes)",
            assert_replay_join_computes(results.window),
        ),
        _check(
            "outcome metric (win-rate over resolved thesis)",
            assert_outcome_metric_nonempty(results.metric_result, seed),
        ),
        _check("weekly digest generation", assert_digest_generated(results.digest, seed)),
        _check("weekly digest snapshot", assert_snapshot_written(results.snapshot, seed)),
        _check(
            "validation register + evaluate",
            assert_validation_evaluated(results.evaluation, seed),
        ),
        _check("supersession detection", assert_supersession_detected(results.superseded_count)),
        _check(
            "retrospective ingest + save",
            assert_retrospective_saved(results.ingestion, results.report_file_ref, seed),
        ),
    ]


def _check(description: str, failure: str | None) -> tuple[bool, str]:
    """Render one assertion line: ``(passed, description-or-failure-detail)``."""
    if failure is None:
        return True, description
    return False, f"{description}: {failure}"


async def _run(db_path: str | None) -> bool:
    """Provision the DB, seed, drive the spine, assert, and print the report."""
    print("=" * 70)
    print("AlphaMind Feedback-Loop Spine Verification")
    print("=" * 70)

    results_lines: list[tuple[bool, str]]
    async with _provisioned_db(db_path) as factory:
        seed = await build_test_seed(factory)
        with tempfile.TemporaryDirectory() as data_tmp:
            results = await _drive_stages(
                factory,
                seed,
                data_root=Path(data_tmp),
            )
        results_lines = _evaluate_assertions(results, seed)

    print()
    for passed, line in results_lines:
        marker = "PASS" if passed else "FAIL"
        print(f"{marker} | {line}")

    all_pass = all(passed for passed, _ in results_lines)
    print()
    print(f"verify feedback_loop: {'PASS' if all_pass else 'FAIL'}")
    return all_pass


def run_verification(db_path: str | None = None) -> bool:
    """Seed, drive the spine end-to-end, assert each stage, print the report.

    When *db_path* is ``None`` a throwaway scratch DB is provisioned in a temp
    directory; an explicit *db_path* is a throwaway scratch location the script
    upgrades to schema head itself. Returns ``True`` iff every assertion passes.
    """
    return asyncio.run(_run(db_path))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse ``--db-path``, run, exit 0 on PASS else 1."""
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Standalone end-to-end verify for the feedback-loop spine."
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help=(
            "Path to a fresh scratch SQLite database to provision. The script runs "
            "`alembic upgrade head` against it. When omitted, a throwaway temp DB is used."
        ),
    )
    args = parser.parse_args(argv)
    passed = run_verification(args.db_path)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
