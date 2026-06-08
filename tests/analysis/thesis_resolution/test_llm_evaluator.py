"""Tests for the targeted LLM component-evaluator — ALP-898.

The evaluator is a CONSUMER of the shared analysis harness core
(``alphamind.analysis._harness_core``): it drives one focused, minimal-context
SDK call for a single ambiguous/qualitative thesis component and returns a
schema-validated :class:`ThesisComponentOutcome` + resolution notes. Every test
mocks only the two sanctioned boundaries — the Claude Agent SDK
(``sdk_query_fn``) and the database (an on-disk SQLite session).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind._kernel.ids import ThesisId
from alphamind.analysis._harness_core import MalformedOutputFailure
from alphamind.analysis.thesis_resolution.llm_evaluator import (
    ComponentLLMOutcome,
    evaluate_component_llm,
)
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Constants / fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
_PROMPT_PATH = "prompts/analysis/thesis_component_evaluator.md"

# A leakage canary: the narrative + assumptions of the component under
# evaluation carry these tokens; a "full thesis" string carries the FORBIDDEN
# token, which the focused context must never contain.
_NARRATIVE = "AMD gaming revenue should re-accelerate into the next print."
_ASSUMPTION_TEXT = "Console refresh cycle lifts AMD gaming segment QoQ."
_MARKET_DATA = "AMD gaming revenue +18% QoQ in the latest 10-Q; console units up."
_FORBIDDEN_FULL_THESIS_TOKEN = "NVDA-long-leg-bracket-parameters"


@pytest.fixture()
def agent_config() -> BaseAgentConfig:
    """A non-roster config pointing at the committed minimal-eval prompt.

    Constructing a ``BaseAgentConfig`` is not roster enrolment — the roster is
    the closed ``AgentName``-keyed ``AgentsConfig.agents`` dict. The resolver
    (04e) supplies a config like this on demand.
    """
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=_PROMPT_PATH,
        latency_budget_seconds=120,
        context_token_budget=4000,
        output_token_budget=2000,
        tools=[],
    )


def _make_component(
    component_type: ThesisComponentType = ThesisComponentType.ENTRY_RATIONALE,
) -> ThesisComponent:
    return ThesisComponent(
        component_id="c1",
        thesis_id=ThesisId("t1"),
        component_type=component_type,
        linked_bracket_leg_type=None,
        instrument_reference="AMD",
        narrative=_NARRATIVE,
        key_assumptions=(KeyAssumption(text=_ASSUMPTION_TEXT, outcome=None),),
        generation_timestamp=NOW,
        resolution_outcome=None,
        resolution_notes=None,
    )


def _eval_payload(
    outcome: str = "VALIDATED",
    notes: str = "Gaming revenue re-accelerated as assumed.",
) -> dict[str, Any]:
    return {"outcome": outcome, "notes": notes}


def _make_sdk_response(
    structured_output: dict[str, Any] | None,
    *,
    text: str = "",
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> list[Any]:
    """Minimal SDK message sequence; structured_output rides ResultMessage."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    assistant = AssistantMessage(
        content=[TextBlock(text=text)] if text else [],
        model="claude-sonnet-4-6",
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )
    result = ResultMessage(
        subtype="result",
        duration_ms=500,
        duration_api_ms=450,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        structured_output=structured_output,
    )
    return [assistant, result]


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _make_stub_query(
    responses: list[list[Any]],
) -> Callable[..., AsyncGenerator[Any]]:
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        async for msg in _async_iter(responses[idx]):
            yield msg

    return _stub


# ---------------------------------------------------------------------------
# 1. Happy path — schema-validated outcome + notes returned
# ---------------------------------------------------------------------------


async def test_returns_validated_outcome_and_notes(agent_config: BaseAgentConfig) -> None:
    """The evaluator parses the structured output into a ComponentLLMOutcome."""
    stub = _make_stub_query([_make_sdk_response(_eval_payload("VALIDATED", "Held up."))])

    result = await evaluate_component_llm(
        _make_component(),
        _MARKET_DATA,
        agent_config=agent_config,
        invocation_id="inv-001",
        sdk_query_fn=stub,
    )

    assert isinstance(result, ComponentLLMOutcome)
    assert result.outcome == ThesisComponentOutcome.VALIDATED
    assert result.notes == "Held up."


@pytest.mark.parametrize(
    "raw_outcome",
    ["VALIDATED", "WRONG", "INCONCLUSIVE"],
)
async def test_each_outcome_member_parses(agent_config: BaseAgentConfig, raw_outcome: str) -> None:
    """All three ThesisComponentOutcome members round-trip from the payload."""
    stub = _make_stub_query([_make_sdk_response(_eval_payload(raw_outcome, "n"))])

    result = await evaluate_component_llm(
        _make_component(),
        _MARKET_DATA,
        agent_config=agent_config,
        invocation_id="inv-002",
        sdk_query_fn=stub,
    )

    assert result.outcome == ThesisComponentOutcome(raw_outcome)


# ---------------------------------------------------------------------------
# 2. Minimal-context: the assembled user message carries ONLY the single
#    component + market data, never the full thesis.
# ---------------------------------------------------------------------------


async def test_focused_context_sends_component_and_market_data_only(
    agent_config: BaseAgentConfig,
) -> None:
    """The user-message prompt contains the component narrative, its key
    assumptions, the instrument, and the market data — and nothing from the
    wider thesis (the forbidden token is absent)."""
    captured_prompts: list[str] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        async for msg in _async_iter(_make_sdk_response(_eval_payload())):
            yield msg

    await evaluate_component_llm(
        _make_component(),
        _MARKET_DATA,
        agent_config=agent_config,
        invocation_id="inv-003",
        sdk_query_fn=_capturing_stub,
    )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert _NARRATIVE in prompt
    assert _ASSUMPTION_TEXT in prompt
    assert "AMD" in prompt
    assert _MARKET_DATA in prompt
    # The full-thesis material must never be assembled into the focused call.
    assert _FORBIDDEN_FULL_THESIS_TOKEN not in prompt


# ---------------------------------------------------------------------------
# 3. Options: json_schema output mode, no tools, autonomous-agent contract.
# ---------------------------------------------------------------------------


async def test_options_use_json_schema_mode_and_no_tools(
    agent_config: BaseAgentConfig,
) -> None:
    """The SDK options flip on output_format json_schema and disable tools."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_eval_payload())):
            yield msg

    await evaluate_component_llm(
        _make_component(),
        _MARKET_DATA,
        agent_config=agent_config,
        invocation_id="inv-004",
        sdk_query_fn=_capturing_stub,
    )

    assert len(captured_options) == 1
    options = captured_options[0]
    assert options.allowed_tools == []
    assert options.tools == []
    assert options.setting_sources == []
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"
    # The schema constrains the outcome to the closed StrEnum vocabulary.
    schema_str = json.dumps(options.output_format["schema"])
    assert "VALIDATED" in schema_str
    assert "INCONCLUSIVE" in schema_str


# ---------------------------------------------------------------------------
# 4. Malformed structured output → MalformedOutputFailure (SDK populated None)
# ---------------------------------------------------------------------------


async def test_missing_structured_output_raises_malformed(
    agent_config: BaseAgentConfig,
) -> None:
    """When the SDK fails to populate structured_output the evaluator raises
    MalformedOutputFailure carrying the agent identity."""
    stub = _make_stub_query([_make_sdk_response(None)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await evaluate_component_llm(
            _make_component(),
            _MARKET_DATA,
            agent_config=agent_config,
            invocation_id="inv-005",
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert err.agent_name == "thesis_component_evaluator"
    assert err.invocation_id == "inv-005"


async def test_invalid_outcome_value_raises_malformed(
    agent_config: BaseAgentConfig,
) -> None:
    """A structured payload whose outcome is outside the enum is rejected."""
    stub = _make_stub_query([_make_sdk_response(_eval_payload("MAYBE", "n"))])

    with pytest.raises(MalformedOutputFailure):
        await evaluate_component_llm(
            _make_component(),
            _MARKET_DATA,
            agent_config=agent_config,
            invocation_id="inv-006",
            sdk_query_fn=stub,
        )


# ---------------------------------------------------------------------------
# 5. agent_calls capture — the call flows through _harness_core so one
#    agent_calls row is written (mocking SDK + DB only).
# ---------------------------------------------------------------------------

_INV_TELEM = "inv-telem-evaluator"
_PLT_TELEM = "plt-telem-evaluator"


@pytest.fixture()
async def telemetry_factory(tmp_path: Path):  # type: ignore[no-untyped-def]
    """On-disk SQLite with the invocations FK target seeded for the row."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
    from alphamind.persistence.models import Base
    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
        make_engine,
        make_session_factory,
    )
    from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

    db_path = tmp_path / "telem.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT_TELEM))
        sess.flush()
        sess.add(stub_invocation_row(_INV_TELEM, process_lifetime_id=_PLT_TELEM))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    yield factory
    await async_engine.dispose()


async def test_capture_writes_one_agent_calls_row(
    agent_config: BaseAgentConfig,
    tmp_path: Path,
    telemetry_factory: Any,
) -> None:
    """The evaluator's call rides _harness_core's capture seam: exactly one
    agent_calls row, named for the non-roster evaluator, with the structured
    output payload and schema ref persisted."""
    from alphamind.state.repository.agent_calls_queries import read_agent_calls_for_invocation

    stub = _make_stub_query([_make_sdk_response(_eval_payload("WRONG", "Catalyst missed."))])
    provenance_root = tmp_path / "provenance"

    async with telemetry_factory() as session:
        await evaluate_component_llm(
            _make_component(),
            _MARKET_DATA,
            agent_config=agent_config,
            invocation_id=_INV_TELEM,
            sdk_query_fn=stub,
            telemetry_session=session,
            provenance_root=provenance_root,
        )
        await session.commit()

    async with telemetry_factory() as session:
        rows = await read_agent_calls_for_invocation(session, _INV_TELEM)

    assert len(rows) == 1
    row = rows[0]
    assert row.agent_name == "thesis_component_evaluator"
    assert row.success is True
    # Structured-output agent: schema ref + output payload are persisted.
    assert row.output_schema_ref is not None
    assert row.output_artifact_ref is not None
    pdir = Path(row.output_artifact_ref)
    assert json.loads((pdir / "output.json").read_text()) == _eval_payload(
        "WRONG", "Catalyst missed."
    )
