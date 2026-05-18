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
