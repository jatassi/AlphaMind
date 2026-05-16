"""Portfolio-manager end-to-end live-SDK verification (ALP-331).

Operator entry point. Reads a recorded synthesizer text from a prior
``verify_synthesizer.py`` archive (per parent-issue ALP-117 decision —
fixture chained, not live composed), constructs four in-code scenario
inputs (normal, halt, emergency, synchronous_rejection) reusing the
strategist-verifier's ``PortfolioManagerView`` builders, and runs the PM
against the real Claude Agent SDK once per scenario. The verdict rubric
per scenario classifies each invocation as PASS / WARN / FAIL;
``--save-fixtures`` writes the parsed :class:`PMCompletionRecord` plus
the engine-stub's submission log to
``tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json``
so downstream pipeline-composition consumers can replay them.

The verdict rubric, the synthesizer-archive reader, and the four in-code
scenario builders live in this module so
``tests/scripts/test_verify_pm.py`` can exercise them without touching
the Anthropic API. The thin shim at ``scripts/verify_pm.py`` defers to
:func:`main` here.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind._kernel.regime import RiskZone
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName
from alphamind.decision.portfolio_manager.harness import HarnessFailure, SDKFailure
from alphamind.decision.portfolio_manager.runner import (
    PMResult,
    load_pm_agent_config,
    run_portfolio_manager,
)
from alphamind.decision.proposal_pre_processor.models import ProposalPreProcessorBundle
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
    PortfolioManagerView,
)
from alphamind.portfolio_state.consumers.strategist import StrategistView
from alphamind.portfolio_state.records.theses import ThesisComponent
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones,
    FeatureFlagsView,
    LibraryConfig,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import CrossConstraintImpact
from alphamind.scripts._artifact_io import load_retrieval_store, stage_artifacts_dir
from alphamind.scripts.verify_strategist import (
    _AS_OF as _STRATEGIST_AS_OF,
)
from alphamind.scripts.verify_strategist import (
    _PORTFOLIO_VALUE,
    build_fixture_defensive_posture_view,
    build_fixture_emergency_view,
    build_fixture_halt_state,
    build_fixture_library_config,
    build_fixture_market_inputs,
    build_fixture_normal_view,
    build_fixture_portfolio_state_snapshot,
    build_fixture_regime_transition_breach,
    build_fixture_sector_resolver,
    build_fixture_state_delivery_config,
)

__all__ = [
    "Verdict",
    "build_emergency_scenario_inputs",
    "build_halt_scenario_inputs",
    "build_normal_scenario_inputs",
    "build_synchronous_rejection_scenario_inputs",
    "describe_verdict_reason",
    "main",
    "read_retrieval_store",
    "read_synthesizer_text",
    "save_scenario_fixture",
    "verdict_for_failure",
    "verdict_for_success",
]


# ---------------------------------------------------------------------------
# Verdict rubric
# ---------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    """Verdict label printed in the operator report and mapped to exit code.

    PASS exits 0; WARN exits 0 (the fixture is still written, the operator
    decides whether to act on it); FAIL exits 1 (any
    :class:`HarnessFailure` subclass).
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def verdict_for_success(*, result: PMResult, scenario: str) -> Verdict:
    """Apply the per-scenario success-path rubric.

    PASS iff the run completed without producing any synchronous
    rejections in the engine-stub's submission log. The
    ``synchronous_rejection`` scenario inverts that rule: zero rejections
    surfaces as WARN — the scenario's purpose is to exercise the
    rejection feedback path, so an all-accepted outcome means the
    scenario fixture needs adjustment, not that the PM is broken.
    """
    rejections = _count_rejections(result.submission_log)
    if scenario == "synchronous_rejection":
        if rejections == 0:
            return Verdict.WARN
        return Verdict.PASS
    if rejections > 0:
        return Verdict.WARN
    return Verdict.PASS


def verdict_for_failure(failure: Exception) -> Verdict:
    """Apply the failure-path rubric: any HarnessFailure → FAIL."""
    del failure
    return Verdict.FAIL


def _count_rejections(submission_log: tuple[Any, ...]) -> int:
    """Count submission-log entries that produced at least one rejected command."""
    return sum(
        1
        for entry in submission_log
        for sub in entry.submission_results
        if sub.status == "rejected"
    )


def describe_verdict_reason(
    *,
    result: PMResult,
    scenario: str,
    verdict: Verdict,
) -> str:
    """Render a human-readable reason string for a verdict.

    Empty for PASS; for WARN/FAIL names the condition the rubric flagged
    so the operator knows what to inspect. Mirrors the strategist's
    failure-rendering shape but lifted out to a separate function so the
    PM's verdict reason can be threaded into both the per-scenario report
    and the synchronous_rejection-specific WARN.
    """
    if verdict is Verdict.PASS:
        return ""
    rejections = _count_rejections(result.submission_log)
    if scenario == "synchronous_rejection" and rejections == 0:
        return (
            "no rejection triggered — the synchronous_rejection scenario produced an "
            "all-accepted outcome; the scenario fixture or initial validation state needs "
            "adjustment so at least one command is rejected"
        )
    if rejections > 0:
        return (
            f"submission log contains {rejections} rejected command(s) — inspect the "
            f"fixture before treating it as a clean baseline"
        )
    return ""


# ---------------------------------------------------------------------------
# Synthesizer-archive reader
# ---------------------------------------------------------------------------


def read_synthesizer_text(
    *,
    archive_root: Path,
    synthesizer_invocation_id: str,
) -> str:
    """Read the synthesizer's recorded ``response.md`` from a prior archive.

    Path: ``<archive_root>/invocations/<inv_id>/analysis/synthesizer/response.md``.
    """
    response_path = (
        archive_root
        / INVOCATIONS_DIRNAME
        / synthesizer_invocation_id
        / "analysis"
        / "synthesizer"
        / "response.md"
    )
    try:
        return response_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"synthesizer response.md not found at {response_path}; "
            f"run scripts/verify_synthesizer.py with --archive-root {archive_root} "
            f"--invocation-id {synthesizer_invocation_id} first"
        ) from exc


def read_retrieval_store(
    *,
    archive_root: Path,
    synthesizer_invocation_id: str,
) -> RetrievalStore:
    """Read the synthesizer's recorded ``retrieval_store.json`` from a prior archive."""
    stage_dir = stage_artifacts_dir(archive_root, synthesizer_invocation_id)
    return load_retrieval_store(stage_dir)


# ---------------------------------------------------------------------------
# Pre-processor-bundle reader — reads the four checked-in fixtures
# ---------------------------------------------------------------------------


_PRE_PROCESSOR_FIXTURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "decision"
    / "proposal_pre_processor"
)


def _read_pre_processor_bundle(scenario_fixture: str) -> ProposalPreProcessorBundle:
    """Load the pre-processor bundle for a given scenario fixture name.

    Maps each of our four scenarios to one of the four checked-in
    pre-processor fixtures: ``normal``, ``halt``, ``emergency``,
    ``normal_with_breach`` (used by the synchronous_rejection scenario).
    """
    path = _PRE_PROCESSOR_FIXTURE_DIR / f"{scenario_fixture}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"pre-processor fixture not found at {path}; "
            "run scripts/verify_proposal_pre_processor.py --save-fixtures first"
        )
    return ProposalPreProcessorBundle.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# StrategistView → PortfolioManagerView lift
# ---------------------------------------------------------------------------


_DAILY_DRAWDOWN_LIMIT_PCT = 2.5  # mirrors strategist verifier's ActiveRiskParameterEntry value


def _ensure_pm_risk_budget_entries(strategist_view: StrategistView) -> StrategistView:
    """Extend a strategist view's risk budget with the daily-drawdown entry the PM
    header renderer requires.

    The strategist's renderer doesn't read ``daily_drawdown_pct`` from the
    risk budget, so the strategist verifier's fixture omits it. The PM
    header's drawdown-context block does, so we add an entry mirroring the
    daily-zone drawdown state already present on the view.
    """
    rb = strategist_view.risk_budget
    if rb.entry_by_rule_id("daily_drawdown_pct") is not None:
        return strategist_view
    daily_drawdown_pct = strategist_view.drawdown.intraday_drawdown_pct
    headroom = _DAILY_DRAWDOWN_LIMIT_PCT - daily_drawdown_pct
    daily_entry = RiskBudgetEntry(
        rule_id="daily_drawdown_pct",
        rule_label="Daily drawdown",
        current_value=daily_drawdown_pct,
        limit_value=_DAILY_DRAWDOWN_LIMIT_PCT,
        headroom=headroom,
        headroom_pct_of_limit=(headroom / _DAILY_DRAWDOWN_LIMIT_PCT) * 100.0,
        zone=strategist_view.drawdown.daily_zone or RiskZone.NORMAL,
        unit="% of equity",
        cumulative_invocation_impact_value=0.0,
    )
    extended = RiskBudgetConsumption(entries=(*rb.entries, daily_entry))
    return dataclasses.replace(strategist_view, risk_budget=extended)


def _empty_thesis_quality_aggregates() -> ThesisQualityAggregate:
    """Empty thesis-quality aggregate — no resolutions in the fixture history."""
    return ThesisQualityAggregate(
        as_of_timestamp=_STRATEGIST_AS_OF,
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
        signal_hit_rates=(),
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


def _lift_to_pm_view(strategist_view: StrategistView) -> PortfolioManagerView:
    """Lift a :class:`StrategistView` to a :class:`PortfolioManagerView`.

    The PM view extends the strategist view with two PM-only fields:
    ``thesis_quality_aggregates`` and ``position_modification_trail``.
    The strategist verifier's fixture builders supply position views with
    a ``modification_trail`` already; the PM view's
    ``position_modification_trail`` dict mirrors that data per-position.
    """
    extended = _ensure_pm_risk_budget_entries(strategist_view)
    trail: dict[str, tuple[Any, ...]] = {}
    for pv in extended.positions:
        if pv.modification_trail:
            trail[pv.position.position_id] = pv.modification_trail
    return PortfolioManagerView(
        positions=extended.positions,
        recent_thesis_resolutions=extended.recent_thesis_resolutions,
        portfolio_pnl=extended.portfolio_pnl,
        drawdown=extended.drawdown,
        sector_exposure=extended.sector_exposure,
        directional_exposure=extended.directional_exposure,
        risk_budget=extended.risk_budget,
        active_risk_parameters=extended.active_risk_parameters,
        intra_invocation_changelog=extended.intra_invocation_changelog,
        recent_pm_decision_log=extended.recent_pm_decision_log,
        abandoned_openings=extended.abandoned_openings,
        abandoned_actions=extended.abandoned_actions,
        thesis_quality_aggregates=_empty_thesis_quality_aggregates(),
        position_modification_trail=trail,
    )


# ---------------------------------------------------------------------------
# PM-side fixtures the strategist verifier doesn't expose
# ---------------------------------------------------------------------------


_AVAILABLE_FOR_NEW_POSITIONS_USD = 70_000.0
_DEFAULT_ACTIVE_SECTORS: frozenset[str] = frozenset({"tech", "semis", "financials", "energy"})


class _EmptyThesisComponentReader:
    """Empty reader — get_thesis_components returns () for any position id.

    The PM exercises the reader through the get_thesis_components MCP
    tool; the verification fixtures don't seed thesis components beyond
    the inline strategist view, so an empty stub is correct.
    """

    async def get_thesis_components(self, position_id: str) -> tuple[ThesisComponent, ...]:
        _ = position_id
        return ()


def _build_thesis_component_reader() -> PortfolioManagerThesisComponentReader:
    return _EmptyThesisComponentReader()


def _build_cross_constraint_impact() -> CrossConstraintImpact:
    """No-op cross-constraint impact — empty per_rule list."""
    return CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
        available_capital_after_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
    )


def _current_price_lookup(ticker: str) -> float:
    """Per-ticker current price covering the test universe."""
    prices = {
        "NVDA": 862.0,
        "MSFT": 420.0,
        "GOOGL": 175.0,
        "AAPL": 175.0,
        "JPM": 200.0,
        "XOM": 110.0,
    }
    return prices.get(ticker, 100.0)


# ---------------------------------------------------------------------------
# Tightened library-config for the synchronous_rejection scenario
# ---------------------------------------------------------------------------


def _build_tightened_library_config() -> LibraryConfig:
    """Library config with ``position_max_size_pct: 0.5`` — tight enough that the
    engine-stub's $1,000 token sizing (1% of $100k portfolio) breaches it.

    Per the parent issue's surfacing condition, the synchronous_rejection
    scenario MUST trigger at least one ``submit_envelope`` rejection. The
    submit_envelope wrapper re-validates each command via
    ``validate_guardrail`` with a token sizing of $1,000; tightening
    ``position_max_size_pct`` from 5.0 (the strategist verifier's default)
    to 0.5 ensures every constructive command (OPEN/ADD) trips the rule
    and is rejected.
    """
    base = build_fixture_library_config()
    tight_limits = dict(base.effective_limits)
    tight_limits["position_max_size_pct"] = 0.5
    zones = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
    return LibraryConfig(
        effective_limits=MappingProxyType(tight_limits),
        escalation_zones=MappingProxyType({k: zones for k in tight_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=base.feature_flags.options_enabled,
            short_selling_enabled=base.feature_flags.short_selling_enabled,
        ),
        active_sectors=base.active_sectors,
        active_regime=base.active_regime,
        active_profile=base.active_profile,
        conservative_buffer_pct=base.conservative_buffer_pct,
    )


# ---------------------------------------------------------------------------
# Scenario-builder common kwargs
# ---------------------------------------------------------------------------


def _common_runner_kwargs(  # noqa: PLR0913 — parametric kwargs assembly
    *,
    mode: Literal["normal", "halt"],
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    pm_view: PortfolioManagerView,
    library_config: LibraryConfig,
    invocation_id: str,
    timestamp: datetime,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> dict[str, Any]:
    """Collect the kwargs every scenario shares — extracted to keep each
    builder focused on its scenario-specific deltas."""
    return {
        "mode": mode,
        "pre_processor_bundle": pre_processor_bundle,
        "synthesizer_text": synthesizer_text,
        "retrieval_store": retrieval_store,
        "pm_view": pm_view,
        "thesis_component_reader": _build_thesis_component_reader(),
        "risk_budget": pm_view.risk_budget,
        "active_risk_parameters": pm_view.active_risk_parameters,
        "profile_feature_flags": library_config.feature_flags,
        "library_config": library_config,
        "library_market": build_fixture_market_inputs(),
        "sector_resolver": build_fixture_sector_resolver(),
        "portfolio_state_snapshot": build_fixture_portfolio_state_snapshot(),
        "active_sectors": _DEFAULT_ACTIVE_SECTORS,
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "state_delivery_config": build_fixture_state_delivery_config(),
        "options_enabled": library_config.feature_flags.options_enabled,
        "short_selling_enabled": library_config.feature_flags.short_selling_enabled,
        "total_portfolio_value_usd": _PORTFOLIO_VALUE,
        "available_for_new_positions_usd": _AVAILABLE_FOR_NEW_POSITIONS_USD,
        "cross_constraint_impact": _build_cross_constraint_impact(),
        "prior_health_snapshots": prior_health_snapshots,
    }


# ---------------------------------------------------------------------------
# Four scenario builders
# ---------------------------------------------------------------------------


def build_normal_scenario_inputs(
    *,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    invocation_id: str = "verify-pm-normal",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Normal scenario — 4 positions across 3 sectors, no halt, no breach.

    Reuses the strategist verifier's ``build_fixture_normal_view`` (4
    positions: NVDA, JPM, XOM, AAPL) and lifts to :class:`PortfolioManagerView`.
    Pairs with the pre-processor's ``normal.json`` fixture.
    """
    strategist_view, prior_health_snapshots = build_fixture_normal_view()
    pm_view = _lift_to_pm_view(strategist_view)
    return _common_runner_kwargs(
        mode="normal",
        pre_processor_bundle=_read_pre_processor_bundle("normal"),
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        pm_view=pm_view,
        library_config=build_fixture_library_config(),
        invocation_id=invocation_id,
        timestamp=timestamp or _STRATEGIST_AS_OF,
        prior_health_snapshots=prior_health_snapshots,
    )


def build_halt_scenario_inputs(
    *,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    invocation_id: str = "verify-pm-halt",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Halt scenario — 6 positions, daily drawdown active, halt-mode rendering.

    Reuses the strategist verifier's ``build_fixture_defensive_posture_view``
    (6 positions; the engine-originated MSFT close is in the changelog).
    Pairs with the pre-processor's ``halt.json`` fixture.
    """
    strategist_view, prior_health_snapshots = build_fixture_defensive_posture_view()
    pm_view = _lift_to_pm_view(strategist_view)
    kwargs = _common_runner_kwargs(
        mode="halt",
        pre_processor_bundle=_read_pre_processor_bundle("halt"),
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        pm_view=pm_view,
        library_config=build_fixture_library_config(),
        invocation_id=invocation_id,
        timestamp=timestamp or _STRATEGIST_AS_OF,
        prior_health_snapshots=prior_health_snapshots,
    )
    kwargs["halt_state"] = build_fixture_halt_state()
    kwargs["current_price_lookup"] = _current_price_lookup
    return kwargs


def build_emergency_scenario_inputs(
    *,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    invocation_id: str = "verify-pm-emergency",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Emergency scenario — regime tightened, NVDA position over the new limit.

    Reuses the strategist verifier's ``build_fixture_emergency_view`` (4
    positions, NVDA at 4.5%) and threads
    ``build_fixture_regime_transition_breach`` so the PM sees the
    regime-transition breach record. Pairs with the pre-processor's
    ``emergency.json`` fixture.
    """
    strategist_view, prior_health_snapshots = build_fixture_emergency_view()
    pm_view = _lift_to_pm_view(strategist_view)
    kwargs = _common_runner_kwargs(
        mode="normal",
        pre_processor_bundle=_read_pre_processor_bundle("emergency"),
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        pm_view=pm_view,
        library_config=build_fixture_library_config(),
        invocation_id=invocation_id,
        timestamp=timestamp or _STRATEGIST_AS_OF,
        prior_health_snapshots=prior_health_snapshots,
    )
    kwargs["regime_transition_breaches"] = (build_fixture_regime_transition_breach(),)
    return kwargs


def build_synchronous_rejection_scenario_inputs(
    *,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    invocation_id: str = "verify-pm-synchronous-rejection",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Synchronous-rejection scenario — tightened limits force at least one rejection.

    The pre-processor's ``normal_with_breach.json`` fixture carries three
    REC-N analyst recommendations the PM would normally approve. The
    library config is tightened so ``position_max_size_pct = 0.5`` —
    tight enough that the engine-stub's $1,000 token sizing (1% of $100k
    portfolio) breaches the rule for every constructive command. The PM
    is expected to receive at least one synchronous rejection and exercise
    the post-rejection modification path.
    """
    strategist_view, prior_health_snapshots = build_fixture_normal_view()
    pm_view = _lift_to_pm_view(strategist_view)
    return _common_runner_kwargs(
        mode="normal",
        pre_processor_bundle=_read_pre_processor_bundle("normal_with_breach"),
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        pm_view=pm_view,
        library_config=_build_tightened_library_config(),
        invocation_id=invocation_id,
        timestamp=timestamp or _STRATEGIST_AS_OF,
        prior_health_snapshots=prior_health_snapshots,
    )


def save_scenario_fixture(
    *,
    scenario: str,
    result: PMResult,
    verdict: Verdict,
    fixtures_dir: Path,
) -> None:
    """Write the fixture for *scenario* under *fixtures_dir*.

    Schema: ``{"completion_record": {...PMCompletionRecord...},
    "submission_log": [{"envelope": ..., "submission_results": [...]}],
    "scenario_metadata": {"name": ..., "verdict": ..., "tokens_used": ...,
    "wall_clock_seconds": ...}}``. Pretty-printed (``indent=2``) so diffs
    review cleanly.
    """
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "completion_record": result.output.model_dump(mode="json"),
        "submission_log": [
            {
                "envelope": entry.envelope.model_dump(mode="json"),
                "submission_results": [
                    sub.model_dump(mode="json") for sub in entry.submission_results
                ],
            }
            for entry in result.submission_log
        ],
        "scenario_metadata": {
            "name": scenario,
            "verdict": verdict.value,
            "tokens_used": result.tokens_used.model_dump(),
            "wall_clock_seconds": result.wall_clock_seconds,
            "tool_calls_used": result.tool_calls_used,
            "retry_count": result.retry_count,
            "stop_reason": result.stop_reason,
        },
    }
    (fixtures_dir / f"{scenario}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Operator-report rendering
# ---------------------------------------------------------------------------


_BANNER = "=== Portfolio Manager live-SDK verification ==="


def _render_success_report(
    *,
    scenario: str,
    invocation_id: str,
    model: str,
    result: PMResult,
    verdict: Verdict,
    reason: str,
) -> str:
    output = result.output
    rejections = _count_rejections(result.submission_log)
    lines = [
        _BANNER,
        f"scenario: {scenario}",
        f"invocation_id: {invocation_id}",
        f"model: {model}",
        (
            f"tokens: input={result.tokens_used.input_tokens} "
            f"output={result.tokens_used.output_tokens} "
            f"cache_read={result.tokens_used.cache_read_tokens} "
            f"cache_write={result.tokens_used.cache_write_tokens}"
        ),
        f"retry_count: {result.retry_count}",
        f"tool_calls_used: {result.tool_calls_used}",
        f"envelopes_submitted: {output.envelopes_submitted}",
        (
            f"verdict_summary: approve={output.verdict_summary.approve} "
            f"approve_with_modification={output.verdict_summary.approve_with_modification} "
            f"reject={output.verdict_summary.reject}"
        ),
        f"submission_log_rejections: {rejections}",
        f"--- Verdict: {verdict.value} ---",
    ]
    if reason:
        lines.append(f"reason: {reason}")
    return "\n".join(lines) + "\n"


def _render_failure_report(
    *,
    scenario: str,
    invocation_id: str,
    model: str,
    failure: Exception,
    verdict: Verdict,
) -> str:
    return (
        "\n".join(
            [
                _BANNER,
                f"scenario: {scenario}",
                f"invocation_id: {invocation_id}",
                f"model: {model}",
                "",
                "--- Harness failure ---",
                f"type: {type(failure).__name__}",
                f"agent_name: {getattr(failure, 'agent_name', 'portfolio_manager')}",
                f"invocation_id: {getattr(failure, 'invocation_id', invocation_id)}",
                f"message: {failure}",
                "",
                f"--- Verdict: {verdict.value} ---",
            ]
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------


def _check_oauth_token_set(invocation_id: str) -> None:
    """Surface missing CLAUDE_CODE_OAUTH_TOKEN as a clean SDKFailure."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise SDKFailure(
            "CLAUDE_CODE_OAUTH_TOKEN is not set in the environment. The "
            "real-SDK PM verification cannot run without it. "
            "Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run.",
            agent_name=AgentName.portfolio_manager.value,
            invocation_id=invocation_id,
        )


# ---------------------------------------------------------------------------
# Scenario dispatch
# ---------------------------------------------------------------------------


_SCENARIOS: tuple[str, ...] = ("normal", "halt", "emergency", "synchronous_rejection")

_ScenarioBuilder = Callable[..., dict[str, Any]]
_SCENARIO_BUILDERS: dict[str, _ScenarioBuilder] = {
    "normal": build_normal_scenario_inputs,
    "halt": build_halt_scenario_inputs,
    "emergency": build_emergency_scenario_inputs,
    "synchronous_rejection": build_synchronous_rejection_scenario_inputs,
}

_SdkQueryFn = Callable[..., AsyncIterator[Any]]


def _format_invocation_id(now: datetime, scenario: str) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-verify-pm-{scenario}"


async def _invoke_scenario(
    *,
    scenario: str,
    invocation_id: str,
    timestamp: datetime,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    archive_root: Path | None,
    sdk_query_fn: _SdkQueryFn | None,
) -> PMResult:
    """Build inputs for *scenario* and call ``run_portfolio_manager``."""
    builder = _SCENARIO_BUILDERS[scenario]
    inputs = builder(
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        invocation_id=invocation_id,
        timestamp=timestamp,
    )
    return await run_portfolio_manager(
        **inputs,
        archive_root=archive_root,
        sdk_query_fn=sdk_query_fn,
    )


def _run_one_scenario(
    *,
    scenario: str,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    archive_root: Path,
    now: datetime,
    sdk_query_fn: _SdkQueryFn | None,
    model: str,
) -> tuple[Verdict, PMResult | None]:
    """Run a single scenario; return verdict and parsed result (or None on FAIL)."""
    invocation_id = _format_invocation_id(now, scenario)
    print(
        f"=== Portfolio Manager live-SDK verification: scenario={scenario}, "
        f"invocation_id={invocation_id}, model={model} ===\nInvoking SDK...",
        flush=True,
    )
    try:
        result = asyncio.run(
            _invoke_scenario(
                scenario=scenario,
                invocation_id=invocation_id,
                timestamp=now,
                synthesizer_text=synthesizer_text,
                retrieval_store=retrieval_store,
                archive_root=archive_root,
                sdk_query_fn=sdk_query_fn,
            )
        )
    except HarnessFailure as failure:
        verdict = verdict_for_failure(failure)
        print(
            _render_failure_report(
                scenario=scenario,
                invocation_id=invocation_id,
                model=model,
                failure=failure,
                verdict=verdict,
            )
        )
        return verdict, None

    verdict = verdict_for_success(result=result, scenario=scenario)
    reason = describe_verdict_reason(result=result, scenario=scenario, verdict=verdict)
    print(
        _render_success_report(
            scenario=scenario,
            invocation_id=invocation_id,
            model=model,
            result=result,
            verdict=verdict,
            reason=reason,
        )
    )
    return verdict, result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the portfolio manager end-to-end against a real CLAUDE_CODE_OAUTH_TOKEN "
            "for four scenarios (normal, halt, emergency, synchronous_rejection), reading "
            "the brief from a recorded synthesizer archive."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--synthesizer-invocation-id", type=str, required=True)
    parser.add_argument("--save-fixtures", action="store_true", default=False)
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("tests/fixtures/decision/pm"),
    )
    parser.add_argument(
        "--scenario",
        choices=(*_SCENARIOS, "all"),
        default="all",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    sdk_query_fn: _SdkQueryFn | None = None,
) -> int:
    """CLI entry point. Returns 0 on all-PASS/WARN, 1 if any scenario FAILed.

    A FAIL on one scenario does NOT short-circuit the loop — every
    scenario runs so the operator gets a full picture before deciding
    next steps. ``--save-fixtures`` writes the fixture file for every
    scenario that returned a non-FAIL verdict.

    The optional ``sdk_query_fn`` parameter is dependency injection for
    tests: when supplied, the runner uses it instead of the real SDK.
    Operator runs leave it ``None`` so the real Anthropic API is hit.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    agent_config = load_pm_agent_config()
    model = str(agent_config.model)

    pre_flight_id = _format_invocation_id(now, "preflight")
    if sdk_query_fn is None:
        try:
            _check_oauth_token_set(pre_flight_id)
        except HarnessFailure as failure:
            verdict = verdict_for_failure(failure)
            print(
                _render_failure_report(
                    scenario="preflight",
                    invocation_id=pre_flight_id,
                    model=model,
                    failure=failure,
                    verdict=verdict,
                )
            )
            return 1

    synthesizer_text = read_synthesizer_text(
        archive_root=args.archive_root,
        synthesizer_invocation_id=args.synthesizer_invocation_id,
    )
    retrieval_store = read_retrieval_store(
        archive_root=args.archive_root,
        synthesizer_invocation_id=args.synthesizer_invocation_id,
    )

    scenarios: tuple[str, ...] = _SCENARIOS if args.scenario == "all" else (args.scenario,)
    verdicts: dict[str, Verdict] = {}
    results: dict[str, PMResult] = {}

    for scenario in scenarios:
        verdict, result = _run_one_scenario(
            scenario=scenario,
            synthesizer_text=synthesizer_text,
            retrieval_store=retrieval_store,
            archive_root=args.archive_root,
            now=now,
            sdk_query_fn=sdk_query_fn,
            model=model,
        )
        verdicts[scenario] = verdict
        if result is not None:
            results[scenario] = result

    if args.save_fixtures:
        for scenario, result in results.items():
            save_scenario_fixture(
                scenario=scenario,
                result=result,
                verdict=verdicts[scenario],
                fixtures_dir=args.fixtures_dir,
            )
        if results:
            print(
                f"[verify_pm] fixtures written to {args.fixtures_dir}: "
                + ", ".join(f"{s}.json" for s in results),
                flush=True,
            )

    summary = " | ".join(f"{s}: {v.value}" for s, v in verdicts.items())
    print(f"=== Summary: {summary} ===")
    return 1 if any(v is Verdict.FAIL for v in verdicts.values()) else 0
