"""Tests for ``alphamind.scheduler.orchestrator.run_invocation`` (story 03b).

The orchestrator is the single async entrypoint the APScheduler driver
(story 04a) and the emergency receiver (story 04b) both call. Per the
design's snapshot-isolation contract
(``docs/design/05-execution-layer/state-persistence.md`` § Snapshot isolation)
it runs three separate transactions per invocation: Phase 1 commits → the
snapshot read happens between phases → Phase 2 commits per envelope. The
orchestrator wires the snapshot through ``SnapshotBackedSynthesizerReader``
into the analysis pipeline and threads the same ``AssembledSnapshot`` into
the decision pipeline.

Tests stub the LLM-dependent inner stages (``run_analysis_pipeline``,
``run_decision_pipeline``) via monkeypatch so the orchestrator wiring is
exercised end-to-end without hitting the Anthropic API.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.modes import Mode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)

# Side-effect import to break the submit_envelope_mcp ↔ portfolio_manager
# circular import: PMEnvelope first, then submit_envelope_mcp.
from alphamind.decision.portfolio_manager.models import PMEnvelope  # noqa: F401
from alphamind.execution.state_persistence.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.write_paths.phase1 import Phase1Summary
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"
_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)


def _write_placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-orch-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-orch-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


@pytest.fixture
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory bound to an initialized SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _make_venue_config() -> VenueConfig:
    creds = AlpacaCredentials(
        rest_url="https://paper-api.alpaca.markets",
        ws_url="wss://paper-api.alpaca.markets",
        api_key_env="ALPACA_PAPER_KEY",
        api_secret_env="ALPACA_PAPER_SECRET",
    )
    return VenueConfig(
        alpaca=Alpaca(paper=creds, live=creds, rate_limit_per_minute=200),
        session_hours=SessionHours(
            regular=SessionWindow(open="09:30", close="16:00"),
            pre_market=SessionWindow(open="04:00", close="09:30"),
            after_hours=SessionWindow(open="16:00", close="20:00"),
        ),
    )


def _make_phase1_inputs(*, staleness_flag: bool = False) -> Any:
    """Build a minimal ``Phase1Inputs`` with no positions / no CA activities."""
    from alphamind.risk_guardrails.guardrail_evaluation import (
        FixtureIvProvider,
        MarketInputs,
    )
    from alphamind.scheduler.phase1_inputs import Phase1Inputs

    return Phase1Inputs(
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


def _make_phase1_summary() -> Phase1Summary:
    return Phase1Summary(
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
            invocation_id="inv-x",
            timestamp=_NOW,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(approve=0, approve_with_modification=0, reject=0),
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


def _patch_no_op_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase1_summary: Phase1Summary | None = None,
    analysis_result: Any | None = None,
    decision_result: Any | None = None,
    phase1_raises: Exception | None = None,
    decision_raises: Exception | None = None,
    captured: dict[str, Any] | None = None,
    staleness_flag: bool = False,
) -> None:
    """Monkey-patch every heavy stage on the orchestrator module to no-op stubs.

    Captures keyword arguments via the ``captured`` dict so tests can assert on
    threading invariants (e.g., ``mode='halt'`` arriving at the decision
    pipeline). The stubs return the canonical empty fixture for each stage.
    """
    from alphamind.scheduler import orchestrator as module

    captured = captured if captured is not None else {}

    async def _gather_stub(**kw: Any) -> Any:
        captured["gather"] = kw
        return _make_phase1_inputs(staleness_flag=staleness_flag)

    async def _process_stub(*args: Any, **kw: Any) -> Phase1Summary:
        captured["phase1"] = {"args": args, "kwargs": kw}
        if phase1_raises is not None:
            raise phase1_raises
        # Production ``process_unprocessed_fills`` stamps ``phase1_completed_at``
        # on the bound row before returning (write_paths/phase1.py:294) AND
        # seeds the ``cash_ledger`` + ``drawdown_state`` singletons as a side
        # effect of fill integration. The stub mirrors both so the
        # orchestrator's post-Phase-1 snapshot read finds the singletons + a
        # stamped row.
        from alphamind.execution.state_persistence.invocation_context.context import (
            stamp_phase_completion,
        )

        handle = args[0]
        await _seed_singletons_via_handle(handle)
        await stamp_phase_completion(handle, column="phase1_completed_at")
        return phase1_summary or _make_phase1_summary()

    async def _analysis_stub(**kw: Any) -> Any:
        captured["analysis"] = kw
        return analysis_result or _make_analysis_result()

    async def _decision_stub(**kw: Any) -> Any:
        captured["decision"] = kw
        if decision_raises is not None:
            raise decision_raises
        return decision_result or _make_decision_result()

    async def _dispatch_stub(**kw: Any) -> Any:
        from alphamind.scheduler.phase2_dispatch import Phase2Summary

        captured["dispatch"] = kw
        return Phase2Summary(commands_submitted=0, commands_rejected=0)

    monkeypatch.setattr(module, "gather_phase1_inputs", _gather_stub)
    monkeypatch.setattr(module, "process_unprocessed_fills", _process_stub)
    monkeypatch.setattr(module, "run_analysis_pipeline", _analysis_stub)
    monkeypatch.setattr(module, "run_decision_pipeline", _decision_stub)
    monkeypatch.setattr(module, "dispatch_phase2", _dispatch_stub)


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
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
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

    async def test_persists_one_row_with_phase1_and_phase2_stamped(
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
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
            now=_NOW,
        )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.invocation_id == summary.invocation_id
        assert row.phase1_completed_at is not None
        assert row.phase2_completed_at is not None
        assert row.fill_collection_summary_json is not None
        assert row.command_execution_summary_json is not None
        assert row.active_overlays_json is not None
        assert row.feature_flags_snapshot_json is not None

    async def test_fill_collection_summary_is_serialized_phase1_summary(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The row's ``fill_collection_summary_json`` is the Phase 1 summary JSON-serialized."""
        from alphamind.scheduler.orchestrator import run_invocation

        phase1 = Phase1Summary(
            fills_processed=3,
            fills_quarantined=1,
            ca_activities_processed=2,
            reconciliation_alerts=0,
        )
        _patch_no_op_pipeline(monkeypatch, phase1_summary=phase1)
        await run_invocation(
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
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


class TestRunInvocationFailures:
    async def test_phase1_exception_leaves_row_with_phase1_completed_at_null(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Phase 1 abort: row stays (committed up-front), phase1_completed_at is NULL.

        Under the three-transaction model the invocation row is committed by
        ``insert_invocation_record`` before Phase 1 opens. A Phase 1 abort
        rolls back Phase 1's own transaction only; the row persists with
        ``phase1_completed_at IS NULL`` so the SQL repository's consistency
        guard refuses snapshot reads against this invocation, and the next
        invocation retries fills.
        """
        from alphamind.scheduler.orchestrator import run_invocation

        _patch_no_op_pipeline(monkeypatch, phase1_raises=RuntimeError("phase 1 boom"))
        with pytest.raises(RuntimeError, match="phase 1 boom"):
            await run_invocation(
                session_factory=async_factory,
                process_lifetime_id="proc-orch-1",
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                firing_run_type=RunType.market_hours_rolling,
                archive_root=archive_root,
                config_dir=SHIPPED_CONFIG_DIR,
                env_path=env_path,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                now=_NOW,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.phase1_completed_at is None
        assert row.phase2_completed_at is None

    async def test_decision_exception_leaves_phase1_committed_skips_phase2(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Between-phase abort: Phase 1 stays committed; Phase 2 is skipped.

        Per ``docs/design/mid-pipeline-failure-handling.md`` an analysis /
        decision-pipeline failure leaves the already-committed Phase 1
        writes durable and skips Phase 2. The row carries
        ``phase1_completed_at`` set, ``phase2_completed_at`` NULL.
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
                session_factory=async_factory,
                process_lifetime_id="proc-orch-1",
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                firing_run_type=RunType.market_hours_rolling,
                archive_root=archive_root,
                config_dir=SHIPPED_CONFIG_DIR,
                env_path=env_path,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                now=_NOW,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.phase1_completed_at is not None
        assert row.phase2_completed_at is None
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
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
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
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
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
        """Phase 1 degradation sets staleness on both the summary and the row."""
        from alphamind.scheduler.orchestrator import run_invocation

        _patch_no_op_pipeline(monkeypatch, staleness_flag=True)
        summary = await run_invocation(
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
            now=_NOW,
        )

        async with async_factory() as session:
            row = (await session.execute(select(InvocationRow))).scalar_one()

        assert summary.staleness_flag is True
        assert row.staleness_flag == 1


def _singleton_records() -> tuple[Any, Any]:
    """Return ``(cash_ledger, drawdown_state)`` records for the snapshot singletons."""
    from alphamind.portfolio_state.records.capital import CashLedger, DrawdownState
    from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone

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
    drawdown = DrawdownState.model_validate(
        {
            "current_drawdown_pct": 0.0,
            "equity_high_water_mark_usd": 100_000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": 0.0,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )
    return cash, drawdown


async def _seed_singletons_via_handle(handle: Any) -> None:
    """Seed the singletons inside Phase 1's open session.

    Mimics what production ``process_unprocessed_fills`` does as a side
    effect of fill integration; joins the Phase 1 transaction so the
    singletons commit together with ``phase1_completed_at``.
    """
    from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
        cash_ledger_record_to_row,
    )
    from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
        drawdown_state_record_to_row,
    )

    cash, drawdown = _singleton_records()
    handle.session.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
    handle.session.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW))


async def _seed_snapshot_singletons(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed the cash_ledger + drawdown_state singletons assemble_snapshot reads.

    Both ``SqlPortfolioStateRepository.get_cash_ledger`` and
    ``.get_drawdown_state`` raise ``RepositoryConsistencyError`` when the
    singleton row is absent; the orchestrator's between-phase snapshot
    assembly therefore requires either a real Phase 1 (which seeds them) or
    explicit seeding in the test fixture.
    """
    from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
        cash_ledger_record_to_row,
    )
    from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
        drawdown_state_record_to_row,
    )

    cash, drawdown = _singleton_records()
    async with factory() as session:
        session.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
        session.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW))
        await session.commit()


class TestRunInvocationSnapshotWiring:
    """The orchestrator threads a real ``AssembledSnapshot`` between phases.

    Pre-ALP-449 the orchestrator wired a deferred ``_EmptySynthesizerReader``
    stub because the unified-transaction model prevented a fresh-session
    snapshot read from seeing the open transaction's ``phase1_completed_at``
    write. The three-transaction refactor commits Phase 1 before the snapshot
    read, so ``SnapshotBackedSynthesizerReader`` wires correctly.
    """

    async def test_analysis_pipeline_receives_snapshot_backed_synthesizer_reader(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_analysis_pipeline gets a SnapshotBackedSynthesizerReader, not an empty stub.

        Forces the three-transaction restructure: Phase 1 commits, then the
        orchestrator assembles a snapshot via fresh sessions (which now see
        committed ``phase1_completed_at``), then wires that snapshot through
        the synthesizer reader into the analysis pipeline.
        """
        from alphamind.portfolio_state.consumers.synthesizer import (
            SnapshotBackedSynthesizerReader,
        )
        from alphamind.scheduler.orchestrator import run_invocation

        # The Phase 1 stub seeds the snapshot singletons via the handle's
        # open session, mimicking what production fill integration does.
        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
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
        """run_decision_pipeline gets a pre-built AssembledSnapshot, not a repository.

        Slice 2 of ALP-449: the decision pipeline no longer assembles its
        own snapshot; the orchestrator builds the snapshot once and threads
        the same value through both the synthesizer reader and the decision
        pipeline. The legacy ``repository`` / ``price_provider`` /
        ``portfolio_state_config`` / ``now`` kwargs drop from the signature.
        """
        from alphamind.portfolio_state.freshness import AssembledSnapshot
        from alphamind.scheduler.orchestrator import run_invocation

        captured: dict[str, Any] = {}
        _patch_no_op_pipeline(monkeypatch, captured=captured)

        await run_invocation(
            session_factory=async_factory,
            process_lifetime_id="proc-orch-1",
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            firing_run_type=RunType.market_hours_rolling,
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
            now=_NOW,
        )

        decision_kwargs = captured["decision"]
        assert isinstance(decision_kwargs["assembled_snapshot"], AssembledSnapshot)
        # The legacy snapshot-assembly kwargs no longer appear on the
        # signature — surfacing them indicates the runner is still doing
        # its own assemble_snapshot call.
        assert "repository" not in decision_kwargs
        assert "price_provider" not in decision_kwargs
        assert "portfolio_state_config" not in decision_kwargs


class TestModeToDecisionLiteral:
    """``_mode_to_decision_literal`` is symmetric with the row-side translator.

    Both raise ``ValueError`` on unknown ``Mode`` members rather than silently
    falling back to ``"normal"``. A future ``Mode`` enum expansion that adds a
    new member would otherwise silently mis-translate into ``"normal"``.
    """

    def test_normal_translates_to_normal(self) -> None:
        from alphamind.scheduler.orchestrator import _mode_to_decision_literal

        assert _mode_to_decision_literal(Mode.normal) == "normal"

    def test_halt_translates_to_halt(self) -> None:
        from alphamind.scheduler.orchestrator import _mode_to_decision_literal

        assert _mode_to_decision_literal(Mode.halt) == "halt"

    def test_unknown_mode_raises_valueerror(self) -> None:
        """Passing a non-Mode value (simulating an enum expansion) raises ValueError."""
        from enum import Enum

        from alphamind.scheduler.orchestrator import _mode_to_decision_literal

        # Simulate a future enum member by passing a fresh Enum value that is
        # not one of Mode.normal / Mode.halt. ``_mode_to_decision_literal``
        # narrows via ``is``-identity and falls into the raise branch.
        class FutureMode(Enum):
            ATTENTIVE = "attentive"

        with pytest.raises(ValueError, match="unexpected Mode member"):
            _mode_to_decision_literal(FutureMode.ATTENTIVE)  # type: ignore[arg-type]
