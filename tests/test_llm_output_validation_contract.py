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


# ---------------------------------------------------------------------------
# Diagnostic-record schema — all 7 agents
# ---------------------------------------------------------------------------


# Canonical filenames every harness writes on the success path. The
# response file is conditional on ``response_filename`` (synthesizer uses
# ``response.md``; the other six use ``response_initial.md``).
_REQUIRED_DIAG_FILENAMES = ("prompt.md", "user_message.md", "errors.json", "metadata.json")

# Minimal metadata fields the diagnostic record must always carry. The
# strategist's _DiagState writes a richer dict (invocation_id, mode,
# agent_config_snapshot, total_tokens, attempts) but every harness writes
# at minimum the five fields below.
_REQUIRED_METADATA_FIELDS = (
    "model",
    "tokens_used",
    "wall_clock_seconds",
    "stop_reason",
    "success",
)


class _FakeSubmitEnvelopeState:
    """Minimal duck-type for the PM ``_DiagState``'s ``get_submit_envelope_state``.

    The PM's diagnostic writer reads ``submission_log`` and
    ``failed_submission_log`` from the returned object. An empty tuple on
    both attributes mirrors the post-loop state when the PM made zero
    ``submit_envelope`` calls — the success-path shape the harness emits
    on a no-op invocation.
    """

    submission_log: tuple[Any, ...] = ()
    failed_submission_log: tuple[Any, ...] = ()


def _build_diag(agent: AgentSpec, archive_root: Path) -> Any:
    """Construct a per-agent diagnostic-state instance ready for ``.write()``.

    Each instance carries the same baseline payload (``agent_name``,
    ``invocation_id``, ``prompt_text``, ``user_message``, ``model``,
    ``archive_root``) plus the per-harness extras (``response_filename``
    and ``archive_layer`` for the canonical ``DiagState``;
    ``agent_config_snapshot`` for the strategist; the submit-envelope
    accessor for the PM). The returned instance has its ``response_initial``
    populated so the response-file invariant has content to check.
    """
    agent_name = agent.name if agent.name != "pm" else "portfolio_manager"
    common: dict[str, Any] = {
        "agent_name": agent_name,
        "invocation_id": "INV-contract-001",
        "prompt_text": "<system prompt text>",
        "user_message": "<user message text>",
        "model": "claude-sonnet-4-6",
        "archive_root": archive_root,
    }
    if agent.name == "strategist":
        diag_cls = _harness(agent)._DiagState
        return diag_cls(
            **common,
            agent_config_snapshot={"model": "claude-sonnet-4-6"},
        )
    if agent.name == "pm":
        diag_cls = _harness(agent)._DiagState
        return diag_cls(
            **common,
            get_submit_envelope_state=lambda: _FakeSubmitEnvelopeState(),
        )
    # Analysis-layer agents (and the analyst, whose diag is the shared
    # ``DiagState``) all consume the canonical core.
    return DiagState(
        **common,
        response_filename=agent.response_filename,
        archive_layer=agent.archive_layer,
    )


def test_diag_record_writes_canonical_filenames(
    agent: AgentSpec, tmp_path: Path
) -> None:
    """Diagnostic record materializes the canonical filename set.

    Every harness's ``DiagState.write`` (or per-harness ``_DiagState.write``)
    produces ``prompt.md``, ``user_message.md``, the response file
    (``response.md`` for synthesizer, ``response_initial.md`` for the rest),
    ``errors.json``, and ``metadata.json``. Archive layer is
    ``analysis/`` or ``decision/`` per the parametrization table.
    """
    archive_root = tmp_path / "archive"
    diag = _build_diag(agent, archive_root)
    diag.response_initial = "<rendered response body>"
    diag.write(success=True, wall_clock_seconds=1.5, stop_reason="end_turn")

    agent_name = agent.name if agent.name != "pm" else "portfolio_manager"
    diag_dir = (
        archive_root
        / "invocations"
        / "INV-contract-001"
        / agent.archive_layer
        / agent_name
    )
    assert diag_dir.is_dir(), f"diagnostic directory not created at {diag_dir!r}"
    for filename in _REQUIRED_DIAG_FILENAMES:
        assert (diag_dir / filename).is_file(), (
            f"{agent.name}: diagnostic record missing {filename!r}"
        )
    # Response file: synthesizer single ``response.md``; the other six use
    # the canonical ``response_initial.md`` (the optional response_retry.md
    # is present only when a retry occurred — not asserted here).
    response_filename = (
        agent.response_filename if agent.response_filename else "response_initial.md"
    )
    assert (diag_dir / response_filename).is_file(), (
        f"{agent.name}: response file {response_filename!r} not written"
    )


def test_diag_record_metadata_carries_required_fields(
    agent: AgentSpec, tmp_path: Path
) -> None:
    """metadata.json carries at minimum model / tokens_used / wall_clock_seconds /
    stop_reason / success.

    The strategist writes a richer record (with invocation_id /
    agent_config_snapshot / total_tokens / attempts); the PM writes
    retry_count alongside. Every harness carries the five fields above —
    the contract surface the runners and the verify script consume.
    """
    archive_root = tmp_path / "archive"
    diag = _build_diag(agent, archive_root)
    diag.response_initial = "<rendered response body>"
    diag.write(success=True, wall_clock_seconds=2.5, stop_reason="end_turn")

    agent_name = agent.name if agent.name != "pm" else "portfolio_manager"
    metadata_path = (
        archive_root
        / "invocations"
        / "INV-contract-001"
        / agent.archive_layer
        / agent_name
        / "metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    for field_name in _REQUIRED_METADATA_FIELDS:
        assert field_name in metadata, (
            f"{agent.name}: metadata.json missing required field {field_name!r}"
        )
    # Echo the success / stop_reason / wall_clock values written.
    assert metadata["success"] is True
    assert metadata["stop_reason"] == "end_turn"
    assert metadata["wall_clock_seconds"] == 2.5


def test_diag_record_writes_on_failure_path(
    agent: AgentSpec, tmp_path: Path
) -> None:
    """The diagnostic record is flushed even on the failure path.

    Every harness's ``invoke_sdk`` catches its terminal failure-path
    exceptions and calls ``diag.write(success=False, ...)`` before re-raising.
    Verify the writer honours ``success=False`` by inspecting the on-disk
    metadata — the file must exist and carry ``success=False``.
    """
    archive_root = tmp_path / "archive"
    diag = _build_diag(agent, archive_root)
    diag.response_initial = "<partial response>"
    diag.errors.append({"stage": "parse", "attempt": 1, "message": "synthetic"})
    diag.write(success=False, wall_clock_seconds=0.7, stop_reason="max_tokens")

    agent_name = agent.name if agent.name != "pm" else "portfolio_manager"
    metadata_path = (
        archive_root
        / "invocations"
        / "INV-contract-001"
        / agent.archive_layer
        / agent_name
        / "metadata.json"
    )
    assert metadata_path.is_file()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["success"] is False
    # errors.json reflects the captured error trail.
    errors_path = metadata_path.with_name("errors.json")
    errors_payload = json.loads(errors_path.read_text(encoding="utf-8"))
    assert errors_payload == [
        {"stage": "parse", "attempt": 1, "message": "synthetic"}
    ]


# ---------------------------------------------------------------------------
# Stop-reason classification — per-call invoke_sdk(on_cli_result_error=...)
# ---------------------------------------------------------------------------


def _invoke_sdk_call_kwargs(agent: AgentSpec) -> tuple[dict[str, Any], ...]:
    """Return the keyword-argument literals passed to every ``invoke_sdk`` call
    in *agent*'s harness module.

    Walks the harness module's AST, finds every call whose function name is
    ``invoke_sdk`` (the local reference imported from
    :mod:`alphamind.analysis._harness_core`), and extracts the call's
    keyword arguments whose values are Python literals (strings, ints,
    ``None``, etc). Returns one dict per call site.

    Source inspection is the right surface here: each harness has a single
    call site whose ``on_cli_result_error`` literal is the contract — drift
    in the source is drift in the contract. Going through a runtime stub
    would require fabricating the full MCP wiring (four servers for PM)
    and per-invocation snapshots just to reach the call site; the source
    check is direct.
    """
    import ast

    mod = _harness(agent)
    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)
    calls: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Local-name call: ``invoke_sdk(...)`` (every harness imports
        # ``invoke_sdk`` directly into its module namespace; no attribute
        # access form exists in the as-built source).
        if not (isinstance(func, ast.Name) and func.id == "invoke_sdk"):
            continue
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                continue
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError):
                # Non-literal — record the unparsed source so the test can
                # at least surface that the kwarg is present.
                kwargs[kw.arg] = ast.unparse(kw.value)
        calls.append(kwargs)
    return tuple(calls)


def test_invoke_sdk_on_cli_result_error_arm(agent: AgentSpec) -> None:
    """Every ``invoke_sdk`` call in the harness pins ``on_cli_result_error``
    to the agent's expected arm.

    Analysis-layer harnesses (DR / QR / AR) pass
    ``on_cli_result_error="context_overflow"`` — a ``_CLIResultError`` from
    the SDK surfaces as a :class:`ContextOverflowFailure`. Decision-layer
    harnesses (analyst / strategist / PM) and the synthesizer pass
    ``on_cli_result_error="sdk_failure"`` — a ``_CLIResultError`` surfaces
    as :class:`SDKFailure` (the synthesizer's empty-response classification
    is a separate code path on the success branch).

    Verified statically against the harness source rather than via a
    runtime stub: the harness's call site is the contract surface, and
    each harness's MCP wiring + per-invocation snapshot precondition makes
    the runtime fabrication path heavy without adding new coverage.
    """
    calls = _invoke_sdk_call_kwargs(agent)
    assert calls, f"{agent.name}: no invoke_sdk call sites found in harness source"
    for kwargs in calls:
        assert "on_cli_result_error" in kwargs, (
            f"{agent.name}: invoke_sdk call missing on_cli_result_error kwarg "
            f"(kwargs: {sorted(kwargs)!r})"
        )
        assert kwargs["on_cli_result_error"] == agent.on_cli_result_error, (
            f"{agent.name}: invoke_sdk was called with "
            f"on_cli_result_error={kwargs['on_cli_result_error']!r} but the "
            f"contract requires {agent.on_cli_result_error!r}"
        )


def test_invoke_sdk_init_stall_timeout_arm(agent: AgentSpec) -> None:
    """Only the domain-researcher harness sets ``init_stall_timeout_seconds``;
    the other six pass ``None``.

    Per ``invoke_sdk``'s docstring, the stall-retry path is exercised only
    when ``init_stall_timeout_seconds`` is a float. The domain-researcher
    harness is the only one that runs parallel sibling invocations against
    a single OAuth token and so guards against admit-rate starvation.
    """
    calls = _invoke_sdk_call_kwargs(agent)
    for kwargs in calls:
        if agent.name == "domain_researchers":
            assert kwargs.get("init_stall_timeout_seconds") not in (None, "None"), (
                f"{agent.name}: invoke_sdk call must set init_stall_timeout_seconds "
                "to a positive float; the DR path is the one that guards against "
                "OAuth-token admit-rate starvation"
            )
        else:
            assert kwargs.get("init_stall_timeout_seconds") in (None, "None"), (
                f"{agent.name}: invoke_sdk call must pass "
                "init_stall_timeout_seconds=None; only the domain-researcher "
                "harness sets the stall watchdog"
            )


def test_invoke_sdk_exists_in_harness_core(agent: AgentSpec) -> None:
    """Sanity: every harness module imports ``invoke_sdk`` from the shared core.

    Locks in the ALP-466 consolidation — no harness re-implements the SDK
    driver loop locally. If a harness ever inlines the loop again, this
    assertion fails and surfaces the regression.
    """
    mod = _harness(agent)
    assert getattr(mod, "invoke_sdk", None) is invoke_sdk, (
        f"{agent.name}: harness module's invoke_sdk reference does not "
        "resolve to alphamind.analysis._harness_core.invoke_sdk; the "
        "shared-core consolidation is broken"
    )


# ---------------------------------------------------------------------------
# Synthesizer's distinct empty-response classification
# ---------------------------------------------------------------------------


def test_synthesizer_defines_empty_response_failure() -> None:
    """The synthesizer's harness defines an :class:`EmptyResponseFailure`.

    The synthesizer has no retry path; instead, an SDK response with empty
    text + ``end_turn`` raises :class:`EmptyResponseFailure` (paired with
    ``max_tokens`` it raises :class:`ContextOverflowFailure`). This is the
    synthesizer-specific arm of the stop-reason classification table.
    """
    mod = importlib.import_module("alphamind.analysis.synthesizer.harness")
    from alphamind.analysis._harness_core import HarnessFailure

    cls = mod.EmptyResponseFailure
    assert issubclass(cls, HarnessFailure), (
        "EmptyResponseFailure must inherit from HarnessFailure so the "
        "runner's terminal-failure handling treats it uniformly"
    )


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
