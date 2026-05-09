"""Tests for ``scripts/verify_state_persistence.py`` (ALP-367).

The verify script exercises the state-persistence substrate end-to-end against
a freshly-migrated SQLite DB (no SDK calls, sub-second). These tests cover the
six phase functions (schema, invocation context, Phase 1 write path, Phase 2
accepted envelope, Phase 2 parse failure, repository read parity) plus the CLI
machinery.

The script's logic lives in ``alphamind.scripts.verify_state_persistence`` so
tests can import it directly and inject a per-test on-disk SQLite DB; the
shim at ``scripts/verify_state_persistence.py`` defers to ``main()`` here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine

_SHIM_PATH = Path(__file__).parents[2] / "scripts" / "verify_state_persistence.py"


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Iterator[Path]:
    """Yield the path of a freshly-migrated on-disk SQLite DB (state-persistence schema applied)."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield db_path


# ---------------------------------------------------------------------------
# Tracer: shim loads and `main` returns 0 against a fresh DB
# ---------------------------------------------------------------------------


def test_shim_exists() -> None:
    assert _SHIM_PATH.exists(), f"Shim not found at {_SHIM_PATH}"


def test_main_returns_zero_on_fresh_db(fresh_db: Path) -> None:
    from alphamind.scripts.verify_state_persistence import main

    rc = main(["--db-path", str(fresh_db)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Phase A — schema verification
# ---------------------------------------------------------------------------


def test_phase_a_passes_against_fully_migrated_db(fresh_db: Path) -> None:
    from alphamind.scripts.verify_state_persistence import run_phase_a_schema

    result = run_phase_a_schema(fresh_db)
    assert result.ok is True
    assert "Phase A" in result.label


def test_phase_a_fails_when_required_table_missing(tmp_path: Path) -> None:
    """A DB missing one of the required state-persistence tables surfaces a
    clear ``missing tables: <name>`` diagnostic and the result is not ok."""
    from sqlalchemy import text

    from alphamind.scripts.verify_state_persistence import run_phase_a_schema

    # Build a fresh DB then drop one required table to simulate a partial migration.
    db_path = tmp_path / "alphamind.db"
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(text("DROP TABLE drawdown_state"))
    sync_engine.dispose()

    result = run_phase_a_schema(db_path)
    assert result.ok is False
    assert result.detail is not None
    assert "drawdown_state" in result.detail


def test_main_exits_nonzero_when_table_missing(tmp_path: Path) -> None:
    """End-to-end CLI assertion: missing table → exit code 1."""
    from sqlalchemy import text

    from alphamind.scripts.verify_state_persistence import main

    db_path = tmp_path / "alphamind.db"
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(text("DROP TABLE positions"))
    sync_engine.dispose()

    rc = main(["--db-path", str(db_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# Phase B — InvocationContext atomicity round-trip
# ---------------------------------------------------------------------------


async def test_phase_b_passes_against_fresh_db(fresh_db: Path) -> None:
    """Clean exit persists the row; raised exception rolls back. Both probed
    in a single phase so a regression in either path surfaces immediately."""
    from alphamind.scripts.verify_state_persistence import run_phase_b_invocation_context

    result = await run_phase_b_invocation_context(fresh_db)
    assert result.ok is True
    assert "Phase B" in result.label


# ---------------------------------------------------------------------------
# Phase C — Phase 1 fill-integration write path
# ---------------------------------------------------------------------------


async def test_phase_c_passes_against_fresh_db(fresh_db: Path) -> None:
    """Phase 1 entry-fill happy path: seeds the necessary order/position/
    bracket/thesis/cash/drawdown rows, appends an unprocessed fill, runs
    process_unprocessed_fills(), asserts the position transitioned PENDING→OPEN
    and the activity log captured the documented event chain."""
    from alphamind.scripts.verify_state_persistence import run_phase_c_phase1_write_path

    result = await run_phase_c_phase1_write_path(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase C" in result.label


# ---------------------------------------------------------------------------
# Phase D — Phase 2 accepted-envelope writeback
# ---------------------------------------------------------------------------


async def test_phase_d_passes_against_fresh_db(fresh_db: Path) -> None:
    """Phase 2 OPEN-command envelope writeback: persists the new position +
    thesis + bracket + orders, with the documented activity-log event chain."""
    from alphamind.scripts.verify_state_persistence import run_phase_d_phase2_envelope

    result = await run_phase_d_phase2_envelope(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase D" in result.label


# ---------------------------------------------------------------------------
# Phase E — Phase 2 Layer-1 parse-failure writeback
# ---------------------------------------------------------------------------


async def test_phase_e_passes_against_fresh_db(fresh_db: Path) -> None:
    """Phase 2 Layer-1 parse failure: malformed payload via _handle_submit_envelope
    must populate both (a) the in-memory failed_submission_log and (b) one
    envelope_parse_failed activity-log entry."""
    from alphamind.scripts.verify_state_persistence import run_phase_e_layer1_parse_failure

    result = await run_phase_e_layer1_parse_failure(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase E" in result.label


# ---------------------------------------------------------------------------
# Phase F — SqlPortfolioStateRepository read parity (snapshot assembly)
# ---------------------------------------------------------------------------


def test_verify_bootstrap_includes_state_persistence_tables() -> None:
    """The bootstrap verifier's table-existence list must cover the
    state-persistence durable substrate (ALP-367 acceptance: bootstrap
    integration). Asserted via source-text grep so the test does not need
    to import the script module (which exec'es at module-level)."""
    bootstrap_path = Path(__file__).parents[2] / "scripts" / "verify_bootstrap.py"
    source = bootstrap_path.read_text()
    expected_state_tables = (
        "process_lifetimes",
        "invocations",
        "activity_log",
        "positions",
        "theses",
        "thesis_components",
        "orders",
        "brackets",
        "bracket_legs",
        "cash_ledger",
        "drawdown_state",
        "fill_records",
        "corporate_action_integration_ledger",
    )
    for table in expected_state_tables:
        assert f'"{table}"' in source, f"verify_bootstrap.py does not list {table}"


async def test_phase_f_passes_against_populated_db(fresh_db: Path) -> None:
    """Phase F runs assemble_snapshot against the SQL-backed repository over
    a populated DB and verifies the resulting snapshot reflects the state
    earlier phases persisted, AND verifies RepositoryConsistencyError fires
    against an invocation whose phase1_completed_at is NULL."""
    from alphamind.scripts.verify_state_persistence import (
        run_phase_c_phase1_write_path,
        run_phase_f_repository_read_parity,
    )

    # Phase C populates a real position + bracket + thesis; Phase F reads
    # them back via SqlPortfolioStateRepository → assemble_snapshot.
    c_result = await run_phase_c_phase1_write_path(fresh_db)
    assert c_result.ok is True, c_result.detail

    f_result = await run_phase_f_repository_read_parity(fresh_db)
    assert f_result.ok is True, f_result.detail
    assert "Phase F" in f_result.label
