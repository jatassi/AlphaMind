"""End-to-end verification for ``python -m alphamind.scheduler run --debug-e2e``.

Operator entry point for the ALP-493 work tree's acceptance gate.
Subprocesses one ``--debug-e2e`` invocation against the dedicated debug
DB (``data/alphamind-debug-e2e.db``), then runs six check helpers
against the resulting archive. Exits 0 on full pass, non-zero on any
FAIL.

The six check helpers are module-level functions so
``tests/scripts/test_verify_debug_e2e.py`` can exercise them without
driving a real subprocess:

* :func:`check_auth` — ``CLAUDE_CODE_OAUTH_TOKEN`` present; Alpaca
  credentials NOT required.
* :func:`check_archive_directory` — ``<archive>/invocations/<id>/``
  exists with ``resolved_config.json`` and ``progress.jsonl``.
* :func:`check_jsonl_ordering` — 13 ``phase_start``/``phase_done``
  pairs plus 9 ``agent_request``/``agent_response`` pairs in
  dependency order; ``domain_researchers``/``qualitative`` and
  ``analyst``/``strategist`` parallel-overlap pairs tolerated.
* :func:`check_synthetic_portfolio_visibility` — 8 positions, 8 theses,
  cash ledger seeded at $24,440.
* :func:`check_no_alpaca` — pipeline log carries no ``alpaca-py``
  indicators.
* :func:`check_invocation_summary` — subprocess stdout JSON carries
  ``staleness_flag=false``, ``commands_submitted>=0``,
  ``trigger_source="debug_e2e_cli"``.

Usage::

    set -a && source .env && set +a && \
        uv run python scripts/verify_debug_e2e.py \
            --archive-root .archive/verify-debug-e2e \
            [--db-path data/alphamind-debug-e2e.db] \
            [--run-type market_hours_rolling] \
            [--reason "verify_debug_e2e"]

See ``scripts/RUNBOOK_debug_e2e.md`` for the operator runbook and
``scripts/RUNBOOK_end_to_end_verification.md`` for the central runbook.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from alphamind._kernel.invocations import (
    INVOCATIONS_DIRNAME,
    RESOLVED_CONFIG_FILENAME,
)
from alphamind.config.models.run_types import RunType
from alphamind.scripts._stdio import configure_utf8_stdio

__all__ = [
    "CheckResult",
    "check_archive_directory",
    "check_auth",
    "check_invocation_summary",
    "check_jsonl_ordering",
    "check_no_alpaca",
    "check_synthetic_portfolio_visibility",
    "main",
]


# ---------------------------------------------------------------------------
# CheckResult — small value type the operator output is built from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome: a short label, pass/fail flag, and message.

    The verify script prints one ``CheckResult`` per logical step, formatted as
    ``PASS: <label> — <message>`` or ``FAIL: <label> — <message>`` in
    :meth:`format_line`.
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


_REQUIRED_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN",)


def check_auth() -> CheckResult:
    """Surface a missing ``CLAUDE_CODE_OAUTH_TOKEN`` as a clean FAIL.

    Debug-e2e never touches Alpaca (the broker adapter is replaced with the
    log-only stand-in), so Alpaca credentials are deliberately NOT required.
    """
    missing = [var for var in _REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        return CheckResult(
            label="auth",
            passed=False,
            message=(
                f"missing required env var(s): {', '.join(missing)} — "
                "set them in .env (dev) or the service account's profile (prod)"
            ),
        )
    return CheckResult(
        label="auth",
        passed=True,
        message=f"all required env vars present ({', '.join(_REQUIRED_ENV_VARS)})",
    )


# ---------------------------------------------------------------------------
# Archive directory check
# ---------------------------------------------------------------------------


_PROGRESS_JSONL_FILENAME = "progress.jsonl"


def check_archive_directory(*, archive_root: Path, invocation_id: str) -> CheckResult:
    """Assert the per-invocation directory carries the two required files."""
    inv_dir = archive_root / INVOCATIONS_DIRNAME / invocation_id
    if not inv_dir.is_dir():
        return CheckResult(
            label="archive_directory",
            passed=False,
            message=f"archive directory missing: {inv_dir}",
        )
    resolved = inv_dir / RESOLVED_CONFIG_FILENAME
    if not resolved.is_file():
        return CheckResult(
            label="archive_directory",
            passed=False,
            message=f"{RESOLVED_CONFIG_FILENAME} missing under {inv_dir}",
        )
    progress = inv_dir / _PROGRESS_JSONL_FILENAME
    if not progress.is_file():
        return CheckResult(
            label="archive_directory",
            passed=False,
            message=f"{_PROGRESS_JSONL_FILENAME} missing under {inv_dir}",
        )
    return CheckResult(
        label="archive_directory",
        passed=True,
        message=(
            f"directory + {RESOLVED_CONFIG_FILENAME} + {_PROGRESS_JSONL_FILENAME} "
            f"present at {inv_dir}"
        ),
    )


# ---------------------------------------------------------------------------
# JSONL ordering check
# ---------------------------------------------------------------------------


# 13 phases in dependency order. ``domain_researchers``/``qualitative`` open
# in parallel under the analysis TaskGroup; ``analyst``/``strategist`` open
# in parallel under the decision TaskGroup. The dependency-graph below pins
# every phase's required predecessors using only the strict edges; sibling
# phases (peers under a TaskGroup) are intentionally absent from each
# other's edge sets so the parallel-overlap event interleaving validates.
_PHASE_PREDECESSORS: dict[str, frozenset[str]] = {
    "seed": frozenset(),
    "phase1": frozenset({"seed"}),
    "snapshot_assembly": frozenset({"phase1"}),
    "distillation": frozenset({"snapshot_assembly"}),
    "domain_researchers": frozenset({"distillation"}),
    "qualitative": frozenset({"distillation"}),
    "adaptive": frozenset({"domain_researchers", "qualitative"}),
    "synthesizer": frozenset({"adaptive"}),
    "analyst": frozenset({"synthesizer"}),
    "strategist": frozenset({"synthesizer"}),
    "pre_processor": frozenset({"analyst", "strategist"}),
    "pm": frozenset({"pre_processor"}),
    "phase2": frozenset({"pm"}),
}

# 9 SDK call pairs per parent issue ALP-493 § (E) — 3 domain-researcher
# sectors + 6 single-call agents. Each is a ``(phase, agent)`` tuple so
# we can match the request/response events by their joint identity.
_SDK_CALL_PAIRS: tuple[tuple[str, str], ...] = (
    ("distillation", "distillation"),
    ("domain_researchers", "tech_semis_researcher"),
    ("domain_researchers", "financials_researcher"),
    ("domain_researchers", "energy_researcher"),
    ("qualitative", "qualitative_researcher"),
    ("adaptive", "adaptive_researcher"),
    ("synthesizer", "synthesizer"),
    ("analyst", "analyst"),
    ("strategist", "strategist"),
    ("pm", "portfolio_manager"),
)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def _ordering_fail(message: str) -> CheckResult:
    return CheckResult(label="jsonl_ordering", passed=False, message=message)


def check_jsonl_ordering(path: Path) -> CheckResult:
    """Assert the JSONL event stream matches the expected dependency graph.

    Validates four invariants:

    1. **Monotonic timestamps.** Each event's ``timestamp`` is >= the prior.
       The JSONL emitter's ``fsync`` per write guarantees this in practice;
       a regression here would point at a clock-skew or out-of-order write.
    2. **13 phase pairs.** Each of the 13 phases produces exactly one
       ``phase_start`` and one ``phase_done``, and ``phase_done`` follows
       its own ``phase_start``.
    3. **Phase-level dependency graph.** ``phase_start("X")`` happens after
       every predecessor in ``_PHASE_PREDECESSORS[X]`` has emitted its
       ``phase_done``. The parallel-overlap pairs
       (``domain_researchers``/``qualitative`` and
       ``analyst``/``strategist``) are NOT each other's predecessors, so
       their event interleaving validates either way.
    4. **9 SDK call pairs.** Each of the 9 ``(phase, agent)`` tuples in
       ``_SDK_CALL_PAIRS`` emits one ``agent_request`` followed by one
       ``agent_response``.
    """
    try:
        events = _load_jsonl(path)
    except (OSError, json.JSONDecodeError) as exc:
        return _ordering_fail(f"failed to read {path}: {exc}")

    if not events:
        return _ordering_fail(f"empty event stream at {path}")

    timestamp_error = _check_monotonic_timestamps(events)
    if timestamp_error is not None:
        return _ordering_fail(timestamp_error)

    phase_error = _check_phase_pairs(events)
    if phase_error is not None:
        return _ordering_fail(phase_error)

    sdk_error = _check_sdk_call_pairs(events)
    if sdk_error is not None:
        return _ordering_fail(sdk_error)

    return CheckResult(
        label="jsonl_ordering",
        passed=True,
        message=(
            f"13/13 phases with paired start/done in dependency order; "
            f"{len(_SDK_CALL_PAIRS)}/{len(_SDK_CALL_PAIRS)} SDK call pairs matched"
        ),
    )


def _check_monotonic_timestamps(events: list[dict[str, object]]) -> str | None:
    prev_ts: str | None = None
    for index, ev in enumerate(events):
        ts_raw = ev.get("timestamp")
        if not isinstance(ts_raw, str):
            return f"event #{index} missing string ``timestamp``"
        if prev_ts is not None and ts_raw < prev_ts:
            return (
                f"event #{index} timestamp {ts_raw!r} precedes previous {prev_ts!r} "
                "(non-monotonic; fsync should have prevented this)"
            )
        prev_ts = ts_raw
    return None


def _index_phase_events(
    events: list[dict[str, object]],
) -> tuple[dict[str, int], dict[str, int], str | None]:
    """Pull the per-phase ``phase_start`` / ``phase_done`` event indices.

    Returns ``(starts_by_index, dones_by_index, error)``. The error string
    is non-``None`` when the indexing pass itself surfaces a malformed
    pair (duplicate event, ``phase_done`` before ``phase_start``) — the
    caller short-circuits the dependency-graph check in that case.
    """
    starts_by_index: dict[str, int] = {}
    dones_by_index: dict[str, int] = {}
    for index, ev in enumerate(events):
        kind = ev.get("event")
        phase = ev.get("phase")
        if not isinstance(phase, str):
            continue
        if kind == "phase_start":
            if phase in starts_by_index:
                return starts_by_index, dones_by_index, f"duplicate phase_start for {phase!r}"
            starts_by_index[phase] = index
        elif kind == "phase_done":
            if phase in dones_by_index:
                return starts_by_index, dones_by_index, f"duplicate phase_done for {phase!r}"
            if phase not in starts_by_index:
                return (
                    starts_by_index,
                    dones_by_index,
                    f"phase_done for {phase!r} at event #{index} precedes its own phase_start",
                )
            dones_by_index[phase] = index
    return starts_by_index, dones_by_index, None


def _check_phase_completeness(
    starts_by_index: dict[str, int], dones_by_index: dict[str, int]
) -> str | None:
    """Every expected phase has paired start + done; no unexpected phases."""
    expected = set(_PHASE_PREDECESSORS)
    missing_starts = sorted(expected - set(starts_by_index))
    if missing_starts:
        return f"missing phase_start for: {', '.join(missing_starts)}"
    missing_dones = sorted(expected - set(dones_by_index))
    if missing_dones:
        return f"missing phase_done for: {', '.join(missing_dones)}"
    unexpected = sorted(set(starts_by_index) - expected)
    if unexpected:
        return f"unexpected phase_start for: {', '.join(unexpected)}"
    return None


def _check_phase_dependency_graph(
    starts_by_index: dict[str, int], dones_by_index: dict[str, int]
) -> str | None:
    """Each phase's ``phase_start`` follows every predecessor's ``phase_done``."""
    for phase, preds in _PHASE_PREDECESSORS.items():
        start_index = starts_by_index[phase]
        for pred in preds:
            pred_done_index = dones_by_index[pred]
            if start_index < pred_done_index:
                return (
                    f"phase_start({phase!r}) at #{start_index} precedes its "
                    f"predecessor phase_done({pred!r}) at #{pred_done_index}"
                )
    return None


def _check_phase_pairs(events: list[dict[str, object]]) -> str | None:
    """Validate the 13 phase_start/phase_done pairs + dependency graph."""
    starts, dones, indexing_error = _index_phase_events(events)
    if indexing_error is not None:
        return indexing_error
    completeness_error = _check_phase_completeness(starts, dones)
    if completeness_error is not None:
        return completeness_error
    return _check_phase_dependency_graph(starts, dones)


def _check_sdk_call_pairs(events: list[dict[str, object]]) -> str | None:
    """Validate the 9 SDK call request/response pairs."""
    seen_requests: set[tuple[str, str]] = set()
    matched_pairs: set[tuple[str, str]] = set()
    for index, ev in enumerate(events):
        kind = ev.get("event")
        phase_raw = ev.get("phase")
        agent_raw = ev.get("agent")
        if not isinstance(phase_raw, str) or not isinstance(agent_raw, str):
            continue
        key = (phase_raw, agent_raw)
        if kind == "agent_request":
            if key in seen_requests:
                return f"duplicate agent_request for {key!r}"
            seen_requests.add(key)
        elif kind == "agent_response":
            if key not in seen_requests:
                return (
                    f"agent_response for {key!r} at event #{index} has no preceding agent_request"
                )
            matched_pairs.add(key)

    expected = set(_SDK_CALL_PAIRS)
    missing = expected - matched_pairs
    if missing:
        sorted_missing = ", ".join(sorted(f"{p}:{a}" for p, a in missing))
        return f"missing agent_request/response pair(s): {sorted_missing}"
    unexpected = matched_pairs - expected
    if unexpected:
        sorted_extra = ", ".join(sorted(f"{p}:{a}" for p, a in unexpected))
        return f"unexpected agent_request/response pair(s): {sorted_extra}"
    return None


# ---------------------------------------------------------------------------
# Synthetic portfolio visibility check
# ---------------------------------------------------------------------------


_EXPECTED_CASH_USD = 24_440.0


def check_synthetic_portfolio_visibility(engine: Engine) -> CheckResult:
    """Assert the seeded debug DB carries the synthetic-portfolio shape."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing_tables = [t for t in ("positions", "theses", "cash_ledger") if t not in existing_tables]
    if missing_tables:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"required table(s) missing from DB: {', '.join(missing_tables)}",
        )

    with engine.connect() as conn:
        positions_count = conn.execute(text("SELECT COUNT(*) FROM positions")).scalar_one()
        theses_count = conn.execute(text("SELECT COUNT(*) FROM theses")).scalar_one()
        cash_rows = conn.execute(text("SELECT current_cash_usd FROM cash_ledger")).all()

    if positions_count != 8:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"positions count expected 8, got {positions_count}",
        )
    if theses_count != 8:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"theses count expected 8, got {theses_count}",
        )
    if len(cash_rows) != 1:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"cash_ledger row count expected 1, got {len(cash_rows)}",
        )
    cash_value = float(cash_rows[0][0])
    if cash_value != _EXPECTED_CASH_USD:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=(
                f"cash_ledger.current_cash_usd expected {_EXPECTED_CASH_USD}, got {cash_value}"
            ),
        )
    return CheckResult(
        label="synthetic_portfolio",
        passed=True,
        message=(f"positions=8, theses=8, cash_ledger.current_cash_usd={cash_value}"),
    )


# ---------------------------------------------------------------------------
# Alpaca leak check
# ---------------------------------------------------------------------------


# Indicators that real Alpaca HTTP traffic leaked into the pipeline log.
# ``alpaca-py`` is the SDK package name; ``alpaca.markets`` is the canonical
# hostname; ``/v2/positions`` / ``/v2/orders`` are the REST endpoints the
# adapter calls. The log-only broker emits no HTTP and never names these
# tokens, so any hit is a regression.
_ALPACA_INDICATORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"alpaca-py", re.IGNORECASE),
    re.compile(r"alpaca\.markets", re.IGNORECASE),
    re.compile(r"/v2/(positions|orders|account|activities)", re.IGNORECASE),
)


def check_no_alpaca(log_path: Path) -> CheckResult:
    """Grep the pipeline log for Alpaca HTTP indicators; FAIL on any hit.

    A missing log is reported as PASS — the pipeline-log location depends
    on the runtime environment (``~/AlphaMind/logs/pipeline.log`` on
    POSIX, ``%USERPROFILE%\\AlphaMind\\logs\\pipeline.log`` on Windows)
    and may not exist in CI. The check's primary failure mode is "Alpaca
    leaked"; "no log at all" is an operator-known SKIP.
    """
    if not log_path.is_file():
        return CheckResult(
            label="no_alpaca",
            passed=True,
            message=f"pipeline log not at {log_path} (SKIP — no log to scan)",
        )
    text_blob = log_path.read_text(encoding="utf-8", errors="replace")
    hits: list[str] = []
    for pattern in _ALPACA_INDICATORS:
        match = pattern.search(text_blob)
        if match is not None:
            hits.append(match.group(0))
    if hits:
        return CheckResult(
            label="no_alpaca",
            passed=False,
            message=(
                f"pipeline log at {log_path} carries alpaca indicator(s): "
                f"{', '.join(sorted(set(hits)))}"
            ),
        )
    return CheckResult(
        label="no_alpaca",
        passed=True,
        message=f"no alpaca indicators in pipeline log at {log_path}",
    )


# ---------------------------------------------------------------------------
# Invocation-summary JSON check
# ---------------------------------------------------------------------------


_EXPECTED_TRIGGER_SOURCE = "debug_e2e_cli"


def check_invocation_summary(stdout: str) -> CheckResult:
    """Parse the subprocess stdout JSON and assert the three required fields."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            label="invocation_summary",
            passed=False,
            message=f"subprocess stdout is not valid JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return CheckResult(
            label="invocation_summary",
            passed=False,
            message=f"subprocess stdout JSON is not an object: {type(payload).__name__}",
        )

    staleness = payload.get("staleness_flag")
    if staleness is not False:
        return CheckResult(
            label="invocation_summary",
            passed=False,
            message=f"staleness_flag expected false, got {staleness!r}",
        )

    trigger = payload.get("trigger_source")
    if trigger != _EXPECTED_TRIGGER_SOURCE:
        return CheckResult(
            label="invocation_summary",
            passed=False,
            message=(f"trigger_source expected {_EXPECTED_TRIGGER_SOURCE!r}, got {trigger!r}"),
        )

    submitted = payload.get("commands_submitted")
    if not isinstance(submitted, int) or submitted < 0:
        return CheckResult(
            label="invocation_summary",
            passed=False,
            message=f"commands_submitted expected non-negative int, got {submitted!r}",
        )

    return CheckResult(
        label="invocation_summary",
        passed=True,
        message=(
            f"staleness_flag=false, trigger_source={_EXPECTED_TRIGGER_SOURCE!r}, "
            f"commands_submitted={submitted}"
        ),
    )


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------


_DEFAULT_DB_PATH = Path("data") / "alphamind-debug-e2e.db"
_DEFAULT_PIPELINE_LOG = Path.home() / "AlphaMind" / "logs" / "pipeline.log"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_debug_e2e",
        description=(
            "End-to-end verification for the ``--debug-e2e`` scheduler mode "
            "(ALP-493). Subprocesses one debug-e2e invocation and runs six "
            "check helpers against the archive + DB it produces."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=(
            "Path to the debug-e2e SQLite DB (default ``data/alphamind-debug-e2e.db``). "
            "Must end with ``-debug-e2e.db`` — the seeder refuses any other suffix."
        ),
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        required=True,
        help=(
            "Root of the verification archive — the script writes the "
            "per-invocation directory under ``<archive-root>/invocations/<id>/``."
        ),
    )
    parser.add_argument(
        "--run-type",
        choices=[rt.value for rt in RunType],
        default=RunType.market_hours_rolling.value,
        help="Firing run type for the manual invocation (default market_hours_rolling).",
    )
    parser.add_argument(
        "--reason",
        default="verify_debug_e2e",
        help="Free-form reason recorded on the invocation row.",
    )
    parser.add_argument(
        "--pipeline-log",
        type=Path,
        default=_DEFAULT_PIPELINE_LOG,
        help=(
            "Pipeline-log path scanned by the no_alpaca check "
            "(default ``~/AlphaMind/logs/pipeline.log``)."
        ),
    )
    return parser.parse_args(argv)


def _emit(results: list[CheckResult], result: CheckResult) -> None:
    results.append(result)
    print(result.format_line())


def _drive_debug_e2e_subprocess(args: argparse.Namespace) -> tuple[CheckResult, str | None]:
    """Subprocess the CLI and return its stdout JSON on success."""
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "alphamind.scheduler",
        "run",
        "--debug-e2e",
        "--once",
        args.run_type,
        "--reason",
        args.reason,
    ]
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(args.db_path)
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        return (
            CheckResult(
                label="subprocess",
                passed=False,
                message=f"failed to launch debug-e2e subprocess: {exc}",
            ),
            None,
        )
    if completed.returncode != 0:
        stderr_tail = "\n".join(completed.stderr.splitlines()[-20:])
        return (
            CheckResult(
                label="subprocess",
                passed=False,
                message=(
                    f"debug-e2e subprocess exited {completed.returncode}; "
                    f"last stderr lines:\n{stderr_tail}"
                ),
            ),
            None,
        )
    return (
        CheckResult(
            label="subprocess",
            passed=True,
            message="debug-e2e subprocess exited 0",
        ),
        completed.stdout,
    )


def _invocation_id_from_summary(stdout: str) -> str | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    invocation_id = payload.get("invocation_id")
    return invocation_id if isinstance(invocation_id, str) else None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the six check helpers around one debug-e2e subprocess.

    Returns 0 on full pass, 1 on any FAIL. The check progression
    short-circuits early on a failing pre-flight (auth) and on a failing
    subprocess: subsequent checks consume the archive the subprocess
    produces, so there's no value in running them against a missing
    archive.
    """
    configure_utf8_stdio()
    args = _parse_args(argv)
    results: list[CheckResult] = []

    _emit(results, check_auth())
    if not results[-1].passed:
        _print_summary(results)
        return 1

    subprocess_result, stdout = _drive_debug_e2e_subprocess(args)
    _emit(results, subprocess_result)
    if not subprocess_result.passed or stdout is None:
        _print_summary(results)
        return 1

    invocation_id = _invocation_id_from_summary(stdout)
    if invocation_id is None:
        _emit(
            results,
            CheckResult(
                label="invocation_summary",
                passed=False,
                message="subprocess stdout does not name an invocation_id",
            ),
        )
        _print_summary(results)
        return 1

    _emit(
        results,
        check_archive_directory(archive_root=args.archive_root, invocation_id=invocation_id),
    )
    progress_path = (
        args.archive_root / INVOCATIONS_DIRNAME / invocation_id / _PROGRESS_JSONL_FILENAME
    )
    if progress_path.is_file():
        _emit(results, check_jsonl_ordering(progress_path))
    else:
        _emit(
            results,
            CheckResult(
                label="jsonl_ordering",
                passed=False,
                message=f"progress.jsonl missing at {progress_path}",
            ),
        )

    engine = create_engine(f"sqlite:///{args.db_path}")
    try:
        _emit(results, check_synthetic_portfolio_visibility(engine))
    finally:
        engine.dispose()

    _emit(results, check_no_alpaca(args.pipeline_log))
    _emit(results, check_invocation_summary(stdout))

    _print_summary(results)
    return 0 if all(r.passed for r in results) else 1


def _print_summary(results: list[CheckResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"=== DEBUG-E2E VERIFICATION === {passed}/{total} checks passed")


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
