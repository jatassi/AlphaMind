"""Unit tests for ``scripts/verify_analyst.py`` (ALP-300).

The real-SDK live verification script's testable logic — the verdict
rubric per scenario, the synthesizer-archive reader, and the CLI
auth-check failure path — is exercised here without touching the
Anthropic API. The live SDK invocation is verified manually by the
operator running the script after this PR lands.

Coverage map:

- ``test_verdict_pass_normal_mode_with_recommendation`` — schema-valid,
  Layer-2/3 clean, ≥1 tool call, ≥1 recommendation → PASS.
- ``test_verdict_pass_normal_mode_explicit_empty`` — schema-valid,
  Layer-2/3 clean, zero tool calls is allowed when recommendations are
  an explicit empty array → PASS.
- ``test_verdict_warn_normal_mode_validator_errors`` — Layer-2 invariant
  failure (e.g. unknown_reference) → WARN.
- ``test_verdict_warn_normal_mode_zero_tools_with_recs`` — non-empty
  recommendations without any validate_guardrail tool call → WARN.
- ``test_verdict_pass_halt_mode_watchlist`` — watchlist entries with
  conviction ≥1 and non-empty thesis → PASS.
- ``test_verdict_warn_halt_mode_tool_calls_present`` — halt mode is
  expected to skip validate_guardrail, so any tool call → WARN.
- ``test_verdict_warn_halt_mode_validator_errors`` — Layer-2/3
  validation failure in halt mode → WARN.
- ``test_verdict_fail_when_harness_failure`` — any HarnessFailure → FAIL.
- ``test_read_synthesizer_text_from_archive`` — the helper that loads
  a recorded synthesizer's ``response.md`` produces the expected text.
- ``test_missing_auth_renders_failure_report`` — CLI surfaces missing
  ``CLAUDE_CODE_OAUTH_TOKEN`` as a clean failure-report block on stdout
  with exit code 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from alphamind.analysis._shared import TokensUsed
from alphamind.config.models.agents import AgentName
from alphamind.decision.analyst.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.analyst.runner import AnalystResult
from alphamind.decision.analyst.validation import (
    ValidationError,
    ValidationResult,
)
from alphamind.scripts.verify_analyst import (
    Verdict,
    main,
    read_synthesizer_text,
    verdict_for_failure,
    verdict_for_success,
)

_INVOCATION_ID = "20260504T120000Z-verify-analyst"
_AGENT_NAME = AgentName.analyst.value


# ---------------------------------------------------------------------------
# Fixture helpers — minimal AnalystOutput payloads for the verdict tests
# ---------------------------------------------------------------------------


_TS = "2026-05-04T14:30:00+00:00"


def _normal_payload(*, recommendations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A minimal normal-mode AnalystOutput payload."""
    return {
        "invocation_id": "inv-test-001",
        "timestamp": _TS,
        "mode": "normal",
        "recommendations": recommendations if recommendations is not None else [],
    }


def _normal_recommendation() -> dict[str, Any]:
    """A minimal valid recommendation payload (mirrors prompts/decision/analyst.md example)."""
    return {
        "recommendation_id": "REC-1",
        "instrument": {"asset_type": "equity", "ticker": "NVDA", "direction": "long"},
        "underlying": "NVDA",
        "sector": "tech",
        "conviction_level": 3,
        "entry_order": {"type": "market"},
        "position_size": {
            "quantity": 4,
            "dollar_value": 3370.00,
            "pct_of_portfolio": 3.37,
            "delta_adjusted_exposure": 3370.00,
        },
        "target": {
            "target_type": "absolute_price",
            "price": 890.00,
            "dollar_pl_target": 190.00,
        },
        "invalidation_legs": [
            {
                "leg_id": "INV-1",
                "type": "price",
                "is_hard": True,
                "condition": {
                    "underlying_trigger": "NVDA",
                    "comparator": "<=",
                    "trigger_price": 820.00,
                },
                "order_parameters": {"order_type": "market"},
            }
        ],
        "time_expectation_hours": 18,
        "guardrail_validation_result": {
            "overall": "PASS",
            "per_rule": [],
            "checked_at": _TS,
        },
        "thesis_narrative": "AI capex acceleration drives a near-term setup [SA-TECH-1].",
        "target_rationale": "Earnings catalyst.",
        "invalidation_rationale": [
            {"leg_id": "INV-1", "rationale": "Below the swing low invalidates."}
        ],
        "position_size_rationale": "Size 3.37% within the moderate-conviction band.",
        "counterarguments_acknowledged": "Macro concerns could spill over.",
    }


def _watchlist_payload(*, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A minimal watchlist-mode AnalystOutput payload."""
    return {
        "invocation_id": "inv-test-001",
        "timestamp": _TS,
        "mode": "watchlist",
        "watchlist": entries
        if entries is not None
        else [
            {
                "ticker": "NVDA",
                "sector": "tech",
                "thesis_summary": "Constructive sentiment turn.",
                "estimated_conviction": 3,
            }
        ],
    }


def _make_analyst_result(
    output: AnalystOutput,
    *,
    tool_calls_used: int = 1,
    retry_count: int = 0,
) -> AnalystResult:
    return AnalystResult(
        output=output,
        retry_count=retry_count,
        tokens_used=TokensUsed(
            input_tokens=10_000, output_tokens=2_000, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=tool_calls_used,
        wall_clock_seconds=12.3,
        stop_reason="end_turn",
    )


def _empty_validation_result() -> ValidationResult:
    return ValidationResult(is_valid=True, errors=(), warnings=())


def _validation_with_errors() -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        errors=(
            ValidationError(
                field_path="recommendations[0].thesis_narrative",
                rule="unknown_reference",
                message="reference 'SA-FIN-99' not in retrieval store",
            ),
        ),
        warnings=(),
    )


# ---------------------------------------------------------------------------
# Verdict rubric — normal mode
# ---------------------------------------------------------------------------


def test_verdict_pass_normal_mode_with_recommendation() -> None:
    """Schema-valid + Layer-2/3 clean + ≥1 tool call + ≥1 recommendation → PASS."""
    payload = _normal_payload(recommendations=[_normal_recommendation()])
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=2)

    verdict = verdict_for_success(
        result=result,
        validation=_empty_validation_result(),
        mode="normal",
    )

    assert verdict is Verdict.PASS


def test_verdict_pass_normal_mode_explicit_empty() -> None:
    """Schema-valid + Layer-2/3 clean + zero recommendations + zero tools → PASS.

    The rubric explicitly allows an empty recommendations array as a
    valid outcome (the analyst's reasoning lives in the archive's
    ``response_initial.md``); zero tool calls is fine when no
    recommendations were drafted.
    """
    payload = _normal_payload(recommendations=[])
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=0)

    verdict = verdict_for_success(
        result=result,
        validation=_empty_validation_result(),
        mode="normal",
    )

    assert verdict is Verdict.PASS


def test_verdict_warn_normal_mode_validator_errors() -> None:
    """Layer-2/3 validation failure → WARN (prompt-tightening signal, not a hard fail)."""
    payload = _normal_payload(recommendations=[_normal_recommendation()])
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=2)

    verdict = verdict_for_success(
        result=result,
        validation=_validation_with_errors(),
        mode="normal",
    )

    assert verdict is Verdict.WARN


def test_verdict_warn_normal_mode_zero_tools_with_recs() -> None:
    """Non-empty recommendations without any validate_guardrail call → WARN.

    The analyst is expected to call validate_guardrail at least once
    when emitting any recommendation; zero calls means the
    pre-submission validation step was skipped.
    """
    payload = _normal_payload(recommendations=[_normal_recommendation()])
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=0)

    verdict = verdict_for_success(
        result=result,
        validation=_empty_validation_result(),
        mode="normal",
    )

    assert verdict is Verdict.WARN


# ---------------------------------------------------------------------------
# Verdict rubric — halt (watchlist) mode
# ---------------------------------------------------------------------------


def test_verdict_pass_halt_mode_watchlist() -> None:
    """Watchlist entries with conviction ≥1 + non-empty thesis + zero tool calls → PASS.

    Halt mode skips validate_guardrail; tool_calls_used must be zero.
    """
    payload = _watchlist_payload()
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=0)

    verdict = verdict_for_success(
        result=result,
        validation=_empty_validation_result(),
        mode="watchlist",
    )

    assert verdict is Verdict.PASS


def test_verdict_warn_halt_mode_tool_calls_present() -> None:
    """Halt mode with any tool call → WARN.

    The halt-mode header instructs the analyst not to call
    validate_guardrail. Any recorded tool call is a contract violation
    that surfaces as WARN.
    """
    payload = _watchlist_payload()
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=1)

    verdict = verdict_for_success(
        result=result,
        validation=_empty_validation_result(),
        mode="watchlist",
    )

    assert verdict is Verdict.WARN


def test_verdict_warn_halt_mode_validator_errors() -> None:
    """Layer-2/3 validation failure in halt mode → WARN."""
    payload = _watchlist_payload()
    output = AnalystOutput.model_validate(payload)
    result = _make_analyst_result(output, tool_calls_used=0)

    verdict = verdict_for_success(
        result=result,
        validation=_validation_with_errors(),
        mode="watchlist",
    )

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
# Fixture saving
# ---------------------------------------------------------------------------


def test_save_fixtures_writes_two_json_files(tmp_path: Path) -> None:
    """``--save-fixtures`` writes ``normal.json`` and ``halt.json``."""
    from alphamind.scripts.verify_analyst import save_fixtures

    normal = AnalystOutput.model_validate(
        _normal_payload(recommendations=[_normal_recommendation()])
    )
    halt = AnalystOutput.model_validate(_watchlist_payload())

    fixtures_dir = tmp_path / "fixtures"
    save_fixtures(
        normal_output=normal,
        halt_output=halt,
        fixtures_dir=fixtures_dir,
    )

    normal_payload = json.loads((fixtures_dir / "normal.json").read_text(encoding="utf-8"))
    halt_payload = json.loads((fixtures_dir / "halt.json").read_text(encoding="utf-8"))
    # Round-trip back to AnalystOutput so the payload's structure is verified.
    AnalystOutput.model_validate(normal_payload)
    AnalystOutput.model_validate(halt_payload)
    assert normal_payload["mode"] == "normal"
    assert halt_payload["mode"] == "watchlist"


# ---------------------------------------------------------------------------
# Missing auth — CLI entry point
# ---------------------------------------------------------------------------


def test_missing_auth_renders_failure_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Missing CLAUDE_CODE_OAUTH_TOKEN renders a clean failure block, exit code 1.

    The script must fail fast without producing a stack trace from
    inside the SDK. Because the CLI requires ``--archive-root`` (a
    prior synthesizer archive must be present) to even get to the
    auth check, we set up a minimal archive structure with the
    response.md present.
    """
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
    assert "Analyst live-SDK verification" in captured
    assert "Harness failure" in captured
    assert "type: SDKFailure" in captured
    assert f"agent_name: {_AGENT_NAME}" in captured
    assert "CLAUDE_CODE_OAUTH_TOKEN" in captured
    assert "Verdict: FAIL" in captured
