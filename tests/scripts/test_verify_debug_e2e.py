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
from alphamind.persistence.models import Base

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_debug_e2e.py"


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
    """PASS when ``<archive>/invocations/<id>/`` carries the two required files."""
    invocation_dir = tmp_path / "invocations" / "20260516T000000Z-debug-e2e"
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
    invocation_dir = tmp_path / "invocations" / "iid"
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "progress.jsonl").write_text("", encoding="utf-8")

    result = verify_module.check_archive_directory(archive_root=tmp_path, invocation_id="iid")
    assert result.passed is False
    assert "resolved_config.json" in result.message


def test_check_archive_directory_fails_when_progress_jsonl_missing(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when ``progress.jsonl`` is absent."""
    invocation_dir = tmp_path / "invocations" / "iid"
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
    """Each ``agent_response`` must carry the 5-field set per parent § (B).

    ``duration_s`` / ``input_tokens`` / ``output_tokens`` / ``tool_calls``
    must be non-null; ``stop_reason`` may be ``None`` but must be
    present as a key.
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
    """The non-stop_reason 4 fields must be non-null.

    ``stop_reason`` is allowed to be null (it's optional per the
    Anthropic SDK); the other four are load-bearing for the report.
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
