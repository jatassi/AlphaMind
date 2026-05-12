"""PMEnvelope validation-result wire-format types.

Layer-2/3 invariant violations and warnings produced by
:func:`alphamind.decision.portfolio_manager.validation.validate_pm_envelope`.
The wire-format types live here in :mod:`alphamind.commands` so the
execution-side Phase 2 write path can reference them without importing
decision-layer code — :class:`ValidationError` rides on
``persist_envelope_rejection``'s argument list to feed
``blocking_criteria`` into the activity log.

Hoisted from :mod:`alphamind.decision.portfolio_manager.validation` by
ALP-458 to break the decision↔execution import cycle.
"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
]


class ValidationError(BaseModel, frozen=True):
    """A single Layer-2/3 violation found in a :class:`PMEnvelope`."""

    field_path: str
    message: str
    criterion: str | None = None


class ValidationWarning(BaseModel, frozen=True):
    """A soft Layer-2 violation that does not disqualify the envelope."""

    field_path: str
    message: str
    criterion: str | None = None


class ValidationResult(BaseModel, frozen=True):
    """Aggregate outcome of running :func:`validate_pm_envelope`."""

    envelope_id: str
    errors: tuple[ValidationError, ...]
    warnings: tuple[ValidationWarning, ...]

    @property
    def is_valid(self) -> bool:
        """``True`` iff ``errors`` is empty. Warnings never disqualify."""
        return len(self.errors) == 0
