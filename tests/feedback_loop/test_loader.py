"""WindowDataset loader + pure-core seam (ALP-882 story 05).

* ``load_window`` against an in-memory-style SQLite (on-disk tmp file, mirroring
  the agent_calls table tests) returns a ``WindowDataset`` containing only the
  in-window records, with the ``refs`` / ``replays`` extension sub-bundles empty.
* The loader is the *only* DB-touching function — a metric ``compute`` runs purely
  over a hand-built ``WindowDataset`` with no session in scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.dataset import (
    RefsBundle,
    ReplaysBundle,
    WindowDataset,
    load_window,
)
from alphamind.feedback_loop.metrics.types import (
    UNCONDITIONED,
    Conditioning,
    Metric,
    MetricId,
    MetricResult,
    Window,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    ValidationId,
    ValidationRecord,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.agent_calls_queries import insert_agent_call
from alphamind.state.repository.validation_queries import insert_validation
from alphamind.state.tables.agent_calls import AgentCallRecord
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

# ---------------------------------------------------------------------------
# Window + seed constants
# ---------------------------------------------------------------------------

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)

_TS_IN = "2026-06-01T09:00:00+00:00"  # inside the window
_TS_OUT = "2026-01-01T09:00:00+00:00"  # before the window

_INV_IN = "inv-in-window"
_INV_OUT = "inv-out-window"
_PLT = "plt-loader-tests"

_VAL_ID = ValidationId("val-loader-1")


def _agent_call(call_id: str, invocation_id: str) -> AgentCallRecord:
    return AgentCallRecord(
        agent_call_id=call_id,
        invocation_id=invocation_id,
        agent_name="analyst",
        attempt_number=1,
        model_id="claude-opus-4-8",
        prompt_path="prompts/analysis/analyst.md",
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json='{"temperature": 1.0}',
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=1200,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


def _validation() -> ValidationRecord:
    return ValidationRecord(
        validation_id=_VAL_ID,
        registered_at=datetime(2026, 6, 2, tzinfo=UTC),
        registered_by_session_id=None,
        edited_artifact="prompts/decision/strategist.md",
        pre_edit_version="abc1234",
        post_edit_version="def5678",
        registered_regime="normal",
        registered_model_id="claude-sonnet-4-5",
        watched_metric_ids=(MetricId("decision_proposals_per_invocation"),),
        window_length_days=7,
        expected_direction=ExpectedDirection.IMPROVED,
        expected_magnitude="5% relative",
        success_criterion="win_rate up 5%",
        failure_criterion="win_rate down 3%",
        evaluation_due_at=datetime(2026, 6, 9, tzinfo=UTC),
        superseded_at=None,
        superseded_reason=None,
    )


@pytest.fixture()
async def seeded_session(
    tmp_path: Path,
) -> AsyncIterator[AsyncSession]:
    """On-disk SQLite seeded with in/out-of-window agent_calls + one validation."""
    db_path = tmp_path / "loader_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        in_inv = stub_invocation_row(_INV_IN, process_lifetime_id=_PLT)
        in_inv.start_at = _TS_IN
        out_inv = stub_invocation_row(_INV_OUT, process_lifetime_id=_PLT)
        out_inv.start_at = _TS_OUT
        sess.add(in_inv)
        sess.add(out_inv)
        insert_validation(sess, _validation())
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        await insert_agent_call(sess_async, _agent_call("call-in", _INV_IN))
        await insert_agent_call(sess_async, _agent_call("call-out", _INV_OUT))
        await sess_async.commit()
        yield sess_async
    await async_engine.dispose()


class TestLoadWindow:
    async def test_returns_window_dataset(self, seeded_session: AsyncSession) -> None:
        dataset = await load_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert isinstance(dataset, WindowDataset)
        assert dataset.start == _WINDOW_START
        assert dataset.end == _WINDOW_END

    async def test_agent_calls_filtered_to_window(self, seeded_session: AsyncSession) -> None:
        dataset = await load_window(seeded_session, _WINDOW_START, _WINDOW_END)
        ids = {c.agent_call_id for c in dataset.agent_calls}
        assert ids == {"call-in"}

    async def test_validations_loaded_via_sync_bridge(self, seeded_session: AsyncSession) -> None:
        """The sync validation_queries helper is bridged onto the async session."""
        dataset = await load_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert {v.validation_id for v in dataset.validations} == {_VAL_ID}

    async def test_extension_bundles_empty(self, seeded_session: AsyncSession) -> None:
        dataset = await load_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert dataset.refs == RefsBundle()
        assert dataset.replays == ReplaysBundle()
        assert dataset.refs.citations == ()
        assert dataset.replays.replays == ()

    async def test_pm_decision_log_empty_when_unseeded(self, seeded_session: AsyncSession) -> None:
        dataset = await load_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert dataset.pm_decision_log == ()

    async def test_regimes_mapped_for_in_window_invocations(
        self, seeded_session: AsyncSession
    ) -> None:
        # Both fixture invocations carry the stub default regime "normal"; only the
        # in-window one is mapped (the out-of-window invocation is excluded).
        dataset = await load_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert dataset.regimes.by_invocation == {_INV_IN: "normal"}


class TestAnalystProposalsLoad:
    """The analyst-proposals sub-bundle counts recommendations off ``output.json``."""

    @pytest.fixture()
    async def session_with_analyst_artifact(self, tmp_path: Path) -> AsyncIterator[AsyncSession]:
        """A normal-mode analyst call with a real ``output.json`` carrying 2 recs."""
        artifact_dir = tmp_path / "analyst_artifact"
        artifact_dir.mkdir()
        (artifact_dir / "output.json").write_text(
            '{"invocation_id":"' + _INV_IN + '","timestamp":"2026-06-01T09:00:00+00:00",'
            '"mode":"normal","recommendations":[{"a":1},{"b":2}],"watchlist":null}',
            encoding="utf-8",
        )

        db_path = tmp_path / "analyst_loader_test.db"
        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(stub_process_lifetime_row(_PLT))
            sess.flush()
            in_inv = stub_invocation_row(_INV_IN, process_lifetime_id=_PLT)
            in_inv.start_at = _TS_IN
            sess.add(in_inv)
            sess.commit()
        sync_engine.dispose()

        async_engine: AsyncEngine = make_async_engine(str(db_path))
        factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
        async with factory() as sess_async:
            call = _agent_call("call-analyst", _INV_IN)
            object.__setattr__(call, "output_artifact_ref", str(artifact_dir))
            await insert_agent_call(sess_async, call)
            await sess_async.commit()
            yield sess_async
        await async_engine.dispose()

    async def test_normal_run_counts_recommendations(
        self, session_with_analyst_artifact: AsyncSession
    ) -> None:
        dataset = await load_window(session_with_analyst_artifact, _WINDOW_START, _WINDOW_END)
        observations = dataset.analyst_proposals.observations
        assert len(observations) == 1
        assert observations[0].invocation_id == _INV_IN
        assert observations[0].proposal_count == 2


class TestPureCoreSeam:
    def test_metric_compute_runs_without_db(self) -> None:
        """A metric core computes purely over a hand-built dataset — no session."""

        def _count_agent_calls(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
            return MetricResult(
                metric_id=MetricId("example_agent_call_count"),
                value=float(len(dataset.agent_calls)),
                posterior_band=None,
                sample_size=len(dataset.agent_calls),
                insufficient_sample=len(dataset.agent_calls) < 5,
            )

        metric = Metric(
            metric_id=MetricId("example_agent_call_count"),
            po_type="process",
            default_window=Window.WEEKLY,
            supported_conditioning=(),
            compute=_count_agent_calls,
        )
        dataset = WindowDataset(
            start=_WINDOW_START,
            end=_WINDOW_END,
            agent_calls=(_agent_call("c1", _INV_IN), _agent_call("c2", _INV_IN)),
            pm_decision_log=(),
            validations=(),
        )

        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 2.0
        assert result.sample_size == 2
        assert result.insufficient_sample is True
        assert result.posterior_band is None


# ---------------------------------------------------------------------------
# ALP-919 — _load_outcomes populates regime / time_of_day conditioning
# ---------------------------------------------------------------------------

_OUTCOMES_WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
_OUTCOMES_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)
_THESIS_RES_TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

_INV_FOR_THESIS = "inv-thesis-gen-1"
_INV_FOR_THESIS_REGIME = "elevated"
_INV_FOR_THESIS_TRIGGER = "continuous_monitor"


async def _make_outcomes_db(tmp_path: Path) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """On-disk SQLite with an invocation + resolved thesis wired to it."""
    import dataclasses

    from alphamind._kernel.ids import InvocationId
    from alphamind.persistence.session import make_session_factory
    from alphamind.state.tables.theses_codec import record_to_rows
    from tests.feedback_loop.metrics._outcome_fixtures import make_resolved_thesis_record
    from tests.state._fk_substrate import stub_position_row

    db_path = tmp_path / "outcomes_cond.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)

    plt = stub_process_lifetime_row()
    inv = stub_invocation_row(_INV_FOR_THESIS)
    inv.active_regime = _INV_FOR_THESIS_REGIME
    inv.trigger_source = _INV_FOR_THESIS_TRIGGER

    thesis_record = make_resolved_thesis_record(
        "thes-cond-1",
        "pos-cond-1",
        resolution_timestamp=_THESIS_RES_TS,
    )
    thesis_record = dataclasses.replace(thesis_record, invocation_id=InvocationId(_INV_FOR_THESIS))

    with make_session_factory(sync_engine)() as sess:
        sess.add(plt)
        sess.flush()
        sess.add(inv)
        sess.flush()
        sess.add(stub_position_row("pos-cond-1"))
        sess.flush()
        thesis_row, comp_rows = record_to_rows(thesis_record)
        sess.add(thesis_row)
        for crow in comp_rows:
            sess.add(crow)
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    return async_engine, factory


class TestLoadOutcomesConditioning:
    """_load_outcomes populates regime/time_of_day from the generating invocation."""

    async def test_thesis_with_invocation_id_populates_regime_and_time_of_day(
        self, tmp_path: Path
    ) -> None:
        """A resolved thesis with a non-NULL invocation_id gets regime + time_of_day."""
        from alphamind.feedback_loop.dataset import _load_outcomes

        async_engine, factory = await _make_outcomes_db(tmp_path)
        try:
            async with factory() as session:
                bundle = await _load_outcomes(
                    session, _OUTCOMES_WINDOW_START, _OUTCOMES_WINDOW_END, None
                )
        finally:
            await async_engine.dispose()

        assert len(bundle.theses) == 1
        outcome = bundle.theses[0]
        assert outcome.conditioning.regime == _INV_FOR_THESIS_REGIME
        assert outcome.conditioning.time_of_day == _INV_FOR_THESIS_TRIGGER

    async def test_thesis_without_invocation_id_has_none_regime_and_time_of_day(
        self, tmp_path: Path
    ) -> None:
        """A resolved thesis with invocation_id=None yields regime=None, time_of_day=None."""
        from alphamind.feedback_loop.dataset import _load_outcomes
        from alphamind.persistence.session import make_session_factory
        from alphamind.state.tables.theses_codec import record_to_rows
        from tests.feedback_loop.metrics._outcome_fixtures import make_resolved_thesis_record
        from tests.state._fk_substrate import stub_position_row

        db_path = tmp_path / "outcomes_null_inv.db"
        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)

        # Seed with no invocation_id on the thesis.
        thesis_record = make_resolved_thesis_record(
            "thes-null-1",
            "pos-null-1",
            resolution_timestamp=_THESIS_RES_TS,
        )
        assert thesis_record.invocation_id is None

        plt = stub_process_lifetime_row()
        inv = stub_invocation_row("inv-null-ctx-1")
        with make_session_factory(sync_engine)() as sess:
            sess.add(plt)
            sess.flush()
            sess.add(inv)
            sess.flush()
            sess.add(stub_position_row("pos-null-1"))
            sess.flush()
            thesis_row, comp_rows = record_to_rows(thesis_record)
            sess.add(thesis_row)
            for crow in comp_rows:
                sess.add(crow)
            sess.commit()
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        try:
            async with factory() as session:
                bundle = await _load_outcomes(
                    session, _OUTCOMES_WINDOW_START, _OUTCOMES_WINDOW_END, None
                )
        finally:
            await async_engine.dispose()

        assert len(bundle.theses) == 1
        outcome = bundle.theses[0]
        assert outcome.conditioning.regime is None
        assert outcome.conditioning.time_of_day is None

    async def test_other_six_dimensions_stay_empty_regardless(self, tmp_path: Path) -> None:
        """The six non-invocation conditioning dimensions remain None/empty."""
        from alphamind.feedback_loop.dataset import _load_outcomes

        async_engine, factory = await _make_outcomes_db(tmp_path)
        try:
            async with factory() as session:
                bundle = await _load_outcomes(
                    session, _OUTCOMES_WINDOW_START, _OUTCOMES_WINDOW_END, None
                )
        finally:
            await async_engine.dispose()

        assert len(bundle.theses) == 1
        cond = bundle.theses[0].conditioning
        assert cond.sector is None
        assert cond.conviction is None
        assert cond.strategist_status is None
        assert cond.anti_patterns == ()
        assert cond.prompt_version is None
        assert cond.model_version is None


class TestPackagedAgentLatencyBudgets:
    """The packaged agents.yaml latency-budget read is the ``CostBudgetsBundle``
    default factory, invoked once per ``load_window`` (12x per default digest run). It
    must parse/validate the file once per process and hand each caller a *fresh* dict so
    the frozen budget bundles never alias one shared mutable mapping."""

    def test_returns_a_distinct_equal_dict_per_call(self) -> None:
        from alphamind.feedback_loop.dataset import _packaged_agent_latency_budgets

        first = _packaged_agent_latency_budgets()
        second = _packaged_agent_latency_budgets()

        assert first == second
        assert first is not second  # a fresh copy, not the shared cached mapping
        first["analyst"] = -1  # mutating one caller's copy must not leak to the next
        assert _packaged_agent_latency_budgets()["analyst"] != -1

    def test_disk_read_is_memoized(self) -> None:
        from alphamind.feedback_loop import dataset as ds

        ds._load_agent_latency_budgets.cache_clear()

        ds._packaged_agent_latency_budgets()
        ds._packaged_agent_latency_budgets()
        ds._packaged_agent_latency_budgets()

        # The lru_cache around the parse/validate took exactly one miss (the first call)
        # and served the rest from cache — one disk read + validate per process.
        info = ds._load_agent_latency_budgets.cache_info()
        assert info.misses == 1
        assert info.hits == 2
