"""Tests for ``scripts/verify_debug_e2e.py`` (story 05 / ALP-502).

The script is the operator-runnable end-to-end gate for the
``--debug-e2e`` mode; its six check helpers (``check_auth``,
``check_archive_directory``, ``check_jsonl_ordering``,
``check_synthetic_portfolio_visibility``, ``check_no_alpaca``,
``check_invocation_summary``) live as module-level functions so this
suite can exercise them without driving a real ``--debug-e2e``
subprocess.

The script is imported via :func:`importlib.util.spec_from_file_location`
because it lives under ``scripts/`` (not under ``src/``) and is not
installed as a package — the operator-runnable entry point is the file
itself, no shim split per parent issue ALP-493's "no shim" decision.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import alphamind.state.tables  # noqa: F401  # registers every state table on Base.metadata
from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind.persistence.models import Base

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_debug_e2e.py"
_INVOCATION_AS_OF = datetime(2026, 5, 26, tzinfo=UTC)


@pytest.fixture(scope="module")
def verify_module() -> ModuleType:
    """Load ``scripts/verify_debug_e2e.py`` as an importable module.

    The script lives under ``scripts/`` rather than ``src/`` per the
    "no shim split" decision in ALP-502; we use ``spec_from_file_location``
    to give the tests a normal module handle.
    """
    spec = importlib.util.spec_from_file_location("verify_debug_e2e", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_debug_e2e"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# check_auth
# ---------------------------------------------------------------------------


def test_check_auth_passes_when_oauth_token_present(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check_auth` must PASS when ``CLAUDE_CODE_OAUTH_TOKEN`` is set."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    result = verify_module.check_auth()
    assert result.passed is True
    assert result.label == "auth"


def test_check_auth_fails_when_oauth_token_missing(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check_auth` must FAIL with a named env-var when token absent."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    result = verify_module.check_auth()
    assert result.passed is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message


def test_check_auth_does_not_require_alpaca_creds(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Debug-e2e never touches Alpaca — credentials are NOT required."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)
    result = verify_module.check_auth()
    assert result.passed is True


# ---------------------------------------------------------------------------
# check_archive_directory
# ---------------------------------------------------------------------------


def test_check_archive_directory_passes_with_required_files(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """PASS when the date-partitioned invocation dir carries the two required files."""
    invocation_dir = invocation_archive_dir(
        archive_root=tmp_path,
        as_of=_INVOCATION_AS_OF,
        invocation_id="20260516T000000Z-debug-e2e",
    )
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "resolved_config.json").write_text("{}", encoding="utf-8")
    (invocation_dir / "progress.jsonl").write_text("", encoding="utf-8")

    result = verify_module.check_archive_directory(
        archive_root=tmp_path, invocation_id="20260516T000000Z-debug-e2e"
    )
    assert result.passed is True
    assert result.label == "archive_directory"


def test_check_archive_directory_fails_when_dir_missing(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when the per-invocation directory doesn't exist."""
    result = verify_module.check_archive_directory(
        archive_root=tmp_path, invocation_id="missing-invocation"
    )
    assert result.passed is False
    assert "missing-invocation" in result.message


def test_check_archive_directory_fails_when_resolved_config_missing(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when ``resolved_config.json`` is absent."""
    invocation_dir = invocation_archive_dir(
        archive_root=tmp_path, as_of=_INVOCATION_AS_OF, invocation_id="iid"
    )
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "progress.jsonl").write_text("", encoding="utf-8")

    result = verify_module.check_archive_directory(archive_root=tmp_path, invocation_id="iid")
    assert result.passed is False
    assert "resolved_config.json" in result.message


def test_check_archive_directory_fails_when_progress_jsonl_missing(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when ``progress.jsonl`` is absent."""
    invocation_dir = invocation_archive_dir(
        archive_root=tmp_path, as_of=_INVOCATION_AS_OF, invocation_id="iid"
    )
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "resolved_config.json").write_text("{}", encoding="utf-8")

    result = verify_module.check_archive_directory(archive_root=tmp_path, invocation_id="iid")
    assert result.passed is False
    assert "progress.jsonl" in result.message


# ---------------------------------------------------------------------------
# check_jsonl_ordering
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    """Append-only JSONL writer mirroring ``JsonlProgressEmitter`` shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, sort_keys=True) + "\n")


def _canonical_event_stream() -> list[dict[str, Any]]:
    """Construct one well-formed event stream covering the 12 in-invocation phases.

    Mirrors the production sequence written to the real-invocation
    ``<archive>/invocations/<invocation_id>/progress.jsonl``:
      phase1 → snapshot_assembly → distillation →
      (domain_researchers, qualitative) interleaved →
      adaptive → synthesizer →
      (analyst, strategist) interleaved →
      pre_processor → pm → phase2

    The ``seed`` event lands in the ``_pre_invocation`` archive
    directory (the canonical invocation_id isn't known until
    ``run_invocation`` returns); it is NOT part of the real-invocation
    stream the verify script inspects.

    Each ``agent_request`` is paired with its ``agent_response`` to give
    9 SDK call pairs (3 domain researchers, qualitative, adaptive,
    synthesizer, analyst, strategist, pm). Distillation is the
    deterministic numerical orchestrator and emits no SDK call.
    """
    t = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)

    def ts(offset_s: int) -> str:
        return (t + timedelta(seconds=offset_s)).isoformat()

    stream: list[dict[str, Any]] = []
    stream.append({"event": "phase_start", "phase": "phase1", "timestamp": ts(2)})
    stream.append({"event": "phase_done", "phase": "phase1", "timestamp": ts(3)})

    stream.append({"event": "phase_start", "phase": "snapshot_assembly", "timestamp": ts(4)})
    stream.append({"event": "phase_done", "phase": "snapshot_assembly", "timestamp": ts(5)})

    stream.append({"event": "phase_start", "phase": "distillation", "timestamp": ts(6)})
    stream.append({"event": "phase_done", "phase": "distillation", "timestamp": ts(9)})

    # domain_researchers + qualitative open in parallel; the four
    # ``agent_request`` events fire in rapid succession at the TaskGroup
    # boundary (interleaved sectors + qualitative), then responses fan in
    # as each agent's SDK call settles. Stream order = real ts order.
    stream.append({"event": "phase_start", "phase": "domain_researchers", "timestamp": ts(10)})
    stream.append({"event": "phase_start", "phase": "qualitative", "timestamp": ts(10)})
    for offset, sector in enumerate(("tech_semis", "financials", "energy")):
        stream.append(
            {
                "event": "agent_request",
                "phase": "domain_researchers",
                "agent": f"{sector}_researcher",
                "model": "sonnet",
                "timestamp": ts(11 + offset),
            }
        )
    stream.append(
        {
            "event": "agent_request",
            "phase": "qualitative",
            "agent": "qualitative_researcher",
            "model": "sonnet",
            "timestamp": ts(14),
        }
    )
    # Responses fan in over the next ~15s.
    for offset, sector in enumerate(("tech_semis", "financials", "energy")):
        stream.append(
            {
                "event": "agent_response",
                "phase": "domain_researchers",
                "agent": f"{sector}_researcher",
                "model": "sonnet",
                "duration_s": 12.0,
                "input_tokens": 8000,
                "cache_read_tokens": 60_000,
                "cache_write_tokens": 0,
                "output_tokens": 1500,
                "tool_calls": 0,
                "stop_reason": "end_turn",
                "timestamp": ts(20 + offset),
            }
        )
    stream.append(
        {
            "event": "agent_response",
            "phase": "qualitative",
            "agent": "qualitative_researcher",
            "model": "sonnet",
            "duration_s": 15.0,
            "input_tokens": 7000,
            "cache_read_tokens": 68_214,
            "cache_write_tokens": 0,
            "output_tokens": 800,
            "tool_calls": 5,
            "stop_reason": "end_turn",
            "timestamp": ts(25),
        }
    )
    # qualitative done before domain_researchers (parallel overlap).
    stream.append({"event": "phase_done", "phase": "qualitative", "timestamp": ts(26)})
    stream.append({"event": "phase_done", "phase": "domain_researchers", "timestamp": ts(28)})

    stream.append({"event": "phase_start", "phase": "adaptive", "timestamp": ts(29)})
    stream.append(
        {
            "event": "agent_request",
            "phase": "adaptive",
            "agent": "adaptive_researcher",
            "model": "sonnet",
            "timestamp": ts(30),
        }
    )
    stream.append(
        {
            "event": "agent_response",
            "phase": "adaptive",
            "agent": "adaptive_researcher",
            "model": "sonnet",
            "duration_s": 20.0,
            "input_tokens": 9000,
            "cache_read_tokens": 70_000,
            "cache_write_tokens": 0,
            "output_tokens": 1200,
            "tool_calls": 10,
            "stop_reason": "end_turn",
            "timestamp": ts(50),
        }
    )
    stream.append({"event": "phase_done", "phase": "adaptive", "timestamp": ts(51)})

    stream.append({"event": "phase_start", "phase": "synthesizer", "timestamp": ts(52)})
    stream.append(
        {
            "event": "agent_request",
            "phase": "synthesizer",
            "agent": "synthesizer",
            "model": "sonnet",
            "timestamp": ts(53),
        }
    )
    stream.append(
        {
            "event": "agent_response",
            "phase": "synthesizer",
            "agent": "synthesizer",
            "model": "sonnet",
            "duration_s": 12.0,
            "input_tokens": 11000,
            "cache_read_tokens": 55_000,
            "cache_write_tokens": 1_024,
            "output_tokens": 1800,
            "tool_calls": 0,
            "stop_reason": "end_turn",
            "timestamp": ts(65),
        }
    )
    stream.append({"event": "phase_done", "phase": "synthesizer", "timestamp": ts(66)})

    # analyst + strategist in parallel
    stream.append({"event": "phase_start", "phase": "analyst", "timestamp": ts(67)})
    stream.append({"event": "phase_start", "phase": "strategist", "timestamp": ts(67)})
    stream.append(
        {
            "event": "agent_request",
            "phase": "analyst",
            "agent": "analyst",
            "model": "opus",
            "timestamp": ts(68),
        }
    )
    stream.append(
        {
            "event": "agent_request",
            "phase": "strategist",
            "agent": "strategist",
            "model": "opus",
            "timestamp": ts(68),
        }
    )
    # Analyst settles first; strategist runs ~10x longer. Real-time order
    # is therefore: analyst response -> analyst phase_done ->
    # strategist response -> strategist phase_done.
    stream.append(
        {
            "event": "agent_response",
            "phase": "analyst",
            "agent": "analyst",
            "model": "opus",
            "duration_s": 60.0,
            "input_tokens": 15000,
            "cache_read_tokens": 80_000,
            "cache_write_tokens": 0,
            "output_tokens": 4000,
            "tool_calls": 2,
            "stop_reason": "end_turn",
            "timestamp": ts(128),
        }
    )
    stream.append({"event": "phase_done", "phase": "analyst", "timestamp": ts(129)})
    stream.append(
        {
            "event": "agent_response",
            "phase": "strategist",
            "agent": "strategist",
            "model": "opus",
            "duration_s": 600.0,
            "input_tokens": 30000,
            "cache_read_tokens": 90_000,
            "cache_write_tokens": 0,
            "output_tokens": 40000,
            "tool_calls": 0,
            "stop_reason": "end_turn",
            "timestamp": ts(668),
        }
    )
    stream.append({"event": "phase_done", "phase": "strategist", "timestamp": ts(669)})

    stream.append({"event": "phase_start", "phase": "pre_processor", "timestamp": ts(670)})
    stream.append({"event": "phase_done", "phase": "pre_processor", "timestamp": ts(671)})

    stream.append({"event": "phase_start", "phase": "pm", "timestamp": ts(672)})
    stream.append(
        {
            "event": "agent_request",
            "phase": "pm",
            "agent": "portfolio_manager",
            "model": "opus",
            "timestamp": ts(673),
        }
    )
    stream.append(
        {
            "event": "agent_response",
            "phase": "pm",
            "agent": "portfolio_manager",
            "model": "opus",
            "duration_s": 360.0,
            "input_tokens": 35000,
            "cache_read_tokens": 100_000,
            "cache_write_tokens": 0,
            "output_tokens": 30000,
            "tool_calls": 8,
            "stop_reason": "end_turn",
            "timestamp": ts(1033),
        }
    )
    stream.append({"event": "phase_done", "phase": "pm", "timestamp": ts(1034)})

    stream.append({"event": "phase_start", "phase": "phase2", "timestamp": ts(1035)})
    stream.append({"event": "phase_done", "phase": "phase2", "timestamp": ts(1036)})

    return stream


def test_check_jsonl_ordering_passes_for_canonical_stream(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """A well-formed event stream covering the 12 in-invocation phases passes."""
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, _canonical_event_stream())

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is True, result.message
    assert result.label == "jsonl_ordering"


def test_check_jsonl_ordering_fails_when_seed_event_leaks_into_real_invocation_stream(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """A real-invocation stream must not carry ``seed`` events.

    Per parent issue ALP-493 § D, the pre-invocation ``seed`` event
    lands in the sentinel ``<archive>/invocations/_pre_invocation/``
    directory because the canonical invocation_id isn't known until
    ``run_invocation`` returns. If a future regression starts emitting
    ``seed`` into the real-invocation file, the check should surface
    it as an unexpected phase.
    """
    t = datetime(2026, 5, 16, 11, 59, 59, tzinfo=UTC)
    stream = [
        {"event": "phase_start", "phase": "seed", "timestamp": t.isoformat()},
        {
            "event": "phase_done",
            "phase": "seed",
            "timestamp": (t + timedelta(seconds=1)).isoformat(),
        },
        *_canonical_event_stream(),
    ]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert "seed" in result.message


def test_check_jsonl_ordering_tolerates_parallel_pair_reverse_start_order(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """`domain_researchers` can fire its `phase_start` AFTER `qualitative`.

    Both belong to the same TaskGroup; the event-loop scheduling order
    between them is not stable.
    """
    stream = _canonical_event_stream()
    # Swap the two phase_start events for the parallel pair.
    idx_dr_start = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "phase_start" and e.get("phase") == "domain_researchers"
    )
    idx_qual_start = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "phase_start" and e.get("phase") == "qualitative"
    )
    stream[idx_dr_start], stream[idx_qual_start] = stream[idx_qual_start], stream[idx_dr_start]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is True, result.message


def test_check_jsonl_ordering_tolerates_analyst_strategist_done_reorder(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """`analyst`/`strategist` can ``phase_done`` in either order.

    The two phase_done emits land outside the TaskGroup
    (``pipeline/decision.py``); a future refactor that moves them inside
    a Task would let them reorder. The verify must tolerate that.
    """
    # Mutate ``phase`` labels in-place rather than swapping positions, so
    # the underlying timestamp order stays monotonic but the phase order
    # is reversed.
    stream = _canonical_event_stream()
    idx_analyst_done = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "phase_done" and e.get("phase") == "analyst"
    )
    idx_strategist_done = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "phase_done" and e.get("phase") == "strategist"
    )
    stream[idx_analyst_done] = {**stream[idx_analyst_done], "phase": "strategist"}
    stream[idx_strategist_done] = {**stream[idx_strategist_done], "phase": "analyst"}
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is True, result.message


def test_check_jsonl_ordering_fails_on_missing_phase(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL if any of the 12 in-invocation phases lacks a `phase_start`."""
    stream = [e for e in _canonical_event_stream() if e.get("phase") != "synthesizer"]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert "synthesizer" in result.message


def test_check_jsonl_ordering_fails_on_phase_done_before_phase_start(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL if a ``phase_done`` precedes its own ``phase_start``.

    Construct the malformed case by swapping the event-kind tags
    in-place rather than swapping list positions — this isolates the
    "done-before-start" failure from the monotonic-timestamp failure.
    """
    stream = _canonical_event_stream()
    idx_start = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "phase_start" and e.get("phase") == "pm"
    )
    idx_done = next(
        i for i, e in enumerate(stream) if e.get("event") == "phase_done" and e.get("phase") == "pm"
    )
    stream[idx_start] = {**stream[idx_start], "event": "phase_done"}
    stream[idx_done] = {**stream[idx_done], "event": "phase_start"}
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert "pm" in result.message


def test_check_jsonl_ordering_fails_on_non_monotonic_timestamp(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL if event timestamps run backwards."""
    stream = _canonical_event_stream()
    # Flip two consecutive timestamps so the second precedes the first.
    stream[5]["timestamp"], stream[4]["timestamp"] = stream[4]["timestamp"], stream[5]["timestamp"]
    # Make sure the flip actually creates an out-of-order pair.
    stream[5]["timestamp"] = "2020-01-01T00:00:00+00:00"
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert "timestamp" in result.message.lower()


def test_check_jsonl_ordering_fails_on_missing_agent_request_response_pair(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL if fewer than 9 SDK call pairs are present."""
    stream = [
        e
        for e in _canonical_event_stream()
        if not (e.get("event") in ("agent_request", "agent_response") and e.get("phase") == "pm")
    ]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False


def test_check_jsonl_ordering_fails_on_orphan_agent_response(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL if an `agent_response` lands without a preceding `agent_request`."""
    stream = _canonical_event_stream()
    # Drop the synthesizer's agent_request, leaving an orphan response.
    stream = [
        e
        for e in stream
        if not (e.get("event") == "agent_request" and e.get("phase") == "synthesizer")
    ]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False


@pytest.mark.parametrize(
    "field",
    [
        "duration_s",
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "tool_calls",
        # ``stop_reason`` is allowed to be null per parent issue ALP-493
        # § (B); only its absence is a regression.
        "stop_reason",
    ],
)
def test_check_jsonl_ordering_fails_when_agent_response_missing_required_field(
    verify_module: ModuleType, tmp_path: Path, field: str
) -> None:
    """Each ``agent_response`` must carry the 7-field set per parent § (B).

    ``duration_s`` / ``input_tokens`` / ``cache_read_tokens`` /
    ``cache_write_tokens`` / ``output_tokens`` / ``tool_calls`` must be
    non-null; ``stop_reason`` may be ``None`` but must be present as a
    key. The cache split was added per ALP-701 so an operator can
    distinguish a cache-hit prompt from a broken context-assembly path.
    """
    stream = _canonical_event_stream()
    # Drop the field from the synthesizer agent_response only.
    mutated: list[dict[str, Any]] = []
    for ev in stream:
        if ev.get("event") == "agent_response" and ev.get("phase") == "synthesizer":
            mutated.append({k: v for k, v in ev.items() if k != field})
        else:
            mutated.append(ev)
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, mutated)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert field in result.message


def test_check_jsonl_ordering_fails_when_required_field_is_null(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """The non-stop_reason 6 fields must be non-null.

    ``stop_reason`` is allowed to be null (it's optional per the
    Anthropic SDK); the other six (``duration_s``, ``input_tokens``,
    ``cache_read_tokens``, ``cache_write_tokens``, ``output_tokens``,
    ``tool_calls``) are load-bearing for the report. Cache split per
    ALP-701.
    """
    stream = _canonical_event_stream()
    mutated: list[dict[str, Any]] = []
    for ev in stream:
        if ev.get("event") == "agent_response" and ev.get("phase") == "synthesizer":
            mutated.append({**ev, "tool_calls": None})
        else:
            mutated.append(ev)
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, mutated)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert "tool_calls" in result.message


def test_check_jsonl_ordering_allows_null_stop_reason(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """``stop_reason`` may legitimately be null without failing the check."""
    stream = _canonical_event_stream()
    mutated: list[dict[str, Any]] = []
    for ev in stream:
        if ev.get("event") == "agent_response":
            mutated.append({**ev, "stop_reason": None})
        else:
            mutated.append(ev)
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, mutated)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is True, result.message


def test_check_jsonl_ordering_tolerates_corrective_retry_pair(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """A second request/response pair from a corrective retry is tolerated.

    A decision-layer harness re-invokes the SDK when a first response
    fails validation; that emits a second fully closed
    ``agent_request``/``agent_response`` pair for the same agent. The
    pipeline still completes, so the check must not fail on it.
    """
    stream = _canonical_event_stream()
    idx = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "agent_response" and e.get("agent") == "strategist"
    )
    resp = stream[idx]
    retry_request = {
        "event": "agent_request",
        "phase": "strategist",
        "agent": "strategist",
        "model": "opus",
        "timestamp": resp["timestamp"],
    }
    stream[idx + 1 : idx + 1] = [retry_request, {**resp}]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is True, result.message


def test_check_jsonl_ordering_fails_on_overlapping_agent_request(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """A second ``agent_request`` before the prior one is closed is a failure."""
    stream = _canonical_event_stream()
    idx = next(
        i
        for i, e in enumerate(stream)
        if e.get("event") == "agent_request" and e.get("agent") == "strategist"
    )
    stream[idx + 1 : idx + 1] = [{**stream[idx]}]
    jsonl = tmp_path / "progress.jsonl"
    _write_jsonl(jsonl, stream)

    result = verify_module.check_jsonl_ordering(jsonl)
    assert result.passed is False
    assert "strategist" in result.message


# ---------------------------------------------------------------------------
# check_synthetic_portfolio_visibility
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with the AlphaMind schema applied."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _seed_synthetic_portfolio(session: Session, *, current_cash_usd: float = 24_440.0) -> None:
    """Insert 8 positions + 8 theses + 1 cash_ledger row directly into a fresh DB.

    Mirrors the row shapes ``wipe_and_seed`` produces — we write the rows
    via the sync ORM here so the helper exercises real DB columns and
    constraints without spinning up the async seeder.
    """
    from decimal import Decimal

    from alphamind.portfolio_state.records.positions import (
        Direction,
        InstrumentType,
        PositionStatus,
    )
    from alphamind.portfolio_state.records.theses import ThesisRecordStatus
    from alphamind.state.tables import (
        CashLedgerRow,
        PositionRow,
        ThesisRow,
    )
    from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID

    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    now_iso = now.isoformat().replace("+00:00", "Z")
    for i in range(8):
        position = PositionRow(
            position_id=f"debug-pos-{i:02d}",
            thesis_id=None,
            bracket_id=None,
            status=PositionStatus.PENDING.value,
            direction=Direction.LONG.value,
            entry_timestamp=now_iso,
            instrument_type=InstrumentType.EQUITY.value,
            details_json=json.dumps(
                {
                    "instrument_type": InstrumentType.EQUITY.value,
                    "ticker": f"SYM{i}",
                    "share_count": 10.0,
                    "average_cost_basis_per_share": 100.0,
                    "borrow_rate_pct": None,
                    "locate_status": None,
                    "margin_held_usd": None,
                }
            ),
            execution_history_json="[]",
            realized_pnl_to_date_usd=None,
            corporate_action_adjustment_needed=0,
            parent_position_id=None,
            origin=None,
        )
        session.add(position)
        thesis = ThesisRow(
            thesis_id=f"debug-thesis-{i:02d}",
            position_id=f"debug-pos-{i:02d}",
            status=ThesisRecordStatus.ACTIVE.value,
            resolution_timestamp=None,
            resolution_category=None,
            summary=f"rationale {i}",
            time_expectation_hours=24.0,
            position_size_rationale=None,
            generation_timestamp=now_iso,
            narrative_json=json.dumps(
                {
                    "key_catalyst": f"Headline {i}",
                    "age_hours": 0.0,
                    "expected_resolution_at": now_iso,
                    "resolution_pnl_usd": None,
                    "entry_fill_gap_usd": None,
                    "components_metadata": {},
                }
            ),
        )
        session.add(thesis)
    cash = Decimal(str(current_cash_usd))
    zero = Decimal(0)
    session.add(
        CashLedgerRow(
            id=CASH_LEDGER_SINGLETON_ID,
            current_cash_usd=cash,
            settled_cash_usd=cash,
            reserved_capital_usd=zero,
            available_buying_power_usd=cash,
            margin_held_usd=zero,
            unsettled_proceeds_json="[]",
            last_updated_at=now_iso,
        )
    )
    session.commit()


def test_check_synthetic_portfolio_visibility_passes_on_seeded_db(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """PASS when 8 positions + 8 theses + 1 cash_ledger@$24,440 are present."""
    _seed_synthetic_portfolio(session)

    result = verify_module.check_synthetic_portfolio_visibility(engine)
    assert result.passed is True, result.message
    assert result.label == "synthetic_portfolio"


def test_check_synthetic_portfolio_visibility_fails_on_wrong_position_count(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """FAIL when ``positions`` carries a count other than 8."""
    from alphamind.portfolio_state.records.positions import (
        Direction,
        InstrumentType,
        PositionStatus,
    )
    from alphamind.state.tables import PositionRow

    now_iso = "2026-05-16T12:00:00Z"
    session.add(
        PositionRow(
            position_id="pos-only",
            thesis_id=None,
            bracket_id=None,
            status=PositionStatus.PENDING.value,
            direction=Direction.LONG.value,
            entry_timestamp=now_iso,
            instrument_type=InstrumentType.EQUITY.value,
            details_json="{}",
            execution_history_json="[]",
            realized_pnl_to_date_usd=None,
            corporate_action_adjustment_needed=0,
            parent_position_id=None,
            origin=None,
        )
    )
    session.commit()

    result = verify_module.check_synthetic_portfolio_visibility(engine)
    assert result.passed is False
    assert "positions" in result.message


def test_check_synthetic_portfolio_visibility_fails_on_wrong_cash(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """FAIL when ``cash_ledger.current_cash_usd`` is not 24,440."""
    _seed_synthetic_portfolio(session, current_cash_usd=50_000.0)

    result = verify_module.check_synthetic_portfolio_visibility(engine)
    assert result.passed is False
    assert "cash" in result.message.lower()


def _seed_empty_portfolio(session: Session, *, current_cash_usd: float = 100_000.0) -> None:
    """Insert just the singleton ``cash_ledger`` row — no positions, no theses (ALP-618).

    Models the fresh-start seeder's post-state: ``positions`` / ``theses`` /
    ``thesis_components`` are wiped clean, only the singleton cash + drawdown
    rows remain.
    """
    from decimal import Decimal

    from alphamind.state.tables import CashLedgerRow
    from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID

    cash = Decimal(str(current_cash_usd))
    zero = Decimal(0)
    session.add(
        CashLedgerRow(
            id=CASH_LEDGER_SINGLETON_ID,
            current_cash_usd=cash,
            settled_cash_usd=cash,
            reserved_capital_usd=zero,
            available_buying_power_usd=cash,
            margin_held_usd=zero,
            unsettled_proceeds_json="[]",
            last_updated_at="2026-05-16T12:00:00Z",
        )
    )
    session.commit()


def test_check_synthetic_portfolio_visibility_passes_for_fresh_start(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """PASS for the fresh-start fixture: 0 / 0 / $100,000 (ALP-618).

    The verify harness builds a ``PortfolioExpectations(0, 0, 100_000.0)``
    when ``--fresh-start`` is set; the same shape parametrized into the
    check must validate the corresponding seeded DB.
    """
    _seed_empty_portfolio(session)

    result = verify_module.check_synthetic_portfolio_visibility(
        engine, expected=verify_module.FRESH_START_EXPECTATIONS
    )
    assert result.passed is True, result.message
    assert "positions=0" in result.message
    assert "theses=0" in result.message
    assert "100000.0" in result.message


def test_check_synthetic_portfolio_visibility_fresh_start_rejects_synthetic_db(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """A managed-portfolio DB FAILS the fresh-start expectations (ALP-618).

    Cross-shape check — if the operator passes ``--fresh-start`` against
    a DB seeded with ``SYNTHETIC_PORTFOLIO``, the check must FAIL on the
    positions count mismatch (8 ≠ 0).
    """
    _seed_synthetic_portfolio(session)

    result = verify_module.check_synthetic_portfolio_visibility(
        engine, expected=verify_module.FRESH_START_EXPECTATIONS
    )
    assert result.passed is False
    assert "positions" in result.message.lower()


def test_check_synthetic_portfolio_visibility_synthetic_rejects_fresh_start_db(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """A fresh-start DB FAILS the default (synthetic) expectations (ALP-618).

    Symmetric cross-shape check — a verify run without ``--fresh-start``
    against a clean-slate DB must FAIL on the positions count mismatch.
    """
    _seed_empty_portfolio(session)

    result = verify_module.check_synthetic_portfolio_visibility(engine)
    assert result.passed is False
    assert "positions" in result.message.lower()


def _add_pm_skeleton_position(session: Session, *, idx: int) -> None:
    """Insert a PENDING position + linked ACTIVE thesis with ``entry_timestamp=NULL``.

    Models the row shape ``_writeback_open`` produces when PM dispatches an
    OPEN command to the log-only broker: the skeleton position lands in
    PENDING status with a null ``entry_timestamp`` (Phase 1 would set it
    on fill — but log-only never produces a fill, so it stays null forever).
    The check must filter these out via the ``entry_timestamp IS NOT NULL``
    predicate so it does not falsely fail any run where the analyst
    proposes a new trade.
    """
    from alphamind.portfolio_state.records.positions import (
        Direction,
        InstrumentType,
        PositionStatus,
    )
    from alphamind.portfolio_state.records.theses import ThesisRecordStatus
    from alphamind.state.tables import PositionRow, ThesisRow

    pid = f"pm-skel-pos-{idx:02d}"
    tid = f"pm-skel-thesis-{idx:02d}"
    session.add(
        PositionRow(
            position_id=pid,
            thesis_id=tid,
            bracket_id=None,
            status=PositionStatus.PENDING.value,
            direction=Direction.LONG.value,
            entry_timestamp=None,
            instrument_type=InstrumentType.EQUITY.value,
            details_json=json.dumps(
                {
                    "instrument_type": InstrumentType.EQUITY.value,
                    "ticker": f"NEW{idx}",
                    "share_count": 0.0,
                    "average_cost_basis_per_share": 0.0,
                    "borrow_rate_pct": None,
                    "locate_status": None,
                    "margin_held_usd": None,
                }
            ),
            execution_history_json="[]",
            realized_pnl_to_date_usd=None,
            corporate_action_adjustment_needed=0,
            parent_position_id=None,
            origin=None,
        )
    )
    session.add(
        ThesisRow(
            thesis_id=tid,
            position_id=pid,
            status=ThesisRecordStatus.ACTIVE.value,
            resolution_timestamp=None,
            resolution_category=None,
            summary=f"PM dispatch {idx}",
            time_expectation_hours=24.0,
            position_size_rationale=None,
            generation_timestamp=datetime(2026, 5, 27, 3, 0, 0, tzinfo=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            narrative_json=json.dumps(
                {
                    "key_catalyst": f"PM dispatch {idx}",
                    "age_hours": 0.0,
                    "expected_resolution_at": "2026-05-28T03:00:00Z",
                    "resolution_pnl_usd": None,
                    "entry_fill_gap_usd": None,
                    "components_metadata": {},
                }
            ),
        )
    )
    session.commit()


def test_check_synthetic_portfolio_visibility_ignores_pm_skeleton_rows_fresh_start(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """PM-dispatched PENDING skeletons must not count toward the fresh-start total.

    Regression: when the analyst proposes new trades under ``--fresh-start``,
    PM's ``_writeback_open`` writes one PENDING position + one ACTIVE thesis
    per OPEN command (entry_timestamp left null until fill arrives, which
    the log-only broker never produces). Before the fix the check counted
    these via ``SELECT COUNT(*) FROM positions`` and failed every fresh-start
    run with any analyst output. The check now filters on
    ``entry_timestamp IS NOT NULL`` so only the seeded fixture's rows count.
    """
    _seed_empty_portfolio(session)
    _add_pm_skeleton_position(session, idx=1)
    _add_pm_skeleton_position(session, idx=2)

    result = verify_module.check_synthetic_portfolio_visibility(
        engine, expected=verify_module.FRESH_START_EXPECTATIONS
    )
    assert result.passed is True, result.message
    assert "positions=0" in result.message
    assert "theses=0" in result.message


def test_check_synthetic_portfolio_visibility_ignores_pm_skeleton_rows_default(
    verify_module: ModuleType, engine: Engine, session: Session
) -> None:
    """PM-dispatched PENDING skeletons must not count toward the default total either.

    Same filter applies in default-fixture mode: the seeded 8 positions all
    have non-null ``entry_timestamp`` (the seeder sets it at seed time), so
    adding PM skeletons with null ``entry_timestamp`` must not bump the
    count above 8.
    """
    _seed_synthetic_portfolio(session)
    _add_pm_skeleton_position(session, idx=1)
    _add_pm_skeleton_position(session, idx=2)

    result = verify_module.check_synthetic_portfolio_visibility(engine)
    assert result.passed is True, result.message
    assert "positions=8" in result.message
    assert "theses=8" in result.message


# ---------------------------------------------------------------------------
# check_no_alpaca
# ---------------------------------------------------------------------------


def test_check_no_alpaca_passes_on_clean_stream(verify_module: ModuleType) -> None:
    """PASS when the captured subprocess stream carries no Alpaca HTTP indicators."""
    stream = (
        "2026-05-16 12:00:00 INFO alphamind.scheduler.orchestrator phase1 start\n"
        "2026-05-16 12:00:01 INFO alphamind.scheduler.orchestrator phase1 done\n"
    )

    result = verify_module.check_no_alpaca(stream)
    assert result.passed is True
    assert result.label == "no_alpaca"


def test_check_no_alpaca_fails_on_alpaca_py_mention(verify_module: ModuleType) -> None:
    """FAIL when ``alpaca-py`` or HTTP-y indicators leak into the stream."""
    stream = "2026-05-16 12:00:01 DEBUG alpaca-py request to /v2/positions\n"

    result = verify_module.check_no_alpaca(stream)
    assert result.passed is False
    assert "alpaca" in result.message.lower()


def test_check_no_alpaca_fails_on_alpaca_markets_host_in_stream(
    verify_module: ModuleType,
) -> None:
    """FAIL on canonical hostname ``alpaca.markets`` in the stream."""
    stream = "DEBUG urllib3.connectionpool: Starting new HTTPS connection to api.alpaca.markets\n"

    result = verify_module.check_no_alpaca(stream)
    assert result.passed is False
    assert "alpaca" in result.message.lower()


def test_check_no_alpaca_passes_on_empty_stream(verify_module: ModuleType) -> None:
    """An empty stream is a clean PASS — the check runs every time."""
    result = verify_module.check_no_alpaca("")
    assert result.passed is True


# ---------------------------------------------------------------------------
# check_invocation_summary
# ---------------------------------------------------------------------------


def test_check_invocation_summary_passes_on_canonical_payload(
    verify_module: ModuleType,
) -> None:
    """PASS when the JSON carries the three required fields with valid values."""
    payload = json.dumps(
        {
            "invocation_id": "20260516T000000Z",
            "trigger_source": "debug_e2e_cli",
            "staleness_flag": False,
            "commands_submitted": 3,
            "commands_rejected": 0,
        }
    )

    result = verify_module.check_invocation_summary(payload)
    assert result.passed is True, result.message
    assert result.label == "invocation_summary"


def test_check_invocation_summary_fails_on_staleness_flag_true(
    verify_module: ModuleType,
) -> None:
    """FAIL when staleness_flag is True — debug-e2e is stale-free by design."""
    payload = json.dumps(
        {
            "invocation_id": "iid",
            "trigger_source": "debug_e2e_cli",
            "staleness_flag": True,
            "commands_submitted": 0,
        }
    )

    result = verify_module.check_invocation_summary(payload)
    assert result.passed is False
    assert "staleness" in result.message.lower()


def test_check_invocation_summary_fails_on_wrong_trigger_source(
    verify_module: ModuleType,
) -> None:
    """FAIL when trigger_source is not ``debug_e2e_cli``."""
    payload = json.dumps(
        {
            "invocation_id": "iid",
            "trigger_source": "cli",
            "staleness_flag": False,
            "commands_submitted": 0,
        }
    )

    result = verify_module.check_invocation_summary(payload)
    assert result.passed is False
    assert "trigger_source" in result.message.lower()


def test_check_invocation_summary_fails_on_negative_commands_submitted(
    verify_module: ModuleType,
) -> None:
    """FAIL when commands_submitted is negative."""
    payload = json.dumps(
        {
            "invocation_id": "iid",
            "trigger_source": "debug_e2e_cli",
            "staleness_flag": False,
            "commands_submitted": -1,
        }
    )

    result = verify_module.check_invocation_summary(payload)
    assert result.passed is False


def test_check_invocation_summary_fails_on_non_json_stdout(
    verify_module: ModuleType,
) -> None:
    """FAIL when the subprocess stdout is not valid JSON."""
    result = verify_module.check_invocation_summary("not json at all")
    assert result.passed is False
    assert "json" in result.message.lower()


# ---------------------------------------------------------------------------
# _drive_debug_e2e_subprocess — argparse → subprocess command-line plumbing
# ---------------------------------------------------------------------------


def test_drive_debug_e2e_subprocess_propagates_archive_root_to_cli(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The verify script must pass ``--archive-root`` through to the CLI.

    Without propagation, the subprocess uses the hardcoded
    ``~/AlphaMind/archive`` default and the verify-script's archive
    checks point at a directory the CLI never wrote.
    """
    captured: dict[str, Any] = {}

    def _stub_run(cmd: list[str], **_kwargs: Any) -> Any:
        captured["cmd"] = cmd

        class _Completed:
            returncode = 0
            stdout = '{"invocation_id": "iid"}'
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    args = verify_module._parse_args(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )
    result, _output = verify_module._drive_debug_e2e_subprocess(args)

    assert result.passed is True
    assert "--archive-root" in captured["cmd"]
    archive_idx = captured["cmd"].index("--archive-root")
    assert captured["cmd"][archive_idx + 1] == str(tmp_path / "archive")


def test_drive_debug_e2e_subprocess_propagates_fresh_start_to_cli(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--fresh-start`` on the verify CLI forwards to the scheduler subprocess (ALP-618).

    Without forwarding, the verify harness would assert the fresh-start
    fixture against a DB the CLI seeded with the synthetic fixture — a
    silent false-failure mode.
    """
    captured: dict[str, Any] = {}

    def _stub_run(cmd: list[str], **_kwargs: Any) -> Any:
        captured["cmd"] = cmd

        class _Completed:
            returncode = 0
            stdout = '{"invocation_id": "iid"}'
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    args = verify_module._parse_args(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--fresh-start",
        ]
    )
    result, _output = verify_module._drive_debug_e2e_subprocess(args)

    assert result.passed is True
    assert "--fresh-start" in captured["cmd"]


def test_drive_debug_e2e_subprocess_omits_fresh_start_when_not_set(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--fresh-start`` on verify, the CLI doesn't receive it either."""
    captured: dict[str, Any] = {}

    def _stub_run(cmd: list[str], **_kwargs: Any) -> Any:
        captured["cmd"] = cmd

        class _Completed:
            returncode = 0
            stdout = '{"invocation_id": "iid"}'
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    args = verify_module._parse_args(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )
    verify_module._drive_debug_e2e_subprocess(args)

    assert "--fresh-start" not in captured["cmd"]


def test_drive_debug_e2e_subprocess_captures_stdout_and_stderr(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The subprocess driver captures stderr alongside stdout for downstream checks.

    ``check_no_alpaca`` consumes the stderr blob to scan for Alpaca
    HTTP indicators; the prior log-file approach was lenient when no
    pipeline log existed.
    """

    def _stub_run(cmd: list[str], **_kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stdout = '{"invocation_id": "iid"}'
            stderr = "some captured stderr text\n"

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    args = verify_module._parse_args(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )
    result, output = verify_module._drive_debug_e2e_subprocess(args)

    assert result.passed is True
    assert output is not None
    assert output.stdout == '{"invocation_id": "iid"}'
    assert output.stderr == "some captured stderr text\n"


# ---------------------------------------------------------------------------
# main() — selector polarity for fresh-start vs synthetic expectations
# ---------------------------------------------------------------------------


def _stub_main_dependencies(
    verify_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout_payload: str = '{"invocation_id": "inv-test", "staleness_flag": false, '
    '"trigger_source": "debug_e2e_cli", "commands_submitted": 0}',
) -> dict[str, Any]:
    """Stub the IO/check layer so ``main()`` runs end-to-end in-process.

    Captures the ``expected`` kwarg threaded into
    :func:`check_synthetic_portfolio_visibility` so a polarity flip in
    the selector ternary at ``main()`` fails loudly (ALP-618).
    """
    captured: dict[str, Any] = {}

    def _stub_run(_cmd: list[str], **_kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stdout = stdout_payload
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    def _passing_archive(*, archive_root: Path, invocation_id: str) -> Any:
        # main() resolves progress_path via find_invocation_archive_dir so
        # the stub must touch the JSONL file inside the same directory the
        # glob would locate — create the date-partitioned path if none exists yet.
        from alphamind._kernel.archive_layout import find_invocation_archive_dir

        inv_dir = find_invocation_archive_dir(
            archive_root=archive_root, invocation_id=invocation_id
        )
        if inv_dir is None:
            inv_dir = invocation_archive_dir(
                archive_root=archive_root,
                as_of=_INVOCATION_AS_OF,
                invocation_id=invocation_id,
            )
            inv_dir.mkdir(parents=True, exist_ok=True)
        (inv_dir / "progress.jsonl").touch()
        return verify_module.CheckResult(label="archive_directory", passed=True, message="stub")

    monkeypatch.setattr(verify_module, "check_archive_directory", _passing_archive)
    monkeypatch.setattr(
        verify_module,
        "check_jsonl_ordering",
        lambda _path: verify_module.CheckResult(
            label="jsonl_ordering", passed=True, message="stub"
        ),
    )

    def _capturing_visibility(_engine: Any, *, expected: Any) -> Any:
        captured["expected"] = expected
        return verify_module.CheckResult(label="synthetic_portfolio", passed=True, message="stub")

    monkeypatch.setattr(
        verify_module, "check_synthetic_portfolio_visibility", _capturing_visibility
    )
    # ALP-703: tool_layer_health walks sdk_trace.jsonl files; the stub
    # invocation directory has none, so without a stub the new check
    # would FAIL the wrapper exit code and break these polarity tests.
    monkeypatch.setattr(
        verify_module,
        "check_tool_layer_health",
        lambda _inv_dir, *, tallies=None: verify_module.CheckResult(
            label="tool_layer_health", passed=True, message="stub"
        ),
    )

    # Pre-flight auth: ensure CLAUDE_CODE_OAUTH_TOKEN is set so check_auth passes.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stub-token")
    return captured


def test_main_threads_synthetic_expectations_by_default(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--fresh-start``, ``main()`` selects ``SYNTHETIC_EXPECTATIONS`` (ALP-618).

    Locks the selector polarity at the ``main()`` ternary — a swapped
    branch would still pass every direct ``check_synthetic_portfolio_visibility``
    test (they pass ``expected`` explicitly), so this is the only seam that
    pins the runtime selector.
    """
    captured = _stub_main_dependencies(verify_module, monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )

    assert exit_code == 0
    assert captured["expected"] is verify_module.SYNTHETIC_EXPECTATIONS


def test_main_threads_fresh_start_expectations_when_flag_set(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``--fresh-start``, ``main()`` selects ``FRESH_START_EXPECTATIONS`` (ALP-618)."""
    captured = _stub_main_dependencies(verify_module, monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--fresh-start",
        ]
    )

    assert exit_code == 0
    assert captured["expected"] is verify_module.FRESH_START_EXPECTATIONS


# ---------------------------------------------------------------------------
# verify_summary.txt persistence
# ---------------------------------------------------------------------------


def test_main_writes_verify_summary_to_inv_dir(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A green run persists wrapper stdout to ``<inv_dir>/verify_summary.txt``.

    The verdict + DATA HEALTH block previously only reached the operator
    via stdout; ``tee``-to-a-side-file was the workaround. The script
    now persists the same content alongside the run's archive so the
    artifact lives with progress.jsonl / per-agent diagnostics.
    """
    _stub_main_dependencies(verify_module, monkeypatch)
    archive_root = tmp_path / "archive"

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(archive_root),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )

    assert exit_code == 0
    # Stub stdout_payload pins invocation_id="inv-test".
    from alphamind._kernel.archive_layout import find_invocation_archive_dir

    inv_dir = find_invocation_archive_dir(archive_root=archive_root, invocation_id="inv-test")
    assert inv_dir is not None, "stub failed to create the invocation dir"
    summary_path = inv_dir / verify_module.VERIFY_SUMMARY_FILENAME
    assert summary_path.is_file(), (
        f"{verify_module.VERIFY_SUMMARY_FILENAME} not written under {inv_dir}"
    )
    content = summary_path.read_text(encoding="utf-8")
    assert "PASS: auth" in content
    assert "=== DEBUG-E2E VERIFICATION ===" in content
    assert "=== DATA HEALTH ===" in content


def test_persist_verify_summary_no_op_when_invocation_id_missing(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """Early pre-flight FAIL (no invocation_id) silently skips the file write.

    Auth / subprocess failures short-circuit before the orchestrator
    inserts an invocation row, so there is no inv_dir to write into.
    The function must no-op rather than raise — losing the side
    artifact must never escalate a pre-existing FAIL.
    """
    verify_module._persist_verify_summary(
        archive_root=tmp_path,
        invocation_id=None,
        content="anything\n",
    )
    # No file is created anywhere under the archive root.
    assert list(tmp_path.iterdir()) == []


def test_persist_verify_summary_no_op_when_inv_dir_missing(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """Subprocess-FAIL-before-archive paths leave no inv_dir; skip the write.

    A subprocess that crashes before ``insert_invocation_record`` commits
    will still have produced an invocation_id (parsed from stdout) but
    no archive directory on disk. The function tolerates that and no-ops.
    """
    verify_module._persist_verify_summary(
        archive_root=tmp_path,
        invocation_id="inv-nonexistent",
        content="anything\n",
    )
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# format_data_health_block
# ---------------------------------------------------------------------------


def test_format_data_health_missing_snapshot(verify_module: ModuleType) -> None:
    """An empty / unreadable snapshot renders the no-snapshot fallback line."""
    rendered = verify_module.format_data_health_block({})
    assert "=== DATA HEALTH ===" in rendered
    assert "no calibration snapshot" in rendered


def test_format_data_health_wrong_schema_version(verify_module: ModuleType) -> None:
    """A stale V1 operator snapshot triggers the version warning post-ALP-709 bump."""
    stale_payload = {
        "schema_version": "1",
        "summary": {"calibrated": 1, "accumulating": 0, "unavailable": 0},
        "unavailable": [],
        "accumulating": [],
    }
    rendered = verify_module.format_data_health_block(stale_payload)
    assert "unrecognized snapshot schema_version='1'" in rendered


def test_format_data_health_empty_lists(verify_module: ModuleType) -> None:
    """A fully-calibrated invocation renders only the counts line."""
    payload = {
        "schema_version": "2",
        "summary": {"calibrated": 4, "accumulating": 0, "unavailable": 0},
        "unavailable": [],
        "accumulating": [],
    }
    rendered = verify_module.format_data_health_block(payload)
    assert "calibrated=4  accumulating=0  unavailable=0" in rendered
    assert "UNAVAILABLE" not in rendered
    assert "ACCUMULATING" not in rendered


def test_format_data_health_populated_lists(verify_module: ModuleType) -> None:
    """Unavailable and accumulating sections list each module with its reason."""
    payload = {
        "schema_version": "2",
        "summary": {"calibrated": 1, "accumulating": 1, "unavailable": 2},
        "unavailable": [
            {
                "module": "q6.dollar_attribution",
                "reason": "DTWEXBGS history unavailable",
            },
            {
                "module": "q7.intermarket_regime.gld_real_yields",
                "reason": "0 < 60 (0 observations)",
            },
        ],
        "accumulating": [
            {
                "module": "q6.funding_stress",
                "reason": "funding_stress_min_observations: 11 < 60",
            },
        ],
    }
    rendered = verify_module.format_data_health_block(payload)
    assert "UNAVAILABLE (2) — operator action required:" in rendered
    assert "q6.dollar_attribution: DTWEXBGS history unavailable" in rendered
    assert "q7.intermarket_regime.gld_real_yields: 0 < 60 (0 observations)" in rendered
    assert "ACCUMULATING (1) — collector healthy, wait:" in rendered
    assert "q6.funding_stress: funding_stress_min_observations: 11 < 60" in rendered


def test_format_data_health_inflow_section_renders_with_metrics(
    verify_module: ModuleType,
) -> None:
    """ALP-709: V2 snapshots with ``inflow_metrics.sentiment`` render an
    always-on INFLOW line between the counts summary and the UNAVAILABLE block.
    """
    payload = {
        "schema_version": "2",
        "summary": {"calibrated": 20, "accumulating": 18, "unavailable": 14},
        "unavailable": [],
        "accumulating": [],
        "inflow_metrics": {
            "sentiment": {
                "window_start": "2026-05-26T20:40:25Z",
                "window_end": "2026-05-26T21:01:53Z",
                "articles_in_window": 0,
                "scored_articles_in_window": 0,
                "calibrated_tickers_total": 20,
                "calibrated_tickers_with_populated_secondary": 0,
            }
        },
    }
    rendered = verify_module.format_data_health_block(payload)
    assert "INFLOW (sentiment)" in rendered
    assert "2026-05-26T20:40:25Z" in rendered
    assert "2026-05-26T21:01:53Z" in rendered
    assert "articles_in_window=0" in rendered
    assert "scored=0" in rendered
    assert "populated_secondary=0/20" in rendered


def test_format_data_health_inflow_section_omitted_when_key_missing(
    verify_module: ModuleType,
) -> None:
    """V2 snapshots without ``inflow_metrics`` render the rest of DATA HEALTH
    normally — no INFLOW line, no crash.
    """
    payload = {
        "schema_version": "2",
        "summary": {"calibrated": 4, "accumulating": 0, "unavailable": 0},
        "unavailable": [],
        "accumulating": [],
    }
    rendered = verify_module.format_data_health_block(payload)
    assert "calibrated=4" in rendered
    assert "INFLOW (sentiment)" not in rendered


def test_format_data_health_rejects_internal_shape_payload(
    verify_module: ModuleType,
) -> None:
    """ALP-709 collision guard: an internal-shape payload (carrying
    ``summary.by_state`` / ``summary.total_blocks``) accidentally landing
    at the operator path renders as the internal-shape warning, not silent
    zeros. Both ``SCHEMA_VERSION`` and ``OPERATOR_SUMMARY_SCHEMA_VERSION``
    are now ``"2"`` so the version field alone no longer disambiguates.
    """
    internal_shape = {
        "schema_version": "2",
        "summary": {
            "total_blocks": 3,
            "by_state": {"calibrated": 1, "accumulating": 2, "unavailable": 0},
            "by_audience": {},
            "by_block_kind": {},
        },
        "accumulating_reasons": {"q1.foo": "bar"},
        "unavailable_reasons": {},
    }
    rendered = verify_module.format_data_health_block(internal_shape)
    assert "internal-shape" in rendered
    assert "calibrated=0" not in rendered


def test_format_data_health_tolerates_null_count_values(
    verify_module: ModuleType,
) -> None:
    """A V2 snapshot carrying explicit JSON ``null`` for an inflow count
    field (manual edit, partial write, future writer bug) renders the
    field as ``0`` rather than crashing the whole DATA HEALTH block.
    """
    payload = {
        "schema_version": "2",
        "summary": {"calibrated": 5, "accumulating": 0, "unavailable": 0},
        "unavailable": [],
        "accumulating": [],
        "inflow_metrics": {
            "sentiment": {
                "window_start": "2026-05-26T20:40:25Z",
                "window_end": "2026-05-26T21:01:53Z",
                "articles_in_window": None,
                "scored_articles_in_window": None,
                "calibrated_tickers_total": 5,
                "calibrated_tickers_with_populated_secondary": None,
            }
        },
    }
    rendered = verify_module.format_data_health_block(payload)
    assert "articles_in_window=0" in rendered
    assert "scored=0" in rendered
    assert "populated_secondary=0/5" in rendered


def test_format_data_health_inflow_section_handles_null_window(
    verify_module: ModuleType,
) -> None:
    """When no calibrated cohort has two baselines the window edges are
    ``None``; the renderer surfaces a friendly placeholder rather than the
    string ``"None"``.
    """
    payload = {
        "schema_version": "2",
        "summary": {"calibrated": 0, "accumulating": 0, "unavailable": 0},
        "unavailable": [],
        "accumulating": [],
        "inflow_metrics": {
            "sentiment": {
                "window_start": None,
                "window_end": None,
                "articles_in_window": 0,
                "scored_articles_in_window": 0,
                "calibrated_tickers_total": 0,
                "calibrated_tickers_with_populated_secondary": 0,
            }
        },
    }
    rendered = verify_module.format_data_health_block(payload)
    assert "INFLOW (sentiment)" in rendered
    assert "None" not in rendered
    assert "populated_secondary=0/0" in rendered


# ---------------------------------------------------------------------------
# check_tool_layer_health — ALP-703
# ---------------------------------------------------------------------------


def _write_sdk_trace(path: Path, records: list[dict[str, Any]]) -> None:
    """Append ``records`` as JSONL into ``path``, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _trace_tool_use(tool: str, *, use_id: str) -> dict[str, Any]:
    return {
        "event": "sdk_message",
        "type": "AssistantMessage",
        "blocks": [{"block": "ToolUseBlock", "tool": tool, "id": use_id}],
    }


def _trace_tool_result(use_id: str, *, quality: str | None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "block": "ToolResultBlock",
        "tool_use_id": use_id,
        "is_error": False,
    }
    if quality is not None:
        block["quality"] = quality
    return {"event": "sdk_message", "type": "UserMessage", "blocks": [block]}


def test_check_tool_layer_health_passes_when_all_complete(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """A run where every tool result is COMPLETE emits PASS with per-tool counts."""
    inv_dir = tmp_path / "inv-001"
    _write_sdk_trace(
        inv_dir / "analysis" / "qualitative_researcher" / "sdk_trace.jsonl",
        [
            _trace_tool_use("mcp__alphamind_qualitative__news_search", use_id="t1"),
            _trace_tool_result("t1", quality="complete"),
            _trace_tool_use("mcp__alphamind_qualitative__news_search", use_id="t2"),
            _trace_tool_result("t2", quality="complete"),
        ],
    )
    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.label == "tool_layer_health"
    assert result.passed is True
    assert "2/2 tool calls complete" in result.message
    assert "unavailable=0" in result.message
    # No degraded tools → no per-tool detail in the headline message;
    # per-tool counts surface in the TOOL LAYER HEALTH block instead.
    assert "degraded" not in result.message


def test_check_tool_layer_health_names_degraded_tools_in_headline(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """The ALP-703 symptom: news_search 6/6 unavailable, prediction_markets 3/3 unavailable.

    The headline message must name each degraded tool with its
    unavailable / total ratio so the operator catches the outage
    at the verdict line without descending into per-agent traces.
    """
    inv_dir = tmp_path / "inv-002"
    records: list[dict[str, Any]] = []
    for i in range(6):
        records.append(_trace_tool_use("mcp__alphamind_qualitative__news_search", use_id=f"n{i}"))
        records.append(_trace_tool_result(f"n{i}", quality="unavailable"))
    for i in range(3):
        records.append(
            _trace_tool_use("mcp__alphamind_qualitative__prediction_markets", use_id=f"p{i}")
        )
        records.append(_trace_tool_result(f"p{i}", quality="unavailable"))
    # Healthy companion calls so the run isn't a single-tool universe.
    records.append(_trace_tool_use("mcp__alphamind_qualitative__ticker_deep_pull", use_id="t1"))
    records.append(_trace_tool_result("t1", quality="complete"))
    _write_sdk_trace(inv_dir / "analysis" / "qualitative_researcher" / "sdk_trace.jsonl", records)

    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.passed is True  # informational — silent outages surface but don't fail the gate
    assert "1/10 tool calls complete" in result.message
    assert "unavailable=9" in result.message
    assert "news_search=6/6 unavailable" in result.message
    assert "prediction_markets=3/3 unavailable" in result.message
    # The healthy companion tool is NOT listed under degraded
    assert "ticker_deep_pull" not in result.message


def test_check_tool_layer_health_aggregates_across_agents(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """One tool's calls from different agents fold into a single tally entry.

    qualitative_researcher and adaptive_researcher share the news_search
    tool through different MCP server names; the strip-prefix rule means
    both surface under ``news_search`` in the tally instead of fragmenting
    into ``mcp__alphamind_qualitative__news_search`` and
    ``mcp__alphamind_adaptive__news_search``.
    """
    inv_dir = tmp_path / "inv-003"
    _write_sdk_trace(
        inv_dir / "analysis" / "qualitative_researcher" / "sdk_trace.jsonl",
        [
            _trace_tool_use("mcp__alphamind_qualitative__news_search", use_id="q1"),
            _trace_tool_result("q1", quality="unavailable"),
        ],
    )
    _write_sdk_trace(
        inv_dir / "analysis" / "adaptive_researcher" / "sdk_trace.jsonl",
        [
            _trace_tool_use("mcp__alphamind_adaptive__news_search", use_id="a1"),
            _trace_tool_result("a1", quality="complete"),
        ],
    )
    result = verify_module.check_tool_layer_health(inv_dir)
    assert "news_search=1/2 unavailable" in result.message


def test_check_tool_layer_health_passes_when_traces_exist_with_no_mcp_calls(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """sdk_trace.jsonl files present but no MCP tool calls → PASS, not FAIL.

    A real run where every agent produced only ``StructuredOutput`` synthetic
    blocks (no real MCP tool calls) is legitimate — the tracer ran, but no
    enrichment tools were called. This must NOT trip the instrumentation-
    regression FAIL the way an empty inv_dir does.
    """
    inv_dir = tmp_path / "inv-no-mcp"
    _write_sdk_trace(
        inv_dir / "analysis" / "tech_semis_researcher" / "sdk_trace.jsonl",
        [{"event": "sdk_message", "type": "AssistantMessage", "blocks": []}],
    )
    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.passed is True
    assert "no MCP tool calls" in result.message


def test_check_tool_layer_health_skips_synthetic_structured_output_tool(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """The SDK emits a synthetic ``StructuredOutput`` ToolUseBlock on every
    output_format=json_schema invocation. Its result has no AlphaMind envelope,
    so it would fall into the ``other`` bucket and falsely tag the synthetic
    tool as DEGRADED on every clean run. The tally must filter to real MCP
    tools only — the same convention the harness uses via ``tool_name_prefix``.
    """
    inv_dir = tmp_path / "inv-with-synthetic"
    _write_sdk_trace(
        inv_dir / "analysis" / "tech_semis_researcher" / "sdk_trace.jsonl",
        [
            _trace_tool_use("StructuredOutput", use_id="so1"),
            _trace_tool_result("so1", quality=None),
            _trace_tool_use("mcp__alphamind_qualitative__ticker_deep_pull", use_id="t1"),
            _trace_tool_result("t1", quality="complete"),
        ],
    )
    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.passed is True
    # Only the real MCP tool counts — the synthetic block is filtered.
    assert "1/1 tool calls complete" in result.message
    assert "other=0" in result.message
    # The tally returned by _tally_tool_outcomes must not contain StructuredOutput.
    tallies = verify_module._tally_tool_outcomes(inv_dir)
    assert "StructuredOutput" not in tallies
    assert "ticker_deep_pull" in tallies


def test_check_tool_layer_health_fails_when_no_sdk_traces_present(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """No sdk_trace.jsonl anywhere → instrumentation regression FAIL.

    When the SDK call tracer doesn't write any traces the check fails
    so an operator catches the regression rather than seeing a silent
    "PASS — no tool calls observed" message that would also fire on a
    legitimate no-tool run.
    """
    inv_dir = tmp_path / "inv-004"
    inv_dir.mkdir()
    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.passed is False
    assert "instrumentation regression" in result.message


def test_check_tool_layer_health_counts_unmatched_use_as_other(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """A ToolUseBlock with no matching ToolResultBlock falls into ``other``.

    The most common path to this state is a stalled SDK call that was
    timed out before the tool returned. The check must not lose the call
    or misclassify it as ``unavailable`` — the operator needs to see the
    truncation signal separately.
    """
    inv_dir = tmp_path / "inv-005"
    _write_sdk_trace(
        inv_dir / "analysis" / "qualitative_researcher" / "sdk_trace.jsonl",
        [
            _trace_tool_use("mcp__alphamind_qualitative__news_search", use_id="u1"),
            # No matching ToolResultBlock — the SDK call was cut off
        ],
    )
    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.passed is True
    assert "other=1" in result.message


def test_check_tool_layer_health_treats_missing_quality_as_other(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """An MCP tool whose result lacks a ``quality`` field buckets as ``other``.

    A real AlphaMind tool that — through a bug or partial migration — emits
    a result envelope without the ``quality`` field must not pollute the
    COMPLETE or UNAVAILABLE counts. The tally surfaces it as ``other`` so
    the malformed envelope is visible to the operator without being
    miscounted as either success or failure.
    """
    inv_dir = tmp_path / "inv-006"
    _write_sdk_trace(
        inv_dir / "analysis" / "qualitative_researcher" / "sdk_trace.jsonl",
        [
            _trace_tool_use("mcp__alphamind_qualitative__news_search", use_id="n1"),
            _trace_tool_result("n1", quality=None),
        ],
    )
    result = verify_module.check_tool_layer_health(inv_dir)
    assert result.passed is True
    assert "other=1" in result.message
    assert "unavailable=0" in result.message
    assert "0/1 tool calls complete" in result.message


# ---------------------------------------------------------------------------
# format_tool_layer_health_block
# ---------------------------------------------------------------------------


def test_format_tool_layer_health_block_empty(verify_module: ModuleType) -> None:
    """No tool calls at all renders the no-calls fallback line under the header."""
    rendered = verify_module.format_tool_layer_health_block({})
    assert "=== TOOL LAYER HEALTH ===" in rendered
    assert "no tool calls observed" in rendered


def test_format_tool_layer_health_block_all_healthy(verify_module: ModuleType) -> None:
    """A run with only COMPLETE tools renders the counts line without a degraded block."""
    tally_cls = verify_module._ToolOutcomeTally
    rendered = verify_module.format_tool_layer_health_block({"news_search": tally_cls(complete=4)})
    assert "tool_calls=4  complete=4  unavailable=0  other=0" in rendered
    assert "DEGRADED" not in rendered


def test_format_tool_layer_health_block_degraded(verify_module: ModuleType) -> None:
    """Degraded tools surface in a DEGRADED block ordered by unavailable count desc."""
    tally_cls = verify_module._ToolOutcomeTally
    rendered = verify_module.format_tool_layer_health_block(
        {
            "news_search": tally_cls(unavailable=6),
            "prediction_markets": tally_cls(unavailable=3),
            "ticker_deep_pull": tally_cls(complete=8),
        }
    )
    assert "tool_calls=17  complete=8  unavailable=9  other=0" in rendered
    assert "DEGRADED (2) — enrichment surface dark:" in rendered
    # Highest unavailable count first.
    news_index = rendered.index("news_search:")
    pm_index = rendered.index("prediction_markets:")
    assert news_index < pm_index
    assert "news_search: 6/6 unavailable, 0 other" in rendered
    assert "prediction_markets: 3/3 unavailable, 0 other" in rendered
    # Healthy tool is NOT listed in the degraded block.
    assert rendered.count("ticker_deep_pull") == 0
