"""Adaptive-researcher end-to-end verification (ALP-265).

Operator entry point: load the recorded upstream-brief fixtures (per
parent-issue resolution H, the upstream pipeline is faked), connect to
the SQLite database, load ``agents_config`` from ``config/agents.yaml``
and the universe roster from ``config/assets.yaml``, then call
:func:`alphamind.analysis.adaptive_research.runner.run_adaptive_researcher`
with a real ``CLAUDE_CODE_OAUTH_TOKEN`` and assert the structural
properties documented in story ALP-265 § Acceptance criteria:

- The runner returns an :class:`AdaptiveResearcherResult` with no
  exceptions.
- The brief structurally re-validates against
  :mod:`alphamind.analysis.adaptive_research.validation` (Layer-2 +
  Layer-3 reference resolution against the fixture upstream IDs).
- The diagnostic archive directory under
  ``<archive_root>/invocations/<invocation_id>/analysis/adaptive_researcher/``
  holds ``prompt.md``, ``user_message.md``, ``response_initial.md``,
  ``errors.json``, ``metadata.json`` (plus ``response_retry.md`` if a
  retry happened).
- The rendered input bundle contains all four section markers
  (``=== ADAPTIVE RESEARCH INPUT``, ``=== VOLATILITY REGIME ===``,
  ``=== DISTILLATION ANOMALY FLAGS``, ``=== SECTOR-RESEARCHER ANOMALIES``).
- Every ``tools_used`` entry in every thread is in the agent's
  registered tool allowlist (``adaptive_researcher.tools`` from
  ``config/agents.yaml``).
- Wall-clock seconds stay within ``latency_budget_seconds``.
- ``output_tokens`` stays within ``output_token_budget``.
- ``tool_calls_used`` stays within ``cumulative_tool_call_limit``.

Returns 0 when every structural assertion passes; 1 otherwise. The
script also prints a non-empty :class:`AdaptiveBrief` text representation
and a summary table (wall-clock, tokens, tool-calls, retry count, thread
count, anomalies-triaged count) so the operator can spot drift before
re-running.

Mirrors :mod:`alphamind.scripts.verify_qualitative_researcher` so the
operator workflow is identical between sibling agents.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from alphamind.analysis.adaptive_research.models import AdaptiveBrief, Assessment
from alphamind.analysis.adaptive_research.runner import (
    AdaptiveResearcherResult,
    run_adaptive_researcher,
)
from alphamind.analysis.adaptive_research.validation import validate_adaptive_brief
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs, _default_archive_root
from alphamind.execution.state_persistence.invocation_paths import INVOCATIONS_DIRNAME
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._artifact_io import (
    dump_adaptive_brief,
    load_correlation_regime_brief,
    load_distillation_outputs,
    load_qualitative_brief,
    load_sector_briefs,
    load_universal_regime_label,
    stage_artifacts_dir,
)
from alphamind.scripts._common import AssertionFailure, load_universe_scope

# ---------------------------------------------------------------------------
# Repo-root resolution and config loaders
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[3]
_DEFAULT_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"

_AGENT_NAME = AgentName.adaptive_researcher.value


# ---------------------------------------------------------------------------
# Diagnostic-archive expectations
# ---------------------------------------------------------------------------

# The harness writes one of these files per invocation; ``response_retry.md``
# is only present when a retry fired so it is intentionally NOT in the
# required set.
_REQUIRED_DIAGNOSTIC_FILES: tuple[str, ...] = (
    "prompt.md",
    "user_message.md",
    "response_initial.md",
    "errors.json",
    "metadata.json",
)


# Input-bundle section markers the renderer always emits — verifies the
# all-four-sections code path was exercised.
_INPUT_BUNDLE_SECTION_MARKERS: tuple[str, ...] = (
    "=== ADAPTIVE RESEARCH INPUT",
    "=== VOLATILITY REGIME ===",
    "=== DISTILLATION ANOMALY FLAGS",
    "=== SECTOR-RESEARCHER ANOMALIES",
)


def _diagnostic_dir(archive_root: Path, invocation_id: str, agent_name: str) -> Path:
    """Mirror the harness's diagnostic-archive layout."""
    return archive_root / INVOCATIONS_DIRNAME / invocation_id / "analysis" / agent_name


# ---------------------------------------------------------------------------
# Budget thresholds — loaded from config/agents.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetThresholds:
    """Per-invocation budgets the verification asserts against.

    All values are derived from the adaptive_researcher entry in
    ``config/agents.yaml``; nothing is hardcoded in this script.
    ``agent_config`` is the raw config so the report can render budget
    utilization for the operator and the tool-allowlist assertion can
    read ``agent_config.tools``.
    """

    wall_clock_seconds: float
    output_token_budget: int
    tool_call_limit: int
    registered_tool_ids: frozenset[str]
    agent_config: BaseAgentConfig


def _load_agents_config(agents_yaml_path: Path | None = None) -> AgentsConfig:
    path = agents_yaml_path or _DEFAULT_AGENTS_YAML
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return AgentsConfig.model_validate(data)


def _budgets_from_config(cfg: AgentsConfig) -> BudgetThresholds:
    agent_config = cfg.agents[AgentName.adaptive_researcher]
    if not isinstance(agent_config, AdaptiveAgentConfig):
        raise TypeError(
            f"Expected adaptive_researcher to be an AdaptiveAgentConfig "
            f"(tool-loop agent); got {type(agent_config).__name__}."
        )
    return BudgetThresholds(
        wall_clock_seconds=float(agent_config.latency_budget_seconds),
        output_token_budget=agent_config.output_token_budget,
        tool_call_limit=agent_config.cumulative_tool_call_limit,
        registered_tool_ids=frozenset(agent_config.tools),
        agent_config=agent_config,
    )


def load_budget_thresholds(agents_yaml_path: Path | None = None) -> BudgetThresholds:
    """Read ``config/agents.yaml`` and return the derived budget envelope."""
    return _budgets_from_config(_load_agents_config(agents_yaml_path))


# ---------------------------------------------------------------------------
# Report value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The structured result of one verification pass."""

    passed: bool
    failures: tuple[AssertionFailure, ...]
    invocation_id: str
    wall_clock_seconds: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    tool_calls_used: int
    retry_count: int
    thread_count: int
    anomalies_triaged_count: int
    anomalies_deferred_count: int
    diagnostic_files_present: tuple[str, ...]
    diagnostic_files_missing: tuple[str, ...]
    brief_preview: str
    bundle_section_markers_present: tuple[str, ...]
    bundle_section_markers_missing: tuple[str, ...]
    budgets: BudgetThresholds


# ---------------------------------------------------------------------------
# Brief preview rendering
# ---------------------------------------------------------------------------


def _render_brief_preview(brief: AdaptiveBrief) -> str:
    """Render a non-empty text preview of the brief.

    Mirrors the prompt's example-output format so the operator sees the
    structured content the runner produced.
    """
    lines: list[str] = []
    lines.append(f"invocation_id: {brief.invocation_id}")
    lines.append(f"threads_investigated_count: {brief.threads_investigated_count}")
    lines.append(f"anomalies_triaged_count: {brief.anomalies_triaged_count}")
    lines.append(f"anomalies_deferred: {list(brief.anomalies_deferred)}")
    lines.append("")
    lines.append("=== INVESTIGATION THREADS ===")
    if not brief.threads:
        lines.append("  (no threads — quiet cycle)")
    for thread in brief.threads:
        lines.append(
            f"  [{thread.thread_id}] {thread.assessment.value} ({thread.confidence.value}) — "
            f"{thread.sector.value} — tickers={list(thread.tickers)}"
        )
        lines.append(f"    trigger:  {thread.trigger}")
        lines.append(f"    question: {thread.question}")
        lines.append(f"    tools_used: {list(thread.tools_used)}")
        for finding in thread.findings:
            lines.append(f"    - finding: {finding}")
        if thread.assessment is Assessment.SIGNAL:
            lines.append(f"    implication: {thread.implication}")
            lines.append(f"    strengthens: {list(thread.strengthens or ())}")
            lines.append(f"    weakens: {list(thread.weakens or ())}")
        elif thread.assessment is Assessment.NOISE:
            lines.append(f"    dismissal_reason: {thread.dismissal_reason}")
        else:  # INCONCLUSIVE
            lines.append(f"    missing: {thread.missing}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure assertion helpers
# ---------------------------------------------------------------------------


def _check_brief_re_validates(
    brief: AdaptiveBrief,
    *,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    universe: frozenset[str],
) -> list[AssertionFailure]:
    """Re-run the structural validator on the runner's brief."""
    result = validate_adaptive_brief(
        brief,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        universe=universe,
    )
    if result.is_valid:
        return []
    return [
        AssertionFailure(
            code="brief-re-validation-failed",
            message=(f"brief failed re-validation: {e.field_path} ({e.rule}): {e.message}"),
        )
        for e in result.errors
    ]


def _check_diagnostic_archive(
    archive_root: Path,
    invocation_id: str,
    agent_name: str,
) -> tuple[list[AssertionFailure], tuple[str, ...], tuple[str, ...]]:
    """Probe the harness's diagnostic-archive layout."""
    failures: list[AssertionFailure] = []
    diag_dir = _diagnostic_dir(archive_root, invocation_id, agent_name)
    present: list[str] = []
    missing: list[str] = []
    for fname in _REQUIRED_DIAGNOSTIC_FILES:
        if (diag_dir / fname).exists():
            present.append(fname)
        else:
            missing.append(fname)
    if missing:
        failures.append(
            AssertionFailure(
                code="diagnostic-archive-missing-file",
                message=(
                    f"agent={agent_name} archive directory {diag_dir} missing: {', '.join(missing)}"
                ),
            )
        )
    return failures, tuple(present), tuple(missing)


def _check_input_bundle_sections(
    bundle_text: str,
) -> tuple[list[AssertionFailure], tuple[str, ...], tuple[str, ...]]:
    """Probe the input-bundle text for all four section markers."""
    present: list[str] = []
    missing: list[str] = []
    for marker in _INPUT_BUNDLE_SECTION_MARKERS:
        if marker in bundle_text:
            present.append(marker)
        else:
            missing.append(marker)
    failures: list[AssertionFailure] = []
    if missing:
        failures.append(
            AssertionFailure(
                code="input-bundle-section-missing",
                message=(f"input bundle missing section marker(s): {', '.join(missing)}"),
            )
        )
    return failures, tuple(present), tuple(missing)


def _check_tools_used_in_allowlist(
    brief: AdaptiveBrief, registered_tool_ids: frozenset[str]
) -> list[AssertionFailure]:
    """Every ``thread.tools_used`` entry must be in the agent's allowlist."""
    failures: list[AssertionFailure] = []
    for thread in brief.threads:
        for tool in thread.tools_used:
            if tool not in registered_tool_ids:
                failures.append(
                    AssertionFailure(
                        code="tool-name-outside-allowlist",
                        message=(
                            f"thread {thread.thread_id} cites tool {tool!r} not in the "
                            f"registered allowlist {sorted(registered_tool_ids)!r}"
                        ),
                    )
                )
    return failures


def _check_budgets(
    result: AdaptiveResearcherResult, budgets: BudgetThresholds
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    if result.wall_clock_seconds > budgets.wall_clock_seconds:
        failures.append(
            AssertionFailure(
                code="wall-clock-budget-exceeded",
                message=(
                    f"wall-clock {result.wall_clock_seconds:.2f}s exceeds "
                    f"budget {budgets.wall_clock_seconds:.2f}s "
                    f"(loaded from config/agents.yaml)"
                ),
            )
        )
    if result.tokens_used.output_tokens > budgets.output_token_budget:
        failures.append(
            AssertionFailure(
                code="output-token-budget-exceeded",
                message=(
                    f"output_tokens {result.tokens_used.output_tokens} exceeds "
                    f"budget {budgets.output_token_budget} "
                    f"(loaded from config/agents.yaml)"
                ),
            )
        )
    if result.tool_calls_used > budgets.tool_call_limit:
        failures.append(
            AssertionFailure(
                code="tool-call-limit-exceeded",
                message=(
                    f"tool_calls_used {result.tool_calls_used} exceeds "
                    f"limit {budgets.tool_call_limit} "
                    f"(loaded from config/agents.yaml)"
                ),
            )
        )
    return failures


# ---------------------------------------------------------------------------
# Public entry: compute_report
# ---------------------------------------------------------------------------


def compute_report(
    *,
    result: AdaptiveResearcherResult,
    invocation_id: str,
    archive_root: Path,
    budgets: BudgetThresholds,
    upstream: UpstreamFixture,
) -> VerificationReport:
    """Run every structural assertion and return a :class:`VerificationReport`."""
    failures: list[AssertionFailure] = []
    failures.extend(
        _check_brief_re_validates(
            result.brief,
            sector_briefs=upstream.sector_briefs,
            qualitative_brief=upstream.qualitative_brief,
            correlation_regime_brief=upstream.correlation_regime_brief,
            universe=upstream.universe,
        )
    )
    diag_failures, present, missing = _check_diagnostic_archive(
        archive_root, invocation_id, _AGENT_NAME
    )
    failures.extend(diag_failures)
    bundle_failures, bundle_present, bundle_missing = _check_input_bundle_sections(
        result.input_bundle.bundle_text
    )
    failures.extend(bundle_failures)
    failures.extend(_check_tools_used_in_allowlist(result.brief, budgets.registered_tool_ids))
    failures.extend(_check_budgets(result, budgets))

    return VerificationReport(
        passed=not failures,
        failures=tuple(failures),
        invocation_id=invocation_id,
        wall_clock_seconds=result.wall_clock_seconds,
        input_tokens=result.tokens_used.input_tokens,
        output_tokens=result.tokens_used.output_tokens,
        cache_read_tokens=result.tokens_used.cache_read_tokens,
        cache_write_tokens=result.tokens_used.cache_write_tokens,
        tool_calls_used=result.tool_calls_used,
        retry_count=result.retry_count,
        thread_count=len(result.brief.threads),
        anomalies_triaged_count=result.brief.anomalies_triaged_count,
        anomalies_deferred_count=len(result.brief.anomalies_deferred),
        diagnostic_files_present=present,
        diagnostic_files_missing=missing,
        brief_preview=_render_brief_preview(result.brief),
        bundle_section_markers_present=bundle_present,
        bundle_section_markers_missing=bundle_missing,
        budgets=budgets,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


_BANNER = "=" * 70


def _render_summary_section(report: VerificationReport) -> list[str]:
    return [
        "",
        "[ Invocation summary ]",
        f"  invocation_id              = {report.invocation_id}",
        f"  wall_clock                 = {report.wall_clock_seconds:.2f}s",
        f"  input_tokens               = {report.input_tokens}",
        f"  output_tokens              = {report.output_tokens}",
        f"  cache_read_tokens          = {report.cache_read_tokens}",
        f"  cache_write_tokens         = {report.cache_write_tokens}",
        f"  tool_calls_used            = {report.tool_calls_used}",
        f"  retry_count                = {report.retry_count}",
        f"  thread_count               = {report.thread_count}",
        f"  anomalies_triaged_count    = {report.anomalies_triaged_count}",
        f"  anomalies_deferred_count   = {report.anomalies_deferred_count}",
    ]


def _render_diagnostic_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Diagnostic archive ]"]
    present_count = len(report.diagnostic_files_present)
    total = len(_REQUIRED_DIAGNOSTIC_FILES)
    status = "OK" if not report.diagnostic_files_missing else "MISSING"
    lines.append(f"  adaptive_researcher    files={present_count}/{total} {status}")
    if report.diagnostic_files_missing:
        lines.append(f"    missing: {', '.join(report.diagnostic_files_missing)}")
    return lines


def _render_bundle_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Input bundle sections ]"]
    present_count = len(report.bundle_section_markers_present)
    total = len(_INPUT_BUNDLE_SECTION_MARKERS)
    status = "OK" if not report.bundle_section_markers_missing else "MISSING"
    lines.append(f"  adaptive_researcher    sections={present_count}/{total} {status}")
    if report.bundle_section_markers_missing:
        lines.append(f"    missing: {', '.join(report.bundle_section_markers_missing)}")
    return lines


def _render_budget_section(report: VerificationReport) -> list[str]:
    return [
        "",
        "[ Budget envelope ]",
        (
            f"  wall_clock = {report.wall_clock_seconds:.2f}s "
            f"(budget {report.budgets.wall_clock_seconds:.2f}s, loaded from agents.yaml)"
        ),
        (
            f"  output_tokens = {report.output_tokens} "
            f"(budget {report.budgets.output_token_budget}, loaded from agents.yaml)"
        ),
        (
            f"  tool_calls_used = {report.tool_calls_used} "
            f"(limit {report.budgets.tool_call_limit}, loaded from agents.yaml)"
        ),
    ]


def _render_brief_section(report: VerificationReport) -> list[str]:
    return ["", "[ Brief preview ]", *report.brief_preview.splitlines()]


def _render_failures_section(report: VerificationReport) -> list[str]:
    if not report.failures:
        return []
    lines: list[str] = ["", "[ Failures ]"]
    for failure in report.failures:
        lines.append(f"  {failure.code}: {failure.message}")
    return lines


def _render_result_line(report: VerificationReport) -> str:
    if report.passed:
        return "RESULT: PASS — every structural assertion holds."
    return f"RESULT: FAIL — {len(report.failures)} assertion(s) failed (see above)."


def format_report(report: VerificationReport) -> str:
    """Render the human-readable summary the operator reads from stdout."""
    lines: list[str] = [
        _BANNER,
        "AlphaMind Adaptive-Researcher End-to-End Verification",
        _BANNER,
    ]
    lines.extend(_render_summary_section(report))
    lines.extend(_render_diagnostic_section(report))
    lines.extend(_render_bundle_section(report))
    lines.extend(_render_budget_section(report))
    lines.extend(_render_brief_section(report))
    lines.extend(_render_failures_section(report))
    lines.extend(["", _BANNER, _render_result_line(report), _BANNER])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


# Runner callable signature — the dependency-injection seam tests use
# instead of touching the SDK.
RunnerCallable = Callable[..., Coroutine[Any, Any, AdaptiveResearcherResult]]


def _format_invocation_id(now: datetime) -> str:
    """``YYYYMMDDTHHMMSSZ-verify-adaptive-researcher``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-verify-adaptive-researcher"


@dataclass(frozen=True, slots=True)
class UpstreamFixture:
    """The faked-upstream payload bundle the runner + validator both consume.

    Bundling these as a single value object keeps the verification entry
    points under the linter's argument-count threshold while preserving
    the typed signatures the production runner and the Layer-3 validator
    expect. The :attr:`universe` field is the asset-universe roster used
    by the validator's ticker-membership check; bundling it alongside
    the upstream briefs keeps the verification call surface uniform.
    """

    sector_briefs: tuple[SectorBrief, ...]
    qualitative_brief: QualitativeBrief
    correlation_regime_brief: CorrelationRegimeBrief
    distillation_outputs: DistillationOutputs
    universal_regime_label: dict[str, Any]
    universe: frozenset[str]


def run_verification(
    *,
    invocation_id: str,
    as_of: datetime,
    archive_root: Path,
    budgets: BudgetThresholds,
    runner_fn: RunnerCallable,
    upstream: UpstreamFixture,
) -> int:
    """Run the adaptive researcher, build the report, print it, return exit code.

    On a passing run, the adaptive brief is dumped to the per-invocation
    stage-artifacts directory so the synthesizer verification script can
    consume it via ``--upstream-from`` (ALP-287).
    """
    result = asyncio.run(runner_fn(invocation_id=invocation_id, as_of=as_of))
    report = compute_report(
        result=result,
        invocation_id=invocation_id,
        archive_root=archive_root,
        budgets=budgets,
        upstream=upstream,
    )
    print(format_report(report))
    if report.passed:
        stage_dir = stage_artifacts_dir(archive_root, invocation_id)
        dump_adaptive_brief(result.brief, stage_dir)
        print(f"[verify_adaptive_researcher] stage artifacts written to {stage_dir}")
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# Production binding for runner_fn
# ---------------------------------------------------------------------------


def _bind_production_runner(
    *,
    session: Any,
    upstream: UpstreamFixture,
    agents_config: dict[str, BaseAgentConfig],
    archive_root: Path,
) -> RunnerCallable:
    """Return a closure that calls :func:`run_adaptive_researcher` with bound deps."""

    async def _runner(*, invocation_id: str, as_of: datetime) -> AdaptiveResearcherResult:
        return await run_adaptive_researcher(
            invocation_id=invocation_id,
            as_of=as_of,
            session=session,
            distillation_outputs=upstream.distillation_outputs,
            sector_briefs=upstream.sector_briefs,
            qualitative_brief=upstream.qualitative_brief,
            correlation_regime_brief=upstream.correlation_regime_brief,
            universal_regime_label=upstream.universal_regime_label,
            universe=upstream.universe,
            agents_config=agents_config,
            archive_root=archive_root,
        )

    return _runner


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _check_oauth_token_set() -> None:
    """Fail fast if the SDK auth token is not in the environment."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        msg = (
            "CLAUDE_CODE_OAUTH_TOKEN is not set in the environment. The "
            "real-SDK adaptive-researcher verification cannot run without "
            "it. Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run."
        )
        raise RuntimeError(msg)


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting either ``Z`` or explicit offset."""
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _load_e2e_fixtures_lazy() -> tuple[
    tuple[SectorBrief, ...],
    QualitativeBrief,
    CorrelationRegimeBrief,
    DistillationOutputs,
    dict[str, Any],
]:
    """Import-and-call ``load_e2e_fixtures`` lazily so the test-tree import
    only happens when the operator actually invokes the script.

    The fixtures live under ``tests/`` per the AC; importing them at module
    top-level would pull the test tree into every operator-tooling invocation
    and weaken the mypy boundary between ``src/`` and ``tests/``.
    """
    from tests.analysis.adaptive_research.fixtures import load_e2e_fixtures

    return load_e2e_fixtures()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the exit code; never raises beyond auth.

    Success criteria:
        - Exit code 0.
        - ``RESULT: PASS`` printed on the final line of the summary banner.
        - The brief-preview section is non-empty (renders investigation
          threads and assessment metadata from the parsed brief).
        - The diagnostic-archive section reports ``files=5/5 OK`` for the
          ``adaptive_researcher`` agent.
        - The input-bundle section reports ``sections=4/4 OK``.

    On failure:
        - Exit code 1.
        - ``RESULT: FAIL`` printed; each failure code surfaces under
          ``[ Failures ]``.
        - Re-run with ``--archive-root`` pointing at a writable scratch
          directory; the harness's prompt + response + error trail will
          be written under
          ``<archive-root>/invocations/<invocation_id>/analysis/adaptive_researcher/``.
        - If the budget assertion fails (wall_clock / output_tokens /
          tool_calls), check ``config/agents.yaml`` for tightened
          ``adaptive_researcher`` budgets, or surface to the operator
          as a regression in the LLM, prompt, or input bundle.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Run the adaptive-researcher runner end-to-end against a real "
            "CLAUDE_CODE_OAUTH_TOKEN and verify the structural conditions in ALP-265."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=main.__doc__,
    )
    parser.add_argument(
        "--invocation-id",
        type=str,
        default=None,
        help=(
            "Override the invocation_id used for the diagnostic archive layout. "
            "Defaults to YYYYMMDDTHHMMSSZ-verify-adaptive-researcher."
        ),
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help=(
            "ISO-8601 timestamp for the current market snapshot "
            "(e.g., 2026-05-02T14:30:00Z). Defaults to now (UTC)."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to the AlphaMind SQLite database (overrides DATABASE_PATH and main.yaml).",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "Path to the invocation-archive root (defaults to "
            "%%USERPROFILE%%/AlphaMind/archive on Windows, ~/AlphaMind/archive elsewhere)."
        ),
    )
    parser.add_argument(
        "--upstream-from",
        type=Path,
        default=None,
        help=(
            "Path to a stage-artifacts directory produced by earlier phases. "
            "When supplied, the upstream-brief tuple (sector_briefs, "
            "qualitative_brief, correlation_regime_brief, distillation_outputs, "
            "universal_regime_label) is loaded from this directory instead of "
            "the test-tree fixtures — proving today's actual upstream artifacts "
            "flow through the adaptive researcher."
        ),
    )
    args = parser.parse_args(argv)

    _check_oauth_token_set()

    now = datetime.now(tz=UTC)
    as_of = _parse_iso8601(args.as_of) if args.as_of else now
    invocation_id = args.invocation_id or _format_invocation_id(now)

    archive_root = args.archive_root if args.archive_root is not None else _default_archive_root()
    universe = frozenset(load_universe_scope())

    agents_yaml_data = _load_agents_config()
    budgets = _budgets_from_config(agents_yaml_data)
    agents_config: dict[str, BaseAgentConfig] = {
        name.value: cfg for name, cfg in agents_yaml_data.agents.items()
    }

    if args.upstream_from is not None:
        sector_briefs = load_sector_briefs(args.upstream_from)
        qualitative_brief = load_qualitative_brief(args.upstream_from)
        correlation_regime_brief = load_correlation_regime_brief(args.upstream_from)
        distillation_outputs = load_distillation_outputs(args.upstream_from)
        regime = load_universal_regime_label(args.upstream_from)
        upstream_source = f"stage artifacts at {args.upstream_from}"
    else:
        sector_briefs, qualitative_brief, correlation_regime_brief, distillation_outputs, regime = (
            _load_e2e_fixtures_lazy()
        )
        upstream_source = "test-tree fixtures"
    upstream = UpstreamFixture(
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        distillation_outputs=distillation_outputs,
        universal_regime_label=regime,
        universe=universe,
    )
    print(
        f"[verify_adaptive_researcher] upstream-brief tuple loaded from {upstream_source} "
        f"(sector_briefs={len(sector_briefs)}, "
        f"qualitative_threads={len(qualitative_brief.threads)}, "
        f"cr_refs={len(correlation_regime_brief.reference_index)}, "
        f"distillation_blocks={len(distillation_outputs.all_blocks)})"
    )

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)
    with factory() as session:
        runner_fn = _bind_production_runner(
            session=session,
            upstream=upstream,
            agents_config=agents_config,
            archive_root=archive_root,
        )
        return run_verification(
            invocation_id=invocation_id,
            as_of=as_of,
            archive_root=archive_root,
            budgets=budgets,
            runner_fn=runner_fn,
            upstream=upstream,
        )


__all__ = [
    "BudgetThresholds",
    "RunnerCallable",
    "UpstreamFixture",
    "VerificationReport",
    "compute_report",
    "format_report",
    "load_budget_thresholds",
    "main",
    "run_verification",
]


if __name__ == "__main__":
    sys.exit(main())
