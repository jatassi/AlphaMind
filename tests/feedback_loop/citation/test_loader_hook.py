"""The story-05 ``refs`` loader hook, filled by 06d (ALP-886).

``load_window`` reads each in-window agent call's ``output.json`` provenance
artifact, extracts the citation refs per layer, joins to thesis-component
resolutions, and populates :class:`WindowDataset.refs` with one
:class:`RefChain` per upstream reference. The file read is the loader's sole
impurity; the DB is the sanctioned boundary here (real on-disk SQLite, mirroring
``tests/feedback_loop/test_loader.py``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.citation.parser import CitationSource
from alphamind.feedback_loop.dataset import load_window
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.agent_calls_queries import insert_agent_call
from alphamind.state.tables.agent_calls import AgentCallRecord
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)
_TS_IN = "2026-06-01T09:00:00+00:00"
_INV = "inv-refs"
_PLT = "plt-refs-tests"


def _agent_call(call_id: str, agent_name: str, artifact_ref: str | None) -> AgentCallRecord:
    return AgentCallRecord(
        agent_call_id=call_id,
        invocation_id=_INV,
        agent_name=agent_name,
        attempt_number=1,
        model_id="claude-opus-4-8",
        prompt_path=f"prompts/{agent_name}.md",
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json='{"temperature": 1.0}',
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=10,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=artifact_ref,
    )


def _write_output(dir_path: Path, payload: object) -> str:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "output.json").write_text(json.dumps(payload), encoding="utf-8")
    return str(dir_path)


@pytest.fixture()
async def session(tmp_path: Path) -> AsyncIterator[tuple[AsyncSession, Path]]:
    db_path = tmp_path / "refs_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        inv = stub_invocation_row(_INV, process_lifetime_id=_PLT)
        inv.start_at = _TS_IN
        sess.add(inv)
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async, tmp_path
    await async_engine.dispose()


class TestRefsHook:
    async def test_synthesizer_citations_populate_refs(
        self, session: tuple[AsyncSession, Path]
    ) -> None:
        sess, root = session
        # The synthesizer's retrieval store carries the universe of upstream refs;
        # synthesis_text cites a subset.
        synth_payload = {
            "synthesis_text": "Lean tech [SA-TECH-1]; the rest are uncited.",
            "retrieval_store": {
                "entries": {"SA-TECH-1": "...", "SA-FIN-1": "...", "QR-1": "..."},
                "freshness_by_source": {},
            },
        }
        ref = _write_output(root / "synth", synth_payload)
        await insert_agent_call(sess, _agent_call("synth", "synthesizer", ref))
        await sess.commit()

        dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)
        by_id = {c.ref.ref_id: c for c in dataset.refs.citations}

        # Every upstream ref in the retrieval store is represented.
        assert set(by_id) == {"SA-TECH-1", "SA-FIN-1", "QR-1"}
        assert by_id["SA-TECH-1"].cited_in_synthesis is True
        assert by_id["SA-FIN-1"].cited_in_synthesis is False
        assert by_id["QR-1"].cited_in_synthesis is False
        assert by_id["SA-TECH-1"].ref.source is CitationSource.SA_TECH

    async def test_empty_when_no_artifacts(self, session: tuple[AsyncSession, Path]) -> None:
        sess, _root = session
        await insert_agent_call(sess, _agent_call("synth", "synthesizer", None))
        await sess.commit()
        dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)
        assert dataset.refs.citations == ()

    async def test_in_window_agent_calls_read_once_per_load_window(
        self, session: tuple[AsyncSession, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The windowed agent-call read is shared between load_window's own bundle and
        # the refs hook; it must hit the DB exactly once per load_window (ALP-912 E).
        sess, root = session
        synth_payload = {
            "synthesis_text": "see [SA-TECH-1]",
            "retrieval_store": {"entries": {"SA-TECH-1": "..."}, "freshness_by_source": {}},
        }
        ref = _write_output(root / "synth", synth_payload)
        await insert_agent_call(sess, _agent_call("synth", "synthesizer", ref))
        await sess.commit()

        from alphamind.state.repository import agent_calls_queries

        real_read = agent_calls_queries.read_agent_calls_in_window
        calls = 0

        async def _counting_read(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return await real_read(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "alphamind.feedback_loop.dataset.read_agent_calls_in_window", _counting_read
        )

        dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)
        assert calls == 1
        # The single read still feeds both the agent_calls bundle and the refs hook.
        assert {c.agent_call_id for c in dataset.agent_calls} == {"synth"}
        assert {c.ref.ref_id for c in dataset.refs.citations} == {"SA-TECH-1"}

    async def test_decision_citations_and_thesis_resolution_join(
        self, session: tuple[AsyncSession, Path]
    ) -> None:
        sess, root = session
        synth_payload = {
            "synthesis_text": "see [SA-TECH-1] and [QR-1]",
            "retrieval_store": {
                "entries": {"SA-TECH-1": "...", "QR-1": "...", "AR-1": "..."},
                "freshness_by_source": {},
            },
        }
        # Analyst cites SA-TECH-1 directly; recommendation narrative carries it.
        analyst_payload = {
            "recommendations": [
                {"thesis_narrative": "Driven by [SA-TECH-1].", "source_references": ["QR-1"]}
            ]
        }
        synth_ref = _write_output(root / "synth", synth_payload)
        analyst_ref = _write_output(root / "analyst", analyst_payload)
        await insert_agent_call(sess, _agent_call("synth", "synthesizer", synth_ref))
        await insert_agent_call(sess, _agent_call("analyst", "analyst", analyst_ref))
        await sess.commit()

        dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)
        by_id = {c.ref.ref_id: c for c in dataset.refs.citations}
        assert by_id["SA-TECH-1"].cited_in_decision is True
        assert by_id["QR-1"].cited_in_decision is True  # via source_references
        assert by_id["AR-1"].cited_in_decision is False
        # No thesis components seeded → no resolution.
        assert by_id["SA-TECH-1"].thesis_component_outcome is None
