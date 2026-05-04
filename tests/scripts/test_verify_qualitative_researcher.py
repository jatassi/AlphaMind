"""Unit tests for ``scripts/verify_qualitative_researcher.py`` (ALP-253).

The real-SDK verification script is exercised through its public entry
points :func:`alphamind.scripts.verify_qualitative_researcher.compute_report`
and :func:`alphamind.scripts.verify_qualitative_researcher.format_report`
against mock runner outputs and tmp_path archive directories. The script
does NOT touch the SDK in these tests — the harness's contract is
covered by ``tests/analysis/qualitative_research/test_harness.py``.

Coverage map per ALP-253:

- All-pass run: every assertion holds, exit code 0.
- Missing diagnostic-archive file under ``invocations/<id>/analysis/...``: fails.
- Wall-clock over budget: fails with documented code.
- Output tokens over budget: fails with documented code.
- Tool calls over budget: fails with documented code.
- Brief re-validation runs against the returned brief.
- Budget loaders read from ``config/agents.yaml`` (no hardcoded numbers).
- Non-empty ``QualitativeBrief`` text representation is printed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind.analysis._shared import SignalQuality, TokensUsed
from alphamind.analysis.qualitative_research.input_bundle import InputBundle
from alphamind.analysis.qualitative_research.models import (
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AllowedModel,
)
from alphamind.scripts.verify_qualitative_researcher import (
    BudgetThresholds,
    compute_report,
    format_report,
    load_budget_thresholds,
    run_verification,
)

_AS_OF = datetime(2026, 5, 2, 14, 30, 0, tzinfo=UTC)
_LAST_INVOCATION_TIME = datetime(2026, 5, 2, 13, 30, 0, tzinfo=UTC)
_INVOCATION_ID = "20260502T143000Z-verify-qual"
_AGENT_NAME = AgentName.qualitative_researcher.value


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_qualitative_brief(*, n_threads: int = 1) -> QualitativeBrief:
    threads = tuple(
        NarrativeThread(
            thread_id=f"QR-{i + 1}",
            summary=f"Thread {i + 1} summary",
            relevance="high",
            direction=ThreadDirection.BULLISH,
            subject="NVDA",
            time_horizon=TimeHorizon.NEAR_TERM,
            evidence=(
                EvidenceLine(
                    source_type="news_digest",
                    observation="strong demand",
                    citation="ND-T1",
                ),
                EvidenceLine(
                    source_type="prediction_market",
                    observation="market agrees",
                    citation="PM-001",
                ),
            ),
            implication="continued upside",
        )
        for i in range(n_threads)
    )
    return QualitativeBrief(
        invocation_id=_INVOCATION_ID,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=threads,
        catalyst_watches=(),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="risk-on",
        ),
    )


def _make_news_digest() -> NewsDigest:
    return NewsDigest(
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        total_collected=12,
        total_shown=8,
        entries=(),
        digest_text="=== NEWS DIGEST ===\n... (rendered text)\n",
    )


def _make_input_bundle() -> InputBundle:
    return InputBundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_text="regime text",
        digest_text="digest text",
        sentiment_text="sentiment text",
        prediction_market_text="prediction market text",
        calendar_text="calendar text",
        thesis_text="thesis text",
        bundle_text="bundle text",
    )


def _make_runner_result(
    *,
    input_tokens: int = 5000,
    output_tokens: int = 500,
    wall_clock_seconds: float = 12.0,
    retry_count: int = 0,
    tool_calls_used: int = 3,
    n_threads: int = 1,
) -> QualitativeResearcherResult:
    return QualitativeResearcherResult(
        brief=_make_qualitative_brief(n_threads=n_threads),
        input_bundle=_make_input_bundle(),
        news_digest=_make_news_digest(),
        tokens_used=TokensUsed(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=tool_calls_used,
        wall_clock_seconds=wall_clock_seconds,
        retry_count=retry_count,
    )


def _seed_archive(
    archive_root: Path,
    invocation_id: str,
    *,
    missing_files: tuple[str, ...] = (),
    omit_agent_dir: bool = False,
) -> None:
    """Seed the harness's diagnostic archive layout under ``archive_root``."""
    base = archive_root / "invocations" / invocation_id / "analysis"
    base.mkdir(parents=True, exist_ok=True)
    if omit_agent_dir:
        return
    expected_files = (
        "prompt.md",
        "user_message.md",
        "response_initial.md",
        "errors.json",
        "metadata.json",
    )
    d = base / _AGENT_NAME
    d.mkdir(parents=True, exist_ok=True)
    for fname in expected_files:
        if fname in missing_files:
            continue
        (d / fname).write_text("stub", encoding="utf-8")


def _make_agent_config() -> AdaptiveAgentConfig:
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/qualitative_researcher.md",
        latency_budget_seconds=180,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search", "prediction_markets", "earnings_commentary"],
        cumulative_tool_call_limit=15,
        cumulative_tool_token_budget=4_000,
        tool_caps={
            "news_search": 8,
            "prediction_markets": 4,
            "earnings_commentary": 4,
        },
    )


def _budgets() -> BudgetThresholds:
    return BudgetThresholds(
        wall_clock_seconds=180.0,
        output_token_budget=1_000,
        tool_call_limit=15,
        agent_config=_make_agent_config(),
    )


# ---------------------------------------------------------------------------
# Tracer bullet — happy path passes
# ---------------------------------------------------------------------------


def test_compute_report_passes_when_all_assertions_hold(tmp_path: Path) -> None:
    """A clean runner output + populated archive → ``passed`` is True."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result()
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is True
    assert report.failures == ()


# ---------------------------------------------------------------------------
# Missing diagnostic-archive file
# ---------------------------------------------------------------------------


def test_missing_diagnostic_archive_file_fails(tmp_path: Path) -> None:
    """Missing ``metadata.json`` surfaces ``diagnostic-archive-missing-file``."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID, missing_files=("metadata.json",))

    result = _make_runner_result()
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "diagnostic-archive-missing-file" in codes


def test_missing_diagnostic_archive_directory_fails(tmp_path: Path) -> None:
    """Missing the entire agent directory under ``invocations/<id>/analysis/`` fails."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID, omit_agent_dir=True)

    result = _make_runner_result()
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "diagnostic-archive-missing-file" in codes


# ---------------------------------------------------------------------------
# Wall-clock and token budget assertions
# ---------------------------------------------------------------------------


def test_wall_clock_over_budget_fails(tmp_path: Path) -> None:
    """Wall-clock exceeding the budget surfaces ``wall-clock-budget-exceeded``."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result(wall_clock_seconds=999.0)
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "wall-clock-budget-exceeded" in codes


def test_output_tokens_over_budget_fails(tmp_path: Path) -> None:
    """Output tokens exceeding the per-agent ``output_token_budget`` fail."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result(output_tokens=10_000)
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "output-token-budget-exceeded" in codes


def test_tool_calls_over_limit_fails(tmp_path: Path) -> None:
    """Tool calls over ``cumulative_tool_call_limit`` surface a failure."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result(tool_calls_used=999)
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "tool-call-limit-exceeded" in codes


# ---------------------------------------------------------------------------
# Brief re-validation
# ---------------------------------------------------------------------------


def test_brief_re_validation_runs_against_returned_brief(tmp_path: Path) -> None:
    """The script re-runs the structural validator on the returned brief."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result(n_threads=2)
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )

    assert report.passed is True
    assert report.thread_count == 2


# ---------------------------------------------------------------------------
# Budget loader reads from agents.yaml (not hardcoded)
# ---------------------------------------------------------------------------


def test_budget_loader_reads_from_agents_yaml() -> None:
    """``load_budget_thresholds`` reads from ``config/agents.yaml``.

    The wall-clock budget is the qualitative-researcher's
    ``latency_budget_seconds``; the output-token budget is the
    ``output_token_budget``; the tool-call limit is
    ``cumulative_tool_call_limit``.
    """
    budgets = load_budget_thresholds()

    assert budgets.wall_clock_seconds == float(budgets.agent_config.latency_budget_seconds)
    assert budgets.output_token_budget == budgets.agent_config.output_token_budget
    # qualitative_researcher is a tool-loop agent; cumulative_tool_call_limit must be set.
    assert isinstance(budgets.agent_config, AdaptiveAgentConfig)
    assert budgets.tool_call_limit == budgets.agent_config.cumulative_tool_call_limit


# ---------------------------------------------------------------------------
# format_report renders the documented sections
# ---------------------------------------------------------------------------


def test_format_report_renders_summary(tmp_path: Path) -> None:
    """The formatted summary contains every documented section."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result()
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )
    rendered = format_report(report)

    assert "AlphaMind Qualitative-Researcher End-to-End Verification" in rendered
    assert "[ Invocation summary ]" in rendered
    assert "[ Diagnostic archive ]" in rendered
    assert "[ Budget envelope ]" in rendered
    assert "[ Brief preview ]" in rendered
    assert "RESULT: PASS" in rendered


def test_format_report_failed_run_reports_failures(tmp_path: Path) -> None:
    """A failed run renders ``RESULT: FAIL`` and lists every failure code."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID, missing_files=("metadata.json",))

    result = _make_runner_result()
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )
    rendered = format_report(report)

    assert "RESULT: FAIL" in rendered
    assert "[ Failures ]" in rendered
    assert "diagnostic-archive-missing-file" in rendered


def test_format_report_brief_preview_is_non_empty(tmp_path: Path) -> None:
    """The rendered brief preview is non-empty (per AC: 'prints a non-empty
    QualitativeBrief text representation')."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    result = _make_runner_result()
    report = compute_report(
        result=result,
        invocation_id=_INVOCATION_ID,
        archive_root=archive_root,
        budgets=_budgets(),
    )
    rendered = format_report(report)

    # The preview section follows the prompt's example-output format with
    # the threads' QR-N identifiers, so the parsed brief's content surfaces.
    assert "QR-1" in rendered
    assert "NVDA" in rendered  # The thread's subject ticker.


# ---------------------------------------------------------------------------
# run_verification — wires up the runner and returns an exit code
# ---------------------------------------------------------------------------


def test_run_verification_returns_zero_on_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_verification`` returns 0 when every assertion passes."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    runner_result = _make_runner_result()

    async def _stub_runner(**_kwargs: Any) -> QualitativeResearcherResult:
        return runner_result

    exit_code = run_verification(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        archive_root=archive_root,
        budgets=_budgets(),
        runner_fn=_stub_runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "RESULT: PASS" in captured.out
    # The script prints a non-empty brief representation (AC).
    assert "QR-1" in captured.out


def test_run_verification_returns_one_on_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_verification`` returns 1 when any assertion fails."""
    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID, missing_files=("metadata.json",))

    runner_result = _make_runner_result()

    async def _stub_runner(**_kwargs: Any) -> QualitativeResearcherResult:
        return runner_result

    exit_code = run_verification(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        archive_root=archive_root,
        budgets=_budgets(),
        runner_fn=_stub_runner,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "RESULT: FAIL" in captured.out


def test_run_verification_writes_qualitative_brief_on_pass(tmp_path: Path) -> None:
    """A passing run dumps the qualitative brief to the stage-artifacts dir."""
    from alphamind.scripts._artifact_io import (
        QUALITATIVE_BRIEF_FILENAME,
        load_qualitative_brief,
        stage_artifacts_dir,
    )

    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID)

    runner_result = _make_runner_result()

    async def _stub_runner(**_kwargs: Any) -> QualitativeResearcherResult:
        return runner_result

    exit_code = run_verification(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        archive_root=archive_root,
        budgets=_budgets(),
        runner_fn=_stub_runner,
    )

    assert exit_code == 0
    stage_dir = stage_artifacts_dir(archive_root, _INVOCATION_ID)
    assert (stage_dir / QUALITATIVE_BRIEF_FILENAME).exists()
    assert load_qualitative_brief(stage_dir) == runner_result.brief


def test_run_verification_skips_qualitative_brief_on_fail(tmp_path: Path) -> None:
    """A failed run does not dump artifacts."""
    from alphamind.scripts._artifact_io import (
        QUALITATIVE_BRIEF_FILENAME,
        stage_artifacts_dir,
    )

    archive_root = tmp_path / "archive"
    _seed_archive(archive_root, _INVOCATION_ID, missing_files=("metadata.json",))

    runner_result = _make_runner_result()

    async def _stub_runner(**_kwargs: Any) -> QualitativeResearcherResult:
        return runner_result

    exit_code = run_verification(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        archive_root=archive_root,
        budgets=_budgets(),
        runner_fn=_stub_runner,
    )

    assert exit_code == 1
    stage_dir = stage_artifacts_dir(archive_root, _INVOCATION_ID)
    assert not (stage_dir / QUALITATIVE_BRIEF_FILENAME).exists()
