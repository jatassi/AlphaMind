"""PMEnvelope + PMCompletionRecord — backwards-compat re-export shim.

The canonical home for these wire-format types is now
:mod:`alphamind.commands.pm_envelope`. This shim preserves the historic
import path :mod:`alphamind.decision.portfolio_manager.models` for callers
that pinned to it before ALP-458 hoisted the types into the
``alphamind.commands`` kernel.

New code should import from :mod:`alphamind.commands` directly.

Phase-output boundary models (:class:`SubmissionLogEntryModel`,
:class:`PMResultModel`) are added here per parent design decision (G) —
per-phase Pydantic boundary models live alongside existing dataclass models
under each agent's ``models.py`` (ALP-692).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from alphamind.commands.pm_envelope import (
    AddCommand,
    AdjustCommand,
    AdjustmentCategory,
    AntiPattern,
    CancelCommand,
    CloseCommand,
    ConcernRecord,
    CriterionAssessment,
    ModificationRecord,
    OMSCommand,
    OpenCommand,
    PMAnalystEnvelope,
    PMCompletionRecord,
    PMEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    RecommendationType,
    SourceProvenance,
    ThesisQualityEvaluation,
    Verdict,
    VerdictSummary,
    completion_record_schema,
    envelope_schema,
)
from alphamind.commands.submission_results import SubmissionResult

__all__ = [
    "AddCommand",
    "AdjustCommand",
    "AdjustmentCategory",
    "AntiPattern",
    "CancelCommand",
    "CloseCommand",
    "ConcernRecord",
    "CriterionAssessment",
    "ModificationRecord",
    "OMSCommand",
    "OpenCommand",
    "PMAnalystEnvelope",
    "PMCompletionRecord",
    "PMEnvelope",
    "PMResultModel",
    "PMStrategistEnvelope",
    "PositionActionEvaluation",
    "RecommendationType",
    "SourceProvenance",
    "SubmissionLogEntryModel",
    "ThesisQualityEvaluation",
    "Verdict",
    "VerdictSummary",
    "completion_record_schema",
    "envelope_schema",
]


# ---------------------------------------------------------------------------
# Phase-output boundary models — ALP-692
# ---------------------------------------------------------------------------


class _TokensUsedModel(BaseModel):
    """Inline Pydantic projection of :class:`alphamind.analysis._shared.TokensUsed`."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class SubmissionLogEntryModel(BaseModel):
    """Frozen Pydantic projection of :class:`alphamind.commands.submission_log.SubmissionLogEntry`.

    ``envelope`` is already a Pydantic model (``PMEnvelope`` union), so it
    passes through directly. ``submission_results`` is a tuple of Pydantic
    ``SubmissionResult`` models — also passes through directly.

    Order in the tuple is preserved verbatim; ``to_domain`` reconstructs the
    same element sequence.
    """

    model_config = ConfigDict(frozen=True)

    envelope: PMEnvelope
    submission_results: tuple[SubmissionResult, ...]

    @classmethod
    def from_domain(cls, entry: object) -> SubmissionLogEntryModel:
        """Construct from a :class:`SubmissionLogEntry` dataclass instance."""
        from alphamind.commands.submission_log import SubmissionLogEntry

        if not isinstance(entry, SubmissionLogEntry):
            raise TypeError(f"Expected SubmissionLogEntry, got {type(entry).__name__}")
        return cls(
            envelope=entry.envelope,
            submission_results=entry.submission_results,
        )

    def to_domain(self) -> Any:
        """Reconstruct a :class:`SubmissionLogEntry` from this model."""
        from alphamind.commands.submission_log import SubmissionLogEntry

        return SubmissionLogEntry(
            envelope=self.envelope,
            submission_results=self.submission_results,
        )


class PMResultModel(BaseModel):
    """Frozen Pydantic boundary model for
    :class:`alphamind.decision.portfolio_manager.runner.PMResult`.

    Wraps the existing :class:`PMCompletionRecord` Pydantic field directly (it
    is already a Pydantic model) and models the metadata + submission log fields
    explicitly. The ``submission_log`` tuple of
    :class:`SubmissionLogEntryModel` preserves element order and field-by-field
    equality across round-trips.

    Used by ``run_decision_pipeline`` to emit ``pm.json`` under
    ``<archive>/<YYYY-MM-DD>/<id>/phase_outputs/`` when running in
    debug-e2e mode (ALP-692).
    """

    model_config = ConfigDict(frozen=True)

    output: PMCompletionRecord
    submission_log: tuple[SubmissionLogEntryModel, ...]
    retry_count: int
    tokens_used: _TokensUsedModel
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None

    @classmethod
    def from_domain(cls, dc: object) -> PMResultModel:
        """Construct from a :class:`PMResult` dataclass instance."""
        import importlib

        _runner = importlib.import_module("alphamind.decision.portfolio_manager.runner")
        PMResult = _runner.PMResult  # noqa: N806

        if not isinstance(dc, PMResult):
            raise TypeError(f"Expected PMResult, got {type(dc).__name__}")
        tu = dc.tokens_used
        return cls(
            output=dc.output,
            submission_log=tuple(
                SubmissionLogEntryModel.from_domain(entry) for entry in dc.submission_log
            ),
            retry_count=dc.retry_count,
            tokens_used=_TokensUsedModel(
                input_tokens=tu.input_tokens,
                output_tokens=tu.output_tokens,
                cache_read_tokens=tu.cache_read_tokens,
                cache_write_tokens=tu.cache_write_tokens,
            ),
            tool_calls_used=dc.tool_calls_used,
            wall_clock_seconds=dc.wall_clock_seconds,
            stop_reason=dc.stop_reason,
        )

    def to_domain(self) -> Any:
        """Reconstruct a :class:`PMResult` from this model."""
        import importlib

        from alphamind.analysis._shared import TokensUsed

        _runner = importlib.import_module("alphamind.decision.portfolio_manager.runner")
        PMResult = _runner.PMResult  # noqa: N806

        return PMResult(
            output=self.output,
            submission_log=tuple(entry.to_domain() for entry in self.submission_log),
            retry_count=self.retry_count,
            tokens_used=TokensUsed(
                input_tokens=self.tokens_used.input_tokens,
                output_tokens=self.tokens_used.output_tokens,
                cache_read_tokens=self.tokens_used.cache_read_tokens,
                cache_write_tokens=self.tokens_used.cache_write_tokens,
            ),
            tool_calls_used=self.tool_calls_used,
            wall_clock_seconds=self.wall_clock_seconds,
            stop_reason=self.stop_reason,
        )
