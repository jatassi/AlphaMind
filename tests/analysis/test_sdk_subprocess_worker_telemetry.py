"""agent_calls telemetry transport through the subprocess worker (ALP-907).

Story 04a (ALP-880) built the ``capture_agent_call`` seam and proved it at the
harness level (one row + four provenance artifacts when a ``telemetry_session``
and ``provenance_root`` are supplied). This story is the *transport*: the
worker dispatchers (``_run_*`` in :mod:`alphamind.analysis._sdk_subprocess_worker`)
must open a per-call telemetry ``AsyncSession`` themselves, thread it (with the
``provenance_root`` reconstructed from the payload) into each ``invoke_*`` call,
and commit it before returning — on both the success and failure paths.

These tests drive the worker dispatchers in-process (no real subprocess spawn)
against a real on-disk SQLite via ``DATABASE_PATH`` — the sanctioned DB
substitute — with the Claude Agent SDK stubbed via ``claude_agent_sdk.query``
(the sanctioned LLM boundary). They assert the *worker* opens + commits the
session; the per-harness capture content is already covered by 04a's
harness-level suites.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.analysis import _sdk_subprocess_worker as worker
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.agent_calls_queries import read_agent_calls_for_invocation
from alphamind.state.tables.agent_calls import AgentCallErrorClass
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_AS_OF = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_INV = "inv-worker-telem"
_PLT = "plt-worker-telem"

_MINIMAL_BRIEF_PAYLOAD: dict[str, Any] = {
    "invocation_id": _INV,
    "sector": "tech_semis",
    "signal_quality": "high",
    "signal_quality_reason": None,
    "findings": [
        {
            "finding_id": "SA-TECH-1",
            "headline": "NVDA breakout",
            "tickers": ["NVDA"],
            "signal_type": "price_action",
            "strength": "strong",
            "detail": "NVDA broke resistance.",
        }
    ],
    "anomalies": [],
    "thesis_candidates": [],
}


# ---------------------------------------------------------------------------
# Stub SDK (sanctioned LLM boundary) — mirrors the domain-harness suite's shape
# ---------------------------------------------------------------------------


def _make_sdk_messages(
    structured_output: dict[str, Any] | None,
    *,
    stop_reason: str | None = "end_turn",
) -> list[Any]:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    usage = {
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assistant = AssistantMessage(
        content=[], model="claude-sonnet-4-6", stop_reason=stop_reason, usage=usage
    )
    _ = TextBlock  # keep import surface identical to the harness suite
    result = ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason=stop_reason,
        usage=usage,
        structured_output=structured_output,
    )
    return [assistant, result]


def _make_stub_query(responses: list[list[Any]]) -> Callable[..., AsyncGenerator[Any]]:
    call_count = 0

    async def _stub(**_kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        for msg in responses[idx]:
            yield msg

    return _stub


@pytest.fixture()
def agent_config() -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/tech_semis_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )


@pytest.fixture()
def telemetry_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """On-disk SQLite at ``DATABASE_PATH`` with the invocation FK target seeded.

    The worker opens its own telemetry engine via ``make_async_engine()`` with
    no argument, so it resolves ``DATABASE_PATH`` — which this fixture points
    at the per-test DB.
    """
    db_path = tmp_path / "worker_telem.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        sess.add(stub_invocation_row(_INV, process_lifetime_id=_PLT))
        sess.commit()
    sync_engine.dispose()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    return db_path


@pytest.fixture()
def read_factory(telemetry_db: Path) -> async_sessionmaker[AsyncSession]:
    """A read-back factory against the same DB the worker wrote to."""
    return make_async_session_factory(make_async_engine(str(telemetry_db)))


async def _read_rows(
    read_factory: async_sessionmaker[AsyncSession],
) -> list[Any]:
    async with read_factory() as session:
        return await read_agent_calls_for_invocation(session, _INV)


def _domain_payload(
    agent_config: BaseAgentConfig, *, provenance_root: Path | None
) -> dict[str, Any]:
    return {
        "agent": "domain_researcher",
        "agent_config": agent_config.model_dump(mode="json"),
        "sector": "tech_semis",
        "user_message": "Analyse tech sector.",
        "invocation_id": _INV,
        "as_of": _AS_OF.isoformat(),
        "archive_root": None,
        "provenance_root": str(provenance_root) if provenance_root is not None else None,
        "progress_jsonl_path": None,
        "phase": "domain_researchers",
    }


# ---------------------------------------------------------------------------
# Tracer bullet: the worker opens + commits the telemetry session itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_success_persists_one_row_and_four_artifacts(
    agent_config: BaseAgentConfig,
    tmp_path: Path,
    read_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A successful worker-path domain-researcher call persists exactly one
    agent_calls row + the four provenance files, with output_artifact_ref equal
    to the per-call provenance directory — the worker (not the caller) opened
    and committed the telemetry session."""
    provenance_root = tmp_path / "provenance"
    stub = _make_stub_query([_make_sdk_messages(_MINIMAL_BRIEF_PAYLOAD)])

    with patch("claude_agent_sdk.query", stub):
        result = await worker._run_domain_researcher(
            _domain_payload(agent_config, provenance_root=provenance_root)
        )

    assert result["kind"] == "success"

    rows = await _read_rows(read_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.agent_name == "tech_semis_researcher"
    assert row.success is True
    assert row.error_class is None
    assert row.output_artifact_ref is not None
    pdir = Path(row.output_artifact_ref)
    assert pdir == provenance_root / "invocations" / _INV / "agent_calls" / row.agent_call_id
    for name in ("system_prompt.md", "output_schema.json", "tools_definition.json", "output.json"):
        assert (pdir / name).exists()


pytestmark = pytest.mark.filterwarnings("error::DeprecationWarning")
