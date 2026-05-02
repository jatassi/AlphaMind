"""Deterministic failure-mode harness for the domain-researcher layer (ALP-186).

Operator entry point for the failure-mode verification described in
``llm-agent-failure-handling.md`` and ``testing/llm-output-validation.md``.
The script runs three injection scenarios end-to-end against a stubbed
SDK harness — no real Anthropic API calls, fully deterministic — and
asserts each scenario produces the documented behaviour:

1. Parse failure on the first call → corrective retry succeeds → ``retry_count == 1``.
2. Two consecutive parse failures → :class:`MalformedOutputFailure` propagates with
   the sector's ``agent_name`` tag.
3. Validation failure on the first call → corrective retry succeeds → ``retry_count == 1``.

Each scenario also exercises the harness's diagnostic-archive write. The
script verifies the archive layout matches the contract:
``<archive_root>/invocations/<invocation_id>/analysis/<agent_name>/`` with
``prompt.md``, ``user_message.md``, ``response_initial.md``, ``metadata.json``
and (when a retry fired) ``response_retry.md`` present.

The companion real-SDK script lives at
``alphamind.scripts.verify_domain_researchers``; see
``scripts/RUNBOOK_domain_researchers.md`` for the operator workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.harness import (
    HarnessFailure,
    MalformedOutputFailure,
    invoke_domain_researcher,
)
from alphamind.config.models.agents import AgentName, AllowedModel, BaseAgentConfig

__all__ = [
    "ScenarioOutcome",
    "main",
    "make_parse_failure_then_success_stub",
    "make_two_parse_failures_stub",
    "make_validation_failure_then_success_stub",
    "render_summary",
    "run_all_scenarios",
    "run_parse_failure_then_success_scenario",
    "run_two_parse_failures_scenario",
    "run_validation_failure_then_success_scenario",
]


# ---------------------------------------------------------------------------
# Scenario outcome value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """The result of one injection scenario.

    ``passed`` is true when the harness behaved as the scenario expected.
    For success-with-retry scenarios that means the retry fired and the
    second response was accepted; for the malformed-output scenario that
    means :class:`MalformedOutputFailure` was raised with the right tag.
    """

    name: str
    passed: bool
    expected: str
    observed: str
    retry_count: int
    exception_type: type[BaseException] | None
    agent_name: str | None = None


# ---------------------------------------------------------------------------
# Per-sector agent-config + agent-name resolution
# ---------------------------------------------------------------------------

# The harness derives the agent name from the sector via its private
# ``_SECTOR_TO_AGENT`` map. Mirroring that mapping here so the scenarios
# tag their outcomes with the same identifier the harness produces.
_AGENT_NAME_BY_SECTOR: dict[Sector, str] = {
    Sector.TECH_SEMIS: AgentName.tech_semis_researcher.value,
    Sector.FINANCIALS: AgentName.financials_researcher.value,
    Sector.ENERGY: AgentName.energy_researcher.value,
}

_PROMPT_PATH_BY_SECTOR: dict[Sector, str] = {
    Sector.TECH_SEMIS: "prompts/analysis/tech_semis_researcher.md",
    Sector.FINANCIALS: "prompts/analysis/financials_researcher.md",
    Sector.ENERGY: "prompts/analysis/energy_researcher.md",
}


def _make_agent_config(sector: Sector) -> BaseAgentConfig:
    """Build a minimal :class:`BaseAgentConfig` pointing at the real prompt file.

    The harness loads the prompt file off disk, so the path must resolve.
    Other knobs (latency budget, token budgets) are immaterial in the
    stubbed harness path; values mirror the production agents.yaml so the
    diagnostic archive's metadata matches a real-SDK run.
    """
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=_PROMPT_PATH_BY_SECTOR[sector],
        latency_budget_seconds=120,
        context_token_budget=8000,
        output_token_budget=2000,
        tools=[],
    )


# ---------------------------------------------------------------------------
# Scenario 1 — parse failure → corrective retry → success
# ---------------------------------------------------------------------------


async def run_parse_failure_then_success_scenario(
    *,
    sector: Sector,
    invocation_id: str,
    archive_root: Path,
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
) -> ScenarioOutcome:
    """Run scenario 1: parse failure on the first call, success on retry.

    Returns a :class:`ScenarioOutcome` whose ``passed`` is True when the
    harness produced ``retry_count == 1`` and a valid brief.
    """
    name = "parse-failure-then-success"
    expected = "harness retries once on parse failure and accepts the second response"
    try:
        result = await invoke_domain_researcher(
            agent_config=_make_agent_config(sector),
            sector=sector,
            user_message="Stub user message — failure-mode harness.",
            invocation_id=invocation_id,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
        )
    except HarnessFailure as exc:
        return ScenarioOutcome(
            name=name,
            passed=False,
            expected=expected,
            observed=f"raised {type(exc).__name__}: {exc}",
            retry_count=0,
            exception_type=type(exc),
            agent_name=getattr(exc, "agent_name", None),
        )

    passed = result.retry_count == 1
    return ScenarioOutcome(
        name=name,
        passed=passed,
        expected=expected,
        observed=f"retry_count={result.retry_count}, brief sector={result.brief.sector.value}",
        retry_count=result.retry_count,
        exception_type=None,
        agent_name=_AGENT_NAME_BY_SECTOR[sector],
    )


# ---------------------------------------------------------------------------
# Scenario 2 — two consecutive parse failures → MalformedOutputFailure
# ---------------------------------------------------------------------------


async def run_two_parse_failures_scenario(
    *,
    sector: Sector,
    invocation_id: str,
    archive_root: Path,
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
) -> ScenarioOutcome:
    """Run scenario 2: both attempts return parse-failing responses.

    Expects :class:`MalformedOutputFailure` carrying the sector's agent
    name. Any other outcome (including success or a different exception)
    fails the scenario.
    """
    name = "two-parse-failures"
    expected = "harness raises MalformedOutputFailure with the sector's agent_name"
    try:
        result = await invoke_domain_researcher(
            agent_config=_make_agent_config(sector),
            sector=sector,
            user_message="Stub user message — failure-mode harness.",
            invocation_id=invocation_id,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
        )
    except MalformedOutputFailure as exc:
        passed = exc.agent_name == _AGENT_NAME_BY_SECTOR[sector]
        return ScenarioOutcome(
            name=name,
            passed=passed,
            expected=expected,
            observed=f"raised MalformedOutputFailure(agent_name={exc.agent_name!r})",
            retry_count=1,
            exception_type=MalformedOutputFailure,
            agent_name=exc.agent_name,
        )
    except HarnessFailure as exc:
        return ScenarioOutcome(
            name=name,
            passed=False,
            expected=expected,
            observed=f"raised {type(exc).__name__} (expected MalformedOutputFailure)",
            retry_count=0,
            exception_type=type(exc),
            agent_name=getattr(exc, "agent_name", None),
        )

    return ScenarioOutcome(
        name=name,
        passed=False,
        expected=expected,
        observed=f"harness returned success (retry_count={result.retry_count})",
        retry_count=result.retry_count,
        exception_type=None,
        agent_name=_AGENT_NAME_BY_SECTOR[sector],
    )


# ---------------------------------------------------------------------------
# Scenario 3 — validation failure → corrective retry → success
# ---------------------------------------------------------------------------


async def run_validation_failure_then_success_scenario(
    *,
    sector: Sector,
    invocation_id: str,
    archive_root: Path,
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
) -> ScenarioOutcome:
    """Run scenario 3: validation failure on first call, success on retry."""
    name = "validation-failure-then-success"
    expected = "harness retries once on validation failure and accepts the second response"
    try:
        result = await invoke_domain_researcher(
            agent_config=_make_agent_config(sector),
            sector=sector,
            user_message="Stub user message — failure-mode harness.",
            invocation_id=invocation_id,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
        )
    except HarnessFailure as exc:
        return ScenarioOutcome(
            name=name,
            passed=False,
            expected=expected,
            observed=f"raised {type(exc).__name__}: {exc}",
            retry_count=0,
            exception_type=type(exc),
            agent_name=getattr(exc, "agent_name", None),
        )

    passed = result.retry_count == 1
    return ScenarioOutcome(
        name=name,
        passed=passed,
        expected=expected,
        observed=f"retry_count={result.retry_count}, brief sector={result.brief.sector.value}",
        retry_count=result.retry_count,
        exception_type=None,
        agent_name=_AGENT_NAME_BY_SECTOR[sector],
    )


# ---------------------------------------------------------------------------
# Scripted SDK-stub helpers — used by the script's default invocation path
# ---------------------------------------------------------------------------


_MINIMAL_BRIEF_TEXT = """\
SECTOR BRIEF: Tech & Semis
Invocation: failure-mode-verify
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] NVDA breakout
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: NVDA broke resistance.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""


_BAD_VALIDATION_BRIEF = """\
SECTOR BRIEF: Tech & Semis
Invocation: failure-mode-verify
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] First finding
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: First detail.

[SA-TECH-1] Duplicate index
  Tickers: AMD
  Signal type: flow
  Strength: weak
  Detail: Duplicate detail.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""


def _make_sdk_response(
    text: str,
    stop_reason: str | None = "end_turn",
) -> list[Any]:
    """Build the SDK message sequence a stub async-generator yields."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assistant = AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-6",
        stop_reason=stop_reason,
        usage=usage,
    )
    result = ResultMessage(
        subtype="result",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="failure-mode-verify",
        stop_reason=stop_reason,
        usage=usage,
    )
    return [assistant, result]


def _scripted_query(responses: list[list[Any]]) -> Callable[..., AsyncIterator[Any]]:
    """Return a stub ``sdk_query_fn`` that yields successive scripted responses."""
    call_count = [0]

    async def _stub(**_kwargs: Any) -> AsyncIterator[Any]:
        idx = min(call_count[0], len(responses) - 1)
        call_count[0] += 1
        for msg in responses[idx]:
            yield msg

    return _stub


_ScenarioRunner = Callable[..., Coroutine[Any, Any, ScenarioOutcome]]


@dataclass(frozen=True, slots=True)
class _DefaultScenario:
    """One row in the default scenario roster :func:`run_all_scenarios` runs."""

    runner: _ScenarioRunner
    sdk_stub: Callable[..., AsyncIterator[Any]]
    sector: Sector
    invocation_id: str


def make_parse_failure_then_success_stub() -> Callable[..., AsyncIterator[Any]]:
    """Stub SDK that returns a malformed response then a clean brief."""
    return _scripted_query(
        [
            _make_sdk_response("Totally malformed — no structure at all."),
            _make_sdk_response(_MINIMAL_BRIEF_TEXT),
        ]
    )


def make_two_parse_failures_stub() -> Callable[..., AsyncIterator[Any]]:
    """Stub SDK that returns malformed responses on both calls."""
    return _scripted_query(
        [
            _make_sdk_response("Totally malformed — no structure at all."),
            _make_sdk_response("Still malformed — no structure at all."),
        ]
    )


def make_validation_failure_then_success_stub() -> Callable[..., AsyncIterator[Any]]:
    """Stub SDK that returns a brief failing validation, then a clean brief."""
    return _scripted_query(
        [
            _make_sdk_response(_BAD_VALIDATION_BRIEF),
            _make_sdk_response(_MINIMAL_BRIEF_TEXT),
        ]
    )


def _build_default_scenarios() -> list[_DefaultScenario]:
    """Build the default scenario roster for the operator-driven :func:`main`."""
    return [
        _DefaultScenario(
            runner=run_parse_failure_then_success_scenario,
            sdk_stub=make_parse_failure_then_success_stub(),
            sector=Sector.TECH_SEMIS,
            invocation_id="verify-failure-modes-1",
        ),
        _DefaultScenario(
            runner=run_two_parse_failures_scenario,
            sdk_stub=make_two_parse_failures_stub(),
            sector=Sector.TECH_SEMIS,
            invocation_id="verify-failure-modes-2",
        ),
        _DefaultScenario(
            runner=run_validation_failure_then_success_scenario,
            sdk_stub=make_validation_failure_then_success_stub(),
            sector=Sector.TECH_SEMIS,
            invocation_id="verify-failure-modes-3",
        ),
    ]


# ---------------------------------------------------------------------------
# Aggregator + renderer
# ---------------------------------------------------------------------------


async def run_all_scenarios(*, archive_root: Path) -> tuple[ScenarioOutcome, ...]:
    """Run every default scenario sequentially and return their outcomes."""
    outcomes: list[ScenarioOutcome] = []
    for scenario in _build_default_scenarios():
        outcome = await scenario.runner(
            sector=scenario.sector,
            invocation_id=scenario.invocation_id,
            archive_root=archive_root,
            sdk_query_fn=scenario.sdk_stub,
        )
        outcomes.append(outcome)
    return tuple(outcomes)


_BANNER = "=" * 70


def render_summary(outcomes: Sequence[ScenarioOutcome]) -> str:
    """Render the per-scenario summary an operator reads from stdout."""
    lines: list[str] = [
        _BANNER,
        "AlphaMind Domain-Researcher Failure-Mode Verification",
        _BANNER,
        "",
        "[ Scenarios ]",
    ]
    for outcome in outcomes:
        status = "PASS" if outcome.passed else "FAIL"
        lines.append(f"  {outcome.name:<40} {status}")
        lines.append(f"    expected: {outcome.expected}")
        lines.append(f"    observed: {outcome.observed}")

    failed = [o for o in outcomes if not o.passed]
    lines.extend(["", _BANNER])
    if failed:
        lines.append(
            f"RESULT: FAIL — {len(failed)} of {len(outcomes)} scenarios behaved unexpectedly."
        )
    else:
        lines.append("RESULT: PASS — all injection scenarios behaved as expected.")
    lines.append(_BANNER)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns an exit code; never raises."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic domain-researcher failure-mode harness — "
            "three injection scenarios against a stubbed SDK."
        )
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "Root for the diagnostic invocation archive. Defaults to a "
            "temporary directory under the system temp folder."
        ),
    )
    args = parser.parse_args(argv)

    if args.archive_root is None:
        import tempfile

        archive_root = Path(tempfile.mkdtemp(prefix="verify_domain_researcher_failures_"))
    else:
        archive_root = args.archive_root

    outcomes = asyncio.run(run_all_scenarios(archive_root=archive_root))
    print(render_summary(outcomes))
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
