"""Tests for decision-layer phase-output Pydantic boundary models — ALP-692.

Covers per-model round-trip cases:
* ``from_domain(dc).to_domain() == dc``
* ``model_dump_json`` + ``model_validate_json`` round-trip
* PMResultModel preserves ``submission_log`` tuple order and field values
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import (
    InvocationId,
    PositionId,
)
from alphamind._kernel.ids import (
    envelope_id as _envelope_id,
)
from alphamind._kernel.ids import (
    recommendation_id as _recommendation_id,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.commands.pm_envelope import (
    ConcernRecord,
    CriterionAssessment,
    PMAnalystEnvelope,
    PMCompletionRecord,
    ThesisQualityEvaluation,
    VerdictSummary,
)
from alphamind.commands.submission_log import SubmissionLogEntry
from alphamind.commands.submission_results import (
    Acknowledgment,
    RejectionPayload,
    SubmissionResult,
    _BreachedRule,
)
from alphamind.commands.validation_results import ValidationResult
from alphamind.decision.analyst.models import AnalystOutput, AnalystResultModel
from alphamind.decision.analyst.runner import AnalystResult
from alphamind.decision.portfolio_manager.models import PMResultModel
from alphamind.decision.portfolio_manager.runner import PMResult
from alphamind.decision.strategist.models import StrategistOutput, StrategistResultModel
from alphamind.decision.strategist.runner import StrategistResult

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "inv-test-001"
_TOKENS = TokensUsed(
    input_tokens=1000,
    output_tokens=200,
    cache_read_tokens=50,
    cache_write_tokens=10,
)


# ---------------------------------------------------------------------------
# AnalystOutput minimal fixture (mode=normal, empty recommendations)
# ---------------------------------------------------------------------------


def _make_analyst_output() -> AnalystOutput:
    return AnalystOutput(
        invocation_id=InvocationId(_INVOCATION_ID),
        timestamp=_NOW,
        mode="normal",
        recommendations=(),
        watchlist=None,
    )


def _make_analyst_result() -> AnalystResult:
    return AnalystResult(
        output=_make_analyst_output(),
        retry_count=2,
        tokens_used=_TOKENS,
        tool_calls_used=5,
        wall_clock_seconds=12.34,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# StrategistOutput minimal fixture
# ---------------------------------------------------------------------------


def _make_strategist_output() -> StrategistOutput:
    from alphamind.decision.strategist.models import PortfolioLevelObservations

    return StrategistOutput(
        invocation_id=InvocationId(_INVOCATION_ID),
        timestamp=_NOW,
        mode="normal",
        position_assessments=(),
        pending_order_assessments=(),
        portfolio_level_observations=PortfolioLevelObservations(
            aggregate_thesis_health="All theses on track.",
            sector_balance_shifts="No shifts observed.",
            thesis_dependency_warnings="None.",
            capital_allocation_observations="Balanced.",
        ),
    )


def _make_strategist_result() -> StrategistResult:
    return StrategistResult(
        output=_make_strategist_output(),
        validation_result=ValidationResult(errors=()),
        tokens_used=_TOKENS,
        metadata={"attempts": 1, "wall_clock_seconds": 9.87},
    )


# ---------------------------------------------------------------------------
# PMCompletionRecord + PMResult fixtures
# ---------------------------------------------------------------------------


def _make_pm_completion_record() -> PMCompletionRecord:
    return PMCompletionRecord(
        invocation_id=InvocationId(_INVOCATION_ID),
        timestamp=_NOW,
        envelopes_submitted=2,
        verdict_summary=VerdictSummary(
            approve=1,
            approve_with_modification=1,
            reject=0,
            override_with_corrective_action=0,
        ),
    )


def _make_thesis_quality_evaluation() -> ThesisQualityEvaluation:
    pass_criterion = CriterionAssessment(status="pass")
    return ThesisQualityEvaluation(
        falsifiability=pass_criterion,
        sizing_proportionality=pass_criterion,
        portfolio_coherence=pass_criterion,
        timing_plausibility=pass_criterion,
        counterargument_consideration=pass_criterion,
    )


def _make_submission_log_entries() -> tuple[SubmissionLogEntry, ...]:
    # Two entries with different verdict/results — tests order preservation.
    tqe = _make_thesis_quality_evaluation()
    entry_a = SubmissionLogEntry(
        envelope=PMAnalystEnvelope(
            envelope_id=_envelope_id("ENV-REC-001"),
            invocation_id=InvocationId(_INVOCATION_ID),
            source_provenance="pm_analyst",
            source_recommendation_id=_recommendation_id("REC-001"),
            recommendation_type="new_entry",
            verdict="approve",
            evaluation=tqe,
            modifications=(),
            concerns=(),
            rationale_narrative="Strong thesis with clear invalidation.",
            commands=(),
        ),
        submission_results=(
            SubmissionResult(
                command_ordinal=1,
                status="accepted",
                command_id="CMD-001",
                acknowledgment=Acknowledgment(position_id=PositionId("pos-abc")),
                rejection_payload=None,
            ),
        ),
    )
    entry_b = SubmissionLogEntry(
        envelope=PMAnalystEnvelope(
            envelope_id=_envelope_id("ENV-REC-002"),
            invocation_id=InvocationId(_INVOCATION_ID),
            source_provenance="pm_analyst",
            source_recommendation_id=_recommendation_id("REC-002"),
            recommendation_type="new_entry",
            verdict="reject",
            evaluation=tqe,
            modifications=(),
            concerns=(
                ConcernRecord(
                    source="sizing_proportionality",
                    summary="Position size exceeds limit.",
                ),
            ),
            rationale_narrative="Position size exceeds limit.",
            commands=(),
        ),
        submission_results=(
            SubmissionResult(
                command_ordinal=1,
                status="rejected",
                command_id="CMD-002",
                acknowledgment=None,
                rejection_payload=RejectionPayload(
                    rules_breached=(
                        _BreachedRule(
                            rule="position_max_size_pct",
                            current=0.12,
                            limit=0.10,
                            overage=0.02,
                            unit="pct",
                        ),
                    ),
                    suggested_modification="Reduce size to meet limit.",
                ),
            ),
        ),
    )
    return (entry_a, entry_b)


def _make_pm_result() -> PMResult:
    return PMResult(
        output=_make_pm_completion_record(),
        submission_log=_make_submission_log_entries(),
        retry_count=0,
        tokens_used=_TOKENS,
        tool_calls_used=8,
        wall_clock_seconds=45.6,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# AnalystResultModel tests
# ---------------------------------------------------------------------------


class TestAnalystResultModel:
    def test_from_domain_to_domain_round_trip(self) -> None:
        dc = _make_analyst_result()
        model = AnalystResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        dc = _make_analyst_result()
        model = AnalystResultModel.from_domain(dc)
        json_str = model.model_dump_json()
        model2 = AnalystResultModel.model_validate_json(json_str)
        assert model2 == model

    def test_model_is_frozen(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        dc = _make_analyst_result()
        model = AnalystResultModel.from_domain(dc)
        with pytest.raises((TypeError, AttributeError, PydanticValidationError)):
            model.retry_count = 99

    def test_metadata_fields_preserved(self) -> None:
        dc = _make_analyst_result()
        model = AnalystResultModel.from_domain(dc)
        assert model.retry_count == dc.retry_count
        assert model.tool_calls_used == dc.tool_calls_used
        assert model.wall_clock_seconds == dc.wall_clock_seconds
        assert model.stop_reason == dc.stop_reason
        assert model.tokens_used.input_tokens == dc.tokens_used.input_tokens


# ---------------------------------------------------------------------------
# StrategistResultModel tests
# ---------------------------------------------------------------------------


class TestStrategistResultModel:
    def test_from_domain_to_domain_round_trip(self) -> None:
        dc = _make_strategist_result()
        model = StrategistResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        dc = _make_strategist_result()
        model = StrategistResultModel.from_domain(dc)
        json_str = model.model_dump_json()
        model2 = StrategistResultModel.model_validate_json(json_str)
        assert model2 == model

    def test_model_is_frozen(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        dc = _make_strategist_result()
        model = StrategistResultModel.from_domain(dc)
        with pytest.raises((TypeError, AttributeError, PydanticValidationError)):
            model.metadata = {}

    def test_metadata_dict_preserved(self) -> None:
        dc = _make_strategist_result()
        model = StrategistResultModel.from_domain(dc)
        assert model.metadata == dc.metadata


# ---------------------------------------------------------------------------
# PMResultModel + SubmissionLogEntryModel tests
# ---------------------------------------------------------------------------


class TestPMResultModel:
    def test_from_domain_to_domain_round_trip(self) -> None:
        dc = _make_pm_result()
        model = PMResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        dc = _make_pm_result()
        model = PMResultModel.from_domain(dc)
        json_str = model.model_dump_json()
        model2 = PMResultModel.model_validate_json(json_str)
        assert model2 == model

    def test_model_is_frozen(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        dc = _make_pm_result()
        model = PMResultModel.from_domain(dc)
        with pytest.raises((TypeError, AttributeError, PydanticValidationError)):
            model.retry_count = 42

    def test_submission_log_order_preserved(self) -> None:
        """Element order must be identical after round-trip."""
        from alphamind.decision.portfolio_manager.runner import PMResult as _PMResult

        dc = _make_pm_result()
        model = PMResultModel.from_domain(dc)
        recovered: _PMResult = model.to_domain()  # type: ignore[assignment]
        assert len(recovered.submission_log) == len(dc.submission_log)
        for orig, rec in zip(dc.submission_log, recovered.submission_log, strict=False):
            assert orig.envelope.envelope_id == rec.envelope.envelope_id

    def test_submission_log_non_empty(self) -> None:
        dc = _make_pm_result()
        model = PMResultModel.from_domain(dc)
        assert len(model.submission_log) == 2

    def test_submission_log_entry_fields_preserved(self) -> None:
        """SubmissionLogEntry fields survive to_domain."""
        from alphamind.decision.portfolio_manager.runner import PMResult as _PMResult

        dc = _make_pm_result()
        model = PMResultModel.from_domain(dc)
        recovered: _PMResult = model.to_domain()  # type: ignore[assignment]
        first_orig = dc.submission_log[0]
        first_rec = recovered.submission_log[0]
        assert first_orig.submission_results[0].status == first_rec.submission_results[0].status
        assert (
            first_orig.submission_results[0].command_id
            == first_rec.submission_results[0].command_id
        )

    def test_empty_submission_log_round_trips(self) -> None:
        dc = PMResult(
            output=_make_pm_completion_record(),
            submission_log=(),
            retry_count=0,
            tokens_used=_TOKENS,
            tool_calls_used=0,
            wall_clock_seconds=5.0,
            stop_reason=None,
        )
        model = PMResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc
