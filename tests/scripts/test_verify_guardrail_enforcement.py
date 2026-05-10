"""Tests for ``scripts/verify_guardrail_enforcement.py`` (ALP-398).

The verify script exercises the Phase 1 guardrail-enforcement orchestrator
end-to-end against a freshly-migrated SQLite DB across four phases (composition
primitive, orchestrator, repository provider, assembler integration). No SDK
calls; no live broker contact. Sub-second runtime against a fresh on-disk DB.

The script's logic lives in ``alphamind.scripts.verify_guardrail_enforcement``
so tests can import it directly and inject a per-test on-disk SQLite DB; the
shim at ``scripts/verify_guardrail_enforcement.py`` defers to ``main()`` here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine

_SHIM_PATH = Path(__file__).parents[2] / "scripts" / "verify_guardrail_enforcement.py"


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
    from alphamind.scripts.verify_guardrail_enforcement import main

    rc = main(["--db-path", str(fresh_db)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Phase 1 — composition primitive (no-tier / CONSTRAINED / HEAVILY_CONSTRAINED / FULL_HALT)
# ---------------------------------------------------------------------------


def test_phase_1_composition_primitive_passes() -> None:
    """Across every tier case (zero-drawdown / tier-1 / tier-2 / tier-3) the
    composition primitive returns the expected ``(ActiveRiskParameterSet,
    DrawdownTier | None)`` tuple. Tier triggers loaded from ``config/guardrails.yaml``."""
    from alphamind.scripts.verify_guardrail_enforcement import (
        run_phase_1_composition_primitive,
    )

    result = run_phase_1_composition_primitive()
    assert result.ok is True, result.detail
    assert "Phase 1" in result.label


# ---------------------------------------------------------------------------
# Phase 2 — orchestrator (compose_phase_1_enforcement bundles result)
# ---------------------------------------------------------------------------


def test_phase_2_orchestrator_passes() -> None:
    """The Phase 1 orchestrator wraps the composition primitive with a
    ``RegimeAdaptationOutput`` and a ``DrawdownState`` for each tier case and
    bundles the result into a ``Phase1EnforcementResult``."""
    from alphamind.scripts.verify_guardrail_enforcement import run_phase_2_orchestrator

    result = run_phase_2_orchestrator()
    assert result.ok is True, result.detail
    assert "Phase 2" in result.label


# ---------------------------------------------------------------------------
# Phase 3 — repository provider (provider yields the result's parameters)
# ---------------------------------------------------------------------------


async def test_phase_3_repository_provider_passes(fresh_db: Path) -> None:
    """``make_active_risk_parameters_provider`` plugs into the SQL repository's
    ``active_risk_parameters_provider`` slot; ``get_active_risk_parameters()``
    returns the bundled result's parameters unchanged."""
    from alphamind.scripts.verify_guardrail_enforcement import (
        run_phase_3_repository_provider,
    )

    result = await run_phase_3_repository_provider(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase 3" in result.label


# ---------------------------------------------------------------------------
# Phase 4 — assembler integration (snapshot.active_risk_parameters matches result)
# ---------------------------------------------------------------------------


async def test_phase_4_assembler_integration_passes(fresh_db: Path) -> None:
    """``assemble_snapshot`` reads through the SQL repository wired with the
    enforcement-layer provider and surfaces the composed parameters at
    ``snapshot.active_risk_parameters``. Assembler enrichment of
    ``parameter_change_flag`` is documented and acceptable (the verify script
    asserts equality on the flag-normalized parameter set)."""
    from alphamind.scripts.verify_guardrail_enforcement import (
        run_phase_4_assembler_integration,
    )

    result = await run_phase_4_assembler_integration(fresh_db)
    assert result.ok is True, result.detail
    assert "Phase 4" in result.label


# ---------------------------------------------------------------------------
# Phase 1 schema fail — required table missing produces FAIL
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_when_required_table_missing(tmp_path: Path) -> None:
    """A DB missing one of the state-persistence tables Phase 3 + Phase 4 read
    surfaces FAIL on at least one phase and returns non-zero."""
    from sqlalchemy import text

    from alphamind.scripts.verify_guardrail_enforcement import main

    db_path = tmp_path / "alphamind.db"
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(text("DROP TABLE invocations"))
    sync_engine.dispose()

    rc = main(["--db-path", str(db_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# main() exits non-zero on phase failure
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_on_failure(
    fresh_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force a Phase 1 failure and assert ``main()`` returns non-zero with the
    failing criterion surfaced in the rendered output."""
    import alphamind.scripts.verify_guardrail_enforcement as module

    def _failing_phase_1() -> module.PhaseResult:
        return module.PhaseResult(
            label="Phase 1 — composition primitive",
            ok=False,
            detail="composition primitive returned wrong tier for fixture",
        )

    monkeypatch.setattr(module, "run_phase_1_composition_primitive", _failing_phase_1)

    rc = module.main(["--db-path", str(fresh_db)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "[FAIL]" in out, "rendered output should mark the failing criterion with [FAIL]"
    assert "RESULT: FAIL" in out, "summary line should announce overall failure"
    assert "wrong tier" in out, "the failing criterion's diagnostic detail should propagate"


# ---------------------------------------------------------------------------
# JSON output mode
# ---------------------------------------------------------------------------


def test_main_json_output_emits_structured_payload(
    fresh_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--output json`` emits a JSON payload with ``all_pass`` + per-phase entries."""
    import json

    from alphamind.scripts.verify_guardrail_enforcement import main

    rc = main(["--db-path", str(fresh_db), "--output", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["all_pass"] is True
    assert isinstance(payload["phases"], list)
    assert len(payload["phases"]) >= 4
    for phase in payload["phases"]:
        assert {"label", "ok", "detail"} <= set(phase.keys())
