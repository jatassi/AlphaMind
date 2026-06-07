"""Canonical validation-result types shared across all LLM-output validators.

Layer-2/3 invariant violations and warnings produced by all agent-side
validators (portfolio-manager, analyst, strategist, domain researchers,
qualitative researcher, adaptive researcher). The types live here in
:mod:`alphamind.commands` so cross-layer consumers (e.g. the execution-side
command execution write path) can reference them without importing decision-layer code.

Originally scoped to :class:`PMEnvelope` validation and hoisted from
:mod:`alphamind.decision.portfolio_manager.validation` by ALP-458.
Extended to serve as the single canonical home for all 7 validators by
ALP-520 — the ``rule`` field supports the analyst/strategist/researcher
vocabulary; ``criterion`` supports the PM evaluation-criterion-set vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from alphamind._kernel.ids import EnvelopeId

__all__ = [
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
]


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A single Layer-2/3 violation found in a validated LLM output.

    ``rule`` carries the analyst/strategist/researcher vocabulary (e.g.
    ``"invalidation_leg_id_pairing"``); ``criterion`` carries the PM
    evaluation-criterion-set vocabulary. ``rule`` defaults to ``""`` (empty)
    and ``criterion`` to ``None`` so neither call-site is forced to supply
    the other's field.
    """

    field_path: str
    message: str
    rule: str = ""
    criterion: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationWarning:
    """A soft Layer-2 violation that does not disqualify the output.

    Same field semantics as :class:`ValidationError` — ``rule`` defaults to
    ``""`` and ``criterion`` to ``None``.
    """

    field_path: str
    message: str
    rule: str = ""
    criterion: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Aggregate outcome of running any LLM-output validator.

    ``errors`` disqualify the output (``is_valid=False``); ``warnings``
    never disqualify. ``envelope_id`` is relevant only to the PM validator;
    all other validators leave it ``None``. ``warnings`` defaults to ``()``
    so analysis-layer validators that produce no warnings need not pass it.
    """

    errors: tuple[ValidationError, ...]
    warnings: tuple[ValidationWarning, ...] = ()
    envelope_id: EnvelopeId | None = None

    @property
    def is_valid(self) -> bool:
        """``True`` iff ``errors`` is empty. Warnings never disqualify."""
        return not self.errors
