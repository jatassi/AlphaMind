"""Unit tests for ``scripts/verify_decision_pipeline.py`` (ALP-404).

The live-SDK verify script is itself the integration test for the
decision-pipeline composition. The tests in this module cover only the
in-process logic that is safe to exercise without the Anthropic API:

- the missing-credentials pre-flight check (renders a clean failure
  report, exit code 1),
- the in-process ``CurrentPriceProvider`` + ``RepositoryFixture`` builder
  helpers (shape correctness, no SDK calls),
- the result-validation predicate (PASS / FAIL classification on a
  hand-rolled ``DecisionPipelineResult`` instance),
- the result serialization (round-trip a built result through
  ``model_dump`` / ``model_validate`` JSON).

The end-to-end live-SDK invocation is verified by an operator running
the script after this PR lands.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName
from alphamind.decision.analyst.runner import AnalystResult
from alphamind.decision.portfolio_manager.runner import PMResult
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.decision.strategist.runner import StrategistResult
from alphamind.pipeline.decision import DecisionPipelineResult
from alphamind.portfolio_state.pricing import PriceQuote, PriceSource
from alphamind.portfolio_state.records.positions import EquityPositionDetails
from alphamind.portfolio_state.repository import RepositoryFixture
from alphamind.scripts.verify_decision_pipeline import (
    Verdict,
    build_fixture_price_provider,
    build_fixture_repository,
    main,
    serialize_pipeline_result,
    validate_pipeline_result,
)

_AGENT_NAME = AgentName.portfolio_manager.value


# ---------------------------------------------------------------------------
# Missing-auth pre-flight check
# ---------------------------------------------------------------------------


def test_missing_auth_renders_failure_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Missing CLAUDE_CODE_OAUTH_TOKEN exits 1 with a clean failure block.

    The verify script must surface the missing-credentials case as an
    operator-readable failure block on stdout (no stack trace) and exit
    code 1, mirroring the per-agent verify scripts' contract.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    exit_code = main(argv=["--archive-root", str(tmp_path / "archive")])

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "Decision pipeline live-SDK verification" in captured
    assert "CLAUDE_CODE_OAUTH_TOKEN" in captured
    assert "Verdict: FAIL" in captured


# ---------------------------------------------------------------------------
# Fixture-builder helpers
# ---------------------------------------------------------------------------


def test_build_fixture_repository_covers_four_sectors() -> None:
    """The default fixture repository carries four open positions, one each
    in tech, semis, financials, energy — matching the parent issue's
    ``RepositoryFixture`` shape.
    """
    fixture = build_fixture_repository()
    assert isinstance(fixture, RepositoryFixture)
    # Four open positions, each in a distinct sector.
    assert len(fixture.open_positions) == 4
    tickers: set[str] = set()
    for pos in fixture.open_positions:
        assert isinstance(pos.details, EquityPositionDetails)
        tickers.add(pos.details.ticker)
    # Each sector is represented by exactly one ticker.
    assert tickers == {"AAPL", "NVDA", "JPM", "XOM"}
    # No abandoned actions, no recent PM decisions.
    assert fixture.recent_pm_decision_log == ()
    # One active thesis (per scope).
    assert len(fixture.active_theses) == 1


def test_build_fixture_repository_carries_position_max_size_pct() -> None:
    """The active-risk-parameter set must include ``position_max_size_pct``
    so the library-snapshot translator does not raise.
    """
    fixture = build_fixture_repository()
    rule_ids = {entry.rule_id for entry in fixture.active_risk_parameters.entries}
    assert "position_max_size_pct" in rule_ids


def test_build_fixture_price_provider_covers_universe() -> None:
    """The in-process price provider returns quotes for every ticker the
    fixture repository's open positions reference.
    """
    quotes = build_fixture_price_provider()
    # All four open-position tickers are priced.
    assert {"AAPL", "NVDA", "JPM", "XOM"}.issubset(quotes.keys())
    for ticker, quote in quotes.items():
        assert isinstance(quote, PriceQuote)
        assert quote.ticker == ticker
        assert quote.price_usd > 0
        assert quote.source is PriceSource.INTRADAY_QUOTE


# ---------------------------------------------------------------------------
# Result-validation predicate
# ---------------------------------------------------------------------------


def _stub_pipeline_result(
    *,
    submission_log: tuple[Any, ...] = (),
    envelopes_submitted: int = 0,
    has_recommendations: bool = True,
    position_assessments_count: int = 4,
) -> DecisionPipelineResult:
    """Build a minimal DecisionPipelineResult for predicate exercises."""
    from alphamind.decision.analyst.models import AnalystOutput
    from alphamind.decision.portfolio_manager.models import (
        PMCompletionRecord,
        VerdictSummary,
    )
    from alphamind.decision.proposal_pre_processor.models import (
        AggregateObservations,
        AnalystSection,
        BasisSection,
        BookHealthSummary,
        ByRecommendedAction,
        ByThesisStatus,
        CombinedSetImpact,
        ConvictionDistribution,
        ConvictionHistogram,
        StrategistSection,
    )
    from alphamind.decision.strategist.models import (
        PortfolioLevelObservations,
        PositionAssessment,
        StrategistOutput,
    )
    from alphamind.decision.strategist.validation import ValidationResult
    from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
    from alphamind.risk_guardrails.guardrail_evaluation import (
        PortfolioStateSnapshot as LibrarySnapshot,
    )

    inv_id = "inv-validate-001"
    timestamp = datetime(2026, 5, 10, 14, 30, tzinfo=UTC)

    analyst_output = AnalystOutput(
        invocation_id=inv_id,
        timestamp=timestamp,
        mode="normal",
        recommendations=() if has_recommendations else None,
        watchlist=None,
    )
    analyst_result = AnalystResult(
        output=analyst_output,
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=10,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=0,
        wall_clock_seconds=1.0,
        stop_reason="end_turn",
    )

    assessments = tuple(
        PositionAssessment(
            assessment_id=f"SA-{i}",
            position_id=f"POS-{i}",
            thesis_id=f"THESIS-{i}",
            underlying="AAPL",
            sector="tech",
            thesis_status="on-track",
            prior_status="on-track",
            recommended_action="hold",
            action_parameters=None,
            exposure_impact=None,
            guardrail_validation_result=None,
            remedy_flag=None,
            status_rationale="On-track.",
            action_rationale="Hold.",
        )
        for i in range(1, position_assessments_count + 1)
    )
    strategist_result = StrategistResult(
        output=StrategistOutput(
            invocation_id=inv_id,
            timestamp=timestamp,
            mode="normal",
            position_assessments=assessments,
            pending_order_assessments=(),
            portfolio_level_observations=PortfolioLevelObservations(
                aggregate_thesis_health="OK.",
                sector_balance_shifts="OK.",
                thesis_dependency_warnings="OK.",
                capital_allocation_observations="OK.",
            ),
        ),
        validation_result=ValidationResult(overall="PASS", failures=(), warnings=()),
        tokens_used=TokensUsed(
            input_tokens=10,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        metadata={"attempts": 1},
    )

    basis = BasisSection(
        analyst_proposal_ids=(),
        strategist_action_ids=(),
        strategist_holds_excluded_count=0,
        snapshot_timestamp=timestamp,
    )
    histogram = ConvictionHistogram(**{"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
    aggregate = AggregateObservations(
        combined_set_impact=CombinedSetImpact(basis=basis, per_rule=(), breaches=()),
        conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
        book_health_summary=BookHealthSummary(
            by_thesis_status=ByThesisStatus(
                **{
                    "on-track": position_assessments_count,
                    "partially-realized": 0,
                    "at-risk": 0,
                    "stale": 0,
                    "invalidated": 0,
                }
            ),
            by_recommended_action=ByRecommendedAction(
                **{
                    "hold": position_assessments_count,
                    "reduce": 0,
                    "close": 0,
                    "adjust-bracket": 0,
                    "add": 0,
                }
            ),
            remedy_flagged_count=0,
            total=position_assessments_count,
        ),
    )
    bundle = ProposalPreProcessorBundle(
        invocation_id=inv_id,
        timestamp=timestamp,
        aggregate_observations=aggregate,
        strategist_section=StrategistSection(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=strategist_result.output.portfolio_level_observations,
        ),
        analyst_section=AnalystSection(
            mode="normal",
            recommendations=(),
            watchlist=None,
        ),
    )

    # Use ``model_construct`` to bypass Pydantic's strict ``SubmissionLogEntry``
    # type-check on ``submission_log`` — the predicate cares about length and
    # presence, not about each entry being a real envelope-shaped dataclass.
    pm_result = PMResult.model_construct(
        output=PMCompletionRecord(
            invocation_id=inv_id,
            timestamp=timestamp,
            envelopes_submitted=envelopes_submitted,
            verdict_summary=VerdictSummary(
                approve=envelopes_submitted, approve_with_modification=0, reject=0
            ),
        ),
        submission_log=submission_log,
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=10,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=0,
        wall_clock_seconds=1.0,
        stop_reason="end_turn",
    )

    pyd_snap = PortfolioStateSnapshot.model_construct()
    # Library snapshot is a frozen @dataclass with required fields; build a
    # zeroed-out instance so the test predicate has something to thread through.
    lib_snap = LibrarySnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct={},
        net_long_pct=0.0,
        net_short_pct=0.0,
        gross_pct=0.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions={},
    )

    return DecisionPipelineResult(
        pydantic_snapshot=pyd_snap,
        library_snapshot=lib_snap,
        analyst_result=analyst_result,
        strategist_result=strategist_result,
        pre_processor_bundle=bundle,
        pm_result=pm_result,
    )


def test_validate_pipeline_result_passes_when_all_invariants_hold() -> None:
    """The validator returns PASS when every acceptance invariant holds."""
    from types import SimpleNamespace

    submission_log = (
        SimpleNamespace(envelope=SimpleNamespace(envelope_id="env-1"), submission_results=()),
    )
    result = _stub_pipeline_result(
        submission_log=submission_log,
        envelopes_submitted=1,
        position_assessments_count=4,
    )
    verdict, errors = validate_pipeline_result(result)
    assert verdict is Verdict.PASS
    assert errors == ()


def test_validate_pipeline_result_fails_on_empty_submission_log() -> None:
    """An empty PM submission log surfaces as FAIL with a documented reason."""
    result = _stub_pipeline_result(
        submission_log=(),
        envelopes_submitted=0,
        position_assessments_count=4,
    )
    verdict, errors = validate_pipeline_result(result)
    assert verdict is Verdict.FAIL
    assert any("PM submitted zero envelopes" in err for err in errors)


def test_validate_pipeline_result_fails_on_envelopes_count_mismatch() -> None:
    """``envelopes_submitted`` must equal ``len(submission_log)``."""
    from types import SimpleNamespace

    submission_log = (
        SimpleNamespace(envelope=SimpleNamespace(envelope_id="env-1"), submission_results=()),
    )
    result = _stub_pipeline_result(
        submission_log=submission_log,
        envelopes_submitted=2,  # mismatched
        position_assessments_count=4,
    )
    verdict, errors = validate_pipeline_result(result)
    assert verdict is Verdict.FAIL
    assert any("envelopes_submitted" in err for err in errors)


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def test_serialize_pipeline_result_round_trips_via_json(tmp_path: Path) -> None:
    """The serialized result writes to disk and JSON-decodes to a dict
    carrying the six DecisionPipelineResult fields.

    Uses an empty submission_log so the serializer's PMEnvelope path is
    exercised through its zero-iterations branch — the per-entry
    serialization is verified end-to-end by the live-SDK script run.
    """
    result = _stub_pipeline_result(
        submission_log=(),
        envelopes_submitted=0,
        position_assessments_count=4,
    )
    target = tmp_path / "result.json"
    serialize_pipeline_result(result, target)
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    expected_keys = {
        "analyst_result",
        "strategist_result",
        "pre_processor_bundle",
        "pm_result",
        "pydantic_snapshot",
        "library_snapshot",
    }
    assert expected_keys.issubset(payload.keys())


def test_retrieval_store_module_importable() -> None:
    """Sanity check — the script module imports cleanly so the verify script
    can be invoked via ``python -m alphamind.scripts.verify_decision_pipeline``.
    """
    from alphamind.scripts import verify_decision_pipeline as module

    assert hasattr(module, "main")
    # The retrieval-store helper is part of the public scaffolding.
    assert hasattr(module, "build_fixture_retrieval_store")
    store = module.build_fixture_retrieval_store()
    assert isinstance(store, RetrievalStore)
