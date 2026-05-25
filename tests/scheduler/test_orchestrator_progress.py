"""Integration test for ALP-497 — orchestrator progress emission.

Drives :func:`alphamind.scheduler.orchestrator.run_invocation` against a
:class:`RecordingProgressEmitter` substitute and asserts:

* Every phase boundary the parent issue ALP-493 § (D) enumerates (excluding
  ``seed`` — story 04 emits that one) is recorded.
* The orchestrator emits ``phase_start`` / ``phase_done`` for ``phase1``,
  ``snapshot_assembly``, and ``phase2``.
* The progress emitter is correctly threaded into ``run_analysis_pipeline``
  and ``run_decision_pipeline`` — both stubs receive the same emitter the
  orchestrator constructed from ``context.debug_e2e.emitter_factory`` and
  use it to emit their own per-phase + per-agent events.
* Exactly nine ``agent_request`` / ``agent_response`` pairs land, in
  dependency order, mirroring the production sequence
  ``distillation → (3 sectors + qualitative) → adaptive → synthesizer →
  (analyst + strategist) → pre_processor → pm``.

The pipeline stubs simulate the real composition runners' progress
emission — the orchestrator-side wiring is what's under test. The
per-harness emit point is exercised by the unit tests under
``tests/analysis/test_harness_core.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import InvocationId
from alphamind._kernel.progress import ProgressEmitter
from alphamind.config.models.main import ExecutionMode
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
from alphamind.execution.write_paths.phase1 import Phase1Summary
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.scheduler.debug_e2e.broker import (
    LogOnlyAccountStateQueries,
    LogOnlyCorporateActionsQueries,
)
from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings
from alphamind.state.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from tests.scheduler._regime_helpers import make_regime_output
from tests.scheduler.test_progress import RecordingProgressEmitter

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"
_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)


_MAKE_CONTEXT_ENGINES: list[Engine] = []


@pytest.fixture(autouse=True)
def _dispose_make_context_engines() -> Any:
    yield
    while _MAKE_CONTEXT_ENGINES:
        _MAKE_CONTEXT_ENGINES.pop().dispose()


def _write_placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-orch-progress-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-orch-progress-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


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


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alphamind.db"


@pytest.fixture
async def async_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Async session factory bound to an initialized SQLite DB with the
    minimal process-lifetime row the orchestrator's FK requires."""
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

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


def _make_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path,
    emitter: RecordingProgressEmitter,
) -> Any:
    """Build a ``RunInvocationContext`` with ``debug_e2e`` wired to the recording emitter.

    The factory ignores the ``invocation_id`` argument and returns the
    pre-constructed recorder; the orchestrator only calls it once per
    invocation, so a single recorder captures the whole stream.
    """
    from alphamind.scheduler.run_context import RunInvocationContext

    sync_engine = make_engine(str(db_path))
    _MAKE_CONTEXT_ENGINES.append(sync_engine)
    sync_session_factory = make_session_factory(sync_engine)

    def _factory(_invocation_id: str) -> ProgressEmitter:
        return emitter

    return RunInvocationContext(
        session_factory=session_factory,
        sync_session_factory=sync_session_factory,
        process_lifetime_id="proc-orch-progress-1",
        archive_root=archive_root,
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
        debug_e2e=DebugE2ESettings(
            account_queries=LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO),
            ca_queries=LogOnlyCorporateActionsQueries(),
            emitter_factory=_factory,
        ),
    )


def _make_phase1_inputs(*, staleness_flag: bool = False) -> Any:
    """Minimal ``Phase1Inputs`` with no positions / no CA activities."""
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


def _singleton_records() -> tuple[Any, Any]:
    """Mirror ``test_orchestrator._singleton_records``."""
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
    """Mirror ``process_unprocessed_fills``'s singleton-seeding side effects.

    Joins the Phase 1 transaction so the singletons commit together with
    ``phase1_completed_at`` — same shape as the helper in
    ``test_orchestrator.py``.
    """
    from alphamind.state.tables.cash_ledger_codec import cash_ledger_record_to_row
    from alphamind.state.tables.drawdown_state_codec import drawdown_state_record_to_row

    cash, drawdown = _singleton_records()
    handle.session.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
    handle.session.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW))


def _make_analysis_result() -> Any:
    """Build a no-op ``AnalysisPipelineResult`` carrying minimal synth output."""
    from types import SimpleNamespace

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
    return SimpleNamespace(
        synthesizer_result=synth,
        distillation_outputs=None,
        domain_researchers_output=None,
        qualitative_result=None,
        adaptive_result=None,
    )


def _make_decision_result() -> Any:
    """Build a no-op ``DecisionPipelineResult`` with empty submission_log."""
    from types import SimpleNamespace

    from alphamind.analysis._shared import TokensUsed
    from alphamind.decision.portfolio_manager.models import (
        PMCompletionRecord,
        VerdictSummary,
    )
    from alphamind.decision.portfolio_manager.runner import PMResult

    pm_result = PMResult(
        output=PMCompletionRecord(
            invocation_id=InvocationId("inv-progress-test"),
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
    return SimpleNamespace(
        pm_result=pm_result,
        pydantic_snapshot=None,
        library_snapshot=None,
        analyst_result=None,
        strategist_result=None,
        pre_processor_bundle=None,
    )


def _emit_agent_call(progress: ProgressEmitter, *, phase: str, agent: str, model: str) -> None:
    """Emit one ``agent_request`` + ``agent_response`` pair with placeholder fields.

    Test helper that lets the pipeline stubs simulate a single SDK call's
    progress emission without re-copying the 5-field
    ``agent_response`` shape from parent issue ALP-493 § (B) at every
    call site.
    """
    progress.agent_request(phase=phase, agent=agent, model=model)
    progress.agent_response(
        phase=phase,
        agent=agent,
        model=model,
        duration_s=1.0,
        input_tokens=10,
        output_tokens=5,
        tool_calls=0,
        stop_reason="end_turn",
    )


def _emit_analysis_pipeline_progress(progress: ProgressEmitter) -> None:
    """Simulate the real ``run_analysis_pipeline``'s progress emission.

    Mirrors the actual sequence in ``alphamind.pipeline.analysis.run_analysis_pipeline``:
    distillation → (3 domain_researcher sectors + qualitative) →
    adaptive → synthesizer. The 6 SDK calls land as 6 ``agent_request`` /
    ``agent_response`` pairs.
    """
    progress.phase_start("distillation")
    progress.phase_done("distillation")

    progress.phase_start("domain_researchers")
    progress.phase_start("qualitative")
    for agent in ("tech_semis_researcher", "financials_researcher", "energy_researcher"):
        _emit_agent_call(
            progress, phase="domain_researchers", agent=agent, model="claude-sonnet-4-6"
        )
    _emit_agent_call(
        progress, phase="qualitative", agent="qualitative_researcher", model="claude-sonnet-4-6"
    )
    progress.phase_done("domain_researchers")
    progress.phase_done("qualitative")

    progress.phase_start("adaptive")
    _emit_agent_call(
        progress, phase="adaptive", agent="adaptive_researcher", model="claude-sonnet-4-6"
    )
    progress.phase_done("adaptive")

    progress.phase_start("synthesizer")
    _emit_agent_call(progress, phase="synthesizer", agent="synthesizer", model="claude-opus-4-7")
    progress.phase_done("synthesizer")


def _emit_decision_pipeline_progress(progress: ProgressEmitter) -> None:
    """Simulate the real ``run_decision_pipeline``'s progress emission.

    Mirrors the actual sequence in ``alphamind.pipeline.decision.run_decision_pipeline``:
    (analyst + strategist) → pre_processor → pm. The 3 SDK calls land
    as 3 ``agent_request`` / ``agent_response`` pairs.
    """
    progress.phase_start("analyst")
    progress.phase_start("strategist")
    _emit_agent_call(progress, phase="analyst", agent="analyst", model="claude-opus-4-7")
    _emit_agent_call(progress, phase="strategist", agent="strategist", model="claude-opus-4-7")
    progress.phase_done("analyst")
    progress.phase_done("strategist")

    progress.phase_start("pre_processor")
    progress.phase_done("pre_processor")

    progress.phase_start("pm")
    _emit_agent_call(progress, phase="pm", agent="portfolio_manager", model="claude-opus-4-7")
    progress.phase_done("pm")


def _patch_no_op_pipeline_with_progress_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the heavy pipeline stages; each stub uses the ``progress`` kwarg.

    The pipeline stubs simulate the real composition runners' progress
    emission so the orchestrator's threading invariant is verified:
    a single emitter constructed at the top of ``run_invocation`` reaches
    both pipelines.
    """
    from alphamind.scheduler import orchestrator as module

    async def _gather_stub(**_kw: Any) -> Any:
        return _make_phase1_inputs()

    async def _process_stub(*args: Any, **_kw: Any) -> Phase1Summary:
        from alphamind.state.invocation_context.context import (
            stamp_phase_completion,
        )

        handle = args[0]
        await _seed_singletons_via_handle(handle)
        await stamp_phase_completion(handle, column="phase1_completed_at")
        return _make_phase1_summary()

    async def _analysis_stub(**kw: Any) -> Any:
        progress = kw["progress"]
        _emit_analysis_pipeline_progress(progress)
        return _make_analysis_result()

    async def _decision_stub(**kw: Any) -> Any:
        progress = kw["progress"]
        _emit_decision_pipeline_progress(progress)
        return _make_decision_result()

    async def _dispatch_stub(**_kw: Any) -> Any:
        from alphamind.scheduler.phase2_dispatch import Phase2Summary

        return Phase2Summary(commands_submitted=0, commands_rejected=0)

    def _regime_stub(**_kw: Any) -> Any:
        return make_regime_output(now=_NOW)

    monkeypatch.setattr(module, "gather_phase1_inputs", _gather_stub)
    monkeypatch.setattr(module, "process_unprocessed_fills", _process_stub)
    monkeypatch.setattr(module, "run_analysis_pipeline", _analysis_stub)
    monkeypatch.setattr(module, "run_decision_pipeline", _decision_stub)
    monkeypatch.setattr(module, "dispatch_phase2", _dispatch_stub)
    monkeypatch.setattr(module, "_resolve_regime_adaptation_for_invocation", _regime_stub)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_run_invocation_records_every_phase_boundary(
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every phase boundary parent issue ALP-493 § (D) names (excluding ``seed``)
    appears as a ``phase_start`` / ``phase_done`` pair in the recorded stream."""
    from alphamind.scheduler.orchestrator import run_invocation

    _patch_no_op_pipeline_with_progress_emit(monkeypatch)
    emitter = RecordingProgressEmitter()

    await run_invocation(
        context=_make_context(
            session_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
            db_path=db_path,
            emitter=emitter,
        ),
        trigger_type="manual",
        trigger_source="cli",
        trigger_reason="test",
        firing_run_type=RunType.market_hours_rolling,
        now=_NOW,
    )

    starts = {fields["phase"] for kind, fields in emitter.events if kind == "phase_start"}
    dones = {fields["phase"] for kind, fields in emitter.events if kind == "phase_done"}

    expected_phases = {
        "phase1",
        "snapshot_assembly",
        "distillation",
        "domain_researchers",
        "qualitative",
        "adaptive",
        "synthesizer",
        "analyst",
        "strategist",
        "pre_processor",
        "pm",
        "phase2",
    }
    assert starts == expected_phases, f"missing phase_start events: {expected_phases - starts}"
    assert dones == expected_phases, f"missing phase_done events: {expected_phases - dones}"


async def test_run_invocation_records_nine_agent_request_response_pairs(
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 9 ``agent_request`` / ``agent_response`` pairs land in dependency order.

    The 9 SDK calls correspond to the parent issue's pre-resolved SDK-call count:
    3 domain researchers + qualitative + adaptive + synthesizer + analyst +
    strategist + portfolio_manager.
    """
    from alphamind.scheduler.orchestrator import run_invocation

    _patch_no_op_pipeline_with_progress_emit(monkeypatch)
    emitter = RecordingProgressEmitter()

    await run_invocation(
        context=_make_context(
            session_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
            db_path=db_path,
            emitter=emitter,
        ),
        trigger_type="manual",
        trigger_source="cli",
        trigger_reason="test",
        firing_run_type=RunType.market_hours_rolling,
        now=_NOW,
    )

    requests = [fields for kind, fields in emitter.events if kind == "agent_request"]
    responses = [fields for kind, fields in emitter.events if kind == "agent_response"]

    assert len(requests) == 9, f"expected 9 agent_request events, got {len(requests)}"
    assert len(responses) == 9, f"expected 9 agent_response events, got {len(responses)}"

    expected_agents_in_order = [
        "tech_semis_researcher",
        "financials_researcher",
        "energy_researcher",
        "qualitative_researcher",
        "adaptive_researcher",
        "synthesizer",
        "analyst",
        "strategist",
        "portfolio_manager",
    ]
    actual_agents = [r["agent"] for r in requests]
    assert actual_agents == expected_agents_in_order, (
        f"agent_request order mismatch: {actual_agents} != {expected_agents_in_order}"
    )

    # Each agent_response carries the five fixed fields per parent issue § (B).
    for response in responses:
        assert "duration_s" in response
        assert "input_tokens" in response
        assert "output_tokens" in response
        assert "tool_calls" in response
        assert "stop_reason" in response


async def test_phase1_done_carries_fills_processed(
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``phase_done("phase1")`` carries the ``fills_processed`` field per AC."""
    from alphamind.scheduler.orchestrator import run_invocation

    _patch_no_op_pipeline_with_progress_emit(monkeypatch)
    emitter = RecordingProgressEmitter()

    await run_invocation(
        context=_make_context(
            session_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
            db_path=db_path,
            emitter=emitter,
        ),
        trigger_type="manual",
        trigger_source="cli",
        trigger_reason="test",
        firing_run_type=RunType.market_hours_rolling,
        now=_NOW,
    )

    phase1_done = next(
        fields
        for kind, fields in emitter.events
        if kind == "phase_done" and fields.get("phase") == "phase1"
    )
    assert phase1_done["fills_processed"] == 0


async def test_phase2_done_carries_commands_submitted(
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``phase_done("phase2")`` carries the ``commands_submitted`` field per AC."""
    from alphamind.scheduler.orchestrator import run_invocation

    _patch_no_op_pipeline_with_progress_emit(monkeypatch)
    emitter = RecordingProgressEmitter()

    await run_invocation(
        context=_make_context(
            session_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
            db_path=db_path,
            emitter=emitter,
        ),
        trigger_type="manual",
        trigger_source="cli",
        trigger_reason="test",
        firing_run_type=RunType.market_hours_rolling,
        now=_NOW,
    )

    phase2_done = next(
        fields
        for kind, fields in emitter.events
        if kind == "phase_done" and fields.get("phase") == "phase2"
    )
    assert phase2_done["commands_submitted"] == 0


async def test_run_invocation_without_debug_e2e_uses_noop_emitter(
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``context.debug_e2e is None`` → orchestrator uses the no-op singleton (no emit).

    Verifies the default production path: when no debug-e2e settings are
    provided the orchestrator does not invoke any emitter factory and
    never touches a recording surface.
    """
    from alphamind.scheduler.orchestrator import run_invocation
    from alphamind.scheduler.run_context import RunInvocationContext

    _patch_no_op_pipeline_with_progress_emit(monkeypatch)

    sync_engine = make_engine(str(db_path))
    _MAKE_CONTEXT_ENGINES.append(sync_engine)
    sync_session_factory = make_session_factory(sync_engine)

    # No ``debug_e2e`` arg — defaults to ``None``.
    context = RunInvocationContext(
        session_factory=async_factory,
        sync_session_factory=sync_session_factory,
        process_lifetime_id="proc-orch-progress-1",
        archive_root=archive_root,
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
    )

    summary = await run_invocation(
        context=context,
        trigger_type="manual",
        trigger_source="cli",
        trigger_reason="test",
        firing_run_type=RunType.market_hours_rolling,
        now=_NOW,
    )

    # The invocation completes normally — the no-op emitter absorbs every event.
    assert summary.invocation_id
