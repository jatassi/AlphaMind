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
* :func:`check_jsonl_ordering` — 12 in-invocation
  ``phase_start``/``phase_done`` pairs plus 9
  ``agent_request``/``agent_response`` pairs in dependency order;
  ``domain_researchers``/``qualitative`` and
  ``analyst``/``strategist`` parallel-overlap pairs tolerated. The
  pre-invocation ``seed`` event lands under
  ``<archive>/invocations/_pre_invocation/progress.jsonl`` and is
  intentionally NOT inspected here.
* :func:`check_synthetic_portfolio_visibility` — positions + theses
  counts and cash-ledger balance match the seeded fixture (8 / 8 /
  $24,440 by default; 0 / 0 / $100,000 with ``--fresh-start``).
* :func:`check_no_alpaca` — captured subprocess stderr carries no
  ``alpaca-py`` indicators.
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

See ``scripts/RUNBOOK_end_to_end_verification.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from alphamind._kernel.archive_layout import (
    CALIBRATION_SNAPSHOT_FILENAME,
    RESOLVED_CONFIG_FILENAME,
    find_invocation_archive_dir,
)
from alphamind.config.models.run_types import RunType
from alphamind.scripts._stdio import configure_utf8_stdio

__all__ = [
    "FRESH_START_EXPECTATIONS",
    "SYNTHETIC_EXPECTATIONS",
    "VERIFY_SUMMARY_FILENAME",
    "CheckResult",
    "PortfolioExpectations",
    "check_archive_directory",
    "check_auth",
    "check_deterministic_prefix",
    "check_invocation_summary",
    "check_jsonl_ordering",
    "check_no_alpaca",
    "check_synthetic_portfolio_visibility",
    "format_data_health_block",
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
VERIFY_SUMMARY_FILENAME = "verify_summary.txt"
"""Operator-facing summary persisted alongside the run's archive.

Mirrors the wrapper stdout (PASS/FAIL lines + ``=== DEBUG-E2E VERIFICATION ===``
summary + ``=== DATA HEALTH ===`` block). Written at the end of
:func:`main` once the invocation directory is resolved, so the verdict
lives with the rest of the run's artifacts instead of relying on the
operator to ``tee`` wrapper output to a side file.
"""


def check_archive_directory(*, archive_root: Path, invocation_id: str) -> CheckResult:
    """Assert the per-invocation directory carries the two required files."""
    inv_dir = find_invocation_archive_dir(archive_root=archive_root, invocation_id=invocation_id)
    if inv_dir is None or not inv_dir.is_dir():
        return CheckResult(
            label="archive_directory",
            passed=False,
            message=f"archive directory missing for {invocation_id!r} under {archive_root}",
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


# 12 in-invocation phases in dependency order. The pre-invocation ``seed``
# phase lands in the sentinel ``_pre_invocation`` archive directory before
# ``run_invocation`` opens the canonical invocation_id, so it is NOT part of
# the per-invocation ``progress.jsonl`` this check inspects (parent issue
# ALP-493 § D — "12 in-invocation phases + 1 pre-invocation seed event").
# ``domain_researchers``/``qualitative`` open in parallel under the analysis
# TaskGroup; ``analyst``/``strategist`` open in parallel under the decision
# TaskGroup. The dependency-graph below pins every phase's required
# predecessors using only the strict edges; sibling phases (peers under a
# TaskGroup) are intentionally absent from each other's edge sets so the
# parallel-overlap event interleaving validates.
_PHASE_PREDECESSORS: dict[str, frozenset[str]] = {
    "phase1": frozenset(),
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
# sectors (tech_semis, financials, energy) under the single
# ``domain_researchers`` phase, plus 6 single-call pairs (qualitative,
# adaptive, synthesizer, analyst, strategist, pm). Distillation is the
# deterministic 7-phase numerical orchestrator and emits no SDK call.
# Each entry is a ``(phase, agent)`` tuple so we can match the
# request/response events by their joint identity.
_SDK_CALL_PAIRS: tuple[tuple[str, str], ...] = (
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
    2. **12 in-invocation phase pairs.** Each of the 12 in-invocation
       phases produces exactly one ``phase_start`` and one ``phase_done``,
       and ``phase_done`` follows its own ``phase_start``. The
       pre-invocation ``seed`` event lands in
       ``<archive>/invocations/_pre_invocation/progress.jsonl`` and is NOT
       part of the stream this check inspects.
    3. **Phase-level dependency graph.** ``phase_start("X")`` happens after
       every predecessor in ``_PHASE_PREDECESSORS[X]`` has emitted its
       ``phase_done``. The parallel-overlap pairs
       (``domain_researchers``/``qualitative`` and
       ``analyst``/``strategist``) are NOT each other's predecessors, so
       their event interleaving validates either way.
    4. **SDK call pairs.** Each of the 9 ``(phase, agent)`` tuples in
       ``_SDK_CALL_PAIRS`` emits at least one ``agent_request`` closed by
       a matching ``agent_response``; a corrective retry adds a second
       fully closed pair for that agent, which is tolerated.
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

    expected_phases = len(_PHASE_PREDECESSORS)
    return CheckResult(
        label="jsonl_ordering",
        passed=True,
        message=(
            f"{expected_phases}/{expected_phases} phases with paired start/done "
            f"in dependency order; "
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
    """Validate the 12 in-invocation phase_start/phase_done pairs + dependency graph."""
    starts, dones, indexing_error = _index_phase_events(events)
    if indexing_error is not None:
        return indexing_error
    completeness_error = _check_phase_completeness(starts, dones)
    if completeness_error is not None:
        return completeness_error
    return _check_phase_dependency_graph(starts, dones)


# Fields every ``agent_response`` must carry per parent issue ALP-493 § (B).
# ``stop_reason`` may legitimately be ``None`` (the Anthropic SDK does not
# always populate it); the other four must be present AND non-null because
# the report builder + cost-tracking downstream rely on them.
_AGENT_RESPONSE_REQUIRED_FIELDS: tuple[str, ...] = (
    "duration_s",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "stop_reason",
)
_AGENT_RESPONSE_NON_NULL_FIELDS: frozenset[str] = frozenset(
    {"duration_s", "input_tokens", "output_tokens", "tool_calls"}
)


def _check_agent_response_fields(
    *, event_index: int, ev: dict[str, object], key: tuple[str, str]
) -> str | None:
    """Assert one ``agent_response`` carries the 5-field set per § (B).

    Returns a non-``None`` error string when a required field is absent
    or — for the four load-bearing fields — null.
    """
    for field in _AGENT_RESPONSE_REQUIRED_FIELDS:
        if field not in ev:
            return (
                f"agent_response for {key!r} at event #{event_index} "
                f"missing required field {field!r}"
            )
        if field in _AGENT_RESPONSE_NON_NULL_FIELDS and ev[field] is None:
            return (
                f"agent_response for {key!r} at event #{event_index} "
                f"has null {field!r} (only stop_reason may be null)"
            )
    return None


def _report_pair_gaps(
    open_requests: set[tuple[str, str]], matched_pairs: set[tuple[str, str]]
) -> str | None:
    """Final SDK-pair tally: orphan requests, missing pairs, unexpected pairs."""
    if open_requests:
        sorted_open = ", ".join(sorted(f"{p}:{a}" for p, a in open_requests))
        return f"agent_request(s) with no matching agent_response: {sorted_open}"
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


def _check_sdk_call_pairs(events: list[dict[str, object]]) -> str | None:
    """Validate the SDK call request/response pairs.

    Each of the 9 ``_SDK_CALL_PAIRS`` ``(phase, agent)`` tuples must emit
    at least one ``agent_request`` closed by a matching ``agent_response``.
    A *corrective retry* legitimately emits a second request/response
    pair for the same agent — the harness's designed recovery from a
    malformed first response — so a repeated, fully closed pair is
    tolerated. What is NOT tolerated: a second ``agent_request`` while the
    prior one for that key is still open (overlapping calls), or an
    ``agent_response`` with no open request.
    """
    open_requests: set[tuple[str, str]] = set()
    matched_pairs: set[tuple[str, str]] = set()
    for index, ev in enumerate(events):
        kind = ev.get("event")
        phase_raw = ev.get("phase")
        agent_raw = ev.get("agent")
        if not isinstance(phase_raw, str) or not isinstance(agent_raw, str):
            continue
        key = (phase_raw, agent_raw)
        if kind == "agent_request":
            if key in open_requests:
                return (
                    f"agent_request for {key!r} at event #{index} opens while "
                    "its prior request is still unclosed"
                )
            open_requests.add(key)
        elif kind == "agent_response":
            if key not in open_requests:
                return (
                    f"agent_response for {key!r} at event #{index} has no preceding agent_request"
                )
            field_error = _check_agent_response_fields(event_index=index, ev=ev, key=key)
            if field_error is not None:
                return field_error
            open_requests.discard(key)
            matched_pairs.add(key)

    return _report_pair_gaps(open_requests, matched_pairs)


# ---------------------------------------------------------------------------
# Synthetic portfolio visibility check
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioExpectations:
    """Expected positions / theses / cash-ledger shape for one fixture.

    Two literals satisfy this contract: the managed
    :data:`SYNTHETIC_PORTFOLIO` expectations (8 / 8 / $24,440) and the
    clean-slate :data:`FRESH_START_PORTFOLIO` expectations (0 / 0 /
    $100,000). The verify harness selects between them via the
    ``--fresh-start`` flag and passes the chosen expectations to
    :func:`check_synthetic_portfolio_visibility` (ALP-618).

    ``cash_usd`` is typed ``float`` on purpose — :class:`SyntheticPortfolio`
    keeps ``starting_cash_usd`` as ``Decimal`` for money-discipline at the
    seeder, but the verify check reads ``cash_ledger.current_cash_usd`` via
    SQLite (``float(cash_rows[0][0])``) and compares against this field;
    keeping both ends of the comparison ``float`` avoids a spurious
    ``Decimal``/``float`` cross-type compare and matches the SQLite round-trip.
    The two literal expectations are exactly representable in IEEE-754.
    """

    positions: int
    theses: int
    cash_usd: float


SYNTHETIC_EXPECTATIONS = PortfolioExpectations(positions=8, theses=8, cash_usd=24_440.0)
FRESH_START_EXPECTATIONS = PortfolioExpectations(positions=0, theses=0, cash_usd=100_000.0)


def check_synthetic_portfolio_visibility(
    engine: Engine,
    *,
    expected: PortfolioExpectations = SYNTHETIC_EXPECTATIONS,
) -> CheckResult:
    """Assert the seeded debug DB carries the expected fixture shape."""
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

    if positions_count != expected.positions:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"positions count expected {expected.positions}, got {positions_count}",
        )
    if theses_count != expected.theses:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"theses count expected {expected.theses}, got {theses_count}",
        )
    if len(cash_rows) != 1:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=f"cash_ledger row count expected 1, got {len(cash_rows)}",
        )
    cash_value = float(cash_rows[0][0])
    if cash_value != expected.cash_usd:
        return CheckResult(
            label="synthetic_portfolio",
            passed=False,
            message=(
                f"cash_ledger.current_cash_usd expected {expected.cash_usd}, got {cash_value}"
            ),
        )
    return CheckResult(
        label="synthetic_portfolio",
        passed=True,
        message=(
            f"positions={positions_count}, theses={theses_count}, "
            f"cash_ledger.current_cash_usd={cash_value}"
        ),
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


def check_no_alpaca(stream: str) -> CheckResult:
    """Scan a captured text stream for Alpaca HTTP indicators; FAIL on any hit.

    The verify script passes the subprocess's captured stderr blob
    here, so the check is stream-based (no filesystem dependency) and
    runs every time — the prior log-file approach was lenient because
    the pipeline-log location is platform-dependent and routinely
    absent in CI, which short-circuited the check to PASS even when a
    regression might have leaked Alpaca traffic.
    """
    hits: list[str] = []
    for pattern in _ALPACA_INDICATORS:
        match = pattern.search(stream)
        if match is not None:
            hits.append(match.group(0))
    if hits:
        return CheckResult(
            label="no_alpaca",
            passed=False,
            message=(
                f"captured stream carries alpaca indicator(s): {', '.join(sorted(set(hits)))}"
            ),
        )
    return CheckResult(
        label="no_alpaca",
        passed=True,
        message="no alpaca indicators in captured stream",
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
# Deterministic-prefix check (fires only on resume runs)
# ---------------------------------------------------------------------------


#: Header-line prefixes (followed by ``: ``) that vary per invocation by
#: design and must be stripped before hashing. Distillation outputs
#: embed the per-run ``invocation_id`` in a header line
#: (``sector_assembly.py:229``, ``correlation_brief.py:683``); the
#: per-run mint makes a naive byte-comparison guaranteed to fail across
#: source vs new archives. Normalising here preserves the
#: "deterministic prefix" invariant the design doc § 5 actually wants
#: to assert (the computation produced the same content) without
#: mistaking the wrapping metadata for a divergence.
_DISTILLATION_VARIABLE_HEADER_PREFIXES: tuple[bytes, ...] = (b"Invocation: ",)


def _sha256_of_file(path: Path) -> str:
    """Stream a file through ``hashlib.sha256`` and return the hex digest.

    Reads the file in binary mode (``Path.open("rb")``) to avoid the
    text-mode line-ending normalization differences that would
    otherwise make this check unstable across platforms — distillation
    writes ``\\n`` line endings deterministically, but reading via
    text mode on Windows can fold CRLF into LF and produce a false
    match. Binary mode pins the comparison to actual on-disk bytes.

    Lines whose prefix matches
    :data:`_DISTILLATION_VARIABLE_HEADER_PREFIXES` are stripped before
    hashing — these are per-invocation metadata (notably
    ``Invocation: <id>``) that the design-doc invariant explicitly
    permits to vary while the rest of the file stays byte-identical.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for raw_line in f:
            stripped = raw_line.lstrip()
            if any(
                stripped.startswith(prefix) for prefix in _DISTILLATION_VARIABLE_HEADER_PREFIXES
            ):
                continue
            hasher.update(raw_line)
    return hasher.hexdigest()


def _relative_files(root: Path) -> dict[str, Path]:
    """Return ``{relative_posix_path: absolute_path}`` for every regular
    file under *root*.

    POSIX-style relative keys keep the cross-archive comparison stable
    across Windows / Mac path separators. Skips symlinks and non-files
    so a stray ``.DS_Store`` -> directory link doesn't poison the hash.
    """
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        out[rel] = path
    return out


def check_deterministic_prefix(
    *,
    source_distillation_dir: Path,
    new_distillation_dir: Path,
) -> CheckResult:
    """Hash distillation outputs pairwise; FAIL on the first byte mismatch.

    Asserts the design-doc § 5 deterministic-prefix invariant: phase1 +
    snapshot_assembly + distillation must produce byte-identical
    outputs on a resume run vs the source run. Distillation is the
    load-bearing comparison surface — it is the deterministic phase
    whose output drives every replayed SDK phase. A mismatch surfaces
    one of two failure modes (operator runbook):

    * the synthetic portfolio fixture changed between runs, or
    * a non-determinism regression slipped into distillation.

    The check fires ONLY on resume runs — the wrapper omits it on a
    fresh debug-e2e invocation, keeping the summary line at ``7/7
    checks passed``.

    Returns a :class:`CheckResult` with ``label='deterministic_prefix'``;
    the caller formats and prints it. PASS message names the file count
    + cumulative hashed bytes; FAIL message names the first divergent
    file and a one-line summary of how the bytes differ (length /
    first-mismatched-byte offset). Missing source / target directories
    are FAIL — the resume invocation is expected to produce
    distillation outputs in the same shape as the source.
    """
    source_files = _relative_files(source_distillation_dir)
    new_files = _relative_files(new_distillation_dir)

    if not source_files:
        return CheckResult(
            label="deterministic_prefix",
            passed=False,
            message=(f"source distillation directory missing or empty: {source_distillation_dir}"),
        )
    if not new_files:
        return CheckResult(
            label="deterministic_prefix",
            passed=False,
            message=(f"new distillation directory missing or empty: {new_distillation_dir}"),
        )

    # Walk in deterministic order so the "first mismatched file" the
    # FAIL message names is reproducible across runs (``_relative_files``
    # already sorts via ``rglob``, but be explicit on the join).
    total_bytes = 0
    for rel in sorted(source_files):
        source_path = source_files[rel]
        new_path = new_files.get(rel)
        if new_path is None:
            return CheckResult(
                label="deterministic_prefix",
                passed=False,
                message=(
                    f"distillation file {rel!r} present in source but missing "
                    f"in new archive (source={source_path}, "
                    f"new_dir={new_distillation_dir})"
                ),
            )
        source_hash = _sha256_of_file(source_path)
        new_hash = _sha256_of_file(new_path)
        if source_hash != new_hash:
            source_len = source_path.stat().st_size
            new_len = new_path.stat().st_size
            return CheckResult(
                label="deterministic_prefix",
                passed=False,
                message=(
                    f"distillation file {rel!r} differs: "
                    f"source={source_hash[:16]}... ({source_len}B), "
                    f"new={new_hash[:16]}... ({new_len}B). "
                    f"Likely cause: non-determinism regression in "
                    f"phase1 / snapshot_assembly / distillation."
                ),
            )
        total_bytes += source_path.stat().st_size

    # Surface extra files in the new archive only after the source files
    # all match — a "new file appeared" surface is a soft mismatch
    # signal worth surfacing but not load-bearing. We FAIL on it because
    # the invariant is byte-identical outputs, and an extra file is a
    # divergence.
    extra = sorted(set(new_files) - set(source_files))
    if extra:
        return CheckResult(
            label="deterministic_prefix",
            passed=False,
            message=(
                f"distillation file(s) {extra} present in new archive but "
                f"missing in source (new_dir={new_distillation_dir})"
            ),
        )

    return CheckResult(
        label="deterministic_prefix",
        passed=True,
        message=(
            f"{len(source_files)} distillation file(s) byte-identical "
            f"({total_bytes}B hashed) vs source archive"
        ),
    )


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------


_DEFAULT_DB_PATH = Path("data") / "alphamind-debug-e2e.db"


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
        "--fresh-start",
        action="store_true",
        help=(
            "Verify the clean-slate FRESH_START_PORTFOLIO fixture "
            "(0 positions / 0 theses / $100,000 cash) instead of the "
            "managed SYNTHETIC_PORTFOLIO fixture (8 / 8 / $24,440). "
            "Forwards to the scheduler subprocess and parameterizes the "
            "synthetic-portfolio visibility check (ALP-618)."
        ),
    )
    parser.add_argument(
        "--resume-from",
        metavar="INVOCATION_ID:PHASE",
        default=None,
        help=(
            "Forward ``--resume-from <invocation-id>:<phase>`` to the "
            "underlying scheduler subprocess so it hydrates SDK-phase "
            "outputs from the named source invocation and re-runs from "
            "``<phase>`` onward (ALP-693 / ALP-696). The wrapper does no "
            "validation — the scheduler CLI is the source of truth on "
            "validity and exits 2 with a named-cause stderr message on "
            "rejection. When set, the wrapper also runs the post-resume "
            "``check_deterministic_prefix`` check that hashes the "
            "distillation outputs against the source archive."
        ),
    )
    return parser.parse_args(argv)


def _emit(results: list[CheckResult], result: CheckResult) -> None:
    results.append(result)
    print(result.format_line())


class _TeeStream(io.TextIOBase):
    """Forward writes to a real stdout while accumulating them in a buffer.

    Used by :func:`main` under :func:`contextlib.redirect_stdout` so every
    operator-facing line (PASS/FAIL, summary, DATA HEALTH) is both shown
    on the terminal in real time and captured for persistence to
    ``<inv_dir>/verify_summary.txt``. Operators no longer need a side
    ``tee`` invocation to keep the verdict after the wrapper exits.
    """

    def __init__(self, downstream: IO[str]) -> None:
        super().__init__()
        self._downstream = downstream
        self._buffer: list[str] = []

    def write(self, data: str) -> int:
        self._downstream.write(data)
        self._buffer.append(data)
        return len(data)

    def flush(self) -> None:
        self._downstream.flush()

    def getvalue(self) -> str:
        return "".join(self._buffer)


@dataclass(frozen=True, slots=True)
class _SubprocessOutput:
    """Captured stdout + stderr of the debug-e2e subprocess."""

    stdout: str
    stderr: str


def _drive_debug_e2e_subprocess(
    args: argparse.Namespace,
) -> tuple[CheckResult, _SubprocessOutput | None]:
    """Subprocess the CLI and return its captured stdout + stderr on success.

    Passes ``--archive-root`` through to the CLI so the per-invocation
    directory lands under the operator-chosen verification archive
    rather than the production-default ``~/AlphaMind/archive``. The
    captured stderr feeds :func:`check_no_alpaca` so the no-Alpaca
    invariant is enforced every run regardless of pipeline-log presence.
    """
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "alphamind.scheduler",
        "run",
        "--debug-e2e",
        "--archive-root",
        str(args.archive_root),
        "--once",
        args.run_type,
        "--reason",
        args.reason,
    ]
    if args.fresh_start:
        cmd.append("--fresh-start")
    if args.resume_from is not None:
        # Forward verbatim — the scheduler CLI is the source of truth on
        # ``--resume-from`` validity (story ALP-693). The wrapper does no
        # parsing or pre-validation; a malformed value surfaces as the
        # scheduler's exit-2 with a named-cause stderr message.
        cmd.extend(["--resume-from", args.resume_from])
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
        _SubprocessOutput(stdout=completed.stdout, stderr=completed.stderr),
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


def _resolve_distillation_dir(*, archive_root: Path, invocation_id: str) -> Path | None:
    """Locate the per-invocation distillation directory under *archive_root*.

    The distillation orchestrator writes outputs to
    ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/distillation/`` (date
    partitioning per ``docs/architecture/infrastructure.md`` § Layer 2).
    Rather than parse the invocation_id timestamp prefix (fragile to
    ID-format changes), glob ``<archive_root>/*/<invocation_id>/distillation``
    so any date partition that carries this invocation matches.

    Returns the resolved path when exactly one match is found; ``None``
    when none or multiple matches exist. The caller decides whether
    absent / ambiguous distillation is a FAIL or a no-op — for the
    resume-only deterministic-prefix check, both surface as FAIL via
    the check helper's empty-directory guard.
    """
    matches = sorted(archive_root.glob(f"*/{invocation_id}/distillation"))
    matches = [m for m in matches if m.is_dir()]
    if len(matches) != 1:
        return None
    return matches[0]


def _source_invocation_id(resume_from: str) -> str:
    """Extract the source invocation_id from a ``<id>:<phase>`` value.

    Mirrors the scheduler CLI's ``rpartition`` split (invocation IDs
    embed ISO-8601 timestamps that already contain colons, so the LAST
    colon separates ID from phase).
    """
    invocation_id, _, _phase = resume_from.rpartition(":")
    return invocation_id


def main(argv: Sequence[str] | None = None) -> int:
    """Run the six check helpers around one debug-e2e subprocess.

    Returns 0 on full pass, 1 on any FAIL. The check progression
    short-circuits early on a failing pre-flight (auth) and on a failing
    subprocess: subsequent checks consume the archive the subprocess
    produces, so there's no value in running them against a missing
    archive.

    Wrapper stdout (PASS/FAIL lines, summary, DATA HEALTH block) is
    teed into a buffer and persisted to
    ``<inv_dir>/verify_summary.txt`` once the invocation directory is
    resolved, so the operator-facing verdict lives with the rest of the
    run's artifacts. Failures before the inv_dir exists (auth FAIL,
    subprocess FAIL, missing-archive FAIL) emit to stdout only — there
    is no inv_dir to write into.
    """
    configure_utf8_stdio()
    args = _parse_args(argv)
    tee = _TeeStream(sys.stdout)
    with contextlib.redirect_stdout(tee):
        exit_code, invocation_id = _run_checks(args)
    _persist_verify_summary(
        archive_root=args.archive_root,
        invocation_id=invocation_id,
        content=tee.getvalue(),
    )
    return exit_code


def _persist_verify_summary(*, archive_root: Path, invocation_id: str | None, content: str) -> None:
    """Write the tee'd wrapper stdout into the run's archive dir, best-effort.

    Silently skips when no ``invocation_id`` was extracted (early pre-flight
    failure) or when the inv_dir hasn't been materialized yet (subprocess
    FAIL before ``insert_invocation_record``). An ``OSError`` during the
    write is swallowed too — losing the side artifact must not turn an
    otherwise-green run into a non-zero exit.
    """
    if invocation_id is None:
        return
    inv_dir = find_invocation_archive_dir(archive_root=archive_root, invocation_id=invocation_id)
    if inv_dir is None or not inv_dir.is_dir():
        return
    with contextlib.suppress(OSError):
        (inv_dir / VERIFY_SUMMARY_FILENAME).write_text(content, encoding="utf-8")


def _run_checks(args: argparse.Namespace) -> tuple[int, str | None]:
    """Body of :func:`main` — extracted so :func:`main` can wrap stdout cleanly.

    Returns ``(exit_code, invocation_id_or_None)``. The invocation_id is
    threaded back so :func:`main` can locate the run's archive dir for
    the summary-file write even on the short-circuit FAIL paths.
    """
    results: list[CheckResult] = []

    _emit(results, check_auth())
    if not results[-1].passed:
        _print_summary(results)
        return 1, None

    subprocess_result, output = _drive_debug_e2e_subprocess(args)
    _emit(results, subprocess_result)
    if not subprocess_result.passed or output is None:
        _print_summary(results)
        return 1, None

    invocation_id = _invocation_id_from_summary(output.stdout)
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
        return 1, None

    _emit(
        results,
        check_archive_directory(archive_root=args.archive_root, invocation_id=invocation_id),
    )
    _inv_dir_for_progress = find_invocation_archive_dir(
        archive_root=args.archive_root, invocation_id=invocation_id
    )
    progress_path = (
        _inv_dir_for_progress / _PROGRESS_JSONL_FILENAME
        if _inv_dir_for_progress is not None
        else None
    )
    if progress_path is not None and progress_path.is_file():
        _emit(results, check_jsonl_ordering(progress_path))
    else:
        _emit(
            results,
            CheckResult(
                label="jsonl_ordering",
                passed=False,
                message=f"progress.jsonl missing (invocation dir not found for {invocation_id!r})",
            ),
        )

    expected = FRESH_START_EXPECTATIONS if args.fresh_start else SYNTHETIC_EXPECTATIONS
    engine = create_engine(f"sqlite:///{args.db_path}")
    try:
        _emit(results, check_synthetic_portfolio_visibility(engine, expected=expected))
    finally:
        engine.dispose()

    # ``check_no_alpaca`` runs against the captured stderr stream so the
    # check fires every time regardless of pipeline-log presence (the
    # prior log-file approach was lenient on missing logs).
    _emit(results, check_no_alpaca(output.stderr))
    _emit(results, check_invocation_summary(output.stdout))

    # ALP-696: the deterministic-prefix check fires ONLY on resume runs.
    # On a fresh debug-e2e invocation the line is absent and the summary
    # stays at ``7/7 checks passed``; on a resume run we hash the source
    # vs new distillation outputs pairwise and emit one extra PASS / FAIL
    # line. Source / new dirs are looked up via glob over the archive
    # root (distillation outputs land under the
    # ``<archive_root>/<DATE>/<invocation_id>/distillation/`` date-partitioned
    # path, not the ``invocations/<id>/`` per-invocation path).
    if args.resume_from is not None:
        source_invocation_id = _source_invocation_id(args.resume_from)
        source_distillation = _resolve_distillation_dir(
            archive_root=args.archive_root, invocation_id=source_invocation_id
        )
        new_distillation = _resolve_distillation_dir(
            archive_root=args.archive_root, invocation_id=invocation_id
        )
        if source_distillation is None:
            _emit(
                results,
                CheckResult(
                    label="deterministic_prefix",
                    passed=False,
                    message=(
                        f"source distillation directory not found under "
                        f"{args.archive_root} for invocation_id="
                        f"{source_invocation_id!r}"
                    ),
                ),
            )
        elif new_distillation is None:
            _emit(
                results,
                CheckResult(
                    label="deterministic_prefix",
                    passed=False,
                    message=(
                        f"new distillation directory not found under "
                        f"{args.archive_root} for invocation_id="
                        f"{invocation_id!r}"
                    ),
                ),
            )
        else:
            _emit(
                results,
                check_deterministic_prefix(
                    source_distillation_dir=source_distillation,
                    new_distillation_dir=new_distillation,
                ),
            )

    _print_summary(results)
    # ALP-540: elevate per-series data health to the operator so collector
    # failures (``unavailable``) don't masquerade as warm-up state
    # (``accumulating``) inside a green run.
    _print_data_health(archive_root=args.archive_root, invocation_id=invocation_id)
    return (0 if all(r.passed for r in results) else 1, invocation_id)


def _print_summary(results: list[CheckResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"=== DEBUG-E2E VERIFICATION === {passed}/{total} checks passed")


# ---------------------------------------------------------------------------
# DATA HEALTH block
# ---------------------------------------------------------------------------


_OPERATOR_SNAPSHOT_SCHEMA_VERSION = "1"
"""Schema version of the operator-facing data-health snapshot we render.

Must match
:data:`alphamind.distillation.calibration_snapshot.OPERATOR_SUMMARY_SCHEMA_VERSION`.
The renderer refuses to interpret any other version so an internal V2
snapshot accidentally landing at this path doesn't render as silent zeros.
"""


def format_data_health_block(snapshot: dict[str, Any]) -> str:
    """Render the operator-facing DATA HEALTH block from a calibration snapshot.

    Reads the structured ``data_calibration_state.json`` payload written by
    :func:`alphamind.distillation.calibration_snapshot.write_operator_data_health_summary`
    and renders a scannable block keyed on the three calibration states. The
    ``unavailable`` list elevates collector failures to the operator's
    attention; the ``accumulating`` list reports series still warming up.

    Tolerates two failure modes:

    - Missing / empty snapshot (distillation didn't run, JSON read failed,
      bootstrap-seed ``{}``) → a single ``"(no calibration snapshot)"`` line.
    - Wrong schema version (internal V2 snapshot landed at the operator
      path) → a single ``"(unrecognized snapshot schema_version=…)"`` line
      so the operator notices instead of seeing silent zeros.
    """
    lines: list[str] = ["=== DATA HEALTH ==="]
    summary = snapshot.get("summary")
    if not isinstance(summary, dict):
        lines.append("  (no calibration snapshot — distillation may not have run)")
        return "\n".join(lines)
    version = snapshot.get("schema_version")
    if version != _OPERATOR_SNAPSHOT_SCHEMA_VERSION:
        lines.append(f"  (unrecognized snapshot schema_version={version!r})")
        return "\n".join(lines)

    calibrated = int(summary.get("calibrated", 0))
    accumulating = int(summary.get("accumulating", 0))
    unavailable = int(summary.get("unavailable", 0))
    lines.append(
        f"  calibrated={calibrated}  accumulating={accumulating}  unavailable={unavailable}"
    )

    for state_label, header, entries in (
        ("UNAVAILABLE", "operator action required", snapshot.get("unavailable") or []),
        ("ACCUMULATING", "collector healthy, wait", snapshot.get("accumulating") or []),
    ):
        if not entries:
            continue
        lines.append("")
        lines.append(f"  {state_label} ({len(entries)}) — {header}:")
        for entry in entries:
            module = str(entry.get("module", "?"))
            reason = str(entry.get("reason", ""))
            lines.append(f"    - {module}: {reason}")

    return "\n".join(lines)


def _print_data_health(*, archive_root: Path, invocation_id: str) -> None:
    """Read the calibration snapshot and print the DATA HEALTH block.

    Robust to a missing or malformed snapshot — prints the block header
    with a "(no calibration snapshot)" line in either case so the operator
    sees the section in every run.
    """
    _inv_dir = find_invocation_archive_dir(archive_root=archive_root, invocation_id=invocation_id)
    if _inv_dir is None:
        print(format_data_health_block({}))
        return
    snapshot_path = _inv_dir / CALIBRATION_SNAPSHOT_FILENAME
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snapshot = {}
    print(format_data_health_block(snapshot))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
