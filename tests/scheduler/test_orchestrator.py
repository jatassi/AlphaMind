"""Tests for ``alphamind.scheduler.orchestrator.run_invocation`` (story 03b).

The orchestrator is the single async entrypoint the APScheduler driver
(story 04a) and the emergency receiver (story 04b) both call. Per the
design's snapshot-isolation contract
(``docs/design/05-execution-layer/state-persistence.md`` § Snapshot isolation)
it runs three separate transactions per invocation: fill collection commits → the
snapshot read happens between phases → command execution commits per envelope. The
orchestrator wires the snapshot through ``SnapshotBackedSynthesizerReader``
into the analysis pipeline and threads the same ``AssembledSnapshot`` into
the decision pipeline.

Tests stub the LLM-dependent inner stages (``run_analysis_pipeline``,
``run_decision_pipeline``) via monkeypatch so the orchestrator wiring is
exercised end-to-end without hitting the Anthropic API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    InvocationId,
    ThesisId,
)
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.modes import Mode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.venue import (
    VenueConfig,
)

# Side-effect import to break the submit_envelope_mcp ↔ portfolio_manager
# circular import: PMEnvelope first, then submit_envelope_mcp.
from alphamind.decision.portfolio_manager.models import PMEnvelope  # noqa: F401
from alphamind.execution.write_paths.fill_collection import FillCollectionSummary
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.invocation_context.records import (
    process_lifetime_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow
from tests.scheduler._regime_helpers import make_regime_output
from tests.scheduler.conftest import (
    _MAKE_CONTEXT_ENGINES,
    SHIPPED_CONFIG_DIR,
    _make_process_lifetime_record,
    _make_venue_config,
)

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


@pytest.fixture
async def async_factory_with_singletons(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Same as ``async_factory`` plus seeded ``cash_ledger`` + ``drawdown_state``.

    Production ``process_unprocessed_fills`` seeds the singletons as a side
    effect of fill integration. Tests that drive the real fill collection writer
    with zero fills (no broker) need the singletons pre-seeded so the
    post-fill-collection snapshot read sees a satisfied repository.
    """
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
            cash, drawdown = _singleton_records()
            from alphamind.state.tables.cash_ledger_codec import (
                cash_ledger_record_to_row,
            )
            from alphamind.state.tables.drawdown_state_codec import (
                drawdown_state_record_to_row,
            )

            sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
            sess.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW))
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


# Sync engines built inside ``_make_context`` register here; the autouse
# ``_dispose_make_context_engines`` fixture disposes them after every test so
# Windows SQLite file handles release before pytest's tmp_path teardown runs.
# (List + fixture hoisted to conftest; name imported below for appends in
# this file's _make_context.)


def _make_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path | None = None,
    debug_e2e: Any | None = None,
) -> Any:
    """Compose the standard ``RunInvocationContext`` test fixtures use.

    Centralizes the bundled fields so the per-test invocations stay focused
    on what's specific to each scenario. The sync session factory is built
    from ``db_path`` so the analysis pipeline's sync read session shares the
    same SQLite file as the async write sessions.

    Pass ``debug_e2e=<settings>`` to exercise the debug-e2e wiring path; the
    orchestrator then threads the bundle's emitter + query factories through
    ``run_invocation``.
    """
    from alphamind.scheduler.run_context import RunInvocationContext

    sync_path = db_path if db_path is not None else env_path.parent / "alphamind.db"
    sync_engine = make_engine(str(sync_path))
    _MAKE_CONTEXT_ENGINES.append(sync_engine)
    sync_session_factory = make_session_factory(sync_engine)

    return RunInvocationContext(
        session_factory=session_factory,
        sync_session_factory=sync_session_factory,
        process_lifetime_id="proc-driver-1",
        archive_root=archive_root,
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
        debug_e2e=debug_e2e,
    )


def _make_fill_collection_inputs(*, staleness_flag: bool = False) -> Any:
    """Build a minimal ``FillCollectionInputs`` with no positions / no CA activities."""
    from alphamind.risk_guardrails.guardrail_evaluation import (
        FixtureIvProvider,
        MarketInputs,
    )
    from alphamind.scheduler.fill_collection_inputs import FillCollectionInputs

    return FillCollectionInputs(
        ca_activities=(),
        alpaca_positions=(),
        alpaca_account=None,
        market_inputs=MarketInputs(
            underlying_prices={},
            risk_free_rate=0.045,
            iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
            as_of=_NOW,
        ),
        staleness_flag=staleness_flag,
    )


def _make_fill_collection_summary() -> FillCollectionSummary:
    return FillCollectionSummary(
        fills_processed=0,
        fills_quarantined=0,
        ca_activities_processed=0,
        reconciliation_alerts=0,
    )


def _make_analysis_result() -> Any:
    """Build a no-op ``AnalysisPipelineResult`` carrying minimal synth output."""
    from alphamind.analysis._shared import TokensUsed
    from alphamind.analysis.synthesizer.retrieval import RetrievalStore
    from alphamind.analysis.synthesizer.runner import SynthesizerResult

    synth = SynthesizerResult(
        synthesis_text="Synthesizer brief.",
        retrieval_store=RetrievalStore(entries={}, freshness_by_source={}),
        tokens_used=TokensUsed(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=0.5,
        stop_reason="end_turn",
    )

    # Stub the orchestrator only reads ``synthesizer_result`` from; the
    # remaining slots stay None.
    from types import SimpleNamespace

    return SimpleNamespace(
        synthesizer_result=synth,
        distillation_outputs=None,
        domain_researchers_output=None,
        qualitative_result=None,
        adaptive_result=None,
    )


def _make_decision_result() -> Any:
    """Build a no-op ``DecisionPipelineResult`` with empty submission_log."""
    from alphamind.analysis._shared import TokensUsed
    from alphamind.decision.portfolio_manager.models import (
        PMCompletionRecord,
        VerdictSummary,
    )
    from alphamind.decision.portfolio_manager.runner import PMResult

    pm_result = PMResult(
        output=PMCompletionRecord(
            invocation_id=InvocationId("inv-x"),
            timestamp=_NOW,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(
                approve=0, approve_with_modification=0, reject=0, override_with_corrective_action=0
            ),
        ),
        submission_log=(),
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=0.5,
        stop_reason="end_turn",
    )

    from types import SimpleNamespace

    return SimpleNamespace(
        pm_result=pm_result,
        pydantic_snapshot=None,
        library_snapshot=None,
        analyst_result=None,
        strategist_result=None,
        pre_processor_bundle=None,
    )


def _make_activities_poll_stub(
    captured: dict[str, Any],
) -> Any:
    """Build a no-op ``run_account_activities_poll`` stub (ALP-846).

    The account-activities poll is a composition-root stage like
    ``gather_fill_collection_inputs``; stubbing it keeps the production default factory
    from building a live Alpaca client during orchestration tests. Hoisted to
    module scope so the heavy stub-patcher stays under the complexity ceiling.
    """

    async def _activities_poll_stub(*args: Any, **kw: Any) -> Any:
        from alphamind.execution.account_activities.poll import PollResult

        captured["activities_poll"] = {"args": args, "kwargs": kw}
        return PollResult(activities_booked=0, cursor=None)

    return _activities_poll_stub


def _patch_no_op_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fill_collection_summary: FillCollectionSummary | None = None,
    analysis_result: Any | None = None,
    decision_result: Any | None = None,
    fill_collection_raises: Exception | None = None,
    fill_collection_transient_failures: int = 0,
    decision_raises: Exception | None = None,
    captured: dict[str, Any] | None = None,
    staleness_flag: bool = False,
) -> None:
    """Monkey-patch every heavy stage on the orchestrator module to no-op stubs.

    Captures keyword arguments via the ``captured`` dict so tests can assert on
    threading invariants (e.g., ``mode='halt'`` arriving at the decision
    pipeline). The stubs return the canonical empty fixture for each stage.

    ``fill_collection_transient_failures`` (ALP-824) makes the fill_collection stub raise a
    transient ``database is locked`` ``OperationalError`` on its first N calls
    before succeeding, exercising the orchestrator's ``run_with_sqlite_busy_retry``
    wrapper around the write unit.
    """
    from alphamind.scheduler import orchestrator as module

    captured = captured if captured is not None else {}
    fill_collection_calls = {"n": 0}

    async def _gather_stub(**kw: Any) -> Any:
        captured["gather"] = kw
        return _make_fill_collection_inputs(staleness_flag=staleness_flag)

    async def _process_stub(*args: Any, **kw: Any) -> FillCollectionSummary:
        captured["fill_collection"] = {"args": args, "kwargs": kw}
        fill_collection_calls["n"] += 1
        if fill_collection_calls["n"] <= fill_collection_transient_failures:
            raise OperationalError(
                "INSERT INTO activity_log ...", {}, Exception("database is locked")
            )
        if fill_collection_raises is not None:
            raise fill_collection_raises
        # Production ``process_unprocessed_fills`` stamps ``fill_collection_completed_at``
        # on the bound row before returning (write_paths/fill_collection.py:294) AND
        # seeds the ``cash_ledger`` + ``drawdown_state`` singletons as a side
        # effect of fill integration. The stub mirrors both so the
        # orchestrator's post-fill-collection snapshot read finds the singletons + a
        # stamped row.
        from alphamind.state.invocation_context.context import (
            stamp_phase_completion,
        )

        handle = args[0]
        await _seed_singletons_via_handle(handle)
        await stamp_phase_completion(handle, column="fill_collection_completed_at")
        return fill_collection_summary or _make_fill_collection_summary()

    async def _analysis_stub(**kw: Any) -> Any:
        captured["analysis"] = kw
        return analysis_result or _make_analysis_result()

    async def _decision_stub(**kw: Any) -> Any:
        captured["decision"] = kw
        if decision_raises is not None:
            raise decision_raises
        return decision_result or _make_decision_result()

    async def _dispatch_stub(**kw: Any) -> Any:
        from alphamind.scheduler.command_execution_dispatch import CommandExecutionSummary

        captured["dispatch"] = kw
        return CommandExecutionSummary(commands_submitted=0, commands_rejected=0)

    def _regime_stub(**kw: Any) -> Any:
        captured["regime"] = kw
        return make_regime_output(now=_NOW)

    monkeypatch.setattr(module, "gather_fill_collection_inputs", _gather_stub)
    monkeypatch.setattr(module, "process_unprocessed_fills", _process_stub)
    monkeypatch.setattr(
        module,
        "run_account_activities_poll",
        _make_activities_poll_stub(captured),
    )
    monkeypatch.setattr(module, "run_analysis_pipeline", _analysis_stub)
    monkeypatch.setattr(module, "run_decision_pipeline", _decision_stub)
    monkeypatch.setattr(module, "dispatch_command_execution", _dispatch_stub)
    monkeypatch.setattr(module, "_resolve_regime_adaptation_for_invocation", _regime_stub)


class TestFillCollectionWriteLockResilience:
    """ALP-824 — fill collection gathers inputs before taking the SQLite write lock, and a
    transient cross-writer collision retries the write unit instead of aborting."""

    async def test_write_lock_taken_after_inputs_gathered(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``gather_fill_collection_inputs`` runs (in a read session) before
        ``begin_write_immediate`` and in a *different* session — so no write lock is
        held across the Alpaca fetch (AC: write lock taken only after gather)."""
        from alphamind.persistence.session import begin_write_immediate as real_begin
        from alphamind.scheduler import orchestrator as module
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        # ``gather`` populates ``captured["gather"]``; assert it is already present
        # when the IMMEDIATE write lock is taken, so the lock is acquired *after*
        # the gather (and never held across the Alpaca fetch).
        observed: dict[str, bool] = {}

        async def _begin_recorder(session: AsyncSession) -> None:
            observed["gather_ran_before_begin"] = "gather" in captured
            await real_begin(session)

        monkeypatch.setattr(module, "begin_write_immediate", _begin_recorder)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory, env_path=env_path, archive_root=archive_root
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert observed["gather_ran_before_begin"] is True
        # The read (gather) session and the write-unit session are distinct objects,
        # so the IMMEDIATE write lock is never held while gather does its fetch.
        read_session = captured["gather"]["handle"].session
        write_session = captured["fill_collection"]["args"][0].session
        assert read_session is not write_session

    async def test_transient_lock_during_write_unit_retries_to_completion(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transient ``database is locked`` on the first write attempt retries; the
        invocation completes with fills processed and ``fill_collection_completed_at`` stamped."""
        from alphamind.scheduler.orchestrator import run_invocation

        fc_summary = FillCollectionSummary(
            fills_processed=2,
            fills_quarantined=0,
            ca_activities_processed=0,
            reconciliation_alerts=0,
        )
        _patch_no_op_pipeline(
            monkeypatch, fill_collection_transient_failures=1, fill_collection_summary=fc_summary
        )

        await run_invocation(
            context=_make_context(
                session_factory=async_factory, env_path=env_path, archive_root=archive_root
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        async with async_factory() as session:
            row = (await session.execute(select(InvocationRow))).scalar_one()
        assert row.fill_collection_completed_at is not None
        assert row.command_execution_completed_at is not None
        assert row.fill_collection_summary_json is not None
        payload = json.loads(row.fill_collection_summary_json)
        assert payload["fills_processed"] == 2


class TestRunInvocationHappyPath:
    async def test_returns_invocation_summary_with_expected_shape(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end happy path returns an ``InvocationSummary`` with correct fields."""
        from alphamind.scheduler.orchestrator import InvocationSummary, run_invocation

        _patch_no_op_pipeline(monkeypatch)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert isinstance(summary, InvocationSummary)
        assert summary.trigger_type == "manual"
        assert summary.firing_run_type is RunType.market_hours_rolling
        assert summary.commands_submitted == 0
        assert summary.commands_rejected == 0
        assert summary.staleness_flag is False
        assert summary.duration_seconds >= 0.0
        assert summary.invocation_id

    async def test_persists_one_row_with_fill_collection_and_command_execution_stamped(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: exactly one ``invocations`` row with both phase columns + summaries."""
        from alphamind.scheduler.orchestrator import run_invocation

        _patch_no_op_pipeline(monkeypatch)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.invocation_id == summary.invocation_id
        assert row.fill_collection_completed_at is not None
        assert row.command_execution_completed_at is not None
        assert row.fill_collection_summary_json is not None
        assert row.command_execution_summary_json is not None
        assert row.active_overlays_json is not None
        assert row.feature_flags_snapshot_json is not None

    async def test_fill_collection_summary_is_serialized_fill_collection_summary(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The row's ``fill_collection_summary_json`` is the fill collection summary
        JSON-serialized."""
        from alphamind.scheduler.orchestrator import run_invocation

        fc_summary = FillCollectionSummary(
            fills_processed=3,
            fills_quarantined=1,
            ca_activities_processed=2,
            reconciliation_alerts=0,
        )
        _patch_no_op_pipeline(monkeypatch, fill_collection_summary=fc_summary)
        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        async with async_factory() as session:
            row = (await session.execute(select(InvocationRow))).scalar_one()
        assert row.fill_collection_summary_json is not None
        payload = json.loads(row.fill_collection_summary_json)
        assert payload == {
            "fills_processed": 3,
            "fills_quarantined": 1,
            "ca_activities_processed": 2,
            "reconciliation_alerts": 0,
        }

    async def test_thesis_ledger_rederived_after_activities_poll(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CR1 — the fill-collection write unit re-derives the thesis PnL ledger AFTER the
        activities poll, so a thesis with a same-invocation OPASN/OPTRD event has
        its ledger reflect that event.

        The poll stub appends a realized-PnL OPASN event for a pre-seeded thesis to
        the write handle's session (what the real poll does to the broker-event
        log). On the old ordering the only rederive ran inside
        ``process_unprocessed_fills`` — before the poll — so the activity's
        realized-PnL delta was lost that invocation; now the orchestrator re-derives
        after the poll and the ledger reflects it.
        """
        from alphamind.scheduler import orchestrator as module
        from alphamind.scheduler.orchestrator import run_invocation
        from alphamind.state.records_broker_event_log import (
            BrokerEventRecord,
            BrokerEventType,
            serialize_event_payload,
        )
        from alphamind.state.tables.broker_event_log_codec import (
            record_to_row as event_record_to_row,
        )
        from alphamind.state.tables.positions_codec import (
            record_to_row as position_record_to_row,
        )
        from alphamind.state.tables.theses_codec import record_to_rows as thesis_record_to_rows
        from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
        from tests.execution.corporate_actions._handler_substrate import (
            make_active_thesis,
            make_open_equity_position,
        )

        # Pre-seed the thesis (+ its position) the activity event attributes to —
        # full records so the post-fill-collection snapshot read decodes them cleanly. The
        # position carries no forward bracket/thesis FK (none is seeded); the thesis
        # FKs back to the position, which exists.
        async with async_factory() as session:
            session.add(
                position_record_to_row(
                    make_open_equity_position(share_count=10.0, thesis_id=None, bracket_id=None)
                )
            )
            await session.flush()
            thesis_row, component_rows = thesis_record_to_rows(make_active_thesis())
            session.add(thesis_row)
            for crow in component_rows:
                session.add(crow)
            await session.commit()

        async def _poll_appends_activity(*args: Any, **kw: Any) -> Any:
            from alphamind.execution.account_activities.poll import PollResult

            handle = args[0]
            event = BrokerEventRecord(
                event_key="aevt-orch-cr1",
                event_type=BrokerEventType.OPASN,
                thesis_id=ThesisId("thesis-1"),
                invocation_id=None,
                position_id=None,
                raw_payload_json=serialize_event_payload({"realized_pnl_delta_usd": 410.0}),
                broker_timestamp=_NOW,
                captured_at=_NOW,
            )
            handle.session.add(event_record_to_row(event))
            return PollResult(activities_booked=1, cursor=None)

        _patch_no_op_pipeline(monkeypatch)
        monkeypatch.setattr(module, "run_account_activities_poll", _poll_appends_activity)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        async with async_factory() as session:
            ledger = (await session.execute(select(ThesisPnlLedgerRow))).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].thesis_id == "thesis-1"
        assert ledger[0].realized_pnl_usd == pytest.approx(410.0)


class TestRunInvocationFailures:
    async def test_fill_collection_exception_leaves_row_with_fill_collection_completed_at_null(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fill collection abort: row stays (committed up-front),
        fill_collection_completed_at is NULL.

        Under the three-transaction model the invocation row is committed by
        ``insert_invocation_record`` before fill collection opens. A fill collection abort
        rolls back fill collection's own transaction only; the row persists with
        ``fill_collection_completed_at IS NULL`` so the SQL repository's consistency
        guard refuses snapshot reads against this invocation, and the next
        invocation retries fills.
        """
        from alphamind.scheduler.orchestrator import run_invocation

        _patch_no_op_pipeline(monkeypatch, fill_collection_raises=RuntimeError("phase 1 boom"))
        with pytest.raises(RuntimeError, match="phase 1 boom"):
            await run_invocation(
                context=_make_context(
                    session_factory=async_factory,
                    env_path=env_path,
                    archive_root=archive_root,
                ),
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                firing_run_type=RunType.market_hours_rolling,
                now=_NOW,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.fill_collection_completed_at is None
        assert row.command_execution_completed_at is None

    async def test_decision_exception_leaves_fill_collection_committed_skips_command_execution(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Between-phase abort: fill collection stays committed; command execution is skipped.

        Per ``docs/design/mid-pipeline-failure-handling.md`` an analysis /
        decision-pipeline failure leaves the already-committed fill collection
        writes durable and skips command execution. The row carries
        ``fill_collection_completed_at`` set, ``command_execution_completed_at`` NULL.
        """
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(
            monkeypatch,
            decision_raises=RuntimeError("decision boom"),
            captured=captured,
        )
        with pytest.raises(RuntimeError, match="decision boom"):
            await run_invocation(
                context=_make_context(
                    session_factory=async_factory,
                    env_path=env_path,
                    archive_root=archive_root,
                ),
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                firing_run_type=RunType.market_hours_rolling,
                now=_NOW,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.fill_collection_completed_at is not None
        assert row.command_execution_completed_at is None
        assert "dispatch" not in captured


class TestRunInvocationModeAndStaleness:
    async def test_halt_mode_flows_to_decision_pipeline(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``Mode.halt`` as the resolved active mode → ``mode='halt'`` on decision call."""
        from alphamind.config.models.regimes import Regime
        from alphamind.config.resolver import RuntimeDimensions
        from alphamind.scheduler import orchestrator as module
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        # Force the runtime resolver to return Mode.halt.
        async def _stub_resolve(session: Any, **kw: Any) -> RuntimeDimensions:
            return RuntimeDimensions(
                active_regime=Regime.normal,
                active_mode=Mode.halt,
                active_overlays=(),
                firing_trigger=RunType.market_hours_rolling,
            )

        monkeypatch.setattr(module, "resolve_runtime_dimensions", _stub_resolve)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert captured["decision"]["mode"] == "halt"

    async def test_normal_mode_flows_to_decision_pipeline(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``Mode.normal`` resolves to ``mode='normal'`` on the decision pipeline call."""
        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        from alphamind.scheduler.orchestrator import run_invocation

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )
        assert captured["decision"]["mode"] == "normal"

    async def test_staleness_flag_persists_to_row_and_summary(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fill collection degradation sets staleness on both the summary and the row."""
        from alphamind.scheduler.orchestrator import run_invocation

        _patch_no_op_pipeline(monkeypatch, staleness_flag=True)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        async with async_factory() as session:
            row = (await session.execute(select(InvocationRow))).scalar_one()

        assert summary.staleness_flag is True
        assert row.staleness_flag == 1


def _singleton_records() -> tuple[Any, Any]:
    """Return ``(cash_ledger, drawdown_state)`` records for the snapshot singletons."""
    from alphamind._kernel.regime import RiskZone
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
    from alphamind.portfolio_state.records.cash import CashLedger

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
    drawdown = DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )
    return cash, drawdown


async def _seed_singletons_via_handle(handle: Any) -> None:
    """Seed the singletons inside the fill collection's open session.

    Mimics what production ``process_unprocessed_fills`` does as a side
    effect of fill integration; joins the fill collection transaction so the
    singletons commit together with ``fill_collection_completed_at``.
    """
    from alphamind.state.tables.cash_ledger_codec import (
        cash_ledger_record_to_row,
    )
    from alphamind.state.tables.drawdown_state_codec import (
        drawdown_state_record_to_row,
    )

    cash, drawdown = _singleton_records()
    handle.session.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
    handle.session.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW))


class TestRunInvocationSnapshotWiring:
    """The orchestrator threads a real ``AssembledSnapshot`` between phases.

    Pre-ALP-449 the orchestrator wired a deferred ``_EmptySynthesizerReader``
    stub because the unified-transaction model prevented a fresh-session
    snapshot read from seeing the open transaction's ``fill_collection_completed_at``
    write. The three-transaction refactor commits fill collection before the snapshot
    read, so ``SnapshotBackedSynthesizerReader`` wires correctly.
    """

    async def test_analysis_pipeline_receives_sync_session(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_analysis_pipeline gets a sync ``Session``, not an ``AsyncSession``.

        The distillation orchestrator threads the session through
        :func:`asyncio.to_thread` into sync SQLAlchemy callsites
        (``session.execute(...).all()``). Passing an ``AsyncSession`` raises
        ``AttributeError: 'coroutine' object has no attribute 'all'`` at
        runtime (ALP-490 blocker 1).
        """
        from sqlalchemy.orm import Session

        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        passed_session = captured["analysis"]["session"]
        assert isinstance(passed_session, Session)
        assert not isinstance(passed_session, AsyncSession)

    async def test_analysis_pipeline_receives_snapshot_backed_synthesizer_reader(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_analysis_pipeline gets a SnapshotBackedSynthesizerReader, not an empty stub.

        Forces the three-transaction restructure: fill collection commits, then the
        orchestrator assembles a snapshot via fresh sessions (which now see
        committed ``fill_collection_completed_at``), then wires that snapshot through
        the synthesizer reader into the analysis pipeline.
        """
        from alphamind.portfolio_state.consumers.synthesizer import (
            SnapshotBackedSynthesizerReader,
        )
        from alphamind.scheduler.orchestrator import run_invocation

        # The fill collection stub seeds the snapshot singletons via the handle's
        # open session, mimicking what production fill integration does.
        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        portfolio_reader = captured["analysis"]["portfolio_reader"]
        assert isinstance(portfolio_reader, SnapshotBackedSynthesizerReader)

    async def test_decision_pipeline_receives_assembled_snapshot(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_decision_pipeline gets a pre-built AssembledSnapshot plus the
        Phase 1 enforcement-composition inputs.

        Slice 2 of ALP-449: the decision pipeline does not call
        ``assemble_snapshot`` itself; the orchestrator builds the snapshot
        once and threads the same value through both the synthesizer reader
        and the decision pipeline. Story ALP-433 adds three new kwargs the
        pipeline reads to compose Phase 1 enforcement on every invocation:
        ``repository`` (for ``DrawdownState`` reads), ``regime_output``,
        and ``progressive_tiers``. ``price_provider`` and
        ``portfolio_state_config`` stay out of the signature — the
        scheduler still owns assembly.
        """
        from alphamind.portfolio_state.freshness import AssembledSnapshot
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        decision_kwargs = captured["decision"]
        assert isinstance(decision_kwargs["assembled_snapshot"], AssembledSnapshot)
        # Story ALP-433: the pipeline reads ``DrawdownState`` via the
        # threaded repository and composes Phase 1 enforcement from the
        # regime output + progressive tiers.
        assert "repository" in decision_kwargs
        assert "regime_output" in decision_kwargs
        assert "progressive_tiers" in decision_kwargs
        # Assembly stays scheduler-owned — these kwargs remain absent.
        assert "price_provider" not in decision_kwargs
        assert "portfolio_state_config" not in decision_kwargs

    async def test_snapshot_assembled_exactly_once_per_invocation(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The synthesizer reader and the decision pipeline share one snapshot.

        Slice 2 of ALP-449: the orchestrator assembles the
        ``AssembledSnapshot`` exactly once between fill collection and the
        analysis pipeline; the same object threads through both
        ``SnapshotBackedSynthesizerReader`` and ``run_decision_pipeline``.
        A regression that re-assembles per consumer would show up as
        ``assemble_count > 1``.
        """
        from alphamind.portfolio_state.assembler import assemble_snapshot
        from alphamind.scheduler import orchestrator as module
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        assemble_count = 0

        def _counting_assemble(*args: Any, **kwargs: Any) -> Any:
            nonlocal assemble_count
            assemble_count += 1
            return assemble_snapshot(*args, **kwargs)

        monkeypatch.setattr(module, "assemble_snapshot", _counting_assemble)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert assemble_count == 1


class TestRunInvocationLibraryConfigWiring:
    """The orchestrator's decision-pipeline ``library_config`` (ALP-505).

    Pre-ALP-505 the orchestrator built ``LibraryConfig`` inline with
    ``escalation_zones={}`` and ``conservative_buffer_pct=0.0`` — placeholders
    the comment promised "upstream guardrail composition" would fill but
    nothing did. The pre-processor's first ``project_all`` then crashed on
    ``config.escalation_zones[spec.effective_limit_key]``; the conservative
    buffer silently zeroed every options-pricing buffer; and risk-budget
    classifications silently collapsed to NORMAL.

    Fix routes ``_build_decision_kwargs`` through the canonical
    ``from_resolved_config`` adapter so escalation zones and the buffer come
    from the same place every other caller reads them.
    """

    async def test_library_config_zones_and_buffer_match_resolved(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every rule has an escalation-zones entry; buffer carries the resolved value."""
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        library_config = captured["decision"]["library_config"]
        assert library_config.escalation_zones.keys() == library_config.effective_limits.keys()
        # ``config/execution.yaml`` sets conservative_delta_buffer_pct=10; assert that
        # value survives the adapter into ``library_config``.
        assert library_config.conservative_buffer_pct == 10.0


class TestRunInvocationFailuresThreeTxBoundaries:
    """Failure semantics at the three transaction boundaries (ALP-449).

    ``TestRunInvocationFailures`` covers fill collection abort and between-phase
    abort already; this class fills in the command execution boundary — a
    dispatch-side raise must leave fill collection durable and ``command_execution_completed_at``
    NULL (matches the design's "commands submitted before the abort
    remain committed" semantic; the per-envelope mechanics themselves
    are tested in ``tests/scheduler/test_command_execution_dispatch.py``).
    """

    async def test_cmd_exec_dispatch_failure_leaves_fill_collection_committed_cmd_exec_null(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dispatch_command_execution raise propagates; the row keeps fill collection,
        drops command execution."""
        from alphamind.scheduler import orchestrator as module
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        async def _raising_dispatch(**kw: Any) -> Any:
            raise RuntimeError("phase 2 boom")

        monkeypatch.setattr(module, "dispatch_command_execution", _raising_dispatch)

        with pytest.raises(RuntimeError, match="phase 2 boom"):
            await run_invocation(
                context=_make_context(
                    session_factory=async_factory,
                    env_path=env_path,
                    archive_root=archive_root,
                ),
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                firing_run_type=RunType.market_hours_rolling,
                now=_NOW,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.fill_collection_completed_at is not None
        assert row.command_execution_completed_at is None

    async def test_pending_submit_strand_withholds_command_execution_stamp_and_warns(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """FL4 — a residual PENDING_SUBMIT strand withholds ``command_execution_completed_at``
        AND surfaces an operator-visible warning (not silent withholding).

        The dispatch stub seeds the lost-backfill signature this invocation: an
        order row in PENDING_SUBMIT keyed by ``client_order_id = inv-{id}.…``. The
        orchestrator's ALP-836 guard then withholds the stamp; FL4 requires that
        residual strand to be logged for operator follow-up since there is no
        recovery sweep that self-heals it.
        """
        import logging

        from alphamind.portfolio_state.records.orders import OrderStatus
        from alphamind.scheduler import orchestrator as module
        from alphamind.scheduler.orchestrator import run_invocation
        from tests.state._fk_substrate import (
            stub_bracket_row,
            stub_order_row,
            stub_position_row,
        )

        async def _dispatch_seeds_strand(**kw: Any) -> Any:
            from alphamind.scheduler.command_execution_dispatch import CommandExecutionSummary

            invocation_id = kw["invocation_id"]
            # The lost-backfill strand: position + bracket + a PENDING_SUBMIT order
            # whose client_order_id is scoped to THIS invocation. Cyclic FKs land in
            # one deferred-FK transaction (post-snapshot, so no snapshot decode).
            async with async_factory() as session:
                session.add(stub_position_row("pos-strand", bracket_id="brk-strand"))
                session.add(stub_bracket_row("brk-strand", "pos-strand", "ord-strand"))
                session.add(
                    stub_order_row(
                        "ord-strand",
                        "brk-strand",
                        position_id="pos-strand",
                        status=OrderStatus.PENDING_SUBMIT.value,
                        client_order_id=f"{invocation_id}.ENV-1.0.0",
                    )
                )
                await session.commit()
            return CommandExecutionSummary(commands_submitted=0, commands_rejected=0)

        _patch_no_op_pipeline(monkeypatch)
        monkeypatch.setattr(module, "dispatch_command_execution", _dispatch_seeds_strand)

        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.orchestrator"):
            await run_invocation(
                context=_make_context(
                    session_factory=async_factory,
                    env_path=env_path,
                    archive_root=archive_root,
                ),
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                firing_run_type=RunType.market_hours_rolling,
                now=_NOW,
            )

        async with async_factory() as session:
            row = (await session.execute(select(InvocationRow))).scalar_one()
        # The strand withholds the stamp …
        assert row.command_execution_completed_at is None
        # … and the residual strand is surfaced for operator follow-up.
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "alphamind.scheduler.orchestrator"
            and "PENDING_SUBMIT" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "command_execution_completed_at withheld" in warnings[0].getMessage()


def _stub_only_llm_and_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub LLM + broker callees; leave fill collection / command execution writers
    in production form.

    Differs from :func:`_patch_no_op_pipeline` by NOT stubbing
    ``process_unprocessed_fills`` and ``dispatch_command_execution`` — those are the
    DB writers the verify-script-style checks expect to land their rows
    and activity-log entries in production form.
    """
    from alphamind.scheduler import orchestrator as module

    async def _gather_stub(**_kw: Any) -> Any:
        return _make_fill_collection_inputs(staleness_flag=False)

    async def _analysis_stub(**_kw: Any) -> Any:
        return _make_analysis_result()

    async def _decision_stub(**_kw: Any) -> Any:
        return _make_decision_result()

    def _regime_stub(**_kw: Any) -> Any:
        return make_regime_output(now=_NOW)

    async def _activities_poll_stub(*_args: Any, **_kw: Any) -> Any:
        # ALP-846 — the activity poll touches the broker, so it joins the
        # broker callees this helper stubs (alongside gather_fill_collection_inputs).
        from alphamind.execution.account_activities.poll import PollResult

        return PollResult(activities_booked=0, cursor=None)

    monkeypatch.setattr(module, "gather_fill_collection_inputs", _gather_stub)
    monkeypatch.setattr(module, "run_account_activities_poll", _activities_poll_stub)
    monkeypatch.setattr(module, "run_analysis_pipeline", _analysis_stub)
    monkeypatch.setattr(module, "run_decision_pipeline", _decision_stub)
    monkeypatch.setattr(module, "_resolve_regime_adaptation_for_invocation", _regime_stub)


_REQUIRED_INVOCATION_ROW_COLUMNS: tuple[str, ...] = (
    "invocation_id",
    "process_lifetime_id",
    "start_at",
    "fill_collection_completed_at",
    "command_execution_completed_at",
    "trigger_type",
    "trigger_source",
    "trigger_reason",
    "git_sha_at_invocation",
    "active_profile",
    "active_regime",
    "active_mode",
    "active_overlays_json",
    "resolved_config_hash",
    "resolved_config_snapshot_path",
    "feature_flags_snapshot_json",
    "data_calibration_state_snapshot_path",
    "data_source_freshness_json",
    "fill_collection_summary_json",
    "command_execution_summary_json",
    "staleness_flag",
)


class TestRunInvocationProductionPathArtifacts:
    """Pin the invocation-row + activity-log invariants against a unit invocation.

    The other test classes in this file stub every heavy callee
    (``gather_fill_collection_inputs``, ``process_unprocessed_fills``,
    ``run_analysis_pipeline``, ``run_decision_pipeline``, ``dispatch_command_execution``)
    so the orchestrator wiring is exercised without hitting the broker or
    LLM. That coverage previously missed two blockers
    (``snapshot_metadata_json`` column population, baseline ``activity_log``
    emission) because both were expected from the orchestrator's own glue
    code — not from the stubbed inner stages.

    This class re-runs the orchestrator with the LLM + broker stages
    stubbed but ``process_unprocessed_fills`` / ``dispatch_command_execution`` in
    production form, then asserts directly against the resulting DB
    state. ALP-502 retired the per-feature verify scripts; the
    invariants the deleted ``check_invocation_row_population`` and
    ``check_activity_log`` helpers enforced are reproduced inline below
    so regressions still surface at test time.
    """

    async def test_invocation_row_columns_populated(
        self,
        async_factory_with_singletons: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every must-be-set column on the ``invocations`` row is populated."""
        from alphamind.scheduler.orchestrator import run_invocation

        _stub_only_llm_and_broker(monkeypatch)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory_with_singletons,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="integration test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        sync_engine = make_engine(str(db_path))
        try:
            with sync_engine.connect() as conn:
                row_result = (
                    conn.exec_driver_sql(
                        "SELECT * FROM invocations WHERE invocation_id = :iid",
                        {"iid": summary.invocation_id},
                    )
                    .mappings()
                    .first()
                )
        finally:
            sync_engine.dispose()
        assert row_result is not None
        row = dict(row_result)
        null_columns = [c for c in _REQUIRED_INVOCATION_ROW_COLUMNS if row.get(c) is None]
        assert not null_columns, f"NULL columns on invocations row: {null_columns}"
        assert row["trigger_type"] == "manual"
        for json_col in ("active_overlays_json", "feature_flags_snapshot_json"):
            json.loads(row[json_col])
        resolved_cfg_path = Path(row["resolved_config_snapshot_path"])
        # Use ``asyncio.to_thread`` to avoid the lint-flagged sync-IO-in-
        # async-function pattern; these calls are filesystem stats, not
        # mutations, so a worker-thread bounce is fine.
        cfg_stat_ok, cfg_size = await asyncio.to_thread(
            lambda: (resolved_cfg_path.is_file(), resolved_cfg_path.stat().st_size)
        )
        assert cfg_stat_ok
        assert cfg_size > 0
        data_cal_path = Path(row["data_calibration_state_snapshot_path"])
        assert await asyncio.to_thread(data_cal_path.is_file)

    async def test_activity_log_carries_at_least_one_entry(
        self,
        async_factory_with_singletons: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``activity_log`` carries at least one entry per invocation.

        With no fills and no commands, the orchestrator's baseline
        ``DISTILLATION_CONFIG_CHANGE`` emission is the only entry and is
        sufficient to satisfy the per-invocation "at least one entry"
        invariant the deleted verify script enforced.
        """
        from alphamind.scheduler.orchestrator import run_invocation

        _stub_only_llm_and_broker(monkeypatch)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory_with_singletons,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="integration test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        sync_engine = make_engine(str(db_path))
        try:
            with sync_engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT event_type FROM activity_log WHERE invocation_id = :iid",
                    {"iid": summary.invocation_id},
                ).all()
        finally:
            sync_engine.dispose()
        assert rows, "expected ≥1 activity_log entry; got none"


class TestRunInvocationDebugE2EWiring:
    """Story ALP-501 — ``debug_e2e`` settings thread through ``gather_fill_collection_inputs``.

    The orchestrator must detect debug-e2e mode by ``context.debug_e2e is not
    None`` (P3 — no parallel boolean flag) and route the bundle's
    ``account_queries`` / ``ca_queries`` to ``gather_fill_collection_inputs`` via the
    Protocol-typed factory kwargs (the seam ALP-494 carved out). Production
    callers (``context.debug_e2e is None``) must continue to pass ``None`` so
    the gatherer falls back to its inline Alpaca-backed defaults.
    """

    async def test_debug_e2e_threads_query_factories_through_fill_collection(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``debug_e2e`` populated → factories non-None + resolve to bundle queries."""
        from alphamind.scheduler.debug_e2e.broker import (
            LogOnlyAccountStateQueries,
            LogOnlyBatchQuoteSource,
            LogOnlyCorporateActionsQueries,
        )
        from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
        from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings
        from alphamind.scheduler.orchestrator import run_invocation

        account_queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        ca_queries = LogOnlyCorporateActionsQueries()
        quote_source = LogOnlyBatchQuoteSource()
        debug_settings = DebugE2ESettings(
            account_queries=account_queries,
            ca_queries=ca_queries,
            quote_source=quote_source,
            emitter_factory=lambda _inv_id, _as_of: NOOP_PROGRESS_EMITTER,
        )

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
                debug_e2e=debug_settings,
            ),
            trigger_type="manual",
            trigger_source="debug_e2e_cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        gather_kw = captured["gather"]
        # All three factories are wired and yield the bundle's instances — the
        # quote-source factory keeps debug-e2e offline (ALP-753).
        assert gather_kw["account_queries_factory"] is not None
        assert gather_kw["ca_queries_factory"] is not None
        assert gather_kw["quote_source_factory"] is not None

        venue = gather_kw["venue_config"]
        mode = gather_kw["execution_mode"]
        assert gather_kw["account_queries_factory"](venue, mode) is account_queries
        assert gather_kw["ca_queries_factory"](venue, mode) is ca_queries
        assert gather_kw["quote_source_factory"](venue, mode) is quote_source

    async def test_production_path_leaves_query_factories_at_none(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``context.debug_e2e is None`` → factories are ``None`` (Alpaca defaults run)."""
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        gather_kw = captured["gather"]
        # The orchestrator must pass the kwargs explicitly so the call signature
        # is stable; ``None`` is the canonical "use the Alpaca default" value.
        assert gather_kw.get("account_queries_factory") is None
        assert gather_kw.get("ca_queries_factory") is None
        assert gather_kw.get("quote_source_factory") is None


class TestRunInvocationBrokerDispatchWiring:
    """ALP-711 — orchestrator threads the broker-routing triple into the decision
    pipeline.

    Production runs (``context.debug_e2e is None``) must pass the full
    ``(venue_config, execution_mode, execution_config)`` triple into
    ``run_decision_pipeline`` so the PM subprocess worker can reconstruct
    the live ``TradingClient`` and route PM-originated commands to Alpaca.
    Non-production runs (``context.debug_e2e is not None``) must leave all
    three at ``None`` so the submit_envelope wrapper's broker-routing gate
    stays False — preserving the log-only behavior the debug-e2e harness
    already wires for fill collection reads.
    """

    async def test_production_path_threads_full_broker_routing_triple(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``context.debug_e2e is None`` → decision pipeline gets the triple."""
        from alphamind.config.models.execution import ExecutionConfig
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        decision_kwargs = captured["decision"]
        assert isinstance(decision_kwargs["venue_config"], VenueConfig)
        assert decision_kwargs["execution_mode"] is ExecutionMode.paper
        assert isinstance(decision_kwargs["execution_config"], ExecutionConfig)

    async def test_debug_e2e_path_leaves_broker_routing_triple_at_none(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``context.debug_e2e`` populated → all three legs are ``None``.

        The submit_envelope wrapper's broker-routing gate requires all three
        legs to be non-None; with the triple at ``None`` accepted commands
        fall through to the synthetic-id placeholder path and no live Alpaca
        call fires. This is the contract the operator's log-only behavior
        relies on (debug-e2e runs must not hit the broker).
        """
        from alphamind.scheduler.debug_e2e.broker import (
            LogOnlyAccountStateQueries,
            LogOnlyBatchQuoteSource,
            LogOnlyCorporateActionsQueries,
        )
        from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
        from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings
        from alphamind.scheduler.orchestrator import run_invocation

        debug_settings = DebugE2ESettings(
            account_queries=LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO),
            ca_queries=LogOnlyCorporateActionsQueries(),
            quote_source=LogOnlyBatchQuoteSource(),
            emitter_factory=lambda _inv_id, _as_of: NOOP_PROGRESS_EMITTER,
        )

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
                debug_e2e=debug_settings,
            ),
            trigger_type="manual",
            trigger_source="debug_e2e_cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        decision_kwargs = captured["decision"]
        assert decision_kwargs["venue_config"] is None
        assert decision_kwargs["execution_mode"] is None
        assert decision_kwargs["execution_config"] is None


def _make_invocation_row(
    invocation_id: str,
    start_at: datetime,
    *,
    command_execution_completed: bool,
) -> InvocationRow:
    """Build a minimal ``InvocationRow`` with the columns required by the schema."""
    iso = start_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id="proc-driver-1",
        start_at=iso,
        fill_collection_completed_at=iso,
        command_execution_completed_at=iso if command_execution_completed else None,
        trigger_type="manual",
        trigger_source="cli",
        trigger_reason="prior",
        git_sha_at_invocation="b" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="c" * 64,
        resolved_config_snapshot_path="/tmp/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/calib.json",
        data_source_freshness_json="{}",
    )


class TestRunInvocationLastInvocationTimeResolution:
    """The analysis pipeline receives a real ``last_invocation_time`` (ALP-535).

    Pre-ALP-535 the orchestrator passed ``last_invocation_time=now`` to
    :func:`run_analysis_pipeline`, producing a zero-width news-digest window
    ``(now, now)`` that selected zero headlines even when storage was
    populated. The qualitative researcher then tagged news as unavailable.

    Fix derives ``last_invocation_time`` from the most recent successful
    prior invocation's ``start_at`` (or ``now - 24h`` when no prior exists)
    so the digest covers the same headline corpus the domain researchers see.
    """

    async def test_falls_back_to_now_minus_24h_when_no_prior_invocation(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import timedelta

        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert captured["analysis"]["last_invocation_time"] == _NOW - timedelta(hours=24)

    async def test_resolves_prior_invocation_start_at_when_prior_exists(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import timedelta

        from alphamind.scheduler.orchestrator import run_invocation

        prior_start_at = _NOW - timedelta(hours=3)
        async with async_factory() as setup_session:
            setup_session.add(
                _make_invocation_row(
                    "inv-prior-0001", prior_start_at, command_execution_completed=True
                )
            )
            await setup_session.commit()

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
                db_path=db_path,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert captured["analysis"]["last_invocation_time"] == prior_start_at

    async def test_skips_aborted_prior_in_favor_of_last_successful(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An aborted prior (command_execution_completed_at IS NULL) is skipped.

        The resolver anchors the digest window to the last *successful*
        invocation so headlines published between that success and a later
        abort remain inside the next window. Mirrors the
        ``_resolve_active_regime`` semantic in ``scheduler/runtime.py``.
        """
        from datetime import timedelta

        from alphamind.scheduler.orchestrator import run_invocation

        successful_start_at = _NOW - timedelta(hours=5)
        aborted_start_at = _NOW - timedelta(hours=2)
        async with async_factory() as setup_session:
            setup_session.add(
                _make_invocation_row(
                    "inv-success-0001", successful_start_at, command_execution_completed=True
                )
            )
            setup_session.add(
                _make_invocation_row(
                    "inv-aborted-0002", aborted_start_at, command_execution_completed=False
                )
            )
            await setup_session.commit()

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
                db_path=db_path,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        assert captured["analysis"]["last_invocation_time"] == successful_start_at


class TestSchemaRunTypeMapping:
    """``_SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE`` must stay exhaustive (ALP-755).

    The mapping feeds the ``invocation_started`` SSE emit. A missing key
    raised ``KeyError`` and silently dropped the start frame for weekend
    runs; this guard fails loudly if a future ``RunType`` member is added
    without a corresponding schema-string entry.
    """

    def test_mapping_covers_every_run_type_member(self) -> None:
        from alphamind.scheduler.orchestrator import (
            _SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE,
        )

        assert set(_SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE) == set(RunType)

    def test_mapping_values_match_member_wire_values(self) -> None:
        from alphamind.scheduler.orchestrator import (
            _SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE,
        )

        for run_type, schema_str in _SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE.items():
            assert schema_str == run_type.value


# ---------------------------------------------------------------------------
# Thesis resolution step (ALP-899 / ALP-834) — runs after fill collection and
# before snapshot assembly, in its own transaction. Mocks only the SDK (the LLM
# fallback for the qualitative ENTRY component) and the database.
# ---------------------------------------------------------------------------

_CLOSER_INV_ID = "inv-2026-05-06T14:30:00Z-prev"


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _thesis_resolved_sdk_stub(outcome: str = "WRONG") -> Any:
    """A ``claude_agent_sdk.query`` stand-in returning a structured component verdict."""

    async def _stub(**_kwargs: Any) -> AsyncIterator[Any]:
        from claude_agent_sdk import AssistantMessage, ResultMessage

        usage = {
            "input_tokens": 80,
            "output_tokens": 30,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        msgs = [
            AssistantMessage(
                content=[], model="claude-sonnet-4-6", stop_reason="end_turn", usage=usage
            ),
            ResultMessage(
                subtype="result",
                duration_ms=400,
                duration_api_ms=350,
                is_error=False,
                num_turns=1,
                session_id="sess-thesis",
                stop_reason="end_turn",
                usage=usage,
                structured_output={"outcome": outcome, "notes": "Qualitative verdict."},
            ),
        ]
        async for m in _async_iter(msgs):
            yield m

    return _stub


async def _seed_closed_position_active_thesis(
    factory: async_sessionmaker[AsyncSession],
    *,
    exit_method: Any,
    thesis_id: str = "thesis-res-1",
    position_id: str = "pos-res-1",
) -> None:
    """Seed a CLOSED position + ACTIVE thesis + ledger + POSITION_CLOSED entry.

    The POSITION_CLOSED entry is tagged with a prior (closer) invocation, so the
    resolver resolves it at THIS invocation regardless of which subsystem closed
    the position — covering both fill-collection and continuous-monitor closes.
    """
    from datetime import timedelta

    from alphamind._kernel.ids import PositionId, ThesisId
    from alphamind._kernel.money import money, signed_money
    from alphamind.portfolio_state.events.activity_log import (
        EVENT_TYPE_TO_GROUP,
        ActivityLogEntry,
        EventSource,
        EventType,
        PositionClosedDetail,
    )
    from alphamind.portfolio_state.records.theses import (
        KeyAssumption,
        ThesisComponent,
        ThesisComponentType,
        ThesisRecord,
        ThesisRecordStatus,
    )
    from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
    from alphamind.state.tables.theses_codec import record_to_rows
    from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
    from tests.state._fk_substrate import stub_invocation_row, stub_position_row

    generation = _NOW - timedelta(hours=24)
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value}",
            thesis_id=ThesisId(thesis_id),
            component_type=ct,
            linked_bracket_leg_type=None,
            instrument_reference="NVDA",
            narrative=f"{ct.value} narrative.",
            key_assumptions=(KeyAssumption(text="A claim.", outcome=None),),
            generation_timestamp=generation,
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    thesis = ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="Resolver thesis.",
        key_catalyst="Catalyst.",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=generation,
        time_expectation_hours=24.0,
        age_hours=24.0,
        expected_resolution_at=generation + timedelta(hours=24.0),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )
    thesis_row, component_rows = record_to_rows(thesis)
    from decimal import Decimal

    async with factory() as sess:
        # process_lifetimes (proc-driver-1) is already seeded by async_factory.
        sess.add(stub_invocation_row(_CLOSER_INV_ID, process_lifetime_id="proc-driver-1"))
        await sess.flush()
        closed_position = stub_position_row(position_id, thesis_id=thesis_id, status="CLOSED")
        # A CLOSED position record requires non-None realized P/L + entry timestamp
        # + non-empty execution history (PositionRecord invariants the snapshot read
        # rehydrates against; one entry fill matching the positions codec shape).
        closed_position.realized_pnl_to_date_usd = -300.0
        closed_position.entry_timestamp = generation.isoformat()
        closed_position.execution_history_json = json.dumps(
            [
                {
                    "fill_timestamp": generation.isoformat(),
                    "fill_price": 150.0,
                    "fill_quantity": 10.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "live_execution_estimate": None,
                }
            ]
        )
        sess.add(closed_position)
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        await sess.flush()
        sess.add(
            ThesisPnlLedgerRow(
                thesis_id=thesis_id,
                realized_pnl_usd=Decimal("-300.0"),
                cost_basis_usd=Decimal("1000.0"),
                provenance_json="{}",
                derived_from_invocation_id=_CLOSER_INV_ID,
                updated_at=_NOW.isoformat().replace("+00:00", "Z"),
                last_derived_event_seq=None,
            )
        )
        entry = ActivityLogEntry(
            entry_id=f"{_CLOSER_INV_ID}-POSITION_CLOSED-1",
            invocation_id=_CLOSER_INV_ID,
            timestamp=_NOW,
            event_type=EventType.POSITION_CLOSED,
            event_group=EVENT_TYPE_TO_GROUP[EventType.POSITION_CLOSED],
            position_id=position_id,
            order_id=None,
            thesis_id=thesis_id,
            source=EventSource.FILL_PROCESSOR,
            detail=PositionClosedDetail(
                exit_method=exit_method,
                exit_price=money(150.0),
                realized_pnl_usd=signed_money(-300.0),
                thesis_resolution_category="",
            ),
        )
        sess.add(activity_log_entry_to_row(entry))
        await sess.commit()


def _read_recent_thesis_resolutions(db_path: Path) -> tuple[Any, ...]:
    """Call the production ``get_recent_thesis_resolutions`` against the run's DB.

    Builds the real SQL repository (the same path ``assemble_snapshot`` uses) so
    the read exercises the codec round-trip whose ``_check_resolved_fields``
    invariant the ALP-834 half-written RESOLVED row violated.
    """
    from alphamind.scheduler.orchestrator import make_repository_providers
    from alphamind.state.config import StatePersistenceConfig
    from alphamind.state.repository import build_sql_portfolio_state_repository

    active_provider, prior_provider = make_repository_providers(_make_active_risk_parameters())
    config = StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/pip-freeze",
            "invocation_provenance_root": "/tmp/provenance",
        }
    )
    async_engine = make_async_engine(str(db_path))
    try:
        repository = build_sql_portfolio_state_repository(
            session_factory=make_async_session_factory(async_engine),
            invocation_id=_CLOSER_INV_ID,
            active_risk_parameters_provider=active_provider,
            prior_active_risk_parameters_provider=prior_provider,
            config=config,
            thesis_quality_aggregates_trailing_windows_days=(5, 20),
        )
        return repository.get_recent_thesis_resolutions(lookback_trading_days=20)
    finally:
        async_engine.sync_engine.dispose()


def _make_active_risk_parameters() -> Any:
    from alphamind.config.models.regimes import Regime
    from alphamind.scheduler.orchestrator import (
        _load_base_profile_rule_values,
        build_active_risk_parameters,
    )

    return build_active_risk_parameters(
        rule_values=_load_base_profile_rule_values(SHIPPED_CONFIG_DIR),
        regime=Regime.normal,
    )


class TestRunInvocationThesisResolution:
    """The resolver step runs in ``run_invocation`` after fill collection and
    before snapshot assembly, in its own transaction (ALP-899 / ALP-834)."""

    async def test_closed_position_thesis_resolved_and_in_recent_resolutions(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A thesis whose position closed this invocation is RESOLVED before
        snapshot assembly runs (assembly invokes ``get_recent_thesis_resolutions``
        and would raise on a half-written RESOLVED row — the ALP-834 regression);
        a post-run ``get_recent_thesis_resolutions`` returns it without raising."""
        import claude_agent_sdk

        from alphamind.portfolio_state.events.activity_log import PositionExitMethod
        from alphamind.scheduler.orchestrator import run_invocation

        await _seed_closed_position_active_thesis(
            async_factory, exit_method=PositionExitMethod.STOP_TRIGGERED
        )
        monkeypatch.setattr(claude_agent_sdk, "query", _thesis_resolved_sdk_stub("WRONG"))
        _patch_no_op_pipeline(monkeypatch, captured={})

        # run_invocation assembles the snapshot between fill collection and the
        # analysis pipeline; if the resolver had left a half-written RESOLVED
        # thesis, assembly's get_recent_thesis_resolutions would raise here.
        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
                db_path=db_path,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        # The ALP-834 regression: get_recent_thesis_resolutions returns the
        # newly-resolved thesis without raising.
        resolutions = _read_recent_thesis_resolutions(db_path)
        assert "thesis-res-1" in {r.thesis_id for r in resolutions}
        # The category + P/L + every component outcome are populated (the
        # half-written-RESOLVED row that wedged ALP-834 had them NULL).
        resolution = next(r for r in resolutions if r.thesis_id == "thesis-res-1")
        assert resolution.resolution_category is not None
        assert resolution.component_outcomes

    async def test_monitor_closed_position_resolved_at_next_invocation(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A position closed by the continuous monitor (engine TARGET_REACHED) is
        resolved at the next invocation — the resolver reads the exit method from
        the POSITION_CLOSED entry regardless of which subsystem closed it."""
        import claude_agent_sdk
        from sqlalchemy import select

        from alphamind.portfolio_state.events.activity_log import PositionExitMethod
        from alphamind.scheduler.orchestrator import run_invocation
        from alphamind.state.tables.theses import ThesisRow

        await _seed_closed_position_active_thesis(
            async_factory, exit_method=PositionExitMethod.TARGET_REACHED
        )
        monkeypatch.setattr(claude_agent_sdk, "query", _thesis_resolved_sdk_stub("VALIDATED"))
        _patch_no_op_pipeline(monkeypatch, captured={})

        await run_invocation(
            context=_make_context(
                session_factory=async_factory,
                env_path=env_path,
                archive_root=archive_root,
                db_path=db_path,
            ),
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            now=_NOW,
        )

        async with async_factory() as session:
            row = (
                await session.execute(
                    select(ThesisRow).where(ThesisRow.thesis_id == "thesis-res-1")
                )
            ).scalar_one()
            assert row.status == "RESOLVED"
            assert row.resolution_category is not None
