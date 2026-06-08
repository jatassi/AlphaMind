"""The story-05 ``refs`` loader hook, filled by 06d (ALP-886).

``load_window`` reads each in-window agent call's ``output.json`` provenance
artifact, extracts the citation refs per layer, joins to thesis-component
resolutions, and populates :class:`WindowDataset.refs` with one
:class:`RefChain` per upstream reference. The file read is the loader's sole
impurity; the DB is the sanctioned boundary here (real on-disk SQLite, mirroring
``tests/feedback_loop/test_loader.py``).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind._kernel.ids import InvocationId
from alphamind.feedback_loop.citation.chain import metric_id_for
from alphamind.feedback_loop.citation.parser import CitationSource
from alphamind.feedback_loop.dataset import load_window
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricId
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.theses import ThesisComponentOutcome
from alphamind.state.repository.agent_calls_queries import insert_agent_call
from alphamind.state.tables.agent_calls import AgentCallRecord
from alphamind.state.tables.theses_codec import record_to_rows
from tests.feedback_loop.metrics._outcome_fixtures import make_resolved_thesis_record
from tests.state._fk_substrate import (
    stub_invocation_row,
    stub_position_row,
    stub_process_lifetime_row,
)

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)
_TS_IN = "2026-06-01T09:00:00+00:00"
_INV = "inv-refs"
_PLT = "plt-refs-tests"


def _agent_call(
    call_id: str, agent_name: str, artifact_ref: str | None, invocation_id: str = _INV
) -> AgentCallRecord:
    return AgentCallRecord(
        agent_call_id=call_id,
        invocation_id=invocation_id,
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


# ---------------------------------------------------------------------------
# ALP-917 — per-invocation citation-chain lineage (de-contamination)
# ---------------------------------------------------------------------------


def _seed_thesis_citing_ref(
    sess: Session,
    *,
    thesis_id: str,
    position_id: str,
    invocation_id: str | None,
    cited_ref: str,
    outcome: ThesisComponentOutcome,
) -> None:
    """Seed a RESOLVED thesis whose first component cites *cited_ref*.

    The component carries *outcome* as its resolution; the parent thesis is
    wired to *invocation_id* (NULL for a legacy row). Reuses the round-trip
    codec for a well-formed parent + component rows, then overrides one
    component's narrative to cite the ref and its outcome.
    """
    record = make_resolved_thesis_record(thesis_id, position_id, resolution_timestamp=_RES_TS)
    if invocation_id is not None:
        record = dataclasses.replace(record, invocation_id=InvocationId(invocation_id))
    thesis_row, comp_rows = record_to_rows(record)
    comp_rows[0].narrative = f"Driven by [{cited_ref}]."
    comp_rows[0].resolution_outcome = outcome.value
    sess.add(stub_position_row(position_id))
    sess.flush()
    sess.add(thesis_row)
    for crow in comp_rows:
        sess.add(crow)
    sess.flush()


_RES_TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
_INV_A = "inv-a"
_INV_B = "inv-b"
_INV_OUT_OF_WINDOW = "inv-oow"


def _synth_payload(ref: str) -> dict[str, object]:
    return {
        "synthesis_text": f"see [{ref}]",
        "retrieval_store": {"entries": {ref: "..."}, "freshness_by_source": {}},
    }


@pytest.fixture()
async def multi_inv_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Path]]:
    """On-disk SQLite with two in-window invocations + one out-of-window invocation."""
    db_path = tmp_path / "lineage.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        for inv_id in (_INV_A, _INV_B, _INV_OUT_OF_WINDOW):
            inv = stub_invocation_row(inv_id, process_lifetime_id=_PLT)
            inv.start_at = _TS_IN
            sess.add(inv)
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory, tmp_path
    finally:
        await async_engine.dispose()


class TestPerInvocationLineage:
    async def test_two_invocations_same_ref_resolve_to_own_component(
        self, multi_inv_db: tuple[async_sessionmaker[AsyncSession], Path]
    ) -> None:
        # inv-a and inv-b both emit QR-1; inv-a's terminus is VALIDATED, inv-b's is
        # WRONG. Each window ref must resolve against its own invocation's component.
        # This is the discriminating case the bare-ref_id design cannot pass.
        factory, root = multi_inv_db
        async with factory() as sess:
            await sess.run_sync(
                lambda s: _seed_thesis_citing_ref(
                    s,
                    thesis_id="thes-a",
                    position_id="pos-a",
                    invocation_id=_INV_A,
                    cited_ref="QR-1",
                    outcome=ThesisComponentOutcome.VALIDATED,
                )
            )
            await sess.run_sync(
                lambda s: _seed_thesis_citing_ref(
                    s,
                    thesis_id="thes-b",
                    position_id="pos-b",
                    invocation_id=_INV_B,
                    cited_ref="QR-1",
                    outcome=ThesisComponentOutcome.WRONG,
                )
            )
            await insert_agent_call(
                sess,
                _agent_call(
                    "synth-a",
                    "synthesizer",
                    _write_output(root / "synth_a", _synth_payload("QR-1")),
                    invocation_id=_INV_A,
                ),
            )
            await insert_agent_call(
                sess,
                _agent_call(
                    "synth-b",
                    "synthesizer",
                    _write_output(root / "synth_b", _synth_payload("QR-1")),
                    invocation_id=_INV_B,
                ),
            )
            await sess.commit()

            dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)

        qr_chains = [c for c in dataset.refs.citations if c.ref.ref_id == "QR-1"]
        outcomes = {c.thesis_component_outcome for c in qr_chains}
        assert outcomes == {ThesisComponentOutcome.VALIDATED, ThesisComponentOutcome.WRONG}
        # Per-source metrics see both correctly-attributed termini: 1 validated of 2.
        survival = metric_id_for("signal_survival_rate", CitationSource.QR)
        validation = metric_id_for("per_source_validation_rate", CitationSource.QR)
        survival_metric = get_metric(survival)
        validation_metric = get_metric(validation)
        assert survival_metric is not None
        assert validation_metric is not None
        assert survival_metric.compute(dataset, UNCONDITIONED).value == 0.5
        assert validation_metric.compute(dataset, UNCONDITIONED).value == 0.5

    async def test_out_of_window_invocation_component_excluded(
        self, multi_inv_db: tuple[async_sessionmaker[AsyncSession], Path]
    ) -> None:
        # A resolved component generated by an invocation with no in-window agent
        # calls must not supply a terminus to an in-window ref.
        factory, root = multi_inv_db
        async with factory() as sess:
            await sess.run_sync(
                lambda s: _seed_thesis_citing_ref(
                    s,
                    thesis_id="thes-oow",
                    position_id="pos-oow",
                    invocation_id=_INV_OUT_OF_WINDOW,
                    cited_ref="QR-1",
                    outcome=ThesisComponentOutcome.VALIDATED,
                )
            )
            # Only inv-a has an in-window agent call; it also emits QR-1.
            await insert_agent_call(
                sess,
                _agent_call(
                    "synth-a",
                    "synthesizer",
                    _write_output(root / "synth_a", _synth_payload("QR-1")),
                    invocation_id=_INV_A,
                ),
            )
            await sess.commit()

            dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)

        by_id = {c.ref.ref_id: c for c in dataset.refs.citations}
        # inv-a's QR-1 has no in-window component → no terminus; the out-of-window
        # VALIDATED component does not leak in.
        assert by_id["QR-1"].thesis_component_outcome is None

    async def test_single_invocation_citation_rates_unchanged_by_partitioning(
        self, multi_inv_db: tuple[async_sessionmaker[AsyncSession], Path]
    ) -> None:
        # Regression guard (out of scope): the citation-rate numerators and
        # synthesizer_recall derive only from the universe / synthesis / decision
        # sets and must be unaffected by per-invocation partitioning. On a single
        # invocation, partitioning is a no-op, so the aggregate values stand.
        factory, root = multi_inv_db
        synth_payload = {
            "synthesis_text": "lean on [QR-1]",
            "retrieval_store": {
                "entries": {"QR-1": "...", "QR-2": "..."},
                "freshness_by_source": {},
            },
        }
        analyst_payload = {
            "recommendations": [{"thesis_narrative": "Driven by [QR-2].", "source_references": []}]
        }
        async with factory() as sess:
            await insert_agent_call(
                sess,
                _agent_call(
                    "synth-a",
                    "synthesizer",
                    _write_output(root / "synth_a", synth_payload),
                    invocation_id=_INV_A,
                ),
            )
            await insert_agent_call(
                sess,
                _agent_call(
                    "analyst-a",
                    "analyst",
                    _write_output(root / "analyst_a", analyst_payload),
                    invocation_id=_INV_A,
                ),
            )
            await sess.commit()
            dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)

        synth_rate = get_metric(metric_id_for("synthesizer_citation_rate", CitationSource.QR))
        decision_rate = get_metric(metric_id_for("decision_layer_citation_rate", CitationSource.QR))
        recall = get_metric(MetricId("synthesizer_recall"))
        assert synth_rate is not None
        assert decision_rate is not None
        assert recall is not None
        # QR-1 cited in synthesis, QR-2 not → 1/2.
        assert synth_rate.compute(dataset, UNCONDITIONED).value == 0.5
        # QR-2 cited in a decision narrative, QR-1 not → 1/2.
        assert decision_rate.compute(dataset, UNCONDITIONED).value == 0.5
        # Of the uncited-by-synthesizer refs (QR-2), 1 recovered by decision → 1/1.
        assert recall.compute(dataset, UNCONDITIONED).value == 1.0

    async def test_legacy_null_invocation_component_excluded(
        self, multi_inv_db: tuple[async_sessionmaker[AsyncSession], Path]
    ) -> None:
        # A component whose parent thesis has a NULL invocation_id contributes no
        # terminus — it has no generating invocation to key on.
        factory, root = multi_inv_db
        async with factory() as sess:
            await sess.run_sync(
                lambda s: _seed_thesis_citing_ref(
                    s,
                    thesis_id="thes-null",
                    position_id="pos-null",
                    invocation_id=None,
                    cited_ref="QR-1",
                    outcome=ThesisComponentOutcome.VALIDATED,
                )
            )
            await insert_agent_call(
                sess,
                _agent_call(
                    "synth-a",
                    "synthesizer",
                    _write_output(root / "synth_a", _synth_payload("QR-1")),
                    invocation_id=_INV_A,
                ),
            )
            await sess.commit()

            dataset = await load_window(sess, _WINDOW_START, _WINDOW_END)

        by_id = {c.ref.ref_id: c for c in dataset.refs.citations}
        assert by_id["QR-1"].thesis_component_outcome is None
