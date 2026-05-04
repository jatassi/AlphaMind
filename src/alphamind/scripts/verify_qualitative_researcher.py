"""Qualitative-researcher end-to-end verification (ALP-253).

Operator entry point: connect to the SQLite database, load
``agents_config`` from ``config/agents.yaml`` and the universe roster
from ``config/assets.yaml``, build the ``universal_regime_label`` from
the most recent ``DistillationOutputs`` (or a synthetic stub if no
recent run exists), then call
:func:`alphamind.analysis.qualitative_research.runner.run_qualitative_researcher`
with a real ``CLAUDE_CODE_OAUTH_TOKEN`` and assert the structural
properties documented in story ALP-253 § Scope hold:

- The runner returns a :class:`QualitativeResearcherResult` with no exceptions.
- The brief structurally re-validates against
  :mod:`alphamind.analysis.qualitative_research.validation`.
- The diagnostic archive directory under
  ``<archive_root>/invocations/<invocation_id>/analysis/qualitative_researcher/``
  holds ``prompt.md``, ``user_message.md``, ``response_initial.md``,
  ``errors.json``, ``metadata.json`` (plus ``response_retry.md`` if a
  retry happened).
- Wall-clock seconds stay within ``latency_budget_seconds`` from
  ``config/agents.yaml``.
- ``output_tokens`` stays within ``output_token_budget`` from
  ``config/agents.yaml``.
- ``tool_calls_used`` stays within ``cumulative_tool_call_limit`` from
  ``config/agents.yaml``.

Returns 0 when every structural assertion passes; 1 otherwise. The
script also prints a non-empty ``QualitativeBrief`` text representation
and a summary table (wall-clock, tokens, tool-calls, retry count,
signal quality) so the operator can spot drift before re-running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.analysis.qualitative_research.runner import (
    QualitativeResearcherResult,
    run_qualitative_researcher,
)
from alphamind.analysis.qualitative_research.validation import validate_qualitative_brief
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.distillation.orchestrator import _default_archive_root
from alphamind.persistence.models import DistillationRegimeState
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._artifact_io import (
    dump_qualitative_brief,
    load_universal_regime_label,
    stage_artifacts_dir,
)
from alphamind.scripts._common import AssertionFailure, load_universe_scope

# ---------------------------------------------------------------------------
# Repo-root resolution and config loaders
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[3]
_DEFAULT_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"

_AGENT_NAME = AgentName.qualitative_researcher.value
_ONE_HOUR = timedelta(hours=1)


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


def _diagnostic_dir(archive_root: Path, invocation_id: str, agent_name: str) -> Path:
    """Mirror the harness's diagnostic-archive layout."""
    return archive_root / "invocations" / invocation_id / "analysis" / agent_name


# ---------------------------------------------------------------------------
# Budget thresholds — loaded from config/agents.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetThresholds:
    """Per-invocation budgets the verification asserts against.

    All values are derived from the qualitative_researcher entry in
    ``config/agents.yaml``; nothing is hardcoded in this script.
    ``agent_config`` is the raw config so the report can render budget
    utilization for the operator.

    - ``wall_clock_seconds`` — qualitative_researcher's
      ``latency_budget_seconds``.
    - ``output_token_budget`` — qualitative_researcher's
      ``output_token_budget``.
    - ``tool_call_limit`` — qualitative_researcher's
      ``cumulative_tool_call_limit`` (it's an :class:`AdaptiveAgentConfig`).
    """

    wall_clock_seconds: float
    output_token_budget: int
    tool_call_limit: int
    agent_config: BaseAgentConfig


def _load_agents_config(agents_yaml_path: Path | None = None) -> AgentsConfig:
    path = agents_yaml_path or _DEFAULT_AGENTS_YAML
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return AgentsConfig.model_validate(data)


def _budgets_from_config(cfg: AgentsConfig) -> BudgetThresholds:
    agent_config = cfg.agents[AgentName.qualitative_researcher]
    if not isinstance(agent_config, AdaptiveAgentConfig):
        raise TypeError(
            f"Expected qualitative_researcher to be an AdaptiveAgentConfig "
            f"(tool-loop agent); got {type(agent_config).__name__}."
        )
    return BudgetThresholds(
        wall_clock_seconds=float(agent_config.latency_budget_seconds),
        output_token_budget=agent_config.output_token_budget,
        tool_call_limit=agent_config.cumulative_tool_call_limit,
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
    catalyst_count: int
    signal_quality: str
    diagnostic_files_present: tuple[str, ...]
    diagnostic_files_missing: tuple[str, ...]
    brief_preview: str
    digest_total_collected: int
    digest_total_shown: int
    budgets: BudgetThresholds


# ---------------------------------------------------------------------------
# Brief preview rendering
# ---------------------------------------------------------------------------


def _render_brief_preview(brief: QualitativeBrief) -> str:
    """Render a non-empty text preview of the brief.

    Mirrors the prompt's example-output format so the operator sees the
    structured content the runner produced. Per AC: "The script prints a
    non-empty QualitativeBrief text representation."
    """
    lines: list[str] = []
    lines.append(f"signal_quality: {brief.signal_quality.value}")
    if brief.signal_quality_reason:
        lines.append(f"signal_quality_reason: {brief.signal_quality_reason}")
    lines.append("")
    lines.append("=== NARRATIVE THREADS ===")
    for thread in brief.threads:
        lines.append(
            f"  [{thread.thread_id}] {thread.subject} — "
            f"{thread.direction.value} ({thread.time_horizon.value})"
        )
        lines.append(f"    summary: {thread.summary}")
        for ev in thread.evidence:
            lines.append(f"    - {ev.source_type}: {ev.observation} ({ev.citation})")
        lines.append(f"    implication: {thread.implication}")
    lines.append("")
    lines.append("=== CATALYST WATCH ===")
    if brief.catalyst_watches:
        for cw in brief.catalyst_watches:
            lines.append(
                f"  [{cw.catalyst_id}] {cw.ticker} — {cw.catalyst_name} (in {cw.hours_to_event}h)"
            )
            lines.append(f"    thesis_impact: {cw.thesis_impact}")
    else:
        lines.append("  (no catalysts in window)")
    lines.append("")
    lines.append("=== SENTIMENT SNAPSHOT ===")
    lines.append(f"  extremes: {brief.sentiment_snapshot.extremes}")
    lines.append(f"  divergences: {brief.sentiment_snapshot.divergences}")
    lines.append(f"  regime: {brief.sentiment_snapshot.regime}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure assertion helpers
# ---------------------------------------------------------------------------


def _check_brief_re_validates(brief: QualitativeBrief) -> list[AssertionFailure]:
    """Re-run the structural validator on the runner's brief.

    The universe-membership check is the runner's responsibility (and
    has already fired before the result reached this script), so the
    re-validation runs with ``universe=None`` — purely a structural
    cross-check.
    """
    result = validate_qualitative_brief(brief)
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


def _check_budgets(
    result: QualitativeResearcherResult, budgets: BudgetThresholds
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
    result: QualitativeResearcherResult,
    invocation_id: str,
    archive_root: Path,
    budgets: BudgetThresholds,
) -> VerificationReport:
    """Run every structural assertion and return a :class:`VerificationReport`."""
    failures: list[AssertionFailure] = []
    failures.extend(_check_brief_re_validates(result.brief))
    diag_failures, present, missing = _check_diagnostic_archive(
        archive_root, invocation_id, _AGENT_NAME
    )
    failures.extend(diag_failures)
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
        catalyst_count=len(result.brief.catalyst_watches),
        signal_quality=result.brief.signal_quality.value,
        diagnostic_files_present=present,
        diagnostic_files_missing=missing,
        brief_preview=_render_brief_preview(result.brief),
        digest_total_collected=result.news_digest.total_collected,
        digest_total_shown=result.news_digest.total_shown,
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
        f"  invocation_id     = {report.invocation_id}",
        f"  wall_clock        = {report.wall_clock_seconds:.2f}s",
        f"  input_tokens      = {report.input_tokens}",
        f"  output_tokens     = {report.output_tokens}",
        f"  cache_read_tokens = {report.cache_read_tokens}",
        f"  cache_write_tokens= {report.cache_write_tokens}",
        f"  tool_calls_used   = {report.tool_calls_used}",
        f"  retry_count       = {report.retry_count}",
        f"  thread_count      = {report.thread_count}",
        f"  catalyst_count    = {report.catalyst_count}",
        f"  signal_quality    = {report.signal_quality}",
        f"  digest_collected  = {report.digest_total_collected}",
        f"  digest_shown      = {report.digest_total_shown}",
    ]


def _render_diagnostic_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Diagnostic archive ]"]
    present_count = len(report.diagnostic_files_present)
    total = len(_REQUIRED_DIAGNOSTIC_FILES)
    status = "OK" if not report.diagnostic_files_missing else "MISSING"
    lines.append(f"  qualitative_researcher    files={present_count}/{total} {status}")
    if report.diagnostic_files_missing:
        lines.append(f"    missing: {', '.join(report.diagnostic_files_missing)}")
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
        "AlphaMind Qualitative-Researcher End-to-End Verification",
        _BANNER,
    ]
    lines.extend(_render_summary_section(report))
    lines.extend(_render_diagnostic_section(report))
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
RunnerCallable = Callable[..., Coroutine[Any, Any, QualitativeResearcherResult]]


def _format_invocation_id(now: datetime) -> str:
    """``YYYYMMDDTHHMMSSZ-verify-qualitative-researcher``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-verify-qualitative-researcher"


def run_verification(
    *,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    archive_root: Path,
    budgets: BudgetThresholds,
    runner_fn: RunnerCallable,
) -> int:
    """Run the qualitative researcher, build the report, print it, return exit code.

    The caller supplies ``runner_fn`` (either the production
    :func:`run_qualitative_researcher` wrapped with bound config, or a stub
    in tests). The function does I/O (prints to stdout) but every
    dependency is injected.

    On a passing run, the qualitative brief is dumped to the per-invocation
    stage-artifacts directory so downstream verification scripts can consume
    it via ``--upstream-from`` (ALP-287).
    """
    result = asyncio.run(
        runner_fn(
            invocation_id=invocation_id,
            as_of=as_of,
            last_invocation_time=last_invocation_time,
        )
    )
    report = compute_report(
        result=result,
        invocation_id=invocation_id,
        archive_root=archive_root,
        budgets=budgets,
    )
    print(format_report(report))
    if report.passed:
        stage_dir = stage_artifacts_dir(archive_root, invocation_id)
        dump_qualitative_brief(result.brief, stage_dir)
        print(f"[verify_qualitative_researcher] stage artifacts written to {stage_dir}")
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# Universal-regime-label resolution
# ---------------------------------------------------------------------------


def _load_recent_regime_label(session: Session) -> tuple[dict[str, Any], str]:
    """Load the most recent ``DistillationRegimeState`` row and project it.

    Returns ``(payload, source_label)`` where ``source_label`` is either
    ``"db"`` or ``"synthetic-stub"``. When the database has no rows in
    ``distillation_regime_state`` the script falls back to a synthetic
    stub matching the runner's expected payload shape (the same shape
    :func:`alphamind.distillation.regime.assemble_regime_block` produces).
    """
    stmt = select(DistillationRegimeState).order_by(desc(DistillationRegimeState.as_of)).limit(1)
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return _synthetic_regime_label(), "synthetic-stub"
    payload: dict[str, Any] = {
        "regime_label": row.regime_label,
        "transition_state": row.transition_state,
        "prior_label": row.prior_label,
        "invocations_held": row.invocations_held,
        "indicator_agreement_count": row.indicator_agreement_count,
        "vix_level": float(row.vix_level),
        "term_structure_basis": float(row.term_structure_basis),
        "vvix_percentile": float(row.vvix_percentile),
        "realized_vol_5d": float(row.realized_vol),
    }
    return payload, "db"


def _synthetic_regime_label() -> dict[str, Any]:
    """Synthetic regime-label stub matching the runner's expected payload shape."""
    return {
        "regime_label": "vol_expansion",
        "transition_state": "early-weak",
        "prior_label": "low_vol_compression",
        "invocations_held": 1,
        "indicator_agreement_count": 2,
        "vix_level": 22.0,
        "term_structure_basis": 1.5,
        "vvix_percentile": 0.7,
        "realized_vol_5d": 0.15,
    }


# ---------------------------------------------------------------------------
# Production binding for runner_fn
# ---------------------------------------------------------------------------


def _bind_production_runner(
    *,
    session: Session,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: dict[str, BaseAgentConfig],
    archive_root: Path,
) -> RunnerCallable:
    """Return a closure that calls :func:`run_qualitative_researcher` with bound deps."""

    async def _runner(
        *,
        invocation_id: str,
        as_of: datetime,
        last_invocation_time: datetime,
    ) -> QualitativeResearcherResult:
        return await run_qualitative_researcher(
            invocation_id=invocation_id,
            as_of=as_of,
            last_invocation_time=last_invocation_time,
            session=session,
            universal_regime_label=universal_regime_label,
            universe=universe,
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
            "real-SDK qualitative-researcher verification cannot run without "
            "it. Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run."
        )
        raise RuntimeError(msg)


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting either ``Z`` or explicit offset."""
    # ``fromisoformat`` accepts ``+00:00`` but not ``Z`` until 3.11+ supports it.
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the exit code; never raises beyond auth.

    Success criteria:
        - Exit code 0.
        - ``RESULT: PASS`` printed on the final line of the summary banner.
        - The brief-preview section is non-empty (renders narrative threads,
          catalyst watch, sentiment snapshot from the parsed brief).
        - The diagnostic-archive section reports ``files=5/5 OK`` for the
          ``qualitative_researcher`` agent.

    On failure:
        - Exit code 1.
        - ``RESULT: FAIL`` printed; each failure code surfaces under
          ``[ Failures ]``.
        - Re-run with ``--archive-root`` pointing at a writable scratch
          directory; the harness's prompt + response + error trail will
          be written under
          ``<archive-root>/invocations/<invocation_id>/analysis/qualitative_researcher/``.
        - If the budget assertion fails (wall_clock / output_tokens /
          tool_calls), check ``config/agents.yaml`` for tightened
          ``qualitative_researcher`` budgets, or surface to the operator
          as a regression in the LLM, prompt, or input bundle.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Run the qualitative-researcher runner end-to-end against a real "
            "CLAUDE_CODE_OAUTH_TOKEN and verify the structural conditions in ALP-253."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=main.__doc__,
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
        "--last-invocation",
        type=str,
        default=None,
        help=(
            "ISO-8601 timestamp for the prior invocation's as_of; bounds the "
            "news-digest window. Defaults to as_of - 1 hour."
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
        "--invocation-id",
        type=str,
        default=None,
        help=(
            "Override the invocation_id used for the diagnostic-archive layout AND "
            "the stage-artifacts directory shared with downstream verification "
            "scripts. Defaults to YYYYMMDDTHHMMSSZ-verify-qualitative-researcher."
        ),
    )
    parser.add_argument(
        "--upstream-from",
        type=Path,
        default=None,
        help=(
            "Path to a stage-artifacts directory produced by an earlier phase. "
            "When supplied, universal_regime_label.json is loaded from this "
            "directory instead of reading from distillation_regime_state (more "
            "reliable than the DB-state fallback, which can stub on a cold DB)."
        ),
    )
    args = parser.parse_args(argv)

    _check_oauth_token_set()

    now = datetime.now(tz=UTC)
    as_of = _parse_iso8601(args.as_of) if args.as_of else now
    last_invocation_time = (
        _parse_iso8601(args.last_invocation)
        if args.last_invocation
        else as_of.replace(microsecond=0).astimezone(UTC) - _ONE_HOUR
    )
    invocation_id = args.invocation_id or _format_invocation_id(now)

    archive_root = args.archive_root if args.archive_root is not None else _default_archive_root()
    universe = frozenset(load_universe_scope())

    agents_yaml_data = _load_agents_config()
    budgets = _budgets_from_config(agents_yaml_data)
    agents_config: dict[str, BaseAgentConfig] = {
        name.value: cfg for name, cfg in agents_yaml_data.agents.items()
    }

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)
    with factory() as session:
        if args.upstream_from is not None:
            regime_label = load_universal_regime_label(args.upstream_from)
            print(
                f"[verify_qualitative_researcher] universal_regime_label loaded from "
                f"{args.upstream_from / 'universal_regime_label.json'}"
            )
        else:
            regime_label, regime_source = _load_recent_regime_label(session)
            print(f"[verify_qualitative_researcher] universal_regime_label source: {regime_source}")
        runner_fn = _bind_production_runner(
            session=session,
            universal_regime_label=regime_label,
            universe=universe,
            agents_config=agents_config,
            archive_root=archive_root,
        )
        return run_verification(
            invocation_id=invocation_id,
            as_of=as_of,
            last_invocation_time=last_invocation_time,
            archive_root=archive_root,
            budgets=budgets,
            runner_fn=runner_fn,
        )


__all__ = [
    "BudgetThresholds",
    "RunnerCallable",
    "VerificationReport",
    "compute_report",
    "format_report",
    "load_budget_thresholds",
    "main",
    "run_verification",
]


if __name__ == "__main__":
    sys.exit(main())
