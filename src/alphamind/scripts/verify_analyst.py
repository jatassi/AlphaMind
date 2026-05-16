"""Analyst end-to-end live-SDK verification (ALP-300).

Operator entry point. Reads a recorded synthesizer text from a prior
``verify_synthesizer.py`` archive (per parent-issue decision C — fixture
chained, not live composed), reconstructs the analyst's input bundle,
and runs the analyst against the real Claude Agent SDK twice — once in
normal mode, once in halt (watchlist) mode (per parent-issue decision
D). The verdict rubric per scenario classifies each invocation as
PASS / WARN / FAIL; ``--save-fixtures`` writes both parsed
:class:`AnalystOutput` payloads to ``tests/fixtures/decision/analyst/``
so downstream feature trees (strategist, proposal pre-processor, PM
verifiers) can consume them as fixtures.

The verdict rubric and the synthesizer-archive reader live in this
module so ``tests/scripts/test_verify_analyst.py`` can exercise them
without touching the Anthropic API. The thin shim at
``scripts/verify_analyst.py`` defers to :func:`main` here.
"""

from __future__ import annotations

import argparse
import asyncio
import enum
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.decision.analyst.harness import (
    HarnessFailure,
    SDKFailure,
)
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.analyst.runner import (
    AnalystResult,
    load_analyst_agent_config,
    run_analyst,
)
from alphamind.decision.analyst.validation import (
    ValidationResult,
    validate_analyst_output,
)
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAvailableCapital,
    AnalystView,
)
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.scripts._artifact_io import load_retrieval_store, stage_artifacts_dir
from alphamind.scripts._stdio import configure_utf8_stdio

__all__ = [
    "Verdict",
    "build_fixture_active_risk_parameters",
    "build_fixture_analyst_view",
    "build_fixture_halt_state",
    "build_fixture_library_config",
    "build_fixture_market_inputs",
    "build_fixture_portfolio_state_snapshot",
    "build_fixture_risk_budget",
    "build_fixture_sector_resolver",
    "build_fixture_state_delivery_config",
    "main",
    "read_synthesizer_text",
    "save_fixtures",
    "verdict_for_failure",
    "verdict_for_success",
]


_AGENT_NAME = AgentName.analyst.value


# ---------------------------------------------------------------------------
# Verdict rubric
# ---------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    """Verdict label printed in the operator report and mapped to exit code.

    PASS and WARN both exit 0 — WARN is a prompt-tightening signal, not a
    failure. FAIL exits non-zero (any :class:`HarnessFailure` subclass).
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def verdict_for_success(
    *,
    result: AnalystResult,
    validation: ValidationResult,
    mode: Literal["normal", "watchlist"],
) -> Verdict:
    """Apply the per-scenario success-path rubric.

    Normal mode:
    - WARN if any Layer-2/3 invariant failed (``is_valid=False``).
    - WARN if non-empty recommendations were emitted with zero tool calls
      — the analyst is contracted to call ``validate_guardrail`` at least
      once per emitted recommendation.
    - PASS otherwise (including the explicit empty-recommendations case).

    Halt (watchlist) mode:
    - WARN if any Layer-2/3 invariant failed.
    - WARN if any tool call was recorded — halt-mode header instructs the
      analyst not to invoke ``validate_guardrail`` (the watchlist mode
      emits no proposals).
    - PASS otherwise. Watchlist-entry shape (conviction ≥ 1, non-empty
      thesis_summary) is enforced by the Pydantic model itself; if the
      payload reached this code it has already passed those checks.
    """
    if not validation.is_valid:
        return Verdict.WARN

    output = result.output
    if mode == "normal":
        recs = output.recommendations or ()
        if recs and result.tool_calls_used == 0:
            return Verdict.WARN
        return Verdict.PASS

    # Watchlist mode: any tool call violates the halt-mode contract.
    if result.tool_calls_used > 0:
        return Verdict.WARN
    return Verdict.PASS


def verdict_for_failure(failure: HarnessFailure) -> Verdict:
    """Apply the failure-path rubric: any HarnessFailure → FAIL."""
    del failure
    return Verdict.FAIL


# ---------------------------------------------------------------------------
# Synthesizer-archive reader
# ---------------------------------------------------------------------------


def read_synthesizer_text(
    *,
    archive_root: Path,
    synthesizer_invocation_id: str,
) -> str:
    """Read the synthesizer's recorded ``response.md`` from a prior archive.

    The synthesizer harness writes its prose to
    ``<archive_root>/invocations/<inv_id>/analysis/synthesizer/response.md``;
    we read it verbatim and pass it as the analyst's brief input (per
    parent-issue decision C — fixture-chained, not live-composed).
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
    """Read the synthesizer's recorded ``retrieval_store.json`` from a prior archive.

    Mirrors :func:`read_synthesizer_text` — both artifacts are produced
    by the synthesizer's verify run when invoked with ``--archive-root``.
    """
    stage_dir = stage_artifacts_dir(archive_root, synthesizer_invocation_id)
    return load_retrieval_store(stage_dir)


# ---------------------------------------------------------------------------
# Canonical-fixture builders
# ---------------------------------------------------------------------------

# The fixture environment mirrors the four-way active-sector taxonomy in
# config/profiles/medium.yaml. Choosing ``medium`` gives the analyst the
# full feature set — options + shorts enabled — so the verify run
# exercises every header section.

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_PORTFOLIO_VALUE = 100_000.0
_RISK_FREE_RATE = 0.045
_SPOT_DEFAULT = 100.0
_IV_DEFAULT = 0.30
_DEFAULT_OPTION_EXPIRATION = date(2026, 5, 28)


# Ticker → sector map covering the test universe, derived from the
# medium profile's ``active_sectors`` and the tickers the synthesizer
# fixtures cite (NVDA, JPM, XOM, MSFT, GOOGL).
_TICKER_TO_SECTOR: dict[str, str] = {
    "NVDA": "semis",
    "MSFT": "tech",
    "GOOGL": "tech",
    "AAPL": "tech",
    "JPM": "financials",
    "XOM": "energy",
}


def build_fixture_sector_resolver() -> Callable[[str], str]:
    """Return a fixture-backed ``ticker -> sector`` callable.

    Tickers outside the small test universe map to ``"tech"`` — the
    runner's contract requires *some* sector for any ticker, and the
    fallback keeps unrecognized tickers in the active-sector set.
    """

    def _resolver(ticker: str) -> str:
        return _TICKER_TO_SECTOR.get(ticker, "tech")

    return _resolver


def build_fixture_analyst_view() -> AnalystView:
    """An empty held-positions analyst view suitable for the verify run."""
    return AnalystView(
        held_positions=(),
        active_thesis_summaries=(),
        available_capital=AnalystAvailableCapital(
            available_for_new_positions_usd=70_000.0,
            available_for_new_positions_pct=70.0,
            per_position_max_size_usd=5_000.0,
            per_position_max_size_pct=5.0,
        ),
        pending_orders=(),
        abandoned_openings=(),
    )


def _make_budget_entry(*, rule_id: str, rule_label: str) -> RiskBudgetEntry:
    current_value = 5.0
    limit_value = 25.0
    headroom = limit_value - current_value
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=(headroom / limit_value) * 100.0,
        zone=RiskZone.NORMAL,
        unit="% of portfolio",
        cumulative_invocation_impact_value=0.0,
    )


def build_fixture_risk_budget() -> RiskBudgetConsumption:
    """Risk-budget covering the four-way taxonomy plus net-long / gross."""
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_financials",
                rule_label="Financials sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_energy",
                rule_label="Energy sector concentration",
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
            ),
        )
    )


def build_fixture_active_risk_parameters() -> ActiveRiskParameterSet:
    """Active risk-parameter set with the position size + daily-drawdown limits."""
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="per_position_max_size",
                rule_label="Per-position max size",
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown limit",
                value=2.5,
                unit="% of equity",
                regime_multiplier_applied=1.0,
                base_value=2.5,
            ),
        ),
        active_overlays=(),
    )


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def build_fixture_library_config() -> LibraryConfig:
    """Library config matching ``config/profiles/medium.yaml`` shape."""
    effective_limits = {
        "position_max_size_pct": 5.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 30.0,
        "gross_exposure_pct": 120.0,
        "options_delta_pct": 40.0,
        "portfolio_theta_pct_per_day": 0.15,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 3.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 30.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def build_fixture_market_inputs() -> MarketInputs:
    """Market-inputs fixture covering the test universe."""
    underlyings = tuple(_TICKER_TO_SECTOR)
    surface = {
        ticker: IvSurfaceEntry(
            underlying=ticker,
            quotes=(
                IvQuote(
                    strike=_SPOT_DEFAULT,
                    expiration=_DEFAULT_OPTION_EXPIRATION,
                    contract_type=ContractType.CALL,
                    implied_volatility=_IV_DEFAULT,
                ),
                IvQuote(
                    strike=_SPOT_DEFAULT,
                    expiration=_DEFAULT_OPTION_EXPIRATION,
                    contract_type=ContractType.PUT,
                    implied_volatility=_IV_DEFAULT,
                ),
            ),
        )
        for ticker in underlyings
    }
    return MarketInputs(
        underlying_prices=MappingProxyType({ticker: _SPOT_DEFAULT for ticker in underlyings}),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=FixtureIvProvider(surface=surface, realized_vol={}),
        as_of=_AS_OF,
    )


def build_fixture_portfolio_state_snapshot() -> PortfolioStateSnapshot:
    """Portfolio-state snapshot with low utilization across all sectors."""
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 8.0, "semis": 6.0, "financials": 3.0, "energy": 3.0}
        ),
        net_long_pct=20.0,
        net_short_pct=0.0,
        gross_pct=20.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def build_fixture_state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )


def build_fixture_halt_state() -> HaltState:
    """Halt state simulating a daily-drawdown halt at 2.5% / 2.5%."""
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.5,
        daily_drawdown_limit_pct=2.5,
    )


# ---------------------------------------------------------------------------
# Fixture saving
# ---------------------------------------------------------------------------


def save_fixtures(
    *,
    normal_output: AnalystOutput,
    halt_output: AnalystOutput,
    fixtures_dir: Path,
) -> None:
    """Write ``normal.json`` and ``halt.json`` under *fixtures_dir*.

    Downstream feature trees (strategist verifier, proposal
    pre-processor verifier, PM verifier) consume these files as their
    canonical analyst-output input fixtures.
    """
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / "normal.json").write_text(
        normal_output.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (fixtures_dir / "halt.json").write_text(
        halt_output.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Operator-report rendering
# ---------------------------------------------------------------------------


_BANNER = "=== Analyst live-SDK verification ==="


def _render_success_report(
    *,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    result: AnalystResult,
    validation: ValidationResult,
    verdict: Verdict,
    scenario: str,
) -> str:
    output = result.output
    if output.mode == "normal":
        rec_count = len(output.recommendations or ())
        watchlist_count = 0
    else:
        rec_count = 0
        watchlist_count = len(output.watchlist or ())
    error_render = (
        ", ".join(f"{e.field_path}::{e.rule}" for e in validation.errors)
        if validation.errors
        else "(none)"
    )
    return (
        "\n".join(
            [
                _BANNER,
                f"scenario: {scenario}",
                f"invocation_id: {invocation_id}",
                f"model: {agent_config.model.value}",
                f"wall_clock: {result.wall_clock_seconds:.2f}s",
                (
                    f"tokens: input={result.tokens_used.input_tokens} "
                    f"output={result.tokens_used.output_tokens} "
                    f"cache_read={result.tokens_used.cache_read_tokens} "
                    f"cache_write={result.tokens_used.cache_write_tokens}"
                ),
                f"tool_calls: {result.tool_calls_used}",
                f"retry_count: {result.retry_count}",
                f"stop_reason: {result.stop_reason}",
                f"mode: {output.mode}",
                f"recommendations: {rec_count}",
                f"watchlist: {watchlist_count}",
                f"validator_errors: {len(validation.errors)} ({error_render})",
                f"--- Verdict: {verdict.value} ---",
            ]
        )
        + "\n"
    )


def _render_failure_report(
    *,
    scenario: str,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    failure: HarnessFailure,
    verdict: Verdict,
) -> str:
    return (
        "\n".join(
            [
                _BANNER,
                f"scenario: {scenario}",
                f"invocation_id: {invocation_id}",
                f"model: {agent_config.model.value}",
                "",
                "--- Harness failure ---",
                f"type: {type(failure).__name__}",
                f"agent_name: {failure.agent_name}",
                f"invocation_id: {failure.invocation_id}",
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
            "real-SDK analyst verification cannot run without it. "
            "Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run.",
            agent_name=_AGENT_NAME,
            invocation_id=invocation_id,
        )


# ---------------------------------------------------------------------------
# CLI scenario dispatch
# ---------------------------------------------------------------------------


def _format_invocation_id(now: datetime, scenario: str) -> str:
    """``YYYYMMDDTHHMMSSZ-verify-analyst-<scenario>``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-verify-analyst-{scenario}"


async def _invoke_scenario(
    *,
    mode: Literal["normal", "watchlist"],
    invocation_id: str,
    timestamp: datetime,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    archive_root: Path | None,
    library_config: LibraryConfig,
    halt_state: HaltState | None,
) -> AnalystResult:
    """Compose the runner kwargs and call :func:`run_analyst`."""
    return await run_analyst(
        mode=mode,
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        analyst_view=build_fixture_analyst_view(),
        risk_budget=build_fixture_risk_budget(),
        active_risk_parameters=build_fixture_active_risk_parameters(),
        profile_feature_flags=library_config.feature_flags,
        library_config=library_config,
        library_market=build_fixture_market_inputs(),
        sector_resolver=build_fixture_sector_resolver(),
        portfolio_state_snapshot=build_fixture_portfolio_state_snapshot(),
        active_sectors=frozenset(library_config.active_sectors),
        invocation_id=invocation_id,
        timestamp=timestamp,
        state_delivery_config=build_fixture_state_delivery_config(),
        options_enabled=library_config.feature_flags.options_enabled,
        short_selling_enabled=library_config.feature_flags.short_selling_enabled,
        halt_state=halt_state,
        archive_root=archive_root,
    )


def _run_one_scenario(
    *,
    scenario: str,
    mode: Literal["normal", "watchlist"],
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    archive_root: Path,
    library_config: LibraryConfig,
    agent_config: BaseAgentConfig,
    now: datetime,
) -> tuple[Verdict, AnalystResult | None]:
    """Run a single scenario and return the verdict and parsed result.

    On success the result is the parsed :class:`AnalystResult`; on
    HarnessFailure the verdict is FAIL and the result is ``None``.
    """
    invocation_id = _format_invocation_id(now, scenario)
    print(
        f"=== Analyst live-SDK verification: scenario={scenario}, "
        f"invocation_id={invocation_id}, model={agent_config.model.value} ===\n"
        "Invoking SDK...",
        flush=True,
    )
    halt_state = build_fixture_halt_state() if mode == "watchlist" else None

    try:
        result = asyncio.run(
            _invoke_scenario(
                mode=mode,
                invocation_id=invocation_id,
                timestamp=now,
                synthesizer_text=synthesizer_text,
                retrieval_store=retrieval_store,
                archive_root=archive_root,
                library_config=library_config,
                halt_state=halt_state,
            )
        )
    except HarnessFailure as failure:
        verdict = verdict_for_failure(failure)
        print(
            _render_failure_report(
                scenario=scenario,
                invocation_id=invocation_id,
                agent_config=agent_config,
                failure=failure,
                verdict=verdict,
            )
        )
        return verdict, None

    validation = validate_analyst_output(
        result.output,
        retrieval_store=retrieval_store,
        active_sectors=frozenset(library_config.active_sectors),
    )
    verdict = verdict_for_success(result=result, validation=validation, mode=mode)
    print(
        _render_success_report(
            scenario=scenario,
            invocation_id=invocation_id,
            agent_config=agent_config,
            result=result,
            validation=validation,
            verdict=verdict,
        )
    )
    return verdict, result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the analyst end-to-end against a real CLAUDE_CODE_OAUTH_TOKEN "
            "for two scenarios (normal mode + halt mode), reading the brief "
            "from a recorded synthesizer archive."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        required=True,
        help=(
            "Path to the invocation-archive root containing a recorded "
            "synthesizer run. The script reads "
            "<archive-root>/invocations/<synth-id>/analysis/synthesizer/response.md "
            "and writes its own diagnostic archive under the same root."
        ),
    )
    parser.add_argument(
        "--synthesizer-invocation-id",
        type=str,
        required=True,
        help=(
            "The synthesizer's invocation_id from a prior verify_synthesizer.py run. "
            "Used to locate the recorded brief text and retrieval store."
        ),
    )
    parser.add_argument(
        "--save-fixtures",
        action="store_true",
        default=False,
        help=(
            "After both scenarios pass, write the parsed AnalystOutput JSON to "
            "tests/fixtures/decision/analyst/{normal,halt}.json so downstream "
            "feature trees can consume them as input fixtures."
        ),
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("tests/fixtures/decision/analyst"),
        help="Override the fixture-output directory (defaults to the in-tree path).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS/WARN, 1 on FAIL.

    Runs the normal-mode scenario first, then the halt-mode scenario.
    Both must produce non-FAIL verdicts for ``--save-fixtures`` to write.
    A FAIL on the first scenario aborts before the second runs.
    """
    configure_utf8_stdio()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    agent_config = load_analyst_agent_config()
    library_config = build_fixture_library_config()

    pre_flight_id = _format_invocation_id(now, "preflight")
    try:
        _check_oauth_token_set(pre_flight_id)
    except HarnessFailure as failure:
        verdict = verdict_for_failure(failure)
        print(
            _render_failure_report(
                scenario="preflight",
                invocation_id=pre_flight_id,
                agent_config=agent_config,
                failure=failure,
                verdict=verdict,
            )
        )
        return 1

    # The synthesizer brief and retrieval store are constant across both
    # scenarios; load once and thread through.
    synthesizer_text = read_synthesizer_text(
        archive_root=args.archive_root,
        synthesizer_invocation_id=args.synthesizer_invocation_id,
    )
    retrieval_store = read_retrieval_store(
        archive_root=args.archive_root,
        synthesizer_invocation_id=args.synthesizer_invocation_id,
    )

    verdicts: list[Verdict] = []
    outputs: dict[str, AnalystOutput] = {}

    scenarios: tuple[tuple[str, Literal["normal", "watchlist"]], ...] = (
        ("normal", "normal"),
        ("halt", "watchlist"),
    )
    for scenario, mode in scenarios:
        verdict, result = _run_one_scenario(
            scenario=scenario,
            mode=mode,
            synthesizer_text=synthesizer_text,
            retrieval_store=retrieval_store,
            archive_root=args.archive_root,
            library_config=library_config,
            agent_config=agent_config,
            now=now,
        )
        verdicts.append(verdict)
        if verdict is Verdict.FAIL:
            return 1
        if result is not None:
            outputs[scenario] = result.output

    if args.save_fixtures and len(outputs) == 2:
        save_fixtures(
            normal_output=outputs["normal"],
            halt_output=outputs["halt"],
            fixtures_dir=args.fixtures_dir,
        )
        print(
            f"[verify_analyst] fixtures written to {args.fixtures_dir}: normal.json + halt.json",
            flush=True,
        )

    summary = " | ".join(
        f"{s}: {v.value}" for s, v in zip(("normal", "halt"), verdicts, strict=True)
    )
    print(f"=== Summary: {summary} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
