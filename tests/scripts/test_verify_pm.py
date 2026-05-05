"""Unit tests for ``scripts/verify_pm.py`` (ALP-331).

The real-SDK live verification script's testable logic — the verdict
rubric, the synthesizer-archive reader, and the four in-code scenario
builders — is exercised here without touching the Anthropic API. The
live SDK invocation is verified manually by the operator running the
script after this PR lands.

Mirrors :mod:`tests.scripts.test_verify_strategist` extended with the
synchronous-rejection scenario.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.decision.portfolio_manager.models import (
    PMCompletionRecord,
)
from alphamind.decision.portfolio_manager.runner import (
    PMResult,
    run_portfolio_manager,
)
from alphamind.execution.oms.submit_envelope_mcp import (
    Acknowledgment,
    SubmissionLogEntry,
    SubmissionResult,
)
from alphamind.scripts.verify_pm import (
    Verdict,
    build_emergency_scenario_inputs,
    build_halt_scenario_inputs,
    build_normal_scenario_inputs,
    build_synchronous_rejection_scenario_inputs,
    main,
    read_retrieval_store,
    read_synthesizer_text,
    save_scenario_fixture,
    verdict_for_success,
)

_TS = "2026-05-04T14:30:00+00:00"


# ---------------------------------------------------------------------------
# Helpers — synthetic PMResult fixtures
# ---------------------------------------------------------------------------


def _make_completion_record(
    *,
    invocation_id: str = "inv-pm-001",
    envelopes_submitted: int = 0,
    approve: int = 0,
    approve_with_modification: int = 0,
    reject: int = 0,
) -> PMCompletionRecord:
    return PMCompletionRecord.model_validate(
        {
            "invocation_id": invocation_id,
            "timestamp": _TS,
            "envelopes_submitted": envelopes_submitted,
            "verdict_summary": {
                "approve": approve,
                "approve_with_modification": approve_with_modification,
                "reject": reject,
            },
        }
    )


def _make_pm_result(
    *,
    record: PMCompletionRecord | None = None,
    submission_log: tuple[SubmissionLogEntry, ...] = (),
) -> PMResult:
    return PMResult(
        output=record or _make_completion_record(),
        submission_log=submission_log,
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=10_000,
            output_tokens=2_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=3,
        wall_clock_seconds=12.5,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# 1. Verdict rubric — clean run → PASS
# ---------------------------------------------------------------------------


def test_verdict_rubric_passes_on_clean_run() -> None:
    """A PMResult with no synchronous rejections classifies as PASS."""
    result = _make_pm_result()

    verdict = verdict_for_success(result=result, scenario="normal")

    assert verdict.value == Verdict.PASS.value
    assert verdict is Verdict.PASS


# ---------------------------------------------------------------------------
# 2. Verdict rubric — submission-log rejection on non-synchronous scenario → WARN
# ---------------------------------------------------------------------------


def _make_envelope_for_log(invocation_id: str = "inv-pm-001") -> Any:
    """Build a minimal PMAnalystEnvelope with one approved AddCommand.

    We need a real PMEnvelope-discriminated-union value (not a stub) so
    SubmissionLogEntry's typing holds. The envelope's verdict has to be
    compatible with the embedded command count, but the verdict-rubric
    code only inspects ``submission_results``, so any well-formed
    envelope suffices.
    """
    from alphamind.decision.portfolio_manager.models import (
        AddCommand,
        CriterionAssessment,
        OMSInstrument,
        OMSPositionSize,
        PMAnalystEnvelope,
        ThesisQualityEvaluation,
    )

    cmd = AddCommand(
        command_type="add",
        position_id="POS-AAPL-001",
        instrument=OMSInstrument(asset_type="equity", direction="long", underlying="AAPL"),
        position_size=OMSPositionSize(sector="tech"),
    )
    pass_criterion = CriterionAssessment(status="pass")
    return PMAnalystEnvelope(
        envelope_id="ENV-REC-1",
        invocation_id=invocation_id,
        source_provenance="pm_analyst",
        source_recommendation_id="REC-1",
        recommendation_type="new_entry",
        verdict="approve",
        evaluation=ThesisQualityEvaluation(
            falsifiability=pass_criterion,
            sizing_proportionality=pass_criterion,
            portfolio_coherence=pass_criterion,
            timing_plausibility=pass_criterion,
            counterargument_consideration=pass_criterion,
        ),
        modifications=(),
        concerns=(),
        rationale_narrative="approving",
        commands=(cmd,),
    )


def _make_rejected_submission_result() -> SubmissionResult:
    """Build a SubmissionResult with status='rejected'."""
    from alphamind.execution.oms.submit_envelope_mcp import RejectionPayload, _BreachedRule

    return SubmissionResult(
        command_ordinal=0,
        status="rejected",
        command_id="inv-pm-001.ENV-REC-1.0.0",
        rejection_payload=RejectionPayload(
            rules_breached=(
                _BreachedRule(
                    rule="net_long_pct",
                    current=58.0,
                    limit=60.0,
                    overage=1.5,
                    unit="% of portfolio",
                ),
            ),
            suggested_modification="Reduce sizing.",
        ),
    )


def test_verdict_rubric_warns_on_synchronous_rejection_in_normal_scenario() -> None:
    """A submission-log rejection in a non-synchronous_rejection scenario surfaces as WARN.

    In the PM context the engine-stub's synchronous rejection is the
    closest analog to a "validator warning" — the PM emitted a command
    that violated a guardrail. The verdict rubric flags it so the
    operator inspects the fixture before treating it as a clean baseline.
    """
    log_entry = SubmissionLogEntry(
        envelope=_make_envelope_for_log(),
        submission_results=(_make_rejected_submission_result(),),
    )
    result = _make_pm_result(submission_log=(log_entry,))

    verdict = verdict_for_success(result=result, scenario="normal")

    assert verdict is Verdict.WARN


# ---------------------------------------------------------------------------
# 3. Verdict rubric — zero rejections in the synchronous_rejection scenario → WARN
# ---------------------------------------------------------------------------


def test_verdict_rubric_warns_on_zero_rejections_in_synchronous_rejection_scenario(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean run of the synchronous_rejection scenario must surface as WARN.

    The scenario's purpose is to exercise the post-rejection feedback
    path; if every command was accepted, the scenario's initial
    validation state is too lenient and needs adjustment. The rubric
    surfaces this as WARN (not FAIL) because the PM itself is not broken.
    """
    from alphamind.scripts.verify_pm import describe_verdict_reason

    # Clean run: no submission_log entries, hence no rejections.
    result = _make_pm_result()

    verdict = verdict_for_success(result=result, scenario="synchronous_rejection")
    reason = describe_verdict_reason(
        result=result, scenario="synchronous_rejection", verdict=verdict
    )

    assert verdict is Verdict.WARN
    assert "no rejection triggered" in reason.lower()


# ---------------------------------------------------------------------------
# 4. Archive reader — locates the synthesizer's response.md + retrieval store
# ---------------------------------------------------------------------------


def test_archive_reader_locates_synthesizer_artifacts(tmp_path: Path) -> None:
    """The reader returns brief text + retrieval store from a recorded archive."""
    invocation_id = "20260504T120000Z-verify-synthesizer"
    archive_root = tmp_path / "archive"
    diag_dir = archive_root / "invocations" / invocation_id / "analysis" / "synthesizer"
    diag_dir.mkdir(parents=True)
    expected_text = "Synthesis prose body cites [SA-TECH-1] and [CR-1].\n"
    (diag_dir / "response.md").write_text(expected_text, encoding="utf-8")
    stage_dir = archive_root / "invocations" / invocation_id / "stage_artifacts"
    stage_dir.mkdir(parents=True)
    (stage_dir / "retrieval_store.json").write_text(
        '{"entries": {}, "freshness_by_source": {}}', encoding="utf-8"
    )

    text = read_synthesizer_text(archive_root=archive_root, synthesizer_invocation_id=invocation_id)
    store = read_retrieval_store(archive_root=archive_root, synthesizer_invocation_id=invocation_id)

    assert text == expected_text
    assert isinstance(store, RetrievalStore)


def test_archive_reader_raises_on_missing_archive(tmp_path: Path) -> None:
    """Missing synthesizer archive raises FileNotFoundError naming the path."""
    archive_root = tmp_path / "archive"

    with pytest.raises(FileNotFoundError, match="synthesizer"):
        read_synthesizer_text(
            archive_root=archive_root,
            synthesizer_invocation_id="missing-invocation-id",
        )


# ---------------------------------------------------------------------------
# 5. Scenario builders — each returns inputs satisfying run_portfolio_manager
# ---------------------------------------------------------------------------


def _required_runner_kwargs() -> set[str]:
    """Return the set of required (non-default) keyword arguments to
    ``run_portfolio_manager``.

    A required parameter is one whose ``Parameter.default`` is
    ``Parameter.empty``. Builders must supply every required kwarg; the
    optional ones (e.g., ``halt_state``, ``regime_transition_breaches``)
    are scenario-conditional.
    """
    sig = inspect.signature(run_portfolio_manager)
    return {
        name
        for name, param in sig.parameters.items()
        if param.default is inspect.Parameter.empty and name != "self"
    }


def _empty_retrieval_store() -> RetrievalStore:
    return RetrievalStore(entries={}, freshness_by_source={})


@pytest.mark.parametrize(
    "builder_name,builder",
    [
        ("normal", build_normal_scenario_inputs),
        ("halt", build_halt_scenario_inputs),
        ("emergency", build_emergency_scenario_inputs),
        ("synchronous_rejection", build_synchronous_rejection_scenario_inputs),
    ],
)
def test_each_scenario_builder_returns_runnable_inputs(
    builder_name: str,
    builder: Any,
) -> None:
    """Each builder returns kwargs that cover ``run_portfolio_manager``'s required set.

    The builders may not actually invoke the runner (which would touch the
    real SDK). This test asserts only that the returned dict satisfies the
    signature contract: every required parameter is present.
    """
    inputs = builder(
        synthesizer_text="## Synthesis prose.\n",
        retrieval_store=_empty_retrieval_store(),
    )

    required = _required_runner_kwargs()
    missing = required - set(inputs)
    assert not missing, f"{builder_name} builder missing required kwargs: {missing}"

    # Halt scenario must include halt_state + current_price_lookup
    # (the runner raises ValueError otherwise).
    if builder_name == "halt":
        assert inputs.get("halt_state") is not None
        assert inputs.get("current_price_lookup") is not None
    # All four scenarios must thread mode in {"normal", "halt"}.
    assert inputs["mode"] in {"normal", "halt"}


# ---------------------------------------------------------------------------
# 6. Fixture export — writes completion_record + submission_log + metadata
# ---------------------------------------------------------------------------


def test_fixture_export_writes_completion_record_plus_submission_log(tmp_path: Path) -> None:
    """The fixture write produces JSON with the three documented keys."""
    log_entry = SubmissionLogEntry(
        envelope=_make_envelope_for_log(),
        submission_results=(
            SubmissionResult(
                command_ordinal=0,
                status="accepted",
                command_id="inv-pm-001.ENV-REC-1.0.0",
                acknowledgment=Acknowledgment(
                    position_id="POS-AAPL-stub",
                    order_id="ORD-AAPL-stub",
                ),
            ),
        ),
    )
    record = _make_completion_record(envelopes_submitted=1, approve=1)
    result = _make_pm_result(record=record, submission_log=(log_entry,))

    save_scenario_fixture(
        scenario="normal",
        result=result,
        verdict=Verdict.PASS,
        fixtures_dir=tmp_path,
    )

    fixture_path = tmp_path / "normal.json"
    assert fixture_path.exists()
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) >= {"completion_record", "submission_log", "scenario_metadata"}
    # completion_record round-trips back to PMCompletionRecord.
    PMCompletionRecord.model_validate(payload["completion_record"])
    # submission_log entries are JSON-shaped.
    assert isinstance(payload["submission_log"], list)
    assert len(payload["submission_log"]) == 1
    assert payload["submission_log"][0]["envelope"]["envelope_id"] == "ENV-REC-1"
    # scenario_metadata names the verdict and tokens.
    assert payload["scenario_metadata"]["name"] == "normal"
    assert payload["scenario_metadata"]["verdict"] == "PASS"
    assert "tokens_used" in payload["scenario_metadata"]
    assert payload["scenario_metadata"]["tokens_used"]["input_tokens"] == 10_000


# ---------------------------------------------------------------------------
# 7. main() — exit code 0 on PASS with stub SDK
# ---------------------------------------------------------------------------


def _stub_sdk_query(payload: dict[str, Any]) -> Any:
    """Build an async-generator stub matching ``claude_agent_sdk.query``.

    The stub yields one ``AssistantMessage`` (no tool calls, no text) and
    one ``ResultMessage`` carrying *payload* as ``structured_output``.
    """
    from collections.abc import AsyncIterator

    from claude_agent_sdk import AssistantMessage, ResultMessage

    usage = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    async def _stub(**_: Any) -> AsyncIterator[Any]:
        yield AssistantMessage(
            content=[],
            model="claude-opus-4-7",
            stop_reason="end_turn",
            usage=usage,
        )
        yield ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            stop_reason="end_turn",
            usage=usage,
            structured_output=payload,
        )

    return _stub


def test_main_returns_exit_code_0_on_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Driving ``main`` with ``--scenario normal`` and a stub SDK exits 0."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stub-token")
    invocation_id = "20260504T120000Z-verify-synthesizer"
    diag_dir = tmp_path / "archive" / "invocations" / invocation_id / "analysis" / "synthesizer"
    diag_dir.mkdir(parents=True)
    (diag_dir / "response.md").write_text("Synthesis prose.\n", encoding="utf-8")
    stage_dir = tmp_path / "archive" / "invocations" / invocation_id / "stage_artifacts"
    stage_dir.mkdir(parents=True)
    (stage_dir / "retrieval_store.json").write_text(
        '{"entries": {}, "freshness_by_source": {}}', encoding="utf-8"
    )

    completion_payload = {
        "invocation_id": "verify-pm-normal",
        "timestamp": _TS,
        "envelopes_submitted": 0,
        "verdict_summary": {"approve": 0, "approve_with_modification": 0, "reject": 0},
    }

    fixtures_dir = tmp_path / "fixtures"
    exit_code = main(
        argv=[
            "--archive-root",
            str(tmp_path / "archive"),
            "--synthesizer-invocation-id",
            invocation_id,
            "--scenario",
            "normal",
            "--save-fixtures",
            "--fixtures-dir",
            str(fixtures_dir),
        ],
        sdk_query_fn=_stub_sdk_query(completion_payload),
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "scenario: normal" in captured
    assert "Verdict: PASS" in captured
    assert (fixtures_dir / "normal.json").exists()
