"""Domain-researcher end-to-end verification (ALP-186).

Operator entry point: connect to the SQLite database, run
:func:`alphamind.distillation.orchestrator.run_external_distillation` to
build :class:`DistillationOutputs` for the current ``as_of``, then call
:func:`alphamind.analysis.domain_researchers.orchestrator.run_domain_researchers`
with a real ``CLAUDE_CODE_OAUTH_TOKEN`` against that distillation snapshot
and assert the structural properties documented in story ALP-186 § Scope hold:

- Three populated :class:`DomainResearcherResult` fields (tech_semis,
  financials, energy).
- Each brief structurally re-validates against
  :mod:`alphamind.analysis.domain_researchers.validation` (reference IDs
  sequential per section starting at 1, prefix consistency).
- The diagnostic archive directory under
  ``<archive_root>/invocations/<invocation_id>/analysis/<agent_name>/``
  holds the documented files.
- Total wall-clock seconds stay under the budget loaded from
  ``config/agents.yaml`` (max latency budget across the three researchers,
  since they run in parallel).
- Total tokens used stay under the per-invocation analysis-layer budget
  loaded from ``config/agents.yaml`` (sum of per-agent context+output
  budgets across the three researchers).

Returns 0 when every structural assertion passes; 1 otherwise. The
script also surfaces a per-sector summary table (wall-clock, tokens,
retry count, finding count, anomaly count, thesis-candidate count,
signal quality) so the operator can spot drift before re-running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.orchestrator import (
    DomainResearchersOutput,
    run_domain_researchers,
)
from alphamind.analysis.domain_researchers.runner import DomainResearcherResult
from alphamind.analysis.domain_researchers.validation import validate_brief
from alphamind.config.models.agents import AgentName, AgentsConfig, BaseAgentConfig
from alphamind.distillation.orchestrator import (
    DistillationOutputs,
    _default_archive_root,
    run_external_distillation,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._artifact_io import (
    dump_sector_briefs,
    load_distillation_outputs,
    stage_artifacts_dir,
)
from alphamind.scripts._common import (
    AssertionFailure,
    load_distillation_config,
    load_universe_scope,
)
from alphamind.scripts._stdio import configure_utf8_stdio

# ---------------------------------------------------------------------------
# Repo-root resolution and config loaders
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[3]
_DEFAULT_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"
_DEFAULT_ASSETS_YAML = _REPO_ROOT / "config" / "assets.yaml"


_RESEARCHER_AGENT_NAMES: tuple[str, ...] = (
    AgentName.tech_semis_researcher.value,
    AgentName.financials_researcher.value,
    AgentName.energy_researcher.value,
)


# ---------------------------------------------------------------------------
# Budget thresholds — loaded from config/agents.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetThresholds:
    """Per-invocation analysis-layer budgets the verification asserts against.

    All values are derived from the per-agent budgets in ``config/agents.yaml``;
    nothing is hardcoded in this script. The ``per_agent`` map is exposed
    so the report can render per-agent budget utilization for the operator.

    - ``wall_clock_seconds`` — max ``latency_budget_seconds`` across the three
      researchers, since they run in parallel.
    - ``token_total`` — sum of ``context_token_budget + output_token_budget``
      across the three researchers; the worst-case envelope per invocation.
    """

    wall_clock_seconds: float
    token_total: int
    per_agent: Mapping[str, BaseAgentConfig]


def load_budget_thresholds(agents_yaml_path: Path | None = None) -> BudgetThresholds:
    """Read ``config/agents.yaml`` and return the derived budget envelope.

    Per the spec note in ALP-186 ("the verification should reference the
    same authoritative source the runtime uses"), the script does NOT hardcode
    numeric thresholds. The per-agent budgets in ``agents.yaml`` ARE the
    authoritative source — ``cost-and-rate-limit-modeling.md`` documents
    that ``agents.yaml`` carries the per-agent latency / token budgets and
    profiles override them; the verification reads the unprofiled defaults.
    """
    path = agents_yaml_path or _DEFAULT_AGENTS_YAML
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    per_agent: dict[str, BaseAgentConfig] = {
        name: cfg.agents[AgentName(name)] for name in _RESEARCHER_AGENT_NAMES
    }
    wall_clock = float(max(cfg.latency_budget_seconds for cfg in per_agent.values()))
    token_total = sum(
        cfg.context_token_budget + cfg.output_token_budget for cfg in per_agent.values()
    )
    return BudgetThresholds(
        wall_clock_seconds=wall_clock,
        token_total=token_total,
        per_agent=per_agent,
    )


# ---------------------------------------------------------------------------
# Sectors-config loader (reads config/assets.yaml, combines tech+semis)
# ---------------------------------------------------------------------------


def load_sectors_config(assets_yaml_path: Path | None = None) -> dict[str, list[str]]:
    """Build the ``{sector_name: [tickers]}`` mapping the runner expects.

    ``config/assets.yaml`` keys sectors as ``tech``, ``semis``, ``financials``,
    ``energy``; the analysis layer's :class:`Sector.TECH_SEMIS` collapses
    ``tech`` and ``semis`` into a single ``tech_semis`` audience, matching
    :data:`alphamind.distillation.sector_assembly.DOMAIN_RESEARCHER_BY_AUDIENCE`.
    """
    path = assets_yaml_path or _DEFAULT_ASSETS_YAML
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    sectors_yaml = data.get("sectors", {})
    return {
        Sector.TECH_SEMIS.value: list(sectors_yaml.get("tech", []))
        + list(sectors_yaml.get("semis", [])),
        Sector.FINANCIALS.value: list(sectors_yaml.get("financials", [])),
        Sector.ENERGY.value: list(sectors_yaml.get("energy", [])),
    }


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
    "metadata.json",
)


def _diagnostic_dir(archive_root: Path, invocation_id: str, agent_name: str) -> Path:
    """Mirror the harness's diagnostic-archive layout."""
    return archive_root / INVOCATIONS_DIRNAME / invocation_id / "analysis" / agent_name


# ---------------------------------------------------------------------------
# Report value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerSectorSnapshot:
    """One sector's slice of the verification report."""

    sector: Sector
    agent_name: str
    wall_clock_seconds: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    retry_count: int
    finding_count: int
    anomaly_count: int
    thesis_candidate_count: int
    signal_quality: str
    diagnostic_files_present: tuple[str, ...]
    diagnostic_files_missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The structured result of one verification pass."""

    passed: bool
    failures: tuple[AssertionFailure, ...]
    per_sector: tuple[PerSectorSnapshot, ...]
    per_sector_findings: Mapping[Sector, int]
    total_wall_clock_seconds: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens_used: int
    total_retry_count: int
    budgets: BudgetThresholds


# ---------------------------------------------------------------------------
# Pure assertion helpers
# ---------------------------------------------------------------------------


def _check_brief_re_validates(
    sector: Sector, result: DomainResearcherResult
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    validation = validate_brief(result.brief)
    if not validation.is_valid:
        for ve in validation.errors:
            failures.append(
                AssertionFailure(
                    code="brief-re-validation-failed",
                    message=(
                        f"sector={sector.value} brief failed re-validation: "
                        f"{ve.field_path} ({ve.rule}): {ve.message}"
                    ),
                )
            )
    return failures


def _check_diagnostic_archive(
    archive_root: Path,
    invocation_id: str,
    agent_name: str,
) -> tuple[list[AssertionFailure], tuple[str, ...], tuple[str, ...]]:
    """Probe the harness's diagnostic-archive layout for one agent."""
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


def _build_per_sector_snapshot(
    sector: Sector,
    agent_name: str,
    result: DomainResearcherResult,
    *,
    archive_root: Path,
    invocation_id: str,
) -> tuple[PerSectorSnapshot, list[AssertionFailure]]:
    diag_failures, present, missing = _check_diagnostic_archive(
        archive_root, invocation_id, agent_name
    )
    snap = PerSectorSnapshot(
        sector=sector,
        agent_name=agent_name,
        wall_clock_seconds=result.wall_clock_seconds,
        input_tokens=result.tokens_used.input_tokens,
        output_tokens=result.tokens_used.output_tokens,
        cache_read_tokens=result.tokens_used.cache_read_tokens,
        cache_write_tokens=result.tokens_used.cache_write_tokens,
        retry_count=result.retry_count,
        finding_count=len(result.brief.findings),
        anomaly_count=len(result.brief.anomalies),
        thesis_candidate_count=len(result.brief.thesis_candidates),
        signal_quality=result.brief.signal_quality.value,
        diagnostic_files_present=present,
        diagnostic_files_missing=missing,
    )
    return snap, diag_failures


# ---------------------------------------------------------------------------
# Public entry: compute_report
# ---------------------------------------------------------------------------


_SECTOR_AGENT_PAIRS: tuple[tuple[Sector, str], ...] = (
    (Sector.TECH_SEMIS, AgentName.tech_semis_researcher.value),
    (Sector.FINANCIALS, AgentName.financials_researcher.value),
    (Sector.ENERGY, AgentName.energy_researcher.value),
)


def _result_for_sector(output: DomainResearchersOutput, sector: Sector) -> DomainResearcherResult:
    return {
        Sector.TECH_SEMIS: output.tech_semis,
        Sector.FINANCIALS: output.financials,
        Sector.ENERGY: output.energy,
    }[sector]


def compute_report(
    *,
    output: DomainResearchersOutput,
    archive_root: Path,
    budgets: BudgetThresholds,
) -> VerificationReport:
    """Run every structural assertion and return a :class:`VerificationReport`.

    The function does no I/O of its own beyond reading the diagnostic
    archive directory; every other dependency (the orchestrator output,
    the budget envelope) is a parameter.
    """
    failures: list[AssertionFailure] = []
    per_sector: list[PerSectorSnapshot] = []
    findings_by_sector: dict[Sector, int] = {}

    for sector, agent_name in _SECTOR_AGENT_PAIRS:
        result = _result_for_sector(output, sector)
        failures.extend(_check_brief_re_validates(sector, result))
        snap, diag_failures = _build_per_sector_snapshot(
            sector,
            agent_name,
            result,
            archive_root=archive_root,
            invocation_id=output.invocation_id,
        )
        failures.extend(diag_failures)
        per_sector.append(snap)
        findings_by_sector[sector] = len(result.brief.findings)

    total_input = sum(snap.input_tokens for snap in per_sector)
    total_output = sum(snap.output_tokens for snap in per_sector)
    total_tokens_used = total_input + total_output
    total_retry = sum(snap.retry_count for snap in per_sector)

    if output.total_wall_clock_seconds > budgets.wall_clock_seconds:
        failures.append(
            AssertionFailure(
                code="wall-clock-budget-exceeded",
                message=(
                    f"total wall-clock {output.total_wall_clock_seconds:.2f}s exceeds "
                    f"budget {budgets.wall_clock_seconds:.2f}s "
                    f"(loaded from config/agents.yaml)"
                ),
            )
        )

    if total_tokens_used > budgets.token_total:
        failures.append(
            AssertionFailure(
                code="token-budget-exceeded",
                message=(
                    f"total tokens used {total_tokens_used} exceeds "
                    f"budget {budgets.token_total} (loaded from config/agents.yaml)"
                ),
            )
        )

    return VerificationReport(
        passed=not failures,
        failures=tuple(failures),
        per_sector=tuple(per_sector),
        per_sector_findings=findings_by_sector,
        total_wall_clock_seconds=output.total_wall_clock_seconds,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_tokens_used=total_tokens_used,
        total_retry_count=total_retry,
        budgets=budgets,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


_BANNER = "=" * 70


def _render_per_sector_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Per-sector results ]"]
    header = (
        f"  {'sector':<14} {'wall_s':>8} {'in_tok':>8} {'out_tok':>8} "
        f"{'retry':>6} {'find':>5} {'anom':>5} {'thes':>5} {'quality':<10}"
    )
    lines.append(header)
    for snap in report.per_sector:
        lines.append(
            f"  {snap.sector.value:<14} {snap.wall_clock_seconds:>8.2f} "
            f"{snap.input_tokens:>8} {snap.output_tokens:>8} "
            f"{snap.retry_count:>6} {snap.finding_count:>5} {snap.anomaly_count:>5} "
            f"{snap.thesis_candidate_count:>5} {snap.signal_quality:<10}"
        )
    return lines


def _render_diagnostic_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Diagnostic archive ]"]
    for snap in report.per_sector:
        present_count = len(snap.diagnostic_files_present)
        total = len(_REQUIRED_DIAGNOSTIC_FILES)
        status = "OK" if not snap.diagnostic_files_missing else "MISSING"
        lines.append(f"  {snap.agent_name:<25} files={present_count}/{total} {status}")
        if snap.diagnostic_files_missing:
            lines.append(f"    missing: {', '.join(snap.diagnostic_files_missing)}")
    return lines


def _render_budget_section(report: VerificationReport) -> list[str]:
    return [
        "",
        "[ Budget envelope ]",
        (
            f"  wall_clock total = {report.total_wall_clock_seconds:.2f}s "
            f"(budget {report.budgets.wall_clock_seconds:.2f}s, loaded from agents.yaml)"
        ),
        (
            f"  tokens total = {report.total_tokens_used} "
            f"(budget {report.budgets.token_total}, loaded from agents.yaml)"
        ),
        f"  total retries = {report.total_retry_count}",
    ]


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
        "AlphaMind Domain-Researcher End-to-End Verification",
        _BANNER,
    ]
    lines.extend(_render_per_sector_section(report))
    lines.extend(_render_diagnostic_section(report))
    lines.extend(_render_budget_section(report))
    lines.extend(_render_failures_section(report))
    lines.extend(["", _BANNER, _render_result_line(report), _BANNER])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


# Researchers callable signature — the dependency-injection seam tests use
# instead of touching the SDK. Coroutine (vs Awaitable) so :func:`asyncio.run`
# accepts the return value directly.
ResearchersCallable = Callable[..., Coroutine[Any, Any, DomainResearchersOutput]]


def _format_invocation_id(now: datetime) -> str:
    """``YYYYMMDDTHHMMSSZ-verify-domain-researchers``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-verify-domain-researchers"


def run_verification(
    *,
    invocation_id: str,
    as_of: datetime,
    archive_root: Path,
    budgets: BudgetThresholds,
    researchers_fn: ResearchersCallable,
) -> int:
    """Run the researchers, build the report, print it, return an exit code.

    The caller supplies ``researchers_fn`` (either the production
    :func:`run_domain_researchers` wrapped with bound config, or a stub
    in tests). The function does I/O (prints to stdout) but every
    dependency is injected.

    On a passing run, the three sector briefs are dumped to the per-invocation
    stage-artifacts directory so downstream verification scripts can consume
    them via ``--upstream-from`` (ALP-287).
    """
    output = asyncio.run(researchers_fn(invocation_id=invocation_id, as_of=as_of))
    report = compute_report(
        output=output,
        archive_root=archive_root,
        budgets=budgets,
    )
    stage_dir: Path | None = None
    if report.passed:
        stage_dir = stage_artifacts_dir(archive_root, invocation_id)
        sector_briefs = (
            output.tech_semis.brief,
            output.financials.brief,
            output.energy.brief,
        )
        dump_sector_briefs(sector_briefs, stage_dir)
    print(format_report(report))
    if stage_dir is not None:
        print(f"[verify_domain_researchers] stage artifacts written to {stage_dir}")
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# Production binding for researchers_fn
# ---------------------------------------------------------------------------


def _bind_production_researchers(
    *,
    session: Session,
    distillation_outputs: DistillationOutputs,
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    archive_root: Path,
) -> ResearchersCallable:
    """Return a closure that calls :func:`run_domain_researchers` with bound deps."""

    async def _runner(*, invocation_id: str, as_of: datetime) -> DomainResearchersOutput:
        return await run_domain_researchers(
            invocation_id=invocation_id,
            as_of=as_of,
            distillation_outputs=distillation_outputs,
            session=session,
            agents_config=agents_config,
            sectors_config=sectors_config,
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
            "real-SDK domain-researcher verification cannot run without it. "
            "Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run."
        )
        raise RuntimeError(msg)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the exit code; never raises beyond auth."""
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description=(
            "Run the domain-researcher orchestrator end-to-end against a real "
            "CLAUDE_CODE_OAUTH_TOKEN and verify the structural conditions in ALP-186."
        )
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
            "scripts. Defaults to YYYYMMDDTHHMMSSZ-verify-domain-researchers."
        ),
    )
    parser.add_argument(
        "--upstream-from",
        type=Path,
        default=None,
        help=(
            "Path to a stage-artifacts directory produced by an earlier phase. "
            "When supplied, distillation_outputs.json is loaded from this directory "
            "instead of running the live distillation orchestrator (avoids "
            "double-spending phase-2 SDK tokens during an end-to-end run)."
        ),
    )
    args = parser.parse_args(argv)

    _check_oauth_token_set()

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)

    archive_root = args.archive_root if args.archive_root is not None else _default_archive_root()

    now = datetime.now(tz=UTC)
    invocation_id = args.invocation_id or _format_invocation_id(now)
    sectors_config = load_sectors_config()
    budgets = load_budget_thresholds()

    with factory() as session:
        if args.upstream_from is not None:
            distillation_outputs = load_distillation_outputs(args.upstream_from)
            print(
                f"[verify_domain_researchers] distillation_outputs loaded from "
                f"{args.upstream_from} (invocation={distillation_outputs.invocation_id})"
            )
        else:
            ticker_scope = load_universe_scope()
            distillation_config = load_distillation_config()
            # Project the Pydantic ``DistillationConfig`` boundary type onto
            # its frozen-dataclass mirror (ALP-471) — the orchestrator's
            # compute path consumes the dataclass form.
            distillation_outputs = asyncio.run(
                run_external_distillation(
                    session=session,
                    config=distillation_config.to_domain(),
                    ticker_scope=ticker_scope,
                    as_of=now,
                    invocation_id=invocation_id,
                    archive_root=archive_root,
                )
            )
        researchers_fn = _bind_production_researchers(
            session=session,
            distillation_outputs=distillation_outputs,
            agents_config=budgets.per_agent,
            sectors_config=sectors_config,
            archive_root=archive_root,
        )
        return run_verification(
            invocation_id=invocation_id,
            as_of=now,
            archive_root=archive_root,
            budgets=budgets,
            researchers_fn=researchers_fn,
        )


__all__ = [
    "BudgetThresholds",
    "PerSectorSnapshot",
    "ResearchersCallable",
    "VerificationReport",
    "compute_report",
    "format_report",
    "load_budget_thresholds",
    "load_sectors_config",
    "main",
    "run_verification",
]


if __name__ == "__main__":
    sys.exit(main())
