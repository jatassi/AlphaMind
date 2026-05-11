"""End-to-end pipeline-scheduler verification — story 05 / ALP-448.

Operator entry point for the pipeline-scheduler work-tree acceptance gate.
Drives one ``--once`` invocation through :func:`run_invocation` against
the paper DB and asserts every artifact landed: 21 must-be-set columns
on the ``invocations`` row, the Phase 1 / Phase 2 timestamps, at least
one ``activity_log`` entry, and the per-invocation archive directory with
``resolved_config.json`` + ``data_calibration_state.json``. Also asserts
the story 04b vocabulary additions are wired (``RunType.emergency``,
``EventType.EMERGENCY_INVOCATION_REQUESTED``, ``config/run_types/emergency.yaml``
parses, ``BreachBehaviorConfig.emergency_invocation_cooldown_minutes`` set).

Per the verify-script convention the testable predicates live as
module-level helpers so :mod:`tests.scripts.test_verify_pipeline_scheduler`
can exercise them with mocks. The end-to-end ``--once`` invocation is
exercised by :func:`main` itself; the unit tests stub it out.

Argparse surface::

    --archive-root DIR                       (required)
    --run-type {pre_open|...|emergency}      (default market_hours_rolling)
    --mode {paper|live}                      (default paper)

A missing ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ALPACA_PAPER_KEY`` /
``ALPACA_PAPER_SECRET`` is surfaced as a clean ``FAIL: auth`` before any
DB write. Exit code is 0 on full pass, non-zero on any FAIL.

See ``scripts/RUNBOOK_pipeline_scheduler.md`` for the operator runbook
and ``scripts/RUNBOOK_end_to_end_verification.md`` for cross-feature
sequencing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.state_persistence.process_lifetime import (
    record_process_lifetime,
)
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.risk_guardrails.breach_behavior.config import load_breach_behavior_config

__all__ = [
    "CheckResult",
    "check_activity_log",
    "check_archive_directory",
    "check_auth",
    "check_db_schema",
    "check_invocation_row_population",
    "check_process_lifetime_row",
    "check_vocabulary",
    "main",
]


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO_ROOT / "config"
_DEFAULT_ENV_PATH = _REPO_ROOT / ".env"

# Required env vars for the verify script. CLAUDE_CODE_OAUTH_TOKEN drives
# the analysis-pipeline / decision-pipeline LLM calls; ALPACA_PAPER_KEY /
# ALPACA_PAPER_SECRET drive Phase 1's broker-adapter fills fetch.
_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
)

# The three tables the verify script reads/writes against. Mirrors the
# state-persistence schema's append-only triad documented in
# ``state-persistence.md`` § Schema.
_REQUIRED_TABLES: tuple[str, ...] = (
    "invocations",
    "process_lifetimes",
    "activity_log",
)

# The 21 must-be-set columns the ``invocations`` row must carry by the
# end of one successful invocation. ``snapshot_metadata_json`` is
# documented as nullable per the storage spec; no production writer
# populates it in the current pipeline, so it is intentionally absent
# from this list. Drives the row-population check below.
_INVOCATION_ROW_COLUMNS: tuple[str, ...] = (
    "invocation_id",
    "process_lifetime_id",
    "start_at",
    "phase1_completed_at",
    "phase2_completed_at",
    "trigger_type",
    "trigger_source",
    "trigger_reason",
    "git_sha_at_invocation",
    "active_profile",
    "active_regime",
    "active_mode",
    "active_overlays_json",
    "resolved_config_hash",
    "resolved_config_snapshot_path",
    "feature_flags_snapshot_json",
    "data_calibration_state_snapshot_path",
    "data_source_freshness_json",
    "fill_collection_summary_json",
    "command_execution_summary_json",
    "staleness_flag",
)


# ---------------------------------------------------------------------------
# CheckResult — small value type the operator output is built from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome: a short label, pass/fail flag, and message.

    The verify script prints one ``CheckResult`` per logical step (auth,
    schema, process_lifetime, --once invocation, row population, activity
    log, archive, vocabulary). Each is formatted as ``PASS: <label> — <message>``
    or ``FAIL: <label> — <message>`` in :meth:`format_line`.
    """

    label: str
    passed: bool
    message: str

    def format_line(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return f"{verdict}: {self.label} — {self.message}"


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------


def check_auth() -> CheckResult:
    """Surface any missing required env var as a clean FAIL.

    The Claude Agent SDK authenticates via ``CLAUDE_CODE_OAUTH_TOKEN`` and
    the Alpaca paper-broker adapter authenticates via ``ALPACA_PAPER_KEY``
    / ``ALPACA_PAPER_SECRET``. All three must be present before the verify
    script touches the DB; running ``--once`` with any one missing would
    surface as an opaque traceback deep inside Phase 1 or the decision
    pipeline.
    """
    missing = [var for var in _REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        return CheckResult(
            label="auth",
            passed=False,
            message=(
                f"missing required env var(s): {', '.join(missing)} — "
                "set them in .env (dev) or the service ObjectName's profile "
                "(prod) per docs/architecture/llm-integration.md § Authentication"
            ),
        )
    return CheckResult(
        label="auth",
        passed=True,
        message=f"all required env vars present ({', '.join(_REQUIRED_ENV_VARS)})",
    )


# ---------------------------------------------------------------------------
# DB schema check
# ---------------------------------------------------------------------------


def check_db_schema(conn: Connection) -> CheckResult:
    """Confirm the three required tables exist with their expected columns.

    Inspects the live DB the verify script is configured against. Missing
    tables surface as a clear FAIL — the most common cause is pointing at
    an empty SQLite file or a pre-migration DB.
    """
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    missing = [t for t in _REQUIRED_TABLES if t not in existing]
    if missing:
        return CheckResult(
            label="schema",
            passed=False,
            message=f"missing tables: {', '.join(missing)}",
        )
    # Spot-check that the ``invocations`` table carries every must-be-set
    # column — the migration head should produce all 21, but a pre-migration
    # DB surfaces as a partial column list rather than a missing table.
    inv_cols = {c["name"] for c in inspector.get_columns("invocations")}
    missing_cols = [c for c in _INVOCATION_ROW_COLUMNS if c not in inv_cols]
    if missing_cols:
        return CheckResult(
            label="schema",
            passed=False,
            message=f"invocations missing columns: {', '.join(missing_cols)}",
        )
    return CheckResult(
        label="schema",
        passed=True,
        message=(
            f"tables present: {', '.join(_REQUIRED_TABLES)}; "
            f"invocations carries all 21 must-be-set columns"
        ),
    )


# ---------------------------------------------------------------------------
# Process lifetime row write check
# ---------------------------------------------------------------------------


async def check_process_lifetime_row(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    archive_root: Path,
) -> tuple[CheckResult, str | None]:
    """Drive one ``record_process_lifetime`` call and confirm the row landed.

    Returns ``(check_result, process_lifetime_id)``. On success the second
    element is the id the writer minted (threaded forward into the e2e
    invocation so the same row is reused — only one row lands per verify
    run). On writer failure the second element is ``None`` and the check
    is FAIL; the most common cause is ``git rev-parse HEAD`` returning
    non-zero (missing git binary, detached worktree).
    """
    try:
        process_lifetime_id = await record_process_lifetime(
            session_factory=session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
    except Exception as exc:
        # The writer's failure modes are intentionally varied (git missing,
        # DB unwritable, pip-freeze fails); surface the raw message.
        return (
            CheckResult(
                label="process_lifetime",
                passed=False,
                message=f"record_process_lifetime raised: {exc}",
            ),
            None,
        )
    return (
        CheckResult(
            label="process_lifetime",
            passed=True,
            message=f"row landed: process_lifetime_id={process_lifetime_id}",
        ),
        process_lifetime_id,
    )


# ---------------------------------------------------------------------------
# Invocation row population check
# ---------------------------------------------------------------------------


def _row_fail(message: str) -> CheckResult:
    """Build a FAIL ``CheckResult`` for the row-population label."""
    return CheckResult(label="row_population", passed=False, message=message)


def _validate_row_paths(row: dict[str, str]) -> str | None:
    """Return an error message if the row's snapshot-path columns are wrong.

    Combines the four snapshot-path failure modes (resolved_config missing
    / empty + data_calibration missing) into one validator so the outer
    ``check_invocation_row_population`` stays at six returns.
    """
    cfg_path = Path(row["resolved_config_snapshot_path"])
    if not cfg_path.exists():
        return f"resolved_config_snapshot_path not on disk: {cfg_path}"
    if cfg_path.stat().st_size == 0:
        return f"resolved_config_snapshot_path file is empty: {cfg_path}"
    data_cal_path = Path(row["data_calibration_state_snapshot_path"])
    if not data_cal_path.exists():
        return f"data_calibration_state_snapshot_path not on disk: {data_cal_path}"
    return None


def _validate_row_json_columns(row: dict[str, str]) -> str | None:
    """Return an error message if any required JSON column failed to parse."""
    for json_col in ("active_overlays_json", "feature_flags_snapshot_json"):
        try:
            json.loads(row[json_col])
        except (TypeError, ValueError) as exc:
            return f"{json_col} did not parse as JSON: {exc}"
    return None


def check_invocation_row_population(conn: Connection, *, invocation_id: str) -> CheckResult:
    """Assert every required column of the ``invocations`` row landed cleanly.

    Specifically:
      * Every one of the 21 must-be-set columns is populated (per the spec
        — including the two phase-completion timestamps and both summary
        JSON columns). ``snapshot_metadata_json`` is intentionally not
        checked: the storage spec lists it as nullable and no production
        writer populates it.
      * ``trigger_type`` is ``'manual'`` (the verify script always drives a
        manual ``--once`` invocation).
      * ``active_overlays_json`` and ``feature_flags_snapshot_json`` parse as
        valid JSON.
      * ``resolved_config_snapshot_path`` points to a non-empty file on disk.
      * ``data_calibration_state_snapshot_path`` points to a file on disk
        (possibly an empty ``{}`` first-invocation snapshot).
    """
    row_result = (
        conn.exec_driver_sql(
            "SELECT * FROM invocations WHERE invocation_id = :iid",
            {"iid": invocation_id},
        )
        .mappings()
        .first()
    )
    if row_result is None:
        return _row_fail(f"no invocations row found for invocation_id={invocation_id!r}")
    row = dict(row_result)

    null_columns = [c for c in _INVOCATION_ROW_COLUMNS if row.get(c) is None]
    if null_columns:
        return _row_fail(f"columns NULL on invocation row: {', '.join(null_columns)}")

    if row["trigger_type"] != "manual":
        return _row_fail(f"trigger_type expected 'manual', got {row['trigger_type']!r}")

    json_error = _validate_row_json_columns(row)
    if json_error is not None:
        return _row_fail(json_error)

    path_error = _validate_row_paths(row)
    if path_error is not None:
        return _row_fail(path_error)

    return CheckResult(
        label="row_population",
        passed=True,
        message=(
            "all 21 must-be-set invocation-row columns populated; trigger_type=manual; "
            "JSON columns parse; resolved_config + data_calibration paths exist"
        ),
    )


# ---------------------------------------------------------------------------
# Activity-log check
# ---------------------------------------------------------------------------


def check_activity_log(conn: Connection, *, invocation_id: str) -> CheckResult:
    """Assert at least one ``activity_log`` entry was emitted for this invocation.

    Per the spec, any subsystem may have emitted one — ``FILL_PROCESSOR``
    if fills landed, ``COMMAND_EXECUTOR`` if commands landed,
    ``CONFIG_RELOAD`` always on first invocation per config change.
    """
    rows = conn.exec_driver_sql(
        "SELECT event_type FROM activity_log WHERE invocation_id = :iid",
        {"iid": invocation_id},
    ).all()
    if not rows:
        return CheckResult(
            label="activity_log",
            passed=False,
            message=(
                f"no activity_log entries found for invocation_id={invocation_id!r}; "
                "expected at least one (FILL_PROCESSOR / COMMAND_EXECUTOR / "
                "CONFIG_RELOAD)"
            ),
        )
    # Count per event_type for the operator summary line.
    breakdown: dict[str, int] = {}
    for (event_type,) in rows:
        breakdown[event_type] = breakdown.get(event_type, 0) + 1
    parts = [f"{et}={n}" for et, n in sorted(breakdown.items())]
    return CheckResult(
        label="activity_log",
        passed=True,
        message=f"{len(rows)} entry/entries: {', '.join(parts)}",
    )


# ---------------------------------------------------------------------------
# Archive directory check
# ---------------------------------------------------------------------------


def check_archive_directory(*, archive_root: Path, invocation_id: str) -> CheckResult:
    """Assert the per-invocation archive directory exists with the required files.

    Spec requires ``<archive_root>/invocations/<invocation_id>/`` to exist
    and to contain at least ``resolved_config.json``; the orchestrator
    also writes ``data_calibration_state.json`` in the same directory.
    """
    inv_dir = archive_root / "invocations" / invocation_id
    if not inv_dir.is_dir():
        return CheckResult(
            label="archive",
            passed=False,
            message=f"archive directory missing: {inv_dir}",
        )
    resolved_cfg = inv_dir / "resolved_config.json"
    if not resolved_cfg.is_file():
        return CheckResult(
            label="archive",
            passed=False,
            message=f"resolved_config.json missing under {inv_dir}",
        )
    return CheckResult(
        label="archive",
        passed=True,
        message=f"directory + resolved_config.json present at {inv_dir}",
    )


# ---------------------------------------------------------------------------
# Vocabulary check (story 04b additions)
# ---------------------------------------------------------------------------


def check_vocabulary(*, config_dir: Path) -> CheckResult:
    """Assert the story-04b vocabulary additions are in place.

    Specifically:
      * ``EventType.EMERGENCY_INVOCATION_REQUESTED`` is importable.
      * ``RunType.emergency`` is a valid member.
      * ``<config_dir>/run_types/emergency.yaml`` parses via the
        run-type resolver.
      * ``BreachBehaviorConfig.emergency_invocation_cooldown_minutes`` is
        ``30`` after loading ``<config_dir>/breach_behavior.yaml``.
    """
    error = _verify_vocab_imports() or _verify_emergency_yaml(config_dir)
    if error is not None:
        return CheckResult(label="vocabulary", passed=False, message=error)

    error = _verify_breach_cooldown(config_dir)
    if error is not None:
        return CheckResult(label="vocabulary", passed=False, message=error)

    return CheckResult(
        label="vocabulary",
        passed=True,
        message=(
            "EventType.EMERGENCY_INVOCATION_REQUESTED ok; RunType.emergency ok; "
            "emergency.yaml parses; cooldown=30"
        ),
    )


def _verify_vocab_imports() -> str | None:
    """Return an error message if the vocabulary additions aren't importable."""
    try:
        from alphamind.portfolio_state.events.activity_log import EventType
    except ImportError as exc:
        return f"failed to import EventType: {exc}"
    if not hasattr(EventType, "EMERGENCY_INVOCATION_REQUESTED"):
        return "EventType.EMERGENCY_INVOCATION_REQUESTED is not defined"
    if not hasattr(RunType, "emergency"):
        return "RunType.emergency is not a member of RunType"
    return None


def _verify_emergency_yaml(config_dir: Path) -> str | None:
    """Return an error message if ``emergency.yaml`` is missing or unparseable."""
    emergency_yaml = config_dir / "run_types" / "emergency.yaml"
    if not emergency_yaml.is_file():
        return f"emergency.yaml not found at {emergency_yaml}"
    try:
        from alphamind.config.loaders import read_yaml_file
        from alphamind.config.models.run_types import RunTypeConfig

        RunTypeConfig.model_validate(read_yaml_file(emergency_yaml))
    except Exception as exc:
        # Surface the validator's message verbatim — the Pydantic error
        # names the offending field/value.
        return f"emergency.yaml failed to parse: {exc}"
    return None


def _verify_breach_cooldown(config_dir: Path) -> str | None:
    """Return an error message if the breach-cooldown default has drifted from 30."""
    breach_yaml = config_dir / "breach_behavior.yaml"
    try:
        breach_cfg = load_breach_behavior_config(breach_yaml)
    except Exception as exc:
        return f"breach_behavior.yaml failed to load: {exc}"
    expected_cooldown = 30
    actual_cooldown = breach_cfg.emergency_invocation_cooldown_minutes
    if actual_cooldown != expected_cooldown:
        return (
            f"emergency_invocation_cooldown_minutes expected "
            f"{expected_cooldown}, got {actual_cooldown}"
        )
    return None


# ---------------------------------------------------------------------------
# Argparse + main entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_pipeline_scheduler",
        description=(
            "End-to-end verification for the pipeline scheduler — drives one "
            "manual --once invocation against the paper DB and asserts every "
            "artifact landed."
        ),
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        required=True,
        help=(
            "Root of the verification archive — the script writes the "
            "per-invocation directory under <archive-root>/invocations/<id>/."
        ),
    )
    parser.add_argument(
        "--run-type",
        choices=[rt.value for rt in RunType],
        default=RunType.market_hours_rolling.value,
        help=("Run-type firing trigger for the manual invocation (default market_hours_rolling)."),
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default paper).",
    )
    return parser.parse_args(argv)


def _print_result(result: CheckResult) -> None:
    print(result.format_line())


def _read_only_engine() -> Engine:
    """Return a synchronous engine for the schema + row + activity-log reads.

    The orchestrator drives the async path internally; the verify script's
    post-invocation checks use a short-lived sync connection because the
    queries are simple table scans and the helpers stay easy to unit-test
    against a sync ``sqlite_engine`` fixture.
    """
    from alphamind.persistence.session import make_engine

    return make_engine()


async def _drive_once_invocation(
    *,
    archive_root: Path,
    run_type: RunType,
    mode: str,
    process_lifetime_id: str,
) -> str:
    """Drive one ``--once`` invocation end-to-end and return the invocation id.

    Mirrors the CLI's ``_run_once`` body in :mod:`alphamind.scheduler.__main__`;
    we cannot directly call that private helper because it parses argparse
    namespaces, but its semantics are identical.

    ``process_lifetime_id`` is supplied by the upstream smoke-test check
    (``check_process_lifetime_row``) so the e2e invocation reuses that
    row instead of writing a second one.
    """
    from alphamind.config.loaders import read_yaml_file
    from alphamind.scheduler.logging_setup import configure_pipeline_logging
    from alphamind.scheduler.orchestrator import run_invocation
    from alphamind.scheduler.run_context import RunInvocationContext

    configure_pipeline_logging()
    await asyncio.to_thread(archive_root.mkdir, parents=True, exist_ok=True)

    venue_config = VenueConfig.model_validate(read_yaml_file(_CONFIG_DIR / "venue.yaml"))
    execution_mode = ExecutionMode.live if mode == "live" else ExecutionMode.paper

    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    try:
        context = RunInvocationContext(
            session_factory=session_factory,
            process_lifetime_id=process_lifetime_id,
            archive_root=archive_root,
            config_dir=_CONFIG_DIR,
            env_path=_DEFAULT_ENV_PATH,
            venue_config=venue_config,
            execution_mode=execution_mode,
        )
        summary = await run_invocation(
            context=context,
            trigger_type="manual",
            trigger_source="verify_pipeline_scheduler",
            trigger_reason="e2e verify",
            firing_run_type=run_type,
            now=datetime.now(UTC),
        )
    finally:
        await engine.dispose()
    return summary.invocation_id


def _run_schema_check() -> CheckResult:
    """Open a short sync connection and run the schema check."""
    engine = _read_only_engine()
    try:
        with engine.connect() as conn:
            return check_db_schema(conn)
    finally:
        engine.dispose()


def _run_process_lifetime_check(archive_root: Path) -> tuple[CheckResult, str | None]:
    """Drive ``check_process_lifetime_row`` against a one-shot async engine."""
    async_engine = make_async_engine()
    async_factory = make_async_session_factory(async_engine)

    async def _run() -> tuple[CheckResult, str | None]:
        try:
            return await check_process_lifetime_row(
                session_factory=async_factory, archive_root=archive_root
            )
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())


def _run_once_invocation(
    *, archive_root: Path, run_type: RunType, mode: str, process_lifetime_id: str
) -> tuple[CheckResult, str | None]:
    """Drive one ``--once`` invocation and bundle the outcome.

    Returns ``(check_result, invocation_id)``. On success the second
    element is the new invocation id (consumed by the downstream row /
    activity-log / archive checks); on failure it is ``None``.

    ``process_lifetime_id`` is reused from the upstream smoke-test row so
    the e2e invocation writes only the orchestrator's ``invocations`` row.
    """
    try:
        invocation_id = asyncio.run(
            _drive_once_invocation(
                archive_root=archive_root,
                run_type=run_type,
                mode=mode,
                process_lifetime_id=process_lifetime_id,
            )
        )
    except Exception as exc:
        # Any orchestrator failure is a FAIL for this check; the exception's
        # type + message names the failing layer for triage.
        return (
            CheckResult(
                label="once_invocation",
                passed=False,
                message=f"run_invocation raised: {type(exc).__name__}: {exc}",
            ),
            None,
        )
    return (
        CheckResult(
            label="once_invocation",
            passed=True,
            message=f"run_invocation returned InvocationSummary (id={invocation_id})",
        ),
        invocation_id,
    )


def _run_post_invocation_db_checks(invocation_id: str) -> list[CheckResult]:
    """Run the row-population and activity-log checks under one sync connection."""
    engine = _read_only_engine()
    try:
        with engine.connect() as conn:
            row = check_invocation_row_population(conn, invocation_id=invocation_id)
            activity = check_activity_log(conn, invocation_id=invocation_id)
    finally:
        engine.dispose()
    return [row, activity]


def _emit(results: list[CheckResult], result: CheckResult) -> None:
    """Append a result to the running list and print its formatted line."""
    results.append(result)
    _print_result(result)


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the pipeline scheduler end-to-end against the paper DB.

    Drives one manual ``--once`` invocation through ``run_invocation``,
    asserts every artifact landed, and prints a per-check PASS/FAIL
    summary an operator can read. Exit 0 on all-pass; non-zero on any FAIL.

    Note: the verify script does NOT auto-load ``.env`` — by convention the
    operator sources it inline before invoking the script (e.g.
    ``set -a && source .env && set +a && uv run python scripts/...``).
    In production NSSM puts the vars in the service account's environment
    directly. This keeps the env-var pre-flight check honest under both
    dev-machine and unit-test conditions.
    """
    args = _parse_args(argv)
    results: list[CheckResult] = []

    # Pre-flight gates — each shortcircuits if a guarantee the next step
    # depends on is broken.
    _emit(results, check_auth())
    if not results[-1].passed:
        _print_summary(results)
        return 1
    _emit(results, _run_schema_check())
    if not results[-1].passed:
        _print_summary(results)
        return 1
    plt_check, process_lifetime_id = _run_process_lifetime_check(args.archive_root)
    _emit(results, plt_check)
    if not plt_check.passed or process_lifetime_id is None:
        _print_summary(results)
        return 1

    # End-to-end --once invocation. The downstream checks depend on this
    # invocation_id existing in the DB / on disk; on FAIL the script bails.
    # The smoke-test row's process_lifetime_id is reused so the e2e
    # invocation does not write a second row.
    invocation_result, invocation_id = _run_once_invocation(
        archive_root=args.archive_root,
        run_type=RunType(args.run_type),
        mode=args.mode,
        process_lifetime_id=process_lifetime_id,
    )
    _emit(results, invocation_result)
    if not invocation_result.passed or invocation_id is None:
        _print_summary(results)
        return 1

    # Post-invocation artifact checks (row + activity log + archive + vocab).
    for r in _run_post_invocation_db_checks(invocation_id):
        _emit(results, r)
    _emit(
        results,
        check_archive_directory(archive_root=args.archive_root, invocation_id=invocation_id),
    )
    _emit(results, check_vocabulary(config_dir=_CONFIG_DIR))

    _print_summary(results)
    return 0 if all(r.passed for r in results) else 1


def _print_summary(results: list[CheckResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"=== PIPELINE SCHEDULER VERIFICATION === {passed}/{total} checks passed")


if __name__ == "__main__":  # pragma: no cover - exercised via the thin shim
    sys.exit(main())
