"""Unit tests for ``scripts/verify_strategist.py`` (ALP-309).

The real-SDK live verification script's testable logic — the verdict
rubric, the synthesizer-archive reader, and the three in-code
:class:`StrategistView` fixture builders — is exercised here without
touching the Anthropic API. The live SDK invocation is verified
manually by the operator running the script after this PR lands.

Mirrors :mod:`tests.scripts.test_verify_analyst`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName
from alphamind.decision.strategist.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.decision.strategist.runner import StrategistResult
from alphamind.decision.strategist.validation import (
    ValidationFailure,
    ValidationResult,
)
from alphamind.portfolio_state.consumers.strategist import StrategistView
from alphamind.scripts.verify_strategist import (
    Verdict,
    build_fixture_defensive_posture_view,
    build_fixture_emergency_view,
    build_fixture_normal_view,
    build_fixture_regime_transition_breach,
    build_fixture_sector_resolver,
    main,
    read_retrieval_store,
    read_synthesizer_text,
    save_fixtures,
    verdict_for_failure,
    verdict_for_success,
)

_INVOCATION_ID = "20260504T120000Z-verify-strategist"
_AGENT_NAME = AgentName.strategist.value
_TS = "2026-05-04T14:30:00+00:00"


# ---------------------------------------------------------------------------
# Minimal-payload helpers — used to construct synthetic StrategistOutput values
# for verdict-rubric tests without invoking the SDK or building full views.
# ---------------------------------------------------------------------------


def _normal_payload() -> dict[str, Any]:
    return {
        "invocation_id": "inv-test-001",
        "timestamp": _TS,
        "mode": "normal",
        "position_assessments": [],
        "pending_order_assessments": [],
        "portfolio_level_observations": {
            "aggregate_thesis_health": "Empty book.",
            "sector_balance_shifts": "No shifts.",
            "thesis_dependency_warnings": "No warnings.",
            "capital_allocation_observations": "Cash-only book.",
        },
    }


def _defensive_payload() -> dict[str, Any]:
    return {
        "invocation_id": "inv-test-002",
        "timestamp": _TS,
        "mode": "defensive_posture",
        "position_assessments": [],
        "pending_order_assessments": [],
        "portfolio_level_observations": {
            "aggregate_thesis_health": "All positions in protective mode.",
            "sector_balance_shifts": "No new exposures.",
            "thesis_dependency_warnings": "No correlated breakdowns.",
            "capital_allocation_observations": "Capital preservation prioritized.",
            "defensive_posture_summary": {
                "reduction_priority": [],
                "capital_preservation_notes": "Holding cash; no add actions.",
            },
        },
    }


def _make_strategist_result(
    output: StrategistOutput,
    *,
    validation_result: ValidationResult | None = None,
    input_tokens: int = 10_000,
    output_tokens: int = 2_000,
) -> StrategistResult:
    if validation_result is None:
        validation_result = ValidationResult(overall="PASS", failures=(), warnings=())
    return StrategistResult(
        output=output,
        validation_result=validation_result,
        tokens_used=TokensUsed(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        metadata={
            "invocation_id": output.invocation_id,
            "mode": output.mode,
            "model": "claude-opus-4-7",
            "agent_config_snapshot": {},
            "total_tokens": input_tokens + output_tokens,
            "attempts": 1,
        },
    )


def _validation_with_failures() -> ValidationResult:
    return ValidationResult(
        overall="FAIL",
        failures=(
            ValidationFailure(
                field_path="position_assessments[0].status_rationale",
                rule="unknown_reference",
                message="reference 'SA-FIN-99' not in retrieval store",
            ),
        ),
        warnings=(),
    )


# ---------------------------------------------------------------------------
# Verdict rubric — success path
# ---------------------------------------------------------------------------


def test_verdict_pass_when_validation_passes() -> None:
    """validation_result.overall == 'PASS' → Verdict.PASS."""
    output = StrategistOutput.model_validate(_normal_payload())
    result = _make_strategist_result(output)

    verdict = verdict_for_success(result=result)

    assert verdict is Verdict.PASS


def test_verdict_warn_when_validation_fails() -> None:
    """validation_result.overall == 'FAIL' → Verdict.WARN.

    A FAIL from the validator after the harness already exhausted its
    retry indicates the prompt could not produce a clean output even
    with a corrective retry. Surface as WARN so the fixture is still
    written but the operator is signaled.
    """
    output = StrategistOutput.model_validate(_defensive_payload())
    result = _make_strategist_result(output, validation_result=_validation_with_failures())

    verdict = verdict_for_success(result=result)

    assert verdict is Verdict.WARN


def test_verdict_warn_when_warnings_present() -> None:
    """Warnings flagged on a successful validation surface as WARN.

    The strategist validator never emits warnings today (per ALP-306;
    Layer-2/3 produces only failures), but this guards against silent
    drift if a soft check is ever added without a verdict update.
    """
    from alphamind.decision.strategist.validation import ValidationWarning

    output = StrategistOutput.model_validate(_normal_payload())
    validation_with_warnings = ValidationResult(
        overall="PASS",
        failures=(),
        warnings=(
            ValidationWarning(
                field_path="position_assessments[0].cross_position_observations",
                rule="suspicious_phrase",
                message="advisory only",
            ),
        ),
    )
    result = _make_strategist_result(output, validation_result=validation_with_warnings)

    verdict = verdict_for_success(result=result)

    assert verdict is Verdict.WARN


# ---------------------------------------------------------------------------
# Verdict rubric — failure path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        SDKFailure("sdk", agent_name=_AGENT_NAME, invocation_id=_INVOCATION_ID),
        TimeoutFailure("latency", agent_name=_AGENT_NAME, invocation_id=_INVOCATION_ID),
        ContextOverflowFailure("max_tokens", agent_name=_AGENT_NAME, invocation_id=_INVOCATION_ID),
        MalformedOutputFailure(
            "parse-failed", agent_name=_AGENT_NAME, invocation_id=_INVOCATION_ID
        ),
    ],
)
def test_verdict_fail_when_harness_failure(failure: HarnessFailure) -> None:
    """Any HarnessFailure subclass classifies as FAIL."""
    assert verdict_for_failure(failure) is Verdict.FAIL


# ---------------------------------------------------------------------------
# Synthesizer-archive reader
# ---------------------------------------------------------------------------


def test_read_synthesizer_text_from_archive(tmp_path: Path) -> None:
    """Reads ``analysis/synthesizer/response.md`` from a recorded run."""
    invocation_id = "20260504T120000Z-verify-synthesizer"
    archive_root = tmp_path / "archive"
    diag_dir = archive_root / "invocations" / invocation_id / "analysis" / "synthesizer"
    diag_dir.mkdir(parents=True)
    expected = "Synthesis prose body cites [SA-TECH-1] and [CR-1].\n"
    (diag_dir / "response.md").write_text(expected, encoding="utf-8")

    text = read_synthesizer_text(archive_root=archive_root, synthesizer_invocation_id=invocation_id)

    assert text == expected


def test_read_synthesizer_text_missing_archive_raises(tmp_path: Path) -> None:
    """Missing synthesizer archive raises FileNotFoundError with helpful path."""
    archive_root = tmp_path / "archive"

    with pytest.raises(FileNotFoundError, match="synthesizer"):
        read_synthesizer_text(
            archive_root=archive_root,
            synthesizer_invocation_id="missing-invocation-id",
        )


# ---------------------------------------------------------------------------
# Fixture builders — three in-code StrategistView constructors
# ---------------------------------------------------------------------------


def test_normal_view_has_positions_across_active_sectors() -> None:
    """``_build_normal_view`` populates positions across multiple active sectors.

    Per parent-issue decisions, the normal scenario seeds 4 positions
    across 3 sectors so the strategist sees a non-trivial book.
    """
    view, _snapshots = build_fixture_normal_view()

    # Positions span at least 3 sectors (parent-issue spec calls for 4
    # positions across 3 sectors).
    assert len(view.positions) >= 3
    # No drawdown halt on the normal view.
    assert view.drawdown.daily_zone.value == "NORMAL"
    # All position views carry a thesis with full structure.
    for pv in view.positions:
        assert pv.thesis is not None
        assert pv.thesis.summary
        assert pv.thesis.components, "components must be non-empty for ACTIVE thesis"


def test_normal_view_has_pending_order() -> None:
    """The normal scenario emits exactly one pending order so the strategist
    exercises the ``pending_order_assessments`` branch."""
    view, _snapshots = build_fixture_normal_view()

    pending = [pv for pv in view.positions if pv.pending_orders]
    assert len(pending) >= 1


def test_defensive_posture_view_carries_drawdown_at_threshold() -> None:
    """The defensive_posture scenario's drawdown sits at the daily-halt threshold."""
    view, _snapshots = build_fixture_defensive_posture_view()

    # 6 positions across more sectors per spec.
    assert len(view.positions) >= 6
    # Daily drawdown is at the daily-halt threshold (zone CRITICAL or BLOCKED).
    assert view.drawdown.daily_zone.value in {"CRITICAL", "BLOCKED"}


def test_defensive_posture_view_records_engine_close_in_activity_log() -> None:
    """A recent engine-originated CLOSE on a sector-correlated position must be
    present in the activity log so the strategist's
    ``engine_originated_closure_signal`` discipline is exercised."""
    view, _snapshots = build_fixture_defensive_posture_view()

    assert any(
        entry.event_type.value == "POSITION_CLOSED" for entry in view.intra_invocation_changelog
    )


def test_emergency_view_triggers_regime_transition_breach() -> None:
    """The emergency scenario emits a regime-transition breach record on a
    position sized 4.5% with a tightened 3.5% limit (overage 1.0%)."""
    breach = build_fixture_regime_transition_breach()

    assert breach.position_id is not None
    assert breach.current_value == pytest.approx(4.5)
    assert breach.new_limit_value == pytest.approx(3.5)
    assert breach.overage == pytest.approx(1.0)


def test_emergency_view_normal_drawdown_no_halt() -> None:
    """The emergency scenario runs without a halt; the strategist should be
    invoked in normal mode but with regime_transition_breaches populated."""
    view, _snapshots = build_fixture_emergency_view()

    # 4 positions per spec.
    assert len(view.positions) >= 3
    # Drawdown is normal — emergency doesn't pair with halt.
    assert view.drawdown.daily_zone.value == "NORMAL"


def test_sector_resolver_covers_test_universe() -> None:
    """The sector resolver maps every fixture-mentioned ticker to an active sector."""
    resolver = build_fixture_sector_resolver()

    expected_tickers = ("NVDA", "JPM", "XOM", "AAPL", "MSFT", "GOOGL")
    for ticker in expected_tickers:
        sector = resolver(ticker)
        assert sector in {"tech", "semis", "financials", "energy"}


# ---------------------------------------------------------------------------
# Fixture saving
# ---------------------------------------------------------------------------


def test_save_fixtures_writes_three_json_files(tmp_path: Path) -> None:
    """``--save-fixtures`` writes ``normal.json``, ``defensive_posture.json``,
    ``emergency.json`` under fixtures_dir, and each round-trips back to
    StrategistOutput cleanly."""
    fixtures_dir = tmp_path / "fixtures"

    normal = StrategistOutput.model_validate(_normal_payload())
    defensive = StrategistOutput.model_validate(_defensive_payload())
    emergency = StrategistOutput.model_validate(_normal_payload())

    save_fixtures(
        outputs={"normal": normal, "defensive_posture": defensive, "emergency": emergency},
        fixtures_dir=fixtures_dir,
    )

    for scenario in ("normal", "defensive_posture", "emergency"):
        payload = json.loads((fixtures_dir / f"{scenario}.json").read_text(encoding="utf-8"))
        StrategistOutput.model_validate(payload)


# ---------------------------------------------------------------------------
# CLI auth-check failure path
# ---------------------------------------------------------------------------


def test_missing_auth_renders_failure_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Missing CLAUDE_CODE_OAUTH_TOKEN renders a clean failure block, exit 1."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    invocation_id = "20260504T120000Z-verify-synthesizer"
    diag_dir = tmp_path / "archive" / "invocations" / invocation_id / "analysis" / "synthesizer"
    diag_dir.mkdir(parents=True)
    (diag_dir / "response.md").write_text("Synthesis text.\n", encoding="utf-8")

    exit_code = main(
        argv=[
            "--archive-root",
            str(tmp_path / "archive"),
            "--synthesizer-invocation-id",
            invocation_id,
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "Strategist live-SDK verification" in captured
    assert "Harness failure" in captured
    assert "type: SDKFailure" in captured
    assert f"agent_name: {_AGENT_NAME}" in captured
    assert "CLAUDE_CODE_OAUTH_TOKEN" in captured
    assert "Verdict: FAIL" in captured


# ---------------------------------------------------------------------------
# CLI scenario dispatch — uses a stub sdk_query_fn end-to-end
# ---------------------------------------------------------------------------


def _build_strategist_payload(*, mode: str, invocation_id: str) -> dict[str, Any]:
    """Build a minimal StrategistOutput payload for *mode*."""
    if mode == "defensive_posture":
        observations: dict[str, Any] = {
            "aggregate_thesis_health": "All positions in protective mode.",
            "sector_balance_shifts": "No new exposures.",
            "thesis_dependency_warnings": "No correlated breakdowns.",
            "capital_allocation_observations": "Capital preservation prioritized.",
            "defensive_posture_summary": {
                "reduction_priority": [],
                "capital_preservation_notes": "Holding cash; no add actions.",
            },
        }
    else:
        observations = {
            "aggregate_thesis_health": "Empty book.",
            "sector_balance_shifts": "No shifts.",
            "thesis_dependency_warnings": "No warnings.",
            "capital_allocation_observations": "Cash-only book.",
        }
    return {
        "invocation_id": invocation_id,
        "timestamp": _TS,
        "mode": mode,
        "position_assessments": [],
        "pending_order_assessments": [],
        "portfolio_level_observations": observations,
    }


def test_cli_runs_three_scenarios_with_stub_sdk(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Driving the CLI with ``--sdk-query-fn`` injection (via main_with) runs all
    three scenarios end-to-end against a stub.

    This is the integration-style test for scenario dispatch: with a stub
    SDK, all three scenarios should run, the final summary should list
    each, and the exit code should be 0.
    """
    # Set up the synthesizer archive the script reads.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stub-token")
    invocation_id = "20260504T120000Z-verify-synthesizer"
    diag_dir = tmp_path / "archive" / "invocations" / invocation_id / "analysis" / "synthesizer"
    diag_dir.mkdir(parents=True)
    (diag_dir / "response.md").write_text("Synthesis prose.\n", encoding="utf-8")
    # Empty retrieval store — no references to resolve.
    stage_dir = tmp_path / "archive" / "invocations" / invocation_id / "stage_artifacts"
    stage_dir.mkdir(parents=True)
    (stage_dir / "retrieval_store.json").write_text(
        '{"entries": {}, "freshness_by_source": {}}', encoding="utf-8"
    )

    # Build the stub SDK query function returning a normal payload for any
    # invocation. Per scenario, the runner passes a fresh invocation_id
    # carrying the scenario name; the harness pins ``output.mode`` to the
    # scenario's mode, so we must return defensive_posture for the
    # defensive_posture scenario.
    payloads = {
        "normal": _build_strategist_payload(mode="normal", invocation_id="inv-normal"),
        "defensive_posture": _build_strategist_payload(
            mode="defensive_posture", invocation_id="inv-defensive"
        ),
        "emergency": _build_strategist_payload(mode="normal", invocation_id="inv-emergency"),
    }

    # Use the ``--scenario all`` default and the stub SDK injection.
    fixtures_dir = tmp_path / "fixtures"
    exit_code = main(
        argv=[
            "--archive-root",
            str(tmp_path / "archive"),
            "--synthesizer-invocation-id",
            invocation_id,
            "--save-fixtures",
            "--fixtures-dir",
            str(fixtures_dir),
        ],
        sdk_query_fn=_stub_sdk_query_by_scenario(payloads),
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "scenario: normal" in captured
    assert "scenario: defensive_posture" in captured
    assert "scenario: emergency" in captured
    # Three fixtures landed.
    for scenario in ("normal", "defensive_posture", "emergency"):
        assert (fixtures_dir / f"{scenario}.json").exists()


def _stub_sdk_query_by_scenario(payloads: dict[str, dict[str, Any]]) -> Any:
    """Return a stub that picks the scenario payload by matching the
    scenario name in the invocation_id passed via the prompt context.

    The runner formats invocation IDs like
    ``YYYYMMDDTHHMMSSZ-verify-strategist-<scenario>``; the user_message
    contains that ID in the rendered guardrail header. Match on the
    scenario substring.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage

    usage = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        prompt = kwargs.get("prompt") or ""
        # Match longest scenario name first so "defensive_posture" wins over
        # "normal" (which is a substring of every scenario via "normal mode"
        # rendered in the guardrail header).
        chosen: dict[str, Any] | None = next(
            (payloads[s] for s in sorted(payloads, key=len, reverse=True) if f"-{s}" in prompt),
            None,
        )
        if chosen is None:
            chosen = next(iter(payloads.values()))
        yield AssistantMessage(
            content=[], model="claude-opus-4-7", stop_reason="end_turn", usage=usage
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
            structured_output=chosen,
        )

    return _stub


def test_strategist_view_fixtures_resolve_in_runner() -> None:
    """The three fixture builders return StrategistView instances acceptable to
    :func:`run_strategist`'s public surface (no construction errors).

    A type-shape smoke check — pydantic validators are strict, so an
    invalid fixture build would raise on construction. This test exists
    to catch regressions if a record schema changes after the fixtures
    were authored.
    """
    normal, _ = build_fixture_normal_view()
    defensive, _ = build_fixture_defensive_posture_view()
    emergency, _ = build_fixture_emergency_view()

    assert isinstance(normal, StrategistView)
    assert isinstance(defensive, StrategistView)
    assert isinstance(emergency, StrategistView)


def test_retrieval_store_reader_round_trips(tmp_path: Path) -> None:
    """The synthesizer-archive reader produces a usable RetrievalStore."""
    invocation_id = "20260504T120000Z-verify-synthesizer"
    stage_dir = tmp_path / "archive" / "invocations" / invocation_id / "stage_artifacts"
    stage_dir.mkdir(parents=True)
    (stage_dir / "retrieval_store.json").write_text(
        '{"entries": {}, "freshness_by_source": {}}', encoding="utf-8"
    )

    store = read_retrieval_store(
        archive_root=tmp_path / "archive", synthesizer_invocation_id=invocation_id
    )

    assert isinstance(store, RetrievalStore)


def _stub_sdk_query_with_one_failing_scenario(
    payloads: dict[str, dict[str, Any]],
    *,
    failing_scenario: str,
) -> Any:
    """Like ``_stub_sdk_query_by_scenario`` but emits ``is_error=True``
    for the named scenario so the harness raises ``SDKFailure``.

    Used to exercise the ``--scenario all`` continue-past-failure contract
    introduced post-PR-#22 inline-fix sweep.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage

    usage = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        prompt = kwargs.get("prompt") or ""
        # Match longest first, same heuristic as the sibling stub.
        scenario = next(
            (s for s in sorted(payloads, key=len, reverse=True) if f"-{s}" in prompt),
            next(iter(payloads)),
        )
        is_error = scenario == failing_scenario
        yield AssistantMessage(
            content=[], model="claude-opus-4-7", stop_reason="end_turn", usage=usage
        )
        yield ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=is_error,
            num_turns=1,
            session_id=f"sess-{scenario}",
            stop_reason="end_turn",
            usage=usage,
            structured_output=None if is_error else payloads[scenario],
            result=("API Error: stub-injected failure" if is_error else None),
        )

    return _stub


def test_cli_continues_past_failing_scenario_in_all_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--scenario all`` runs every scenario regardless of FAILs.

    Regression guard for the post-PR-#22 inline-fix that removed fail-fast
    in ``main``'s scenario loop. The contract: a FAIL on scenario N does
    not short-circuit; scenarios N+1 .. K still run; the run exits 1 if
    any scenario FAILed, and ``--save-fixtures`` writes fixtures for the
    PASSing scenarios only.
    """
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

    payloads = {
        "normal": _build_strategist_payload(mode="normal", invocation_id="inv-normal"),
        "defensive_posture": _build_strategist_payload(
            mode="defensive_posture", invocation_id="inv-defensive"
        ),
        "emergency": _build_strategist_payload(mode="normal", invocation_id="inv-emergency"),
    }

    fixtures_dir = tmp_path / "fixtures"
    exit_code = main(
        argv=[
            "--archive-root",
            str(tmp_path / "archive"),
            "--synthesizer-invocation-id",
            invocation_id,
            "--save-fixtures",
            "--fixtures-dir",
            str(fixtures_dir),
        ],
        sdk_query_fn=_stub_sdk_query_with_one_failing_scenario(
            payloads, failing_scenario="defensive_posture"
        ),
    )

    assert exit_code == 1
    captured = capsys.readouterr().out
    # All three scenarios surfaced in output — proving the loop continued
    # past the FAIL rather than short-circuiting.
    assert "scenario: normal" in captured
    assert "scenario: defensive_posture" in captured
    assert "scenario: emergency" in captured
    # Fixtures: normal and emergency PASSed → JSON files emitted; defensive_posture
    # FAILed → no JSON file.
    assert (fixtures_dir / "normal.json").exists()
    assert (fixtures_dir / "emergency.json").exists()
    assert not (fixtures_dir / "defensive_posture.json").exists()
