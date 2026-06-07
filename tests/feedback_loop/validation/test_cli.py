"""Headless validation CLI — register / evaluate / list (ALP-889 story 07b).

The CLI is the surface ``/feedback-validate`` drives: it accepts JSON on
``--input`` (or stdin) and emits JSON on stdout. Exercised end-to-end against an
on-disk SQLite DB seeded with a registering invocation + agent call so the
register path can snapshot provenance.
"""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import alphamind.feedback_loop.metrics as metrics_pkg
import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.metrics.types import MetricId
from alphamind.feedback_loop.validation import cli
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.tables.agent_calls import AgentCallRecord
from alphamind.state.tables.agent_calls_codec import record_to_row
from alphamind.state.tables.agent_calls import AgentCallsRow
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_PROBE_MODULE = "cli_probe"
_PROBE_ID = MetricId("cli_probe_call_count")
_PLT = "plt-cli"
_INV = "inv-cli"
_ARTIFACT = "prompts/decision/strategist.md"
_REGISTERED_AT = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def planted_metric(tmp_path: Path) -> Iterator[MetricId]:
    (tmp_path / f"{_PROBE_MODULE}.py").write_text(
        textwrap.dedent(
            '''\
            """Throwaway in-window agent-call counter planted for CLI tests."""

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
                    metric_id=MetricId("cli_probe_call_count"),
                    value=float(count),
                    posterior_band=None,
                    sample_size=count,
                    insufficient_sample=count == 0,
                )


            METRICS = (
                Metric(
                    metric_id=MetricId("cli_probe_call_count"),
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


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "cli.db")
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    post_mid = _REGISTERED_AT + timedelta(days=1)
    pre_mid = _REGISTERED_AT - timedelta(days=1)
    with make_session_factory(engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        reg = stub_invocation_row(_INV, process_lifetime_id=_PLT)
        reg.start_at = _iso(post_mid)
        sess.add(reg)
        post_inv = stub_invocation_row("inv-post", process_lifetime_id=_PLT)
        post_inv.start_at = _iso(post_mid)
        sess.add(post_inv)
        pre_inv = stub_invocation_row("inv-pre", process_lifetime_id=_PLT)
        pre_inv.start_at = _iso(pre_mid)
        sess.add(pre_inv)
        sess.flush()
        for call_id, inv in (
            ("call-reg", _INV),
            ("call-post-1", "inv-post"),
            ("call-post-2", "inv-post"),
            ("call-post-3", "inv-post"),
            ("call-pre-1", "inv-pre"),
        ):
            sess.add(AgentCallsRow(**record_to_row(_agent_call(call_id, inv))))
        sess.commit()
    engine.dispose()
    return path


def _write_json(tmp_path: Path, name: str, payload: dict[str, object]) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _registration_payload() -> dict[str, object]:
    return {
        "validation_id": "val-cli-1",
        "registering_invocation_id": _INV,
        "registered_at": _iso(_REGISTERED_AT),
        "edited_artifact": _ARTIFACT,
        "pre_edit_version": "abc1234",
        "post_edit_version": "def5678",
        "watched_metric_ids": [str(_PROBE_ID)],
        "window_length_days": 21,
        "expected_direction": "improved",
        "expected_magnitude": "more",
        "success_criterion": "count up",
        "failure_criterion": "count down",
    }


class TestRegisterCommand:
    def test_emits_validation_id(
        self,
        db_path: str,
        tmp_path: Path,
        planted_metric: MetricId,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        input_path = _write_json(tmp_path, "reg.json", _registration_payload())
        rc = cli.main(["register", "--db-path", db_path, "--input", input_path])
        assert rc == 0
        emitted = json.loads(capsys.readouterr().out)
        assert emitted["validation_id"] == "val-cli-1"

    def test_persists_with_snapshotted_provenance(
        self,
        db_path: str,
        tmp_path: Path,
        planted_metric: MetricId,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        input_path = _write_json(tmp_path, "reg.json", _registration_payload())
        cli.main(["register", "--db-path", db_path, "--input", input_path])
        capsys.readouterr()
        rc = cli.main(["list", "--db-path", db_path])
        assert rc == 0
        listed = json.loads(capsys.readouterr().out)
        row = next(v for v in listed["pending"] if v["validation_id"] == "val-cli-1")
        assert row["registered_regime"] == "normal"
        assert row["registered_model_id"] == "claude-opus-4-8"


class TestListCommand:
    def test_lists_pending(
        self,
        db_path: str,
        tmp_path: Path,
        planted_metric: MetricId,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli.main([
            "register",
            "--db-path",
            db_path,
            "--input",
            _write_json(tmp_path, "reg.json", _registration_payload()),
        ])
        capsys.readouterr()
        rc = cli.main(["list", "--db-path", db_path])
        assert rc == 0
        listed = json.loads(capsys.readouterr().out)
        assert [v["validation_id"] for v in listed["pending"]] == ["val-cli-1"]


class TestEvaluateCommand:
    def _judgments(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "confounder_flagged": False,
            "failure_criterion_crossed": False,
            "confounder_notes": None,
            "narrative": "evaluated via CLI",
        }
        payload.update(overrides)
        return payload

    def test_emits_outcome(
        self,
        db_path: str,
        tmp_path: Path,
        planted_metric: MetricId,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli.main([
            "register",
            "--db-path",
            db_path,
            "--input",
            _write_json(tmp_path, "reg.json", _registration_payload()),
        ])
        capsys.readouterr()
        rc = cli.main([
            "evaluate",
            "--db-path",
            db_path,
            "--validation-id",
            "val-cli-1",
            "--outcome-id",
            "out-cli-1",
            "--evaluated-at",
            _iso(_REGISTERED_AT + timedelta(days=21)),
            "--input",
            _write_json(tmp_path, "judge.json", self._judgments()),
        ])
        assert rc == 0
        emitted = json.loads(capsys.readouterr().out)
        # post window has 3 + 1 (reg) calls vs pre window 1 → improved.
        assert emitted["superseded"] is False
        assert emitted["outcome"]["verdict"] == "improved"
        assert emitted["outcome"]["outcome_id"] == "out-cli-1"

    def test_evaluate_then_no_longer_pending(
        self,
        db_path: str,
        tmp_path: Path,
        planted_metric: MetricId,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli.main([
            "register",
            "--db-path",
            db_path,
            "--input",
            _write_json(tmp_path, "reg.json", _registration_payload()),
        ])
        cli.main([
            "evaluate",
            "--db-path",
            db_path,
            "--validation-id",
            "val-cli-1",
            "--outcome-id",
            "out-cli-1",
            "--evaluated-at",
            _iso(_REGISTERED_AT + timedelta(days=21)),
            "--input",
            _write_json(tmp_path, "judge.json", self._judgments()),
        ])
        capsys.readouterr()
        cli.main(["list", "--db-path", db_path])
        listed = json.loads(capsys.readouterr().out)
        assert listed["pending"] == []
