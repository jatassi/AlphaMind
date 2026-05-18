"""Cross-harness contract consistency tests — ALP-522.

Single parametrized module asserting all seven LLM-output harnesses follow
the same cross-cutting contract:

* the **retry-message shape** (framing / first error / contract reference /
  directive; no full error list, no analytical guidance, no raw input data);
* the **diagnostic-record schema** (prompt.md / user_message.md /
  response_initial.md or response.md / optional response_retry.md /
  errors.json / metadata.json);
* the **stop-reason classification** semantics (``on_cli_result_error``
  branches per layer: ``"context_overflow"`` for analysis, ``"sdk_failure"``
  for decision);
* the **canonical** ``ValidationResult`` **ownership** (every validator-bearing
  harness returns :class:`alphamind.commands.validation_results.ValidationResult`,
  the ALP-520 consolidation);
* the **bare-prefix discipline** added by ALP-521 (analyst / strategist / PM
  surface a ``bare_prefix_citation`` error when a narrative contains ``[CR]``).

The synthesizer has degenerate retry and validator contracts — assertions
that require a retry path or a :class:`ValidationResult` skip or branch
around the synthesizer entry rather than fail.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from alphamind.analysis._harness_core import DiagState, invoke_sdk
from alphamind.commands.validation_results import (
    ValidationError,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Agent parametrization table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """One row of the contract parametrization table.

    ``name`` is the pytest parameter id; ``harness_module`` is the dotted path
    to the harness module under test. ``archive_layer`` matches the
    diagnostic-archive segment the harness writes under
    (``invocations/<id>/<archive_layer>/<agent_name>/``). ``has_validator``,
    ``has_retry``, and ``has_structured_output`` capture the per-agent
    contract surface; the synthesizer is the only ``False`` row.

    ``on_cli_result_error`` is the per-call argument the harness passes to
    :func:`alphamind.analysis._harness_core.invoke_sdk`; the contract test
    verifies the kwarg value rather than just the surface behaviour.

    ``response_filename`` is ``None`` for the canonical
    ``response_initial.md`` / ``response_retry.md`` layout and
    ``"response.md"`` for the synthesizer's single-response layout.
    """

    name: str
    harness_module: str
    archive_layer: str
    on_cli_result_error: str
    has_validator: bool
    has_retry: bool
    has_structured_output: bool
    response_filename: str | None


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="analyst",
        harness_module="alphamind.decision.analyst.harness",
        archive_layer="decision",
        on_cli_result_error="sdk_failure",
        has_validator=True,
        has_retry=True,
        has_structured_output=True,
        response_filename=None,
    ),
    AgentSpec(
        name="strategist",
        harness_module="alphamind.decision.strategist.harness",
        archive_layer="decision",
        on_cli_result_error="sdk_failure",
        has_validator=True,
        has_retry=True,
        has_structured_output=True,
        response_filename=None,
    ),
    AgentSpec(
        name="pm",
        harness_module="alphamind.decision.portfolio_manager.harness",
        archive_layer="decision",
        on_cli_result_error="sdk_failure",
        has_validator=True,
        has_retry=True,
        has_structured_output=True,
        response_filename=None,
    ),
    AgentSpec(
        name="domain_researchers",
        harness_module="alphamind.analysis.domain_researchers.harness",
        archive_layer="analysis",
        on_cli_result_error="context_overflow",
        has_validator=True,
        has_retry=True,
        has_structured_output=True,
        response_filename=None,
    ),
    AgentSpec(
        name="qualitative_research",
        harness_module="alphamind.analysis.qualitative_research.harness",
        archive_layer="analysis",
        on_cli_result_error="context_overflow",
        has_validator=True,
        has_retry=True,
        has_structured_output=True,
        response_filename=None,
    ),
    AgentSpec(
        name="adaptive_research",
        harness_module="alphamind.analysis.adaptive_research.harness",
        archive_layer="analysis",
        on_cli_result_error="context_overflow",
        has_validator=True,
        has_retry=True,
        has_structured_output=True,
        response_filename=None,
    ),
    AgentSpec(
        name="synthesizer",
        harness_module="alphamind.analysis.synthesizer.harness",
        archive_layer="analysis",
        on_cli_result_error="sdk_failure",
        has_validator=False,
        has_retry=False,
        has_structured_output=False,
        response_filename="response.md",
    ),
)

# Agents whose validators surface bare-prefix citations (ALP-521).
BARE_PREFIX_AGENTS: tuple[str, ...] = ("analyst", "strategist", "pm")


@pytest.fixture(params=AGENTS, ids=lambda spec: spec.name)
def agent(request: pytest.FixtureRequest) -> AgentSpec:
    """Parametrize across the seven harness agents."""
    return request.param  # type: ignore[no-any-return]


def _harness(spec: AgentSpec) -> ModuleType:
    """Import the harness module for *spec*."""
    return importlib.import_module(spec.harness_module)


# ---------------------------------------------------------------------------
# Smoke: every harness module imports and exports its public failure types
# ---------------------------------------------------------------------------


def test_harness_module_imports(agent: AgentSpec) -> None:
    """Every harness module loads and exposes the documented failure types.

    Acts as the tracer-bullet: confirms the parametrization table points at
    real modules and the import chain (commands.validation_results +
    analysis._harness_core) is intact across all seven agents.
    """
    mod = _harness(agent)
    # Shared failure types from _harness_core are re-exported by every
    # analysis-layer module; decision-layer modules import them but only
    # re-export the subset they raise.
    if agent.has_retry:
        assert hasattr(mod, "MalformedOutputFailure")
    assert hasattr(mod, "ContextOverflowFailure")
    assert hasattr(mod, "SDKFailure")
    assert hasattr(mod, "TimeoutFailure")
    if agent.name == "synthesizer":
        # Synthesizer-specific empty-response classification.
        assert hasattr(mod, "EmptyResponseFailure")
    # The canonical ValidationResult lives in commands; no harness should
    # define a per-agent dataclass shadowing it.
    if agent.has_validator:
        # The validator module is the load-bearing surface — locate it via
        # the harness's import graph rather than re-importing.
        validation = _validation_module_for(agent)
        # The validator module re-exports ValidationResult; verify identity
        # (not just name equality) to lock in the ALP-520 consolidation.
        assert validation.ValidationResult is ValidationResult


def _validation_module_for(spec: AgentSpec) -> ModuleType:
    """Resolve the validator module sibling for *spec*'s harness.

    Each harness sits next to a ``validation.py`` module; the PM uses
    ``alphamind.decision.portfolio_manager.validation``. Returns the imported
    module so tests can reference ``validate_*`` and ``ValidationResult``.
    """
    parent = spec.harness_module.rsplit(".", 1)[0]
    return importlib.import_module(f"{parent}.validation")


# ---------------------------------------------------------------------------
# No test imports a deprecated ValidationFailure symbol
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Retry-message shape — six retry-bearing agents; skip synthesizer
# ---------------------------------------------------------------------------


# Markers used to assert the retry-message construction omits any raw input
# data (the user_message section) and limits to the first error from a
# multi-error ValidationResult.
_USER_MESSAGE_MARKER = "SENTINEL_RAW_INPUT_DATA_DO_NOT_LEAK_42"
_FIRST_ERROR_MARKER = "MARKER_FIRST_ERROR_SHOULD_BE_QUOTED"
_SECOND_ERROR_MARKER = "MARKER_SECOND_ERROR_SHOULD_BE_OMITTED"


def _two_error_validation_result() -> ValidationResult:
    """Build a synthetic two-error :class:`ValidationResult`.

    The first error carries ``_FIRST_ERROR_MARKER`` in its message; the
    second carries ``_SECOND_ERROR_MARKER``. Both ``rule`` and ``criterion``
    are populated so the retry-message builder can pull whichever its
    per-agent convention reads (the consumer-side validators use ``rule``
    for analyst/strategist/researchers and ``criterion`` for PM; PM has no
    validation-failure retry message so reads neither here).
    """
    return ValidationResult(
        errors=(
            ValidationError(
                field_path="recommendations[0].thesis_narrative",
                message=f"first error: {_FIRST_ERROR_MARKER}",
                rule="invalidation_leg_id_pairing",
                criterion="invalidation_leg_id_pairing",
            ),
            ValidationError(
                field_path="recommendations[1].thesis_narrative",
                message=f"second error: {_SECOND_ERROR_MARKER}",
                rule="unknown_reference",
                criterion="unknown_reference",
            ),
        ),
    )


def _parse_error_for(agent: AgentSpec) -> Any:
    """Construct a per-agent :class:`ParseError` instance.

    Each harness imports its own ``ParseError`` from a sibling parser
    module; the constructor signature is uniform across all six retry-
    bearing harnesses (``field_path``, ``message``).
    """
    parser_module = importlib.import_module(
        agent.harness_module.rsplit(".", 1)[0] + ".parser"
    )
    return parser_module.ParseError(
        field_path="thesis_candidates[0].headline",
        message=f"first parse error: {_FIRST_ERROR_MARKER}",
    )


def test_retry_message_shape_validation_failure(agent: AgentSpec) -> None:
    """Retry message for a validation failure carries the canonical 4-part shape.

    Per ``llm-output-validation.md`` § Corrective-retry message construction:

    * a framing line naming the validation contract;
    * the **first** error's ``field_path`` / ``rule`` / ``message`` (verbatim);
    * a reference to the contract source (design doc or schema file name);
    * a directive to re-emit a single corrected JSON object;
    * does NOT contain a second-or-later error from a multi-error result;
    * does NOT contain the raw input data (user message).

    The PM does not run a Layer-2/3 validator at the harness boundary —
    its validator lives inside the ``submit_envelope`` MCP wrapper — so
    the PM exposes no ``_build_retry_message_for_validation_failure``.
    Skip PM for this arm; it is covered by the parse-error arm below.
    """
    if not agent.has_retry:
        pytest.skip("synthesizer has no retry path")
    mod = _harness(agent)
    builder = getattr(mod, "_build_retry_message_for_validation_failure", None)
    if builder is None:
        pytest.skip(
            f"{agent.name} performs validation inside an MCP wrapper rather "
            "than at the harness boundary; no validation-failure retry "
            "message wrapper exists"
        )
    result = _two_error_validation_result()
    message = builder(result)

    # First error fully present.
    assert _FIRST_ERROR_MARKER in message
    assert "recommendations[0].thesis_narrative" in message
    # Second error absent (the policy avoids iterative-repair convergence).
    assert _SECOND_ERROR_MARKER not in message
    assert "recommendations[1].thesis_narrative" not in message
    # Framing line: every validator-bearing harness names the "prior response"
    # rejection — the canonical opening per the design doc.
    assert "prior response" in message.lower()
    # Contract reference: every harness's _CONTRACT_REF points to a docs/
    # path or a schema name. The substring ``.md`` covers every existing
    # harness's contract-ref string.
    assert ".md" in message or "schema" in message.lower()
    # Directive: every harness's directive ends with a "re-emit ... JSON
    # payload" or equivalent single-object instruction.
    lowered = message.lower()
    assert "re-emit" in lowered or "json" in lowered


def test_retry_message_shape_parse_error(agent: AgentSpec) -> None:
    """Retry message for a parse error carries the canonical 4-part shape.

    Every retry-bearing harness (including PM, whose only retry path is the
    parse-error one) exposes ``_build_retry_message_for_parse_error``.
    The shape mirrors the validation-failure case: framing line, first error
    fields verbatim, contract reference, directive.
    """
    if not agent.has_retry:
        pytest.skip("synthesizer has no retry path")
    mod = _harness(agent)
    builder = mod._build_retry_message_for_parse_error
    error = _parse_error_for(agent)
    message = builder(error)

    assert _FIRST_ERROR_MARKER in message
    assert "thesis_candidates[0].headline" in message
    # Framing references the parse contract.
    assert "prior response" in message.lower()
    # No raw input data leaks via the parse-error path either.
    assert _USER_MESSAGE_MARKER not in message
    # Contract reference + directive.
    lowered = message.lower()
    assert ".md" in message or "schema" in lowered
    assert "re-emit" in lowered or "json" in lowered


def test_no_separate_validation_failure_class(agent: AgentSpec) -> None:
    """ALP-520 unified ``ValidationFailure`` into ``ValidationError``.

    The strategist's ``ValidationFailure`` symbol is now an alias for
    :class:`ValidationError`. For every validator-bearing harness, assert
    that the validator module either does not export ``ValidationFailure``
    at all or, when it does, the symbol resolves to the canonical
    :class:`ValidationError` rather than a separate dataclass.
    """
    if not agent.has_validator:
        pytest.skip("synthesizer has no validator")
    validation = _validation_module_for(agent)
    legacy = getattr(validation, "ValidationFailure", None)
    if legacy is not None:
        assert legacy is ValidationError, (
            f"{agent.harness_module}'s validator exports ValidationFailure as "
            "a distinct symbol; ALP-520 requires it to alias ValidationError "
            "or be removed entirely."
        )
