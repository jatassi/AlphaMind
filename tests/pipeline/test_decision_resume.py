"""Tests for decision-layer replay short-circuit — ALP-695.

Mirror of the analysis-layer ``test_analysis_resume.py`` (ALP-694) for
``run_decision_pipeline``. When ``debug_e2e.resume_context`` is set and a
decision phase is in ``phases_to_replay``, the runner hydrates the typed
result from disk, copies the per-agent diagnostic directory, emits a
``phase_done(phase, replayed_from=...)`` event, and skips the agent's
runner entirely.

Test-list shape per the parent dispatch spec:

1. ``run_decision_pipeline`` skips analyst+strategist together when both
   are in ``phases_to_replay`` (target=pm). Both files load; pm runs
   normally.
2. When target=analyst, the TaskGroup runs normally — no decision-layer
   replays (upstream replays are analysis-layer, out of scope here).
3. When ``resume_context is None``, byte-identical behaviour — no extra
   emission, no extra IO.
4. Replay-emit-order: ``phase_start`` → ``read_phase_output`` →
   diagnostic-dir copy → ``phase_done(phase, replayed_from=<source-id>)``.
5. Skipped phases hydrate to a result equal to what the runner would have
   produced.
6. Skipped phases copy their per-agent diagnostic directory end-to-end.
7. ``git grep -l ResumeContext src/alphamind/decision/`` returns nothing.

The four agent runners are monkeypatched at the composition module's
namespace exactly as in ``tests/pipeline/test_decision.py`` so the tests
never touch the filesystem outside the on-disk phase-output and
diagnostic-dir fixtures.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind._kernel.ids import InvocationId
from alphamind.analysis._shared import TokensUsed
from alphamind.commands.pm_envelope import (
    PMCompletionRecord,
    VerdictSummary,
)
from alphamind.commands.validation_results import ValidationResult
from alphamind.decision.analyst.runner import AnalystResult
from alphamind.decision.portfolio_manager.runner import PMResult
from alphamind.decision.strategist.runner import StrategistResult
from alphamind.scheduler.debug_e2e.phase_outputs import write_phase_output
from alphamind.scheduler.debug_e2e.resume import ResumeContext, phases_to_replay

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_INVOCATION_ID = "inv-decision-resume-001"
_SOURCE_INVOCATION_ID = "inv-source-archive-001"
_TIMESTAMP = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _tokens() -> TokensUsed:
    return TokensUsed(input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0)


# ---------------------------------------------------------------------------
# Stage result factories (minimal valid values)
# ---------------------------------------------------------------------------


def _make_analyst_result() -> AnalystResult:
    from alphamind.decision.analyst.models import AnalystOutput

    return AnalystResult(
        output=AnalystOutput(
            invocation_id=InvocationId(_SOURCE_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            mode="normal",
            recommendations=(),
        ),
        retry_count=2,
        tokens_used=_tokens(),
        tool_calls_used=1,
        wall_clock_seconds=5.0,
        stop_reason="end_turn",
    )


def _make_strategist_result() -> StrategistResult:
    from alphamind.decision.strategist.models import (
        PortfolioLevelObservations,
        StrategistOutput,
    )

    return StrategistResult(
        output=StrategistOutput(
            invocation_id=InvocationId(_SOURCE_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=PortfolioLevelObservations(
                aggregate_thesis_health="Stable.",
                sector_balance_shifts="None.",
                thesis_dependency_warnings="None.",
                capital_allocation_observations="Balanced.",
            ),
        ),
        validation_result=ValidationResult(errors=()),
        tokens_used=_tokens(),
        metadata={"attempts": 1},
    )


def _make_pm_result() -> PMResult:
    return PMResult(
        output=PMCompletionRecord(
            invocation_id=InvocationId(_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(
                approve=0,
                approve_with_modification=0,
                reject=0,
                override_with_corrective_action=0,
            ),
        ),
        submission_log=(),
        retry_count=0,
        tokens_used=_tokens(),
        tool_calls_used=0,
        wall_clock_seconds=3.0,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Pre-processor bundle helper (re-use existing fixture)
# ---------------------------------------------------------------------------


def _make_pre_processor_bundle() -> Any:
    from tests.pipeline.test_decision import _make_pre_processor_bundle as _upstream

    return _upstream()


# ---------------------------------------------------------------------------
# CallLog: records which stubs were called
# ---------------------------------------------------------------------------


class _CallLog:
    """Captures stage invocation order so tests can assert skipping."""

    def __init__(self) -> None:
        self.order: list[str] = []


# ---------------------------------------------------------------------------
# Monkeypatch helpers
# ---------------------------------------------------------------------------


def _patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log: _CallLog,
    analyst_result: AnalystResult | None = None,
    strategist_result: StrategistResult | None = None,
    pm_result: PMResult | None = None,
) -> None:
    """Patch every agent runner at the composition module's namespace."""
    from alphamind.pipeline import decision as composition

    analyst_val = analyst_result or _make_analyst_result()
    strategist_val = strategist_result or _make_strategist_result()
    pp_val = _make_pre_processor_bundle()
    pm_val = pm_result or _make_pm_result()

    async def _analyst_stub(**kw: Any) -> AnalystResult:
        log.order.append("analyst")
        return analyst_val

    async def _strategist_stub(**kw: Any) -> StrategistResult:
        log.order.append("strategist")
        return strategist_val

    def _pp_stub(**kw: Any) -> Any:
        log.order.append("pre_processor")
        return pp_val

    async def _pm_stub(**kw: Any) -> PMResult:
        log.order.append("pm")
        return pm_val

    monkeypatch.setattr(composition, "run_analyst", _analyst_stub)
    monkeypatch.setattr(composition, "run_strategist", _strategist_stub)
    monkeypatch.setattr(composition, "run_proposal_pre_processor", _pp_stub)
    monkeypatch.setattr(composition, "run_portfolio_manager", _pm_stub)


def _make_minimal_inputs() -> dict[str, Any]:
    from tests.pipeline.test_decision import _make_minimal_inputs as _upstream

    return _upstream()


_DEBUG_E2E_SENTINEL = object()


def _drive(
    archive_root: Path | None,
    *,
    debug_e2e: object | None = _DEBUG_E2E_SENTINEL,
    resume_context: ResumeContext | None = None,
    progress: Any = None,
    **overrides: Any,
) -> Any:
    from alphamind.pipeline.decision import run_decision_pipeline

    async def _go() -> Any:
        kwargs = _make_minimal_inputs()
        kwargs["archive_root"] = archive_root
        kwargs["invocation_id"] = _INVOCATION_ID
        kwargs["debug_e2e"] = debug_e2e
        kwargs["resume_context"] = resume_context
        if progress is not None:
            kwargs["progress"] = progress
        kwargs.update(overrides)
        return await run_decision_pipeline(**kwargs)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Source-archive fixture helpers
# ---------------------------------------------------------------------------


def _seed_source_archive(
    *,
    archive_root: Path,
    invocation_id: str,
    analyst_result: AnalystResult | None = None,
    strategist_result: StrategistResult | None = None,
    pm_result: PMResult | None = None,
) -> Path:
    """Write phase-output JSON files + diagnostic directories under the source archive.

    Returns the source archive directory (i.e. ``<root>/invocations/<id>/``).
    """
    from alphamind.decision.analyst.models import AnalystResultModel
    from alphamind.decision.portfolio_manager.models import PMResultModel
    from alphamind.decision.strategist.models import StrategistResultModel

    source_dir = archive_root / "invocations" / invocation_id
    source_dir.mkdir(parents=True, exist_ok=True)

    if analyst_result is not None:
        write_phase_output(
            archive_dir=source_dir,
            phase="analyst",
            model=AnalystResultModel.from_domain(analyst_result),
        )
        _seed_diag_dir(source_dir, "analyst")
    if strategist_result is not None:
        write_phase_output(
            archive_dir=source_dir,
            phase="strategist",
            model=StrategistResultModel.from_domain(strategist_result),
        )
        _seed_diag_dir(source_dir, "strategist")
    if pm_result is not None:
        write_phase_output(
            archive_dir=source_dir,
            phase="pm",
            model=PMResultModel.from_domain(pm_result),
        )
        _seed_diag_dir(source_dir, "portfolio_manager")
    return source_dir


def _seed_diag_dir(source_archive_dir: Path, agent: str) -> None:
    """Seed a per-agent diagnostic dir with a representative marker file."""
    diag_dir = source_archive_dir / "decision" / agent
    diag_dir.mkdir(parents=True, exist_ok=True)
    (diag_dir / "prompt.md").write_text(
        f"# {agent} prompt - source\n", encoding="utf-8"
    )
    (diag_dir / "response_initial.md").write_text(
        f"# {agent} response - source\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tests: replay-from-pm (target=pm — analyst+strategist both replayed)
# ---------------------------------------------------------------------------


class TestReplayFromPM:
    def test_skips_analyst_and_strategist_when_both_in_phases_to_replay(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When target=pm, analyst+strategist are both in phases_to_replay.

        The composition runner skips the entire analyst+strategist
        TaskGroup, hydrates both results from disk, and runs pm normally
        downstream.
        """
        archive_root = tmp_path
        source_analyst = _make_analyst_result()
        source_strategist = _make_strategist_result()
        _seed_source_archive(
            archive_root=archive_root,
            invocation_id=_SOURCE_INVOCATION_ID,
            analyst_result=source_analyst,
            strategist_result=source_strategist,
        )

        log = _CallLog()
        _patch_runners(monkeypatch, log=log)

        resume_ctx = ResumeContext(
            source_archive_dir=archive_root / "invocations" / _SOURCE_INVOCATION_ID,
            resume_phase="pm",
            phases_to_replay=phases_to_replay("pm"),
        )

        result = _drive(archive_root=archive_root, resume_context=resume_ctx)

        # analyst and strategist runners were NOT called — TaskGroup
        # was skipped entirely.
        assert "analyst" not in log.order
        assert "strategist" not in log.order
        # pm DID run downstream.
        assert "pm" in log.order
        # The result carries the disk-loaded analyst + strategist values.
        assert result.analyst_result == source_analyst
        assert result.strategist_result == source_strategist


# ---------------------------------------------------------------------------
# Tests: replay-from-analyst (target=analyst — neither phase replayed)
# ---------------------------------------------------------------------------


class TestReplayFromAnalyst:
    def test_task_group_runs_normally_when_target_is_analyst(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When target=analyst, neither analyst nor strategist is in
        ``phases_to_replay`` (the target itself is RE-RUN, and strategist is
        co-upstream of pm — it's at-or-after the target). The TaskGroup
        runs normally.

        Decision-layer replays are only triggered by target=pm; for
        target=analyst the upstream replays happen in the analysis layer
        (ALP-694, out of scope here).
        """
        archive_root = tmp_path
        # No on-disk phase outputs needed — analyst/strategist won't try to
        # replay. Seed only an empty source dir for context realism.
        (archive_root / "invocations" / _SOURCE_INVOCATION_ID).mkdir(parents=True)

        log = _CallLog()
        _patch_runners(monkeypatch, log=log)

        resume_ctx = ResumeContext(
            source_archive_dir=archive_root / "invocations" / _SOURCE_INVOCATION_ID,
            resume_phase="analyst",
            phases_to_replay=phases_to_replay("analyst"),
        )
        # Sanity: phases_to_replay("analyst") excludes both analyst and
        # strategist (only upstream analysis-layer phases are present).
        assert "analyst" not in resume_ctx.phases_to_replay
        assert "strategist" not in resume_ctx.phases_to_replay

        _drive(archive_root=archive_root, resume_context=resume_ctx)

        # Both analyst and strategist runners DID run.
        assert "analyst" in log.order
        assert "strategist" in log.order
        # pm runs downstream as usual.
        assert "pm" in log.order


# ---------------------------------------------------------------------------
# Tests: resume_context is None — byte-identical behaviour
# ---------------------------------------------------------------------------


class TestResumeContextNone:
    def test_runner_runs_all_stages_when_resume_context_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When ``resume_context is None``, every stage runs (no replays)."""
        log = _CallLog()
        _patch_runners(monkeypatch, log=log)

        _drive(archive_root=tmp_path, resume_context=None)

        assert "analyst" in log.order
        assert "strategist" in log.order
        assert "pre_processor" in log.order
        assert "pm" in log.order

    def test_no_extra_io_when_resume_context_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No replay-side IO (no diagnostic-dir copy, no read_phase_output).

        Without ``resume_context``, the runner never touches the source
        archive directory. The only files created under ``tmp_path`` are
        the per-phase JSON outputs from the emission path (ALP-692), not
        replay-side copies. The absence of a ``decision/`` subdir confirms
        no diagnostic-dir copy happened.
        """
        log = _CallLog()
        _patch_runners(monkeypatch, log=log)

        # Pre-create source-side artefacts that the replay path WOULD copy
        # if it were active. After the run, target-side ``decision/`` must
        # NOT exist — the runner never reached for them.
        _seed_source_archive(
            archive_root=tmp_path,
            invocation_id=_SOURCE_INVOCATION_ID,
            analyst_result=_make_analyst_result(),
            strategist_result=_make_strategist_result(),
        )

        _drive(archive_root=tmp_path, resume_context=None)

        # Target-side diagnostic dir was never created — confirms no copy.
        target_decision_dir = tmp_path / "invocations" / _INVOCATION_ID / "decision"
        assert not target_decision_dir.exists(), (
            f"Replay-side diagnostic copy ran but should not have: "
            f"{target_decision_dir} exists"
        )
