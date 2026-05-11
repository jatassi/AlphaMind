"""Tests for the pipeline scheduler verify script (story 05 / ALP-448).

The verify script's per-check helpers live in ``alphamind.scripts.verify_pipeline_scheduler``;
this module exercises them with the in-memory engine fixture so the
testable predicates are covered without touching the real LLM SDK,
broker, or production DB. The end-to-end ``--once`` invocation is
exercised in the verify script itself (operator-driven), not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from alphamind.persistence.models import Base
from alphamind.scripts.verify_pipeline_scheduler import (
    CheckResult,
    check_archive_directory,
    check_auth,
    check_db_schema,
    check_invocation_row_population,
    check_vocabulary,
)

# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------


class TestCheckAuth:
    def test_passes_when_all_env_vars_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        monkeypatch.setenv("ALPACA_PAPER_KEY", "k")
        monkeypatch.setenv("ALPACA_PAPER_SECRET", "s")
        result = check_auth()
        assert result.passed
        assert result.label == "auth"

    def test_fails_when_oauth_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ALPACA_PAPER_KEY", "k")
        monkeypatch.setenv("ALPACA_PAPER_SECRET", "s")
        result = check_auth()
        assert not result.passed
        assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message

    def test_fails_when_alpaca_paper_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
        monkeypatch.setenv("ALPACA_PAPER_SECRET", "s")
        result = check_auth()
        assert not result.passed
        assert "ALPACA_PAPER_KEY" in result.message

    def test_fails_when_alpaca_paper_secret_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        monkeypatch.setenv("ALPACA_PAPER_KEY", "k")
        monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)
        result = check_auth()
        assert not result.passed
        assert "ALPACA_PAPER_SECRET" in result.message


# ---------------------------------------------------------------------------
# DB schema check
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_engine() -> Engine:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


class TestCheckDbSchema:
    def test_passes_when_required_tables_present(self, sqlite_engine: Engine) -> None:
        with sqlite_engine.connect() as conn:
            result = check_db_schema(conn)
        assert result.passed, result.message
        assert "invocations" in result.message
        assert "process_lifetimes" in result.message
        assert "activity_log" in result.message

    def test_fails_when_table_missing(self, sqlite_engine: Engine) -> None:
        # Drop one of the required tables so the check fails with a clear message.
        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE activity_log")
        with sqlite_engine.connect() as conn:
            result = check_db_schema(conn)
        assert not result.passed
        assert "activity_log" in result.message


# ---------------------------------------------------------------------------
# Invocation row population check
# ---------------------------------------------------------------------------


def _seed_full_invocation_row(
    sqlite_engine: Engine,
    *,
    invocation_id: str = "inv-test-1",
    process_lifetime_id: str = "plt-test-1",
    phase1_completed_at: str | None = "2026-05-11T14:30:01Z",
    phase2_completed_at: str | None = "2026-05-11T14:30:02Z",
    trigger_type: str = "manual",
    fill_summary: str | None = '{"fills_processed": 0}',
    command_summary: str | None = '{"commands_submitted": 0}',
    staleness_flag: int | None = 0,
    snapshot_metadata_json: str | None = None,
    resolved_config_path: str = "/tmp/cfg.json",
    data_cal_path: str = "/tmp/data_cal.json",
    active_overlays_json: str = "[]",
    feature_flags_snapshot_json: str = "{}",
) -> None:
    """Insert a process_lifetimes row and one full invocations row for testing."""
    with sqlite_engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO process_lifetimes (
              process_lifetime_id, process_role, process_start_at, process_pid,
              hostname, git_sha, git_branch, git_dirty, python_version,
              pip_freeze_hash, pip_freeze_snapshot_path, anthropic_sdk_version,
              claude_agent_sdk_version, os_release
            ) VALUES (
              :pid, 'pipeline', '2026-05-11T14:30:00Z', 1, 'h', 'sha', 'main', 0,
              'py', 'hash', '/tmp/pip.txt', 'na', 'na', 'os'
            )
            """,
            {"pid": process_lifetime_id},
        )
        conn.exec_driver_sql(
            """
            INSERT INTO invocations (
              invocation_id, process_lifetime_id, start_at, phase1_completed_at,
              phase2_completed_at, trigger_type, trigger_source, trigger_reason,
              git_sha_at_invocation, active_profile, active_regime, active_mode,
              active_overlays_json, resolved_config_hash, resolved_config_snapshot_path,
              feature_flags_snapshot_json, data_calibration_state_snapshot_path,
              data_source_freshness_json, fill_collection_summary_json,
              command_execution_summary_json, staleness_flag, snapshot_metadata_json
            ) VALUES (
              :iid, :pid, '2026-05-11T14:30:00Z', :p1, :p2, :ttype, 'cli',
              'reason', 'sha', 'profile', 'regime', 'normal', :ovr, 'hash',
              :cfg, :ffj, :dcj, '{}', :fsj, :csj, :sf, :smj
            )
            """,
            {
                "iid": invocation_id,
                "pid": process_lifetime_id,
                "p1": phase1_completed_at,
                "p2": phase2_completed_at,
                "ttype": trigger_type,
                "ovr": active_overlays_json,
                "cfg": resolved_config_path,
                "ffj": feature_flags_snapshot_json,
                "dcj": data_cal_path,
                "fsj": fill_summary,
                "csj": command_summary,
                "sf": staleness_flag,
                "smj": snapshot_metadata_json,
            },
        )


class TestCheckInvocationRowPopulation:
    def test_passes_on_fully_populated_row(self, sqlite_engine: Engine, tmp_path: Path) -> None:
        cfg_path = tmp_path / "resolved_config.json"
        cfg_path.write_text('{"profile": "test"}')
        data_cal_path = tmp_path / "data_cal.json"
        data_cal_path.write_text("{}")
        # snapshot_metadata_json defaults to NULL — the storage spec lists it
        # as nullable and the row-population check excludes it.
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-1",
            resolved_config_path=str(cfg_path),
            data_cal_path=str(data_cal_path),
        )
        with sqlite_engine.connect() as conn:
            result = check_invocation_row_population(conn, invocation_id="inv-1")
        assert result.passed, result.message
        assert "21" in result.message  # references the 21-must-be-set-field shape

    def test_fails_when_phase1_completed_at_null(
        self, sqlite_engine: Engine, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text("{}")
        data_cal_path = tmp_path / "dc.json"
        data_cal_path.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-2",
            phase1_completed_at=None,
            resolved_config_path=str(cfg_path),
            data_cal_path=str(data_cal_path),
        )
        with sqlite_engine.connect() as conn:
            result = check_invocation_row_population(conn, invocation_id="inv-2")
        assert not result.passed
        assert "phase1_completed_at" in result.message

    def test_fails_when_trigger_type_not_manual(
        self, sqlite_engine: Engine, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text("{}")
        data_cal_path = tmp_path / "dc.json"
        data_cal_path.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-3",
            trigger_type="scheduled",
            resolved_config_path=str(cfg_path),
            data_cal_path=str(data_cal_path),
        )
        with sqlite_engine.connect() as conn:
            result = check_invocation_row_population(conn, invocation_id="inv-3")
        assert not result.passed
        assert "trigger_type" in result.message

    def test_fails_when_active_overlays_invalid_json(
        self, sqlite_engine: Engine, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text("{}")
        data_cal_path = tmp_path / "dc.json"
        data_cal_path.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-4",
            active_overlays_json="not-json{{",
            resolved_config_path=str(cfg_path),
            data_cal_path=str(data_cal_path),
        )
        with sqlite_engine.connect() as conn:
            result = check_invocation_row_population(conn, invocation_id="inv-4")
        assert not result.passed
        assert "active_overlays_json" in result.message

    def test_fails_when_resolved_config_file_missing(
        self, sqlite_engine: Engine, tmp_path: Path
    ) -> None:
        data_cal_path = tmp_path / "dc.json"
        data_cal_path.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-5",
            resolved_config_path=str(tmp_path / "no-such-file.json"),
            data_cal_path=str(data_cal_path),
        )
        with sqlite_engine.connect() as conn:
            result = check_invocation_row_population(conn, invocation_id="inv-5")
        assert not result.passed
        assert "resolved_config_snapshot_path" in result.message

    def test_fails_when_resolved_config_file_empty(
        self, sqlite_engine: Engine, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text("")  # empty file
        data_cal_path = tmp_path / "dc.json"
        data_cal_path.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-6",
            resolved_config_path=str(cfg_path),
            data_cal_path=str(data_cal_path),
        )
        with sqlite_engine.connect() as conn:
            result = check_invocation_row_population(conn, invocation_id="inv-6")
        assert not result.passed
        assert "resolved_config_snapshot_path" in result.message


# ---------------------------------------------------------------------------
# Archive directory check
# ---------------------------------------------------------------------------


class TestCheckArchiveDirectory:
    def test_passes_when_directory_and_files_exist(self, tmp_path: Path) -> None:
        inv_dir = tmp_path / "invocations" / "inv-arch-1"
        inv_dir.mkdir(parents=True)
        (inv_dir / "resolved_config.json").write_text('{"profile": "x"}')
        (inv_dir / "data_calibration_state.json").write_text("{}")
        result = check_archive_directory(archive_root=tmp_path, invocation_id="inv-arch-1")
        assert result.passed, result.message

    def test_fails_when_directory_missing(self, tmp_path: Path) -> None:
        result = check_archive_directory(archive_root=tmp_path, invocation_id="inv-missing")
        assert not result.passed
        assert "inv-missing" in result.message

    def test_fails_when_resolved_config_json_missing(self, tmp_path: Path) -> None:
        inv_dir = tmp_path / "invocations" / "inv-no-cfg"
        inv_dir.mkdir(parents=True)
        (inv_dir / "data_calibration_state.json").write_text("{}")
        result = check_archive_directory(archive_root=tmp_path, invocation_id="inv-no-cfg")
        assert not result.passed
        assert "resolved_config.json" in result.message


# ---------------------------------------------------------------------------
# Vocabulary check (story 04b additions)
# ---------------------------------------------------------------------------


class TestCheckVocabulary:
    def test_passes_with_real_emergency_vocab(self) -> None:
        """All four story-04b additions should be present in the worktree."""
        repo_root = Path(__file__).resolve().parents[2]
        result = check_vocabulary(config_dir=repo_root / "config")
        assert result.passed, result.message

    def test_fails_when_emergency_yaml_missing(self, tmp_path: Path) -> None:
        """If config_dir lacks run_types/emergency.yaml, the check fails."""
        (tmp_path / "run_types").mkdir()
        # Write a minimal but invalid emergency.yaml for the run-types resolver.
        # We bypass it entirely by simply not creating the file.
        # Place a stub breach_behavior.yaml so the other branch can still execute.
        bb_yaml = {
            "breach_behavior": {
                "forced_reduction": {
                    "short_trim_target_pct_of_limit": 50.0,
                    "total_short_immediate_threshold_pct_of_limit": 150.0,
                },
                "drawdown_velocity": {
                    "window_minutes": 60,
                    "threshold_pct_of_daily_limit": 50.0,
                },
                "multi_rule_breach": {"simultaneous_deferred_rules_count": 2},
                "cascade": {"max_steps": 3},
                "delta_buffer": {"secondary_check_buffer_factor": 1.5},
                "emergency_invocation_cooldown_minutes": 30,
            }
        }
        (tmp_path / "breach_behavior.yaml").write_text(yaml.safe_dump(bb_yaml))
        result = check_vocabulary(config_dir=tmp_path)
        assert not result.passed
        assert "emergency.yaml" in result.message

    def test_fails_when_emergency_cooldown_mismatched(self, tmp_path: Path) -> None:
        # Set up a complete fake config dir mirroring the real layout.
        run_types_dir = tmp_path / "run_types"
        run_types_dir.mkdir()
        # Place a minimal emergency.yaml mirroring the production shape so
        # the load_run_type_config helper accepts it. The schema is enforced
        # by the resolver, so we use a copy of the production one.
        repo_root = Path(__file__).resolve().parents[2]
        emergency_yaml = (repo_root / "config" / "run_types" / "emergency.yaml").read_text()
        (run_types_dir / "emergency.yaml").write_text(emergency_yaml)

        bb_yaml = {
            "breach_behavior": {
                "forced_reduction": {
                    "short_trim_target_pct_of_limit": 50.0,
                    "total_short_immediate_threshold_pct_of_limit": 150.0,
                },
                "drawdown_velocity": {
                    "window_minutes": 60,
                    "threshold_pct_of_daily_limit": 50.0,
                },
                "multi_rule_breach": {"simultaneous_deferred_rules_count": 2},
                "cascade": {"max_steps": 3},
                "delta_buffer": {"secondary_check_buffer_factor": 1.5},
                "emergency_invocation_cooldown_minutes": 999,  # NOT 30
            }
        }
        (tmp_path / "breach_behavior.yaml").write_text(yaml.safe_dump(bb_yaml))
        result = check_vocabulary(config_dir=tmp_path)
        assert not result.passed
        assert "cooldown" in result.message.lower() or "30" in result.message


# ---------------------------------------------------------------------------
# CheckResult is a simple value type
# ---------------------------------------------------------------------------


class TestCheckResultFormatter:
    def test_format_pass(self) -> None:
        r = CheckResult(label="auth", passed=True, message="all set")
        assert r.format_line() == "PASS: auth — all set"

    def test_format_fail(self) -> None:
        r = CheckResult(label="auth", passed=False, message="missing X")
        assert r.format_line() == "FAIL: auth — missing X"


# ---------------------------------------------------------------------------
# main() — full pre-flight failure path
# ---------------------------------------------------------------------------


class TestMainAuthFailure:
    def test_main_exits_non_zero_on_missing_oauth(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A missing CLAUDE_CODE_OAUTH_TOKEN causes main() to exit non-zero
        before any DB write."""
        from alphamind.scripts.verify_pipeline_scheduler import main

        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ALPACA_PAPER_KEY", "k")
        monkeypatch.setenv("ALPACA_PAPER_SECRET", "s")

        exit_code = main(
            argv=[
                "--archive-root",
                str(tmp_path),
                "--run-type",
                "market_hours_rolling",
                "--mode",
                "paper",
            ]
        )
        assert exit_code != 0
        out = capsys.readouterr().out
        assert "FAIL: auth" in out
        assert "CLAUDE_CODE_OAUTH_TOKEN" in out


# ---------------------------------------------------------------------------
# Activity-log check (helper exists; threads through the seeded data)
# ---------------------------------------------------------------------------


class TestCheckActivityLog:
    def test_passes_on_at_least_one_entry(self, sqlite_engine: Engine, tmp_path: Path) -> None:
        from alphamind.scripts.verify_pipeline_scheduler import check_activity_log

        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text("{}")
        dc = tmp_path / "dc.json"
        dc.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-act-1",
            resolved_config_path=str(cfg_path),
            data_cal_path=str(dc),
        )
        # Seed one activity-log entry. Use valid event_type / group / source
        # values per the CHECK-constraint catalog in
        # ``alphamind.portfolio_state.events.activity_log``.
        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql(
                """
                INSERT INTO activity_log (
                  entry_id, invocation_id, entry_at, event_type, event_group,
                  source, detail_json
                ) VALUES (
                  'e1', 'inv-act-1', '2026-05-11T14:30:00Z',
                  'DISTILLATION_CONFIG_CHANGE', 'CONFIGURATION',
                  'CONFIG_RELOAD', '{}'
                )
                """
            )
        with sqlite_engine.connect() as conn:
            result = check_activity_log(conn, invocation_id="inv-act-1")
        assert result.passed
        assert "DISTILLATION_CONFIG_CHANGE" in result.message

    def test_fails_on_empty(self, sqlite_engine: Engine, tmp_path: Path) -> None:
        from alphamind.scripts.verify_pipeline_scheduler import check_activity_log

        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text("{}")
        dc = tmp_path / "dc.json"
        dc.write_text("{}")
        _seed_full_invocation_row(
            sqlite_engine,
            invocation_id="inv-act-empty",
            resolved_config_path=str(cfg_path),
            data_cal_path=str(dc),
        )
        with sqlite_engine.connect() as conn:
            result = check_activity_log(conn, invocation_id="inv-act-empty")
        assert not result.passed


# ---------------------------------------------------------------------------
# Process lifetime row write check
# ---------------------------------------------------------------------------


class TestCheckProcessLifetimeRow:
    @pytest.mark.asyncio
    async def test_passes_when_row_lands(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from alphamind.scripts.verify_pipeline_scheduler import check_process_lifetime_row

        async def _stub(**_kwargs: Any) -> str:
            return "plt-abc-123"

        monkeypatch.setattr(
            "alphamind.scripts.verify_pipeline_scheduler.record_process_lifetime",
            _stub,
        )

        result, _ = await check_process_lifetime_row(
            session_factory=object(),  # type: ignore[arg-type]
            archive_root=tmp_path,
        )
        assert result.passed
        assert "plt-abc-123" in result.message

    @pytest.mark.asyncio
    async def test_fails_when_writer_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from alphamind.scripts.verify_pipeline_scheduler import check_process_lifetime_row

        async def _stub(**_kwargs: Any) -> str:
            raise RuntimeError("git unavailable")

        monkeypatch.setattr(
            "alphamind.scripts.verify_pipeline_scheduler.record_process_lifetime",
            _stub,
        )

        result, _ = await check_process_lifetime_row(
            session_factory=object(),  # type: ignore[arg-type]
            archive_root=tmp_path,
        )
        assert not result.passed
        assert "git unavailable" in result.message


class TestCheckProcessLifetimeRowReturnsId:
    """``check_process_lifetime_row`` exposes the row id it wrote.

    ALP-450 item 3: the verify script's smoke-test write (this function)
    and the e2e invocation both call ``record_process_lifetime``, leaving
    the smoke-test row orphaned. The fix threads the smoke-test row's
    id forward so the e2e invocation reuses it; that requires exposing
    the id on the check's return value.
    """

    @pytest.mark.asyncio
    async def test_passing_result_carries_process_lifetime_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from alphamind.scripts.verify_pipeline_scheduler import check_process_lifetime_row

        async def _stub(**_kwargs: Any) -> str:
            return "plt-abc-123"

        monkeypatch.setattr(
            "alphamind.scripts.verify_pipeline_scheduler.record_process_lifetime",
            _stub,
        )

        result, process_lifetime_id = await check_process_lifetime_row(
            session_factory=object(),  # type: ignore[arg-type]
            archive_root=tmp_path,
        )
        assert result.passed
        assert process_lifetime_id == "plt-abc-123"

    @pytest.mark.asyncio
    async def test_failing_result_returns_none_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from alphamind.scripts.verify_pipeline_scheduler import check_process_lifetime_row

        async def _stub(**_kwargs: Any) -> str:
            raise RuntimeError("git unavailable")

        monkeypatch.setattr(
            "alphamind.scripts.verify_pipeline_scheduler.record_process_lifetime",
            _stub,
        )

        result, process_lifetime_id = await check_process_lifetime_row(
            session_factory=object(),  # type: ignore[arg-type]
            archive_root=tmp_path,
        )
        assert not result.passed
        assert process_lifetime_id is None


class TestDriveOnceInvocationReusesProcessLifetimeId:
    """``_drive_once_invocation`` reuses the smoke-test row's id.

    ALP-450 item 3: with the smoke-test row id threaded forward,
    ``_drive_once_invocation`` must NOT call ``record_process_lifetime``
    a second time — exactly one row lands per verify run.
    """

    @pytest.mark.asyncio
    async def test_does_not_call_record_process_lifetime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from alphamind.config.models.run_types import RunType
        from alphamind.scripts import verify_pipeline_scheduler as module

        record_calls = {"count": 0}

        async def _record_stub(**_kwargs: Any) -> str:
            record_calls["count"] += 1
            return "should-not-be-called"

        monkeypatch.setattr(module, "record_process_lifetime", _record_stub)

        # Stub run_invocation; the orchestrator stays untouched. We don't
        # care what summary returns; just that _drive_once_invocation
        # forwarded the pre-existing id and didn't call the writer.
        from types import SimpleNamespace

        async def _run_invocation_stub(**kwargs: Any) -> Any:
            # Record the threading invariant: context carries the supplied id.
            assert kwargs["context"].process_lifetime_id == "plt-supplied-1"
            return SimpleNamespace(invocation_id="inv-stub-1", duration_seconds=0.0)

        # ``_drive_once_invocation`` imports run_invocation INSIDE the function;
        # patch the source module so the inner import picks up the stub.
        monkeypatch.setattr(
            "alphamind.scheduler.orchestrator.run_invocation",
            _run_invocation_stub,
        )

        class _StubEngine:
            async def dispose(self) -> None:
                return None

        monkeypatch.setattr(module, "make_async_engine", lambda: _StubEngine())
        monkeypatch.setattr(module, "make_async_session_factory", lambda _engine: object())

        invocation_id = await module._drive_once_invocation(
            archive_root=tmp_path,
            run_type=RunType.market_hours_rolling,
            mode="paper",
            process_lifetime_id="plt-supplied-1",
        )

        assert invocation_id == "inv-stub-1"
        assert record_calls["count"] == 0
