"""Integration tests for ``run_invocation`` (ALP-450 item 5).

The unit-level tests in ``test_orchestrator.py`` stub every heavy callee
(``gather_phase1_inputs``, ``process_unprocessed_fills``,
``run_analysis_pipeline``, ``run_decision_pipeline``, ``dispatch_phase2``)
so the orchestrator wiring is exercised without hitting the broker /
LLM / write paths in their production forms. That coverage missed the
two blockers caught in /review on PR #44 (``snapshot_metadata_json``
column population and the baseline ``activity_log`` emission) because
both were expected from the orchestrator's own glue code — not from
the stubbed inner stages.

This module pins the verify-script-style end-state assertions
(``check_invocation_row_population`` PASS + ``check_activity_log`` PASS)
against a unit-test invocation so the same regressions surface at test
time instead of verify-script time. The heavy callees stay stubbed —
the LLM-driven stages cannot reasonably run inside unit tests — but
the orchestrator's row-population + activity-log emitters run in
their production form.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)

# Side-effect import: break the submit_envelope_mcp ↔ portfolio_manager
# circular before the orchestrator imports either.
from alphamind.decision.portfolio_manager.models import PMEnvelope  # noqa: F401
from alphamind.execution.state_persistence.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).parent.parent.parent
_SHIPPED_CONFIG_DIR = _REPO_ROOT / "config"
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
        process_lifetime_id="proc-integ-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-integ-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


def _seed_portfolio_singletons(sess: Any) -> None:
    """Seed the ``cash_ledger`` + ``drawdown_state`` singleton rows.

    Production ``process_unprocessed_fills`` seeds these as a side effect of
    fill integration; with the integration test driving zero fills (no broker
    in unit tests), we seed them up-front so the orchestrator's post-Phase-1
    snapshot read finds satisfied singletons.
    """
    from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
        cash_ledger_record_to_row,
    )
    from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
        drawdown_state_record_to_row,
    )
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
    sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
    sess.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW))


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
            _seed_portfolio_singletons(sess)
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alphamind.db"


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


def _make_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
) -> Any:
    from alphamind.scheduler.run_context import RunInvocationContext

    return RunInvocationContext(
        session_factory=session_factory,
        process_lifetime_id="proc-integ-1",
        archive_root=archive_root,
        config_dir=_SHIPPED_CONFIG_DIR,
        env_path=env_path,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
    )


def _stub_heavy_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the heavy callees but keep the orchestrator's own emitters in production form.

    Per ALP-450 item 5: stub only the LLM-driven and broker-dependent
    callees (``gather_phase1_inputs``, ``run_analysis_pipeline``,
    ``run_decision_pipeline``). The Phase 1 / Phase 2 DB writers and the
    orchestrator's ``_emit_baseline_config_change_entry`` run in their
    production forms so the row-population + activity-log emission paths
    are actually exercised.
    """
    from alphamind.analysis._shared import TokensUsed
    from alphamind.analysis.synthesizer.retrieval import RetrievalStore
    from alphamind.analysis.synthesizer.runner import SynthesizerResult
    from alphamind.decision.portfolio_manager.models import (
        PMCompletionRecord,
        VerdictSummary,
    )
    from alphamind.decision.portfolio_manager.runner import PMResult
    from alphamind.risk_guardrails.guardrail_evaluation import (
        FixtureIvProvider,
        MarketInputs,
    )
    from alphamind.scheduler import orchestrator as module
    from alphamind.scheduler.phase1_inputs import Phase1Inputs

    async def _gather_stub(**_kw: Any) -> Phase1Inputs:
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
            staleness_flag=False,
        )

    async def _analysis_stub(**_kw: Any) -> Any:
        from types import SimpleNamespace

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

    async def _decision_stub(**_kw: Any) -> Any:
        from types import SimpleNamespace

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
        return SimpleNamespace(
            pm_result=pm_result,
            pydantic_snapshot=None,
            library_snapshot=None,
            analyst_result=None,
            strategist_result=None,
            pre_processor_bundle=None,
        )

    monkeypatch.setattr(module, "gather_phase1_inputs", _gather_stub)
    monkeypatch.setattr(module, "run_analysis_pipeline", _analysis_stub)
    monkeypatch.setattr(module, "run_decision_pipeline", _decision_stub)


class TestRunInvocationProductionPathArtifacts:
    """Pin the verify-script row + activity-log checks against a unit invocation.

    With the heavy LLM / broker stages stubbed but the orchestrator's own
    DB writers (``_emit_baseline_config_change_entry``, the row stampers,
    the snapshot persister) running in production form, the verify script's
    artifact checks must PASS. Regressions to the row-population path or
    the baseline activity-log emission surface here at test time rather
    than only at verify-script time.
    """

    async def test_check_invocation_row_population_passes(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end run → ``check_invocation_row_population`` PASS."""
        from alphamind.scheduler.orchestrator import run_invocation
        from alphamind.scripts.verify_pipeline_scheduler import (
            check_invocation_row_population,
        )

        _stub_heavy_stages(monkeypatch)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory,
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
                result = check_invocation_row_population(conn, invocation_id=summary.invocation_id)
        finally:
            sync_engine.dispose()
        assert result.passed, result.message

    async def test_check_activity_log_passes(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end run → ``check_activity_log`` PASS.

        The orchestrator's ``_emit_baseline_config_change_entry`` writes
        one ``DISTILLATION_CONFIG_CHANGE`` entry per invocation; with no
        fills and no commands, that is the only entry, and the
        verify-script's "at least one entry" guarantee is satisfied.
        """
        from alphamind.scheduler.orchestrator import run_invocation
        from alphamind.scripts.verify_pipeline_scheduler import check_activity_log

        _stub_heavy_stages(monkeypatch)
        summary = await run_invocation(
            context=_make_context(
                session_factory=async_factory,
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
                result = check_activity_log(conn, invocation_id=summary.invocation_id)
        finally:
            sync_engine.dispose()
        assert result.passed, result.message
