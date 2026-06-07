"""EVALUATE shell — pre/post metric computation, verdict, outcome write (ALP-889).

``evaluate_validation`` is the imperative shell: it loads the pre-edit + post-edit
windows via ``load_window``, computes the watched metrics' ``MetricResult``s via
the registry, derives the verdict, looks up the most-recent prior outcome on the
same artifact for the rollback derivation, derives ``rollback_status``, and writes
the ``validation_outcomes`` record. A superseded validation short-circuits — no
outcome is written and the supersession state is returned.

The watched metric is planted at runtime (the registry-discovery pattern) so a
fixture can drive a deterministic pre/post movement and therefore a known verdict.
The planted metric counts in-window agent calls, so seeding more calls in the
post-edit window than the pre-edit window produces an upward movement.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import alphamind.feedback_loop.metrics as metrics_pkg
import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.metrics.types import MetricId
from alphamind.feedback_loop.validation.evaluate import (
    EvaluationJudgments,
    evaluate_validation,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    RollbackStatus,
    ValidationId,
    Verdict,
)
from alphamind.feedback_loop.validation.register import register_validation
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.agent_calls_queries import insert_agent_call
from alphamind.state.repository.validation_queries import (
    mark_validation_superseded,
    read_outcomes_by_artifact,
)
from alphamind.feedback_loop.validation.records import SupersededReason
from alphamind.state.tables.agent_calls import AgentCallRecord, AgentCallsRow
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_PROBE_MODULE = "evaluate_probe"
_PROBE_ID = MetricId("evaluate_probe_call_count")

_PLT = "plt-eval"
_ARTIFACT = "prompts/decision/strategist.md"
_REGISTERED_AT = datetime(2026, 6, 1, tzinfo=UTC)
_WINDOW_DAYS = 21


@pytest.fixture()
def planted_metric(tmp_path: Path) -> Iterator[MetricId]:
    """Plant an agent-call-counting metric on the registry ``__path__``."""
    (tmp_path / f"{_PROBE_MODULE}.py").write_text(
        textwrap.dedent(
            '''\
            """Throwaway in-window agent-call counter planted for EVALUATE tests."""

            from __future__ import annotations

            from alphamind.feedback_loop.dataset import WindowDataset
            from alphamind.feedback_loop.metrics.types import (
                Conditioning,
                Metric,
                MetricId,
                MetricResult,
                Window,
            )


            def _compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
                count = len(dataset.agent_calls)
                return MetricResult(
                    metric_id=MetricId("evaluate_probe_call_count"),
                    value=float(count),
                    posterior_band=None,
                    sample_size=count,
                    insufficient_sample=count == 0,
                )


            METRICS = (
                Metric(
                    metric_id=MetricId("evaluate_probe_call_count"),
                    po_type="process",
                    default_window=Window.WEEKLY,
                    supported_conditioning=(),
                    compute=_compute,
                ),
            )
            '''
        ),
        encoding="utf-8",
    )
    metrics_pkg.__path__.append(str(tmp_path))
    metrics_pkg.reset_registry_cache()
    try:
        yield _PROBE_ID
    finally:
        metrics_pkg.__path__.remove(str(tmp_path))
        sys.modules.pop(f"{metrics_pkg.__name__}.{_PROBE_MODULE}", None)
        metrics_pkg.reset_registry_cache()


def _agent_call(call_id: str, invocation_id: str) -> AgentCallRecord:
    return AgentCallRecord(
        agent_call_id=call_id,
        invocation_id=invocation_id,
        agent_name="strategist",
        attempt_number=1,
        model_id="claude-opus-4-8",
        prompt_path=_ARTIFACT,
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json="{}",
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=1,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Harness:
    """A seeded DB + an async session factory for the evaluate shell."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)
        self._async_factory: async_sessionmaker[AsyncSession] | None = None

    def seed_validation(
        self,
        *,
        validation_id: str = "val-eval",
        pre_calls: int,
        post_calls: int,
        expected_direction: ExpectedDirection = ExpectedDirection.IMPROVED,
        prior_outcome_artifact_verdict: Verdict | None = None,
    ) -> None:
        sync_engine = make_engine(self._db_path)
        Base.metadata.create_all(sync_engine)
        post_window_mid = _REGISTERED_AT + timedelta(days=1)
        pre_window_mid = _REGISTERED_AT - timedelta(days=1)
        with make_session_factory(sync_engine)() as sess:
            sess.add(stub_process_lifetime_row(_PLT))
            sess.flush()
            calls: list[AgentCallRecord] = []
            for idx in range(pre_calls):
                inv_id = f"inv-pre-{idx}"
                inv = stub_invocation_row(inv_id, process_lifetime_id=_PLT)
                inv.start_at = _iso(pre_window_mid)
                sess.add(inv)
                calls.append(_agent_call(f"call-pre-{idx}", inv_id))
            for idx in range(post_calls):
                inv_id = f"inv-post-{idx}"
                inv = stub_invocation_row(inv_id, process_lifetime_id=_PLT)
                inv.start_at = _iso(post_window_mid)
                sess.add(inv)
                calls.append(_agent_call(f"call-post-{idx}", inv_id))
            # A registering invocation for provenance, inside the post window.
            reg_inv = stub_invocation_row("inv-reg", process_lifetime_id=_PLT)
            reg_inv.start_at = _iso(post_window_mid)
            sess.add(reg_inv)
            calls.append(_agent_call("call-reg", "inv-reg"))
            sess.flush()
            from alphamind.state.tables.agent_calls_codec import record_to_row

            for call in calls:
                sess.add(AgentCallsRow(**record_to_row(call)))
            sess.flush()
            register_validation(
                sess,
                validation_id=validation_id,
                registering_invocation_id="inv-reg",
                registered_at=_REGISTERED_AT,
                edited_artifact=_ARTIFACT,
                pre_edit_version="abc1234",
                post_edit_version="def5678",
                watched_metric_ids=(_PROBE_ID,),
                window_length_days=_WINDOW_DAYS,
                expected_direction=expected_direction,
                expected_magnitude="more",
                success_criterion="count up",
                failure_criterion="count down",
                registered_regime="normal",
                registered_model_id="claude-opus-4-8",
            )
            if prior_outcome_artifact_verdict is not None:
                from alphamind.feedback_loop.validation.records import (
                    ValidationOutcomeRecord,
                )
                from alphamind.state.repository.validation_queries import (
                    insert_validation_outcome,
                )

                register_validation(
                    sess,
                    validation_id=f"{validation_id}-prior",
                    registering_invocation_id="inv-reg",
                    registered_at=_REGISTERED_AT - timedelta(days=60),
                    edited_artifact=_ARTIFACT,
                    pre_edit_version="000",
                    post_edit_version="111",
                    watched_metric_ids=(_PROBE_ID,),
                    window_length_days=_WINDOW_DAYS,
                    expected_direction=expected_direction,
                    expected_magnitude="more",
                    success_criterion="count up",
                    failure_criterion="count down",
                    registered_regime="normal",
                    registered_model_id="claude-opus-4-8",
                )
                sess.flush()
                insert_validation_outcome(
                    sess,
                    ValidationOutcomeRecord(
                        outcome_id="out-prior",  # type: ignore[arg-type]
                        validation_id=f"{validation_id}-prior",  # type: ignore[arg-type]
                        evaluated_at=_REGISTERED_AT - timedelta(days=30),
                        evaluated_by_session_id=None,
                        verdict=prior_outcome_artifact_verdict,
                        posterior_summary={},
                        confounder_notes=None,
                        narrative="prior",
                        rollback_status=RollbackStatus.NOT_APPLICABLE,
                    ),
                )
            sess.commit()
        sync_engine.dispose()
        self._async_factory = make_async_session_factory(make_async_engine(self._db_path))

    def supersede(self, validation_id: str) -> None:
        sync_engine = make_engine(self._db_path)
        with make_session_factory(sync_engine)() as sess:
            mark_validation_superseded(
                sess,
                ValidationId(validation_id),
                SupersededReason.REGIME_TRANSITION,
                _REGISTERED_AT + timedelta(days=2),
            )
            sess.commit()
        sync_engine.dispose()

    def session(self) -> AsyncSession:
        assert self._async_factory is not None
        return self._async_factory()


@pytest.fixture()
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    h = _Harness(tmp_path / "evaluate.db")
    yield h


def _judgments(
    *,
    confounder_flagged: bool = False,
    failure_criterion_crossed: bool = False,
) -> EvaluationJudgments:
    return EvaluationJudgments(
        confounder_flagged=confounder_flagged,
        failure_criterion_crossed=failure_criterion_crossed,
        confounder_notes=None,
    )


async def _evaluate(
    harness: _Harness,
    *,
    validation_id: str = "val-eval",
    judgments: EvaluationJudgments,
    outcome_id: str = "out-1",
):  # noqa: ANN202 — returns EvaluationResult; inferred at call sites
    async with harness.session() as sess:
        result = await evaluate_validation(
            sess,
            validation_id=ValidationId(validation_id),
            outcome_id=outcome_id,  # type: ignore[arg-type]
            evaluated_at=_REGISTERED_AT + timedelta(days=_WINDOW_DAYS),
            judgments=judgments,
            narrative="evaluated",
        )
        await sess.commit()
        return result


class TestEvaluateVerdict:
    async def test_improved_when_post_window_count_rises(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=1, post_calls=5)
        result = await _evaluate(harness, judgments=_judgments())
        assert result.outcome is not None
        assert result.outcome.verdict is Verdict.IMPROVED

    async def test_degraded_when_post_window_count_falls(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=5, post_calls=1)
        result = await _evaluate(
            harness, judgments=_judgments(failure_criterion_crossed=True)
        )
        assert result.outcome is not None
        assert result.outcome.verdict is Verdict.DEGRADED

    async def test_outcome_persisted(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=1, post_calls=5)
        await _evaluate(harness, judgments=_judgments())
        async with harness.session() as sess:
            outcomes = await sess.run_sync(
                lambda s: read_outcomes_by_artifact(s, _ARTIFACT)
            )
        assert any(o.outcome_id == "out-1" for o in outcomes)

    async def test_posterior_summary_carries_pre_post_readings(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=1, post_calls=5)
        result = await _evaluate(harness, judgments=_judgments())
        assert result.outcome is not None
        summary = result.outcome.posterior_summary
        assert str(_PROBE_ID) in summary


class TestEvaluateRollbackStatus:
    async def test_mandatory_clean_failure(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=5, post_calls=1)
        result = await _evaluate(
            harness, judgments=_judgments(failure_criterion_crossed=True)
        )
        assert result.outcome is not None
        assert result.outcome.rollback_status is RollbackStatus.MANDATORY_CLEAN_FAILURE

    async def test_optional_inconclusive_after_prior_inconclusive(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(
            pre_calls=3,
            post_calls=3,
            prior_outcome_artifact_verdict=Verdict.INCONCLUSIVE,
        )
        # Equal counts → no_change movement, but confounder makes it inconclusive.
        result = await _evaluate(
            harness, judgments=_judgments(confounder_flagged=True)
        )
        assert result.outcome is not None
        assert result.outcome.verdict is Verdict.INCONCLUSIVE
        assert result.outcome.rollback_status is RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE


class TestEvaluateSuperseded:
    async def test_superseded_writes_no_outcome(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=1, post_calls=5)
        harness.supersede("val-eval")
        result = await _evaluate(harness, judgments=_judgments())
        assert result.outcome is None
        assert result.superseded is True
        assert result.superseded_reason is SupersededReason.REGIME_TRANSITION

    async def test_superseded_persists_nothing(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=1, post_calls=5)
        harness.supersede("val-eval")
        await _evaluate(harness, judgments=_judgments())
        async with harness.session() as sess:
            outcomes = await sess.run_sync(
                lambda s: read_outcomes_by_artifact(s, _ARTIFACT)
            )
        assert outcomes == ()


class TestEvaluateMissing:
    async def test_unknown_validation_raises(
        self, harness: _Harness, planted_metric: MetricId
    ) -> None:
        harness.seed_validation(pre_calls=1, post_calls=5)
        with pytest.raises(KeyError):
            await _evaluate(harness, validation_id="no-such-val", judgments=_judgments())
