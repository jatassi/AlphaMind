"""Tests for ``scripts/verify_oms_commands.py`` (ALP-376).

The verify script exercises the OMS commands surface end-to-end against a
freshly-migrated SQLite DB across five phases (canonical model round-trip,
command-ID utility, PM envelope path, engine envelope path, summary). No SDK
calls; no live broker contact. Sub-minute runtime against a fresh on-disk DB.

The script's logic lives in ``alphamind.scripts.verify_oms_commands`` so tests
can import it directly and inject a per-test on-disk SQLite DB; the shim at
``scripts/verify_oms_commands.py`` defers to ``main()`` here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine

_SHIM_PATH = Path(__file__).parents[2] / "scripts" / "verify_oms_commands.py"


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
    from alphamind.scripts.verify_oms_commands import main

    rc = main(["--db-path", str(fresh_db)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Phase 1 — canonical model round-trip (all five command variants + engine envelope)
# ---------------------------------------------------------------------------


def test_phase_1_canonical_round_trip_passes() -> None:
    """All five OMS command variants (OPEN / CLOSE / ADJUST / CANCEL / ADD)
    plus :class:`EngineEnvelope` round-trip cleanly via the canonical
    ``TypeAdapter[OMSCommand]`` / ``TypeAdapter[EngineEnvelope]``."""
    from alphamind.scripts.verify_oms_commands import run_phase_1_canonical_round_trip

    result = run_phase_1_canonical_round_trip()
    assert result.ok is True, result.detail
    assert "Phase 1" in result.label


# ---------------------------------------------------------------------------
# Phase 2 — command-ID utility (PM derivation + engine derivation + parse + attempt_seq)
# ---------------------------------------------------------------------------


def test_phase_2_command_id_utility_passes() -> None:
    """PM-derivation matches ``oms-command-ids.md`` worked example,
    engine-derivation matches the ``MON.{session}.{trigger}.{ord}`` shape,
    parse round-trips both, and ``compute_attempt_seq`` returns the
    post_rejection modification count."""
    from alphamind.scripts.verify_oms_commands import run_phase_2_command_id_utility

    result = run_phase_2_command_id_utility()
    assert result.ok is True, result.detail
    assert "Phase 2" in result.label


# ---------------------------------------------------------------------------
# Phase 3 — PM envelope path against a fresh DB
# ---------------------------------------------------------------------------


async def test_phase_3_pm_envelope_path_passes(fresh_db: Path) -> None:
    """A canonical PMEnvelope carrying one OPEN command, submitted via
    ``build_submit_envelope_mcp_server``, persists the new position with
    **real** dollar_value (not a stub) and lands the documented activity-log
    event chain (order_submitted, thesis_created, capital_reserved, pm_decision)."""
    from alphamind.scripts.verify_oms_commands import run_phase_3_pm_envelope_path

    result = await run_phase_3_pm_envelope_path(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase 3" in result.label


# ---------------------------------------------------------------------------
# Phase 4 — engine envelope path against the same DB
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# main() returns non-zero on failure
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_on_failure(
    fresh_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force a deliberate Phase 3 failure (the engine-stub writeback returns
    a stub dollar_value instead of the real one) and assert ``main()``
    returns non-zero with the failing criterion surfaced in the output."""
    import alphamind.scripts.verify_oms_commands as module

    # Replace ``run_phase_3_pm_envelope_path`` with one that surfaces the
    # canonical "still using the retired $1k stub" failure path. This is the
    # key regression the verify script is supposed to catch — the failure
    # detail should name the offending criterion.
    async def _failing_phase_3(_db_path: Path) -> module.PhaseResult:
        return module.PhaseResult(
            label="Phase 3 — PM envelope path",
            ok=False,
            detail=(
                "capital_reserved.amount_usd=1000.0, expected 5000.0 "
                "(regression to retired $1k stub?)"
            ),
        )

    monkeypatch.setattr(module, "run_phase_3_pm_envelope_path", _failing_phase_3)

    rc = module.main(["--db-path", str(fresh_db)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "[FAIL]" in out, "rendered output should mark the failing criterion with [FAIL]"
    assert "RESULT: FAIL" in out, "summary line should announce overall failure"
    assert "$1k stub" in out, "the failing criterion's diagnostic detail should propagate"


async def test_phase_4_engine_envelope_path_passes(fresh_db: Path) -> None:
    """An :class:`EngineEnvelope` carrying one CLOSE submitted via
    ``submit_engine_envelope`` persists the close order, returns
    ``accepted`` with an engine-pattern command_id, and emits an activity-log
    entry whose detail carries ``risk_management_subtype="engine_guardrail"``
    and the trigger record's ``position_selection_rationale``."""
    from alphamind.scripts.verify_oms_commands import (
        run_phase_3_pm_envelope_path,
        run_phase_4_engine_envelope_path,
    )

    # Phase 3 must run first so an open position exists to close.
    p3 = await run_phase_3_pm_envelope_path(fresh_db)
    assert p3.ok is True, p3.detail

    result = await run_phase_4_engine_envelope_path(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase 4" in result.label
