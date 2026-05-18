"""Cross-harness contract consistency tests — ALP-522.

Asserts all seven LLM-output harnesses follow the same cross-cutting
contract: retry-message shape, diagnostic-record schema, stop-reason
classification semantics, canonical :class:`ValidationResult` ownership
(ALP-520), and bare-prefix discipline (ALP-521).

The synthesizer has degenerate retry and validator contracts —
assertions that require either skip or branch around the synthesizer
rather than fail.
"""

from __future__ import annotations

import ast
import importlib
import json
import typing
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from alphamind._kernel.ids import InvocationId
from alphamind.analysis._harness_core import (
    CLIResultErrorMapping,
    DiagState,
    HarnessFailure,
    invoke_sdk,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.validation_results import ValidationError, ValidationResult

_CLI_RESULT_ERROR_ARMS: frozenset[str] = frozenset(typing.get_args(CLIResultErrorMapping))
_EMPTY_RETRIEVAL_STORE = RetrievalStore(entries={}, freshness_by_source={})

# ---------------------------------------------------------------------------
# Agent parametrization table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """One row of the contract parametrization table."""

    name: str
    harness_module: str
    archive_layer: str
    on_cli_result_error: str
    has_validator: bool
    has_retry: bool
    has_structured_output: bool
    response_filename: str | None


def _spec(
    name: str,
    module: str,
    layer: str,
    cli_err: str,
    *,
    has_full: bool = True,
    response_filename: str | None = None,
) -> AgentSpec:
    """Factory for the parametrization table — ``has_full=False`` is the
    synthesizer's degenerate-contract row (no validator, no retry, no
    structured output)."""
    return AgentSpec(
        name=name,
        harness_module=module,
        archive_layer=layer,
        on_cli_result_error=cli_err,
        has_validator=has_full,
        has_retry=has_full,
        has_structured_output=has_full,
        response_filename=response_filename,
    )


AGENTS: tuple[AgentSpec, ...] = (
    _spec("analyst", "alphamind.decision.analyst.harness", "decision", "sdk_failure"),
    _spec("strategist", "alphamind.decision.strategist.harness", "decision", "sdk_failure"),
    _spec("pm", "alphamind.decision.portfolio_manager.harness", "decision", "sdk_failure"),
    _spec(
        "domain_researchers",
        "alphamind.analysis.domain_researchers.harness",
        "analysis",
        "context_overflow",
    ),
    _spec(
        "qualitative_research",
        "alphamind.analysis.qualitative_research.harness",
        "analysis",
        "context_overflow",
    ),
    _spec(
        "adaptive_research",
        "alphamind.analysis.adaptive_research.harness",
        "analysis",
        "context_overflow",
    ),
    _spec(
        "synthesizer",
        "alphamind.analysis.synthesizer.harness",
        "analysis",
        "sdk_failure",
        has_full=False,
        response_filename="response.md",
    ),
)

BARE_PREFIX_AGENTS = ("analyst", "strategist", "pm")

_VALIDATOR_FUNCTIONS: dict[str, str] = {
    "analyst": "validate_analyst_output",
    "strategist": "validate_strategist_output",
    "pm": "validate_pm_envelope",
    "domain_researchers": "validate_brief",
    "qualitative_research": "validate_qualitative_brief",
    "adaptive_research": "validate_adaptive_brief",
}


@pytest.fixture(params=AGENTS, ids=lambda spec: spec.name)
def agent(request: pytest.FixtureRequest) -> AgentSpec:
    """Parametrize across the seven harness agents."""
    return request.param  # type: ignore[no-any-return]


def _harness(spec: AgentSpec) -> ModuleType:
    return importlib.import_module(spec.harness_module)


def _validation_module_for(spec: AgentSpec) -> ModuleType:
    """Import the validator module sibling for *spec*'s harness."""
    parent = spec.harness_module.rsplit(".", 1)[0]
    return importlib.import_module(f"{parent}.validation")


def _diag_dir(agent: AgentSpec, archive_root: Path) -> Path:
    agent_name = "portfolio_manager" if agent.name == "pm" else agent.name
    return archive_root / "invocations" / "INV-contract-001" / agent.archive_layer / agent_name


# ---------------------------------------------------------------------------
# Smoke: every harness module imports and re-exports its public surface
# ---------------------------------------------------------------------------


def test_harness_module_imports(agent: AgentSpec) -> None:
    """Every harness module loads with the documented failure types and
    the canonical :class:`ValidationResult` re-export (when applicable).
    """
    mod = _harness(agent)
    if agent.has_retry:
        assert hasattr(mod, "MalformedOutputFailure")
    assert hasattr(mod, "ContextOverflowFailure")
    assert hasattr(mod, "SDKFailure")
    assert hasattr(mod, "TimeoutFailure")
    if agent.name == "synthesizer":
        assert hasattr(mod, "EmptyResponseFailure")
    if agent.has_validator:
        validation = _validation_module_for(agent)
        assert validation.ValidationResult is ValidationResult


def test_invoke_sdk_exists_in_harness_core(agent: AgentSpec) -> None:
    """Every harness module's ``invoke_sdk`` reference points at the shared
    core function (ALP-466 consolidation)."""
    mod = _harness(agent)
    assert getattr(mod, "invoke_sdk", None) is invoke_sdk


def test_synthesizer_defines_empty_response_failure() -> None:
    """Synthesizer-specific arm of the stop-reason classification table:
    empty response + ``end_turn`` raises :class:`EmptyResponseFailure`."""
    mod = importlib.import_module("alphamind.analysis.synthesizer.harness")
    assert issubclass(mod.EmptyResponseFailure, HarnessFailure)


# ---------------------------------------------------------------------------
# Retry-message shape — six retry-bearing agents; skip synthesizer
# ---------------------------------------------------------------------------


_USER_MESSAGE_MARKER = "SENTINEL_RAW_INPUT_DATA_DO_NOT_LEAK_42"
_FIRST_ERROR_MARKER = "MARKER_FIRST_ERROR_SHOULD_BE_QUOTED"
_SECOND_ERROR_MARKER = "MARKER_SECOND_ERROR_SHOULD_BE_OMITTED"


def _two_error_validation_result() -> ValidationResult:
    """Synthetic two-error ValidationResult. First error carries the
    first-error marker; second carries the second-error marker. Both
    ``rule`` and ``criterion`` are set so each per-agent retry-message
    builder reads its documented field."""
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
    parser = importlib.import_module(agent.harness_module.rsplit(".", 1)[0] + ".parser")
    return parser.ParseError(
        field_path="thesis_candidates[0].headline",
        message=f"first parse error: {_FIRST_ERROR_MARKER}",
    )


def _assert_canonical_retry_shape(message: str) -> None:
    """Assert *message* satisfies the canonical 4-part retry shape.

    Per ``llm-output-validation.md`` § Corrective-retry message
    construction: framing line, first-error fields, contract reference,
    directive. No raw input data leaks via the user-message marker.
    """
    lowered = message.lower()
    assert "prior response" in lowered
    assert _FIRST_ERROR_MARKER in message
    assert _USER_MESSAGE_MARKER not in message
    assert ".md" in message or "schema" in lowered
    assert "re-emit" in lowered or "json" in lowered


def test_retry_message_shape_validation_failure(agent: AgentSpec) -> None:
    """Validation-failure retry carries the canonical 4-part shape and
    omits the second-or-later error (the iterative-repair-avoidance
    policy).

    PM has no harness-level validation-failure retry — its Layer-2/3
    validator lives inside the submit_envelope MCP wrapper — so PM is
    covered by the parse-error arm below.
    """
    if not agent.has_retry:
        pytest.skip("synthesizer has no retry path")
    mod = _harness(agent)
    builder = getattr(mod, "_build_retry_message_for_validation_failure", None)
    if builder is None:
        pytest.skip(
            f"{agent.name} performs validation inside an MCP wrapper rather "
            "than at the harness boundary"
        )
    message = builder(_two_error_validation_result())
    _assert_canonical_retry_shape(message)
    assert "recommendations[0].thesis_narrative" in message
    # The second-or-later error is omitted by policy.
    assert _SECOND_ERROR_MARKER not in message
    assert "recommendations[1].thesis_narrative" not in message


def test_retry_message_shape_parse_error(agent: AgentSpec) -> None:
    """Parse-error retry carries the canonical 4-part shape."""
    if not agent.has_retry:
        pytest.skip("synthesizer has no retry path")
    mod = _harness(agent)
    message = mod._build_retry_message_for_parse_error(_parse_error_for(agent))
    _assert_canonical_retry_shape(message)
    assert "thesis_candidates[0].headline" in message


# ---------------------------------------------------------------------------
# Diagnostic-record schema — all 7 agents
# ---------------------------------------------------------------------------


_REQUIRED_DIAG_FILENAMES = ("prompt.md", "user_message.md", "errors.json", "metadata.json")
_REQUIRED_METADATA_FIELDS = ("model", "tokens_used", "wall_clock_seconds", "stop_reason", "success")


class _FakeSubmitEnvelopeState:
    """Duck-type for the PM ``_DiagState``'s submit-envelope accessor:
    empty submission_log + empty failed_submission_log mirrors the
    no-op-PM success path."""

    submission_log: tuple[Any, ...] = ()
    failed_submission_log: tuple[Any, ...] = ()


def _build_diag(agent: AgentSpec, archive_root: Path) -> Any:
    """Construct *agent*'s diagnostic-state instance ready for ``.write()``.

    Strategist + PM use private ``_DiagState`` classes (carrying
    ``agent_config_snapshot`` and the submit-envelope accessor
    respectively); the other five reuse the canonical ``DiagState``.
    """
    agent_name = "portfolio_manager" if agent.name == "pm" else agent.name
    common: dict[str, Any] = {
        "agent_name": agent_name,
        "invocation_id": "INV-contract-001",
        "prompt_text": "<system prompt text>",
        "user_message": "<user message text>",
        "model": "claude-sonnet-4-6",
        "archive_root": archive_root,
    }
    if agent.name == "strategist":
        return _harness(agent)._DiagState(
            **common,
            agent_config_snapshot={"model": "claude-sonnet-4-6"},
        )
    if agent.name == "pm":
        return _harness(agent)._DiagState(
            **common,
            get_submit_envelope_state=lambda: _FakeSubmitEnvelopeState(),
        )
    return DiagState(
        **common,
        response_filename=agent.response_filename,
        archive_layer=agent.archive_layer,
    )


def test_diag_record_writes_canonical_filenames(agent: AgentSpec, tmp_path: Path) -> None:
    """Diagnostic record materialises the canonical filename set under
    ``invocations/<id>/<archive_layer>/<agent>/``."""
    archive_root = tmp_path / "archive"
    diag = _build_diag(agent, archive_root)
    diag.response_initial = "<rendered response body>"
    diag.write(success=True, wall_clock_seconds=1.5, stop_reason="end_turn")

    diag_dir = _diag_dir(agent, archive_root)
    assert diag_dir.is_dir()
    for filename in _REQUIRED_DIAG_FILENAMES:
        assert (diag_dir / filename).is_file(), f"{agent.name}: missing {filename}"
    response_filename = agent.response_filename or "response_initial.md"
    assert (diag_dir / response_filename).is_file()


def test_diag_record_metadata_carries_required_fields(agent: AgentSpec, tmp_path: Path) -> None:
    """metadata.json carries at minimum model / tokens_used /
    wall_clock_seconds / stop_reason / success."""
    archive_root = tmp_path / "archive"
    diag = _build_diag(agent, archive_root)
    diag.response_initial = "<rendered response body>"
    diag.write(success=True, wall_clock_seconds=2.5, stop_reason="end_turn")

    metadata = json.loads((_diag_dir(agent, archive_root) / "metadata.json").read_text())
    for field_name in _REQUIRED_METADATA_FIELDS:
        assert field_name in metadata, f"{agent.name}: metadata missing {field_name}"
    assert metadata["success"] is True
    assert metadata["stop_reason"] == "end_turn"
    assert metadata["wall_clock_seconds"] == 2.5


def test_diag_record_writes_on_failure_path(agent: AgentSpec, tmp_path: Path) -> None:
    """Diagnostic writer flushes on the failure path (success=False)
    with the captured error trail in errors.json."""
    archive_root = tmp_path / "archive"
    diag = _build_diag(agent, archive_root)
    diag.response_initial = "<partial response>"
    diag.errors.append({"stage": "parse", "attempt": 1, "message": "synthetic"})
    diag.write(success=False, wall_clock_seconds=0.7, stop_reason="max_tokens")

    diag_dir = _diag_dir(agent, archive_root)
    metadata = json.loads((diag_dir / "metadata.json").read_text())
    assert metadata["success"] is False
    assert json.loads((diag_dir / "errors.json").read_text()) == [
        {"stage": "parse", "attempt": 1, "message": "synthetic"}
    ]


# ---------------------------------------------------------------------------
# Stop-reason classification — per-call invoke_sdk kwargs
# ---------------------------------------------------------------------------


@cache
def _invoke_sdk_call_kwargs(agent: AgentSpec) -> tuple[dict[str, Any], ...]:
    """Statically extract the keyword arguments of every ``invoke_sdk(...)``
    call in *agent*'s harness source.

    Source inspection is the right surface here: each harness has a
    single call site whose ``on_cli_result_error`` literal is the
    contract, and runtime stubbing would require fabricating four MCP
    servers' worth of decision-layer state to reach the call site.

    Cached because two parametrized tests share the result; AgentSpec is
    frozen so it's hashable.
    """
    mod = _harness(agent)
    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    calls: list[dict[str, Any]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "invoke_sdk"):
            continue
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                continue
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError):
                kwargs[kw.arg] = ast.unparse(kw.value)
        calls.append(kwargs)
    return tuple(calls)


def test_invoke_sdk_on_cli_result_error_arm(agent: AgentSpec) -> None:
    """Every ``invoke_sdk`` call pins ``on_cli_result_error`` to the
    agent's expected classification arm — ``"context_overflow"`` for
    analysis harnesses, ``"sdk_failure"`` for decision and synthesizer.
    """
    assert agent.on_cli_result_error in _CLI_RESULT_ERROR_ARMS, (
        f"{agent.name}: expected arm {agent.on_cli_result_error!r} is not in "
        f"CLIResultErrorMapping {_CLI_RESULT_ERROR_ARMS}; AGENTS table is "
        "out of sync with the production type alias"
    )
    calls = _invoke_sdk_call_kwargs(agent)
    assert calls, f"{agent.name}: no invoke_sdk call sites found"
    for kwargs in calls:
        assert kwargs.get("on_cli_result_error") == agent.on_cli_result_error, (
            f"{agent.name}: invoke_sdk on_cli_result_error="
            f"{kwargs.get('on_cli_result_error')!r}; contract requires "
            f"{agent.on_cli_result_error!r}"
        )


def test_invoke_sdk_init_stall_timeout_arm(agent: AgentSpec) -> None:
    """Only the domain-researcher harness sets ``init_stall_timeout_seconds``
    (it's the one that races sibling invocations against a single OAuth
    token); the other six pass ``None``."""
    for kwargs in _invoke_sdk_call_kwargs(agent):
        value = kwargs.get("init_stall_timeout_seconds")
        if agent.name == "domain_researchers":
            assert value not in (None, "None"), f"{agent.name}: must set init_stall_timeout_seconds"
        else:
            assert value in (None, "None"), f"{agent.name}: init_stall_timeout_seconds must be None"


# ---------------------------------------------------------------------------
# Canonical ValidationResult ownership
# ---------------------------------------------------------------------------


def test_validator_return_type_is_canonical_validation_result(agent: AgentSpec) -> None:
    """Each validator's return annotation resolves to the canonical
    :class:`ValidationResult` (ALP-520 consolidation)."""
    if not agent.has_validator:
        pytest.skip("synthesizer has no validator")
    fn = getattr(_validation_module_for(agent), _VALIDATOR_FUNCTIONS[agent.name])
    hints = typing.get_type_hints(fn)
    assert hints["return"] is ValidationResult


def _build_minimal_output_for(agent: AgentSpec) -> Any:
    """Build the simplest valid output payload for *agent*'s validator."""
    if agent.name == "analyst":
        from alphamind.decision.analyst.models import AnalystOutput

        return AnalystOutput(
            invocation_id=InvocationId("inv-contract-001"),
            timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            mode="normal",
            recommendations=(),
        )
    if agent.name == "strategist":
        from alphamind.decision.strategist.models import (
            PortfolioLevelObservations,
            StrategistOutput,
        )

        return StrategistOutput(
            invocation_id=InvocationId("inv-contract-001"),
            timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=PortfolioLevelObservations(
                aggregate_thesis_health="quiet",
                sector_balance_shifts="none",
                thesis_dependency_warnings="none",
                capital_allocation_observations="cash-heavy",
            ),
        )
    if agent.name == "domain_researchers":
        from alphamind.analysis._shared import Sector
        from alphamind.analysis.domain_researchers.models import SectorBrief, SignalQuality

        return SectorBrief(
            invocation_id="inv-contract-001",
            sector=Sector.TECH_SEMIS,
            signal_quality=SignalQuality.HIGH,
            signal_quality_reason=None,
            findings=(),
            anomalies=(),
            thesis_candidates=(),
        )
    if agent.name == "qualitative_research":
        from alphamind.analysis.qualitative_research.models import (
            EvidenceLine,
            NarrativeThread,
            QualitativeBrief,
            SentimentSnapshot,
            SignalQuality,
            ThreadDirection,
            TimeHorizon,
        )

        return QualitativeBrief(
            invocation_id="inv-contract-001",
            signal_quality=SignalQuality.HIGH,
            signal_quality_reason=None,
            threads=(
                NarrativeThread(
                    thread_id="QR-1",
                    summary="quiet day",
                    relevance="market",
                    direction=ThreadDirection.MIXED,
                    subject="market",
                    time_horizon=TimeHorizon.IMMEDIATE,
                    evidence=(
                        EvidenceLine(source_type="news", observation="obs", citation="cite"),
                        EvidenceLine(source_type="social", observation="obs2", citation="cite2"),
                    ),
                    implication="hold",
                ),
            ),
            catalyst_watches=(),
            sentiment_snapshot=SentimentSnapshot(
                extremes="none", divergences="none", regime="neutral"
            ),
        )
    if agent.name == "adaptive_research":
        from alphamind.analysis.adaptive_research.models import AdaptiveBrief

        return AdaptiveBrief(
            invocation_id="inv-contract-001",
            threads_investigated_count=0,
            anomalies_triaged_count=0,
            anomalies_deferred=(),
            threads=(),
        )
    raise AssertionError(f"no minimal-output builder for {agent.name!r}")


def _build_validator_kwargs(agent: AgentSpec) -> dict[str, Any]:
    """Construct the per-validator keyword inputs required to invoke it."""
    active_sectors = frozenset({"tech", "semis", "financials", "energy"})
    if agent.name in {"analyst", "strategist"}:
        return {"retrieval_store": _EMPTY_RETRIEVAL_STORE, "active_sectors": active_sectors}
    if agent.name in {"qualitative_research", "domain_researchers"}:
        return {}
    if agent.name == "adaptive_research":
        from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

        qr_spec = next(a for a in AGENTS if a.name == "qualitative_research")
        return {
            "sector_briefs": (),
            "qualitative_brief": _build_minimal_output_for(qr_spec),
            "correlation_regime_brief": CorrelationRegimeBrief(
                text="(no correlation context)",
                reference_index={},
                freshness_min=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            ),
        }
    raise AssertionError(f"no validator-kwargs builder for {agent.name!r}")


def test_validator_returns_validation_result_instance(agent: AgentSpec) -> None:
    """Invoking the validator on a minimal-valid output returns an actual
    :class:`ValidationResult` instance (runtime confirmation).

    PM is skipped — its input surface (ProposalPreProcessorBundle +
    PortfolioManagerView + sector_resolver) is fabricated only in the
    per-harness test fixtures; the annotation-surface test +
    ``test_harness_module_imports`` identity check cover PM here.
    """
    if not agent.has_validator:
        pytest.skip("synthesizer has no validator")
    if agent.name == "pm":
        pytest.skip("PM validator inputs are fabricated only in per-harness fixtures")
    fn = getattr(_validation_module_for(agent), _VALIDATOR_FUNCTIONS[agent.name])
    result = fn(_build_minimal_output_for(agent), **_build_validator_kwargs(agent))
    assert isinstance(result, ValidationResult)


# ---------------------------------------------------------------------------
# Bare-prefix discipline — analyst, strategist, pm only
# ---------------------------------------------------------------------------


def _bare_prefix_helper_for(agent: AgentSpec) -> Any:
    """Return *agent*'s narrative-references helper. PM names it differently
    (``_check_narrative_references_against_store``) because PM has both a
    wrapper iterating envelope narratives and the underlying per-narrative
    helper. Analyst/strategist expose the per-narrative helper directly.
    """
    validation = _validation_module_for(agent)
    if agent.name == "pm":
        return validation._check_narrative_references_against_store
    return validation._check_narrative_references


def test_bare_prefix_citation_detected(agent: AgentSpec) -> None:
    """Analyst / strategist / PM surface ``[CR]`` (a bare ReferencePrefix
    body with no ``-N`` index) as a ``bare_prefix_citation`` error
    (ALP-521 wiring of ``find_bare_prefix_citations``).

    Analyst/strategist tag via the ``rule`` field; PM tags via
    ``criterion`` (its evaluation-criterion-set vocabulary).
    """
    if agent.name not in BARE_PREFIX_AGENTS:
        pytest.skip("consumer-side discipline only")
    helper = _bare_prefix_helper_for(agent)
    errors = tuple(
        helper(
            "The thesis is supported by [CR] and other context.",
            field_path="dummy.field",
            retrieval_store=RetrievalStore(entries={}, freshness_by_source={}),
        )
    )
    bare_prefix_errors = [e for e in errors if "bare_prefix_citation" in (e.rule, e.criterion)]
    assert bare_prefix_errors, (
        f"{agent.name}: no bare_prefix_citation error surfaced for [CR]; "
        f"tags={[(e.rule, e.criterion) for e in errors]!r}"
    )
    err = bare_prefix_errors[0]
    if agent.name == "pm":
        assert err.criterion == "bare_prefix_citation"
    else:
        assert err.rule == "bare_prefix_citation"


def test_no_separate_validation_failure_class(agent: AgentSpec) -> None:
    """ALP-520 unified ``ValidationFailure`` into ``ValidationError``.

    When a validator module still exports ``ValidationFailure`` (the
    strategist keeps it for backwards compatibility), it must be the
    canonical :class:`ValidationError` rather than a distinct class.
    """
    if not agent.has_validator:
        pytest.skip("synthesizer has no validator")
    validation = _validation_module_for(agent)
    legacy = getattr(validation, "ValidationFailure", None)
    if legacy is not None:
        assert legacy is ValidationError, (
            f"{agent.harness_module}: ValidationFailure is a distinct class; "
            "ALP-520 requires it to alias ValidationError or be removed"
        )
