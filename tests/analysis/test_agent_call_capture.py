"""Tests for agent_calls telemetry capture (ALP-880).

The capture layer extends the existing ``DiagState``/``diag_dir`` mechanism so
every completed LLM agent call persists:

* one ``agent_calls`` row (provenance + metrics + outcome), and
* four raw provenance artifacts under
  ``data/provenance/invocations/{invocation_id}/agent_calls/{agent_call_id}/``.

These tests cover the pure functional core (record assembly, error-class
mapping, provenance path) and the imperative shell (file writes + async DB
insert) in isolation; the harness-integration tests live alongside each
harness's existing suite.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.analysis._agent_call_capture import (
    AgentCallCapture,
    error_class_for_failure,
    persist_agent_call,
    provenance_dir,
)
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
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

_INV = "inv-cap-001"
_PLT = "plt-cap-tests"


@pytest.fixture()
async def async_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite with the invocation FK target seeded."""
    db_path = tmp_path / "capture_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        sess.add(stub_invocation_row(_INV, process_lifetime_id=_PLT))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    yield make_async_session_factory(async_engine)
    await async_engine.dispose()


def _capture(**overrides: object) -> AgentCallCapture:
    base: dict[str, object] = {
        "agent_call_id": "ac-001",
        "invocation_id": _INV,
        "agent_name": "demo_agent",
        "attempt_number": 1,
        "model_id": "claude-sonnet-4-6",
        "prompt_path": "prompts/demo.md",
        "prompt_git_sha": "abc123",
        "prompt_content_hash": "deadbeef",
        "system_prompt_text": "SYSTEM PROMPT",
        "sampling_params": {"max_tokens": 4096},
        "output_schema": {"type": "object"},
        "tools_definition": ["mcp__x__tool"],
        "output_payload": {"verdict": "ok"},
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "wall_clock_ms": 1234,
        "stop_reason": "end_turn",
        "success": True,
        "error_class": None,
        "error_message": None,
    }
    base.update(overrides)
    return AgentCallCapture(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# prompt_git_sha memoization (ALP-909 L3)
# ---------------------------------------------------------------------------


def test_prompt_git_sha_memoizes_by_path_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git hash-object`` runs at most once per ``(path, mtime)``; an unchanged
    file is served from the cache, a changed mtime recomputes."""
    import subprocess

    from alphamind.analysis import _agent_call_capture as cap

    prompt = tmp_path / "demo.md"
    prompt.write_text("v1", encoding="utf-8")

    calls: list[str] = []
    real_run = subprocess.run

    def _counting_run(args: Any, **kwargs: Any) -> Any:
        calls.append(str(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(cap, "_PROMPT_GIT_SHA_CACHE", {})
    monkeypatch.setattr("alphamind.analysis._agent_call_capture.subprocess.run", _counting_run)

    sha1 = cap.prompt_git_sha(str(prompt))
    sha2 = cap.prompt_git_sha(str(prompt))  # unchanged → cache hit, no re-spawn
    assert sha1 == sha2
    assert len(calls) == 1

    # Mutate the file so its mtime + size change → recompute.
    import os

    prompt.write_text("v2-longer", encoding="utf-8")
    os.utime(prompt, (prompt.stat().st_atime, prompt.stat().st_mtime + 5))
    sha3 = cap.prompt_git_sha(str(prompt))
    assert len(calls) == 2
    assert sha3 != sha1  # different content → different blob SHA


def test_prompt_git_sha_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing prompt file yields ``""`` and never raises (best-effort contract)."""
    from alphamind.analysis import _agent_call_capture as cap

    monkeypatch.setattr(cap, "_PROMPT_GIT_SHA_CACHE", {})
    assert cap.prompt_git_sha(str(tmp_path / "nope.md")) == ""


def test_prompt_git_sha_does_not_leak_across_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two distinct prompt paths get distinct cache entries (no cross-path leak)."""
    from alphamind.analysis import _agent_call_capture as cap

    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("alpha", encoding="utf-8")
    b.write_text("beta", encoding="utf-8")

    monkeypatch.setattr(cap, "_PROMPT_GIT_SHA_CACHE", {})
    assert cap.prompt_git_sha(str(a)) != cap.prompt_git_sha(str(b))


# ---------------------------------------------------------------------------
# Provenance path
# ---------------------------------------------------------------------------


def test_provenance_dir_uses_documented_layout(tmp_path: Path) -> None:
    got = provenance_dir(provenance_root=tmp_path, invocation_id=_INV, agent_call_id="ac-001")
    assert got == tmp_path / "invocations" / _INV / "agent_calls" / "ac-001"


# ---------------------------------------------------------------------------
# Error-class mapping (one per HarnessFailure subclass)
# ---------------------------------------------------------------------------


def test_error_class_timeout() -> None:
    exc = TimeoutFailure("t", agent_name="a", invocation_id=_INV)
    assert error_class_for_failure(exc) is AgentCallErrorClass.timeout


def test_error_class_malformed_output() -> None:
    exc = MalformedOutputFailure("m", agent_name="a", invocation_id=_INV)
    assert error_class_for_failure(exc) is AgentCallErrorClass.malformed_output


def test_error_class_context_overflow() -> None:
    exc = ContextOverflowFailure("c", agent_name="a", invocation_id=_INV)
    assert error_class_for_failure(exc) is AgentCallErrorClass.context_overflow


def test_error_class_sdk_failure_maps_to_model_api_error() -> None:
    exc = SDKFailure("s", agent_name="a", invocation_id=_INV)
    assert error_class_for_failure(exc) is AgentCallErrorClass.model_api_error


def test_error_class_empty_response_maps_to_empty_response() -> None:
    """``EmptyResponseFailure`` maps to its own class, not model_api_error (ALP-909 C4)."""
    from alphamind.analysis.synthesizer.harness import EmptyResponseFailure

    exc = EmptyResponseFailure("e", agent_name="a", invocation_id=_INV)
    assert error_class_for_failure(exc) is AgentCallErrorClass.empty_response


# ---------------------------------------------------------------------------
# Record assembly + artifact write (functional core + fs shell)
# ---------------------------------------------------------------------------


def test_to_record_carries_provenance_and_metrics(tmp_path: Path) -> None:
    capture = _capture()
    record = capture.to_record(provenance_root=tmp_path)
    assert record.agent_call_id == "ac-001"
    assert record.invocation_id == _INV
    assert record.agent_name == "demo_agent"
    assert record.model_id == "claude-sonnet-4-6"
    assert record.prompt_path == "prompts/demo.md"
    assert record.prompt_git_sha == "abc123"
    assert record.prompt_content_hash == "deadbeef"
    assert record.input_tokens == 100
    assert record.output_tokens == 50
    assert record.cache_read_tokens == 10
    assert record.cache_write_tokens == 5
    assert record.wall_clock_ms == 1234
    assert record.stop_reason == "end_turn"
    assert record.success is True
    assert record.error_class is None
    assert json.loads(record.sampling_params_json) == {"max_tokens": 4096}
    # The artifact reference points at the per-call provenance directory.
    assert record.output_artifact_ref == str(
        tmp_path / "invocations" / _INV / "agent_calls" / "ac-001"
    )


def test_write_artifacts_emits_four_files(tmp_path: Path) -> None:
    capture = _capture()
    capture.write_artifacts(provenance_root=tmp_path)
    pdir = tmp_path / "invocations" / _INV / "agent_calls" / "ac-001"
    assert (pdir / "system_prompt.md").read_text(encoding="utf-8") == "SYSTEM PROMPT"
    assert json.loads((pdir / "output_schema.json").read_text()) == {"type": "object"}
    assert json.loads((pdir / "tools_definition.json").read_text()) == ["mcp__x__tool"]
    assert json.loads((pdir / "output.json").read_text()) == {"verdict": "ok"}


def test_write_artifacts_emits_null_payloads_for_missing_optional_fields(tmp_path: Path) -> None:
    """A narrative agent (no schema / no tools / no structured output) still
    gets all four files so the layout is uniform across the 9 agents."""
    capture = _capture(output_schema=None, tools_definition=None, output_payload=None)
    capture.write_artifacts(provenance_root=tmp_path)
    pdir = tmp_path / "invocations" / _INV / "agent_calls" / "ac-001"
    assert (pdir / "system_prompt.md").exists()
    assert json.loads((pdir / "output_schema.json").read_text()) is None
    assert json.loads((pdir / "tools_definition.json").read_text()) is None
    assert json.loads((pdir / "output.json").read_text()) is None


def test_failure_capture_records_error_class(tmp_path: Path) -> None:
    capture = _capture(
        success=False,
        stop_reason="error",
        error_class=AgentCallErrorClass.timeout,
        error_message="exceeded budget",
        output_payload=None,
    )
    record = capture.to_record(provenance_root=tmp_path)
    assert record.success is False
    assert record.error_class is AgentCallErrorClass.timeout
    assert record.error_message == "exceeded budget"


# ---------------------------------------------------------------------------
# Async persist shell — artifacts + row land together (DB boundary mocked-out
# via a real in-memory SQLite, the sanctioned DB substitute)
# ---------------------------------------------------------------------------


async def test_persist_writes_one_row_and_four_artifacts(
    tmp_path: Path, async_factory: async_sessionmaker[AsyncSession]
) -> None:
    capture = _capture(invocation_id=_INV)
    provenance_root = tmp_path / "provenance"

    async with async_factory() as session:
        await persist_agent_call(session, capture, provenance_root=provenance_root)
        await session.commit()

    async with async_factory() as session:
        rows = await read_agent_calls_for_invocation(session, _INV)

    assert len(rows) == 1
    row = rows[0]
    assert row.agent_call_id == "ac-001"
    assert row.success is True
    pdir = provenance_dir(
        provenance_root=provenance_root, invocation_id=_INV, agent_call_id="ac-001"
    )
    assert row.output_artifact_ref == str(pdir)
    for name in ("system_prompt.md", "output_schema.json", "tools_definition.json", "output.json"):
        assert (pdir / name).exists()


pytestmark = pytest.mark.filterwarnings("error::DeprecationWarning")
