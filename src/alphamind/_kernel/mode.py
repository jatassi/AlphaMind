"""Consolidated ``PipelineMode`` enum for orchestrator → agent translation.

Three parallel ``Mode``-translation helpers historically lived across the
composition layer:

* ``pipeline/decision.py`` carried two dict literals
  (``_ANALYST_MODE_FOR_PIPELINE`` and ``_STRATEGIST_MODE_FOR_PIPELINE``) that
  translated the pipeline's ``"normal" | "halt"`` mode into the analyst's
  ``"normal" | "watchlist"`` and the strategist's
  ``"normal" | "defensive_posture"`` literal types.
* ``scheduler/orchestrator.py`` carried ``_mode_to_decision_literal`` that
  translated the config-layer :class:`alphamind.config.models.modes.Mode`
  into the decision-pipeline's ``"normal" | "halt"`` literal.
* ``scheduler/invocation.py`` carried ``_mode_to_active_mode_literal`` that
  translated the same :class:`Mode` into the row-layer ``ActiveMode``
  (``"normal" | "defensive_posture" | "halted"``).

ALP-472 consolidates the four translations onto a single :class:`PipelineMode`
enum with typed ``to_*`` methods. Adding a new mode tightens every downstream
``to_*`` call site via the ``Literal`` return type — ``mypy --strict`` flags
every site that does not handle the new member.

Kernel-leaf invariant: this module imports nothing first-party. The
:meth:`PipelineMode.from_config_mode` adapter accepts the
:class:`alphamind.config.models.modes.Mode` instance duck-typed via its
``.value`` attribute so the kernel does not pull the config layer into its
own dependency closure.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

__all__ = ["PipelineMode"]


class PipelineMode(StrEnum):
    """Canonical pipeline-mode vocabulary.

    Member values match the wire vocabulary the row-layer and decision-layer
    consumers persist or render. ``NORMAL`` and ``HALT`` are the two
    runtime-resolvable values produced by the resolver's mode-resolution path
    today; ``DEFENSIVE_POSTURE`` is reserved for the operator-pinning
    mechanism a future story lands (parent issue ``ALP-431`` § Notes for the
    orchestrator — surfacing condition iv) and is unreachable from the
    current ``Mode`` enum.
    """

    NORMAL = "normal"
    HALT = "halt"
    DEFENSIVE_POSTURE = "defensive_posture"

    def to_analyst_pipeline_mode(self) -> Literal["normal", "watchlist"]:
        """Translate to the analyst runner's mode literal.

        Mirrors the pre-refactor dispatch table
        ``{"normal": "normal", "halt": "watchlist"}`` from
        ``pipeline/decision.py``. ``DEFENSIVE_POSTURE`` is unreachable from
        the current decision-pipeline mode input (``Literal["normal",
        "halt"]``) and raises ``ValueError`` if it ever surfaces.
        """
        if self is PipelineMode.NORMAL:
            return "normal"
        if self is PipelineMode.HALT:
            return "watchlist"
        msg = (
            f"PipelineMode.{self.name} has no analyst-pipeline-mode mapping; "
            "the analyst runner accepts only 'normal' | 'watchlist'"
        )
        raise ValueError(msg)

    def to_strategist_pipeline_mode(self) -> Literal["normal", "defensive_posture"]:
        """Translate to the strategist runner's mode literal.

        Mirrors the pre-refactor dispatch table
        ``{"normal": "normal", "halt": "defensive_posture"}`` from
        ``pipeline/decision.py``. ``DEFENSIVE_POSTURE`` is unreachable from
        the current decision-pipeline mode input (``Literal["normal",
        "halt"]``) and raises ``ValueError`` if it ever surfaces.
        """
        if self is PipelineMode.NORMAL:
            return "normal"
        if self is PipelineMode.HALT:
            return "defensive_posture"
        msg = (
            f"PipelineMode.{self.name} has no strategist-pipeline-mode mapping; "
            "the strategist runner accepts only 'normal' | 'defensive_posture'"
        )
        raise ValueError(msg)

    def to_decision_literal(self) -> Literal["normal", "halt"]:
        """Translate to the decision-pipeline ``mode`` literal.

        ``run_decision_pipeline`` takes ``mode: Literal["normal", "halt"]``;
        the orchestrator must narrow ``PipelineMode`` (which carries the
        broader vocabulary) before threading the value through. The current
        runner does not handle ``DEFENSIVE_POSTURE``; if a future story
        widens the decision-pipeline contract, this method updates in lock-
        step with the new ``Literal`` return type and ``mypy --strict`` flags
        every caller that does not adapt.
        """
        if self is PipelineMode.NORMAL:
            return "normal"
        if self is PipelineMode.HALT:
            return "halt"
        msg = (
            f"PipelineMode.{self.name} has no decision-pipeline literal mapping; "
            "run_decision_pipeline accepts only 'normal' | 'halt'"
        )
        raise ValueError(msg)

    def to_active_mode_literal(self) -> Literal["normal", "defensive_posture", "halted"]:
        """Translate to the row-layer ``active_mode`` literal.

        The ``invocations`` table's ``active_mode`` column accepts
        ``"normal" | "defensive_posture" | "halted"``. Direct ``.value`` is
        wrong: ``PipelineMode.HALT.value == "halt"``, but the column accepts
        ``"halted"``. This adapter handles the row-side translation.
        """
        if self is PipelineMode.NORMAL:
            return "normal"
        if self is PipelineMode.HALT:
            return "halted"
        if self is PipelineMode.DEFENSIVE_POSTURE:
            return "defensive_posture"
        msg = (
            f"PipelineMode.{self.name} has no active-mode literal mapping; "
            "the row column accepts only 'normal' | 'defensive_posture' | 'halted'"
        )
        raise ValueError(msg)

    @classmethod
    def from_config_mode(cls, mode: Any) -> PipelineMode:
        """Adapt the config-layer ``Mode`` (or any ``.value``-bearing enum) to ``PipelineMode``.

        Duck-typed on ``mode.value`` so the kernel-leaf contract (no
        first-party imports) holds. Raises ``ValueError`` on an unknown
        value — mirrors the pre-refactor :func:`_mode_to_decision_literal`
        contract: a future ``Mode`` enum expansion that does not have a
        corresponding ``PipelineMode`` member fails fast rather than
        silently translating into a default.
        """
        value = getattr(mode, "value", None)
        if value is None:
            msg = f"from_config_mode expects an enum with ``.value``; got {mode!r}"
            raise TypeError(msg)
        try:
            return cls(value)
        except ValueError as exc:
            valid = tuple(member.value for member in cls)
            msg = f"unexpected Mode value {value!r}; PipelineMode knows only {valid!r}"
            raise ValueError(msg) from exc
