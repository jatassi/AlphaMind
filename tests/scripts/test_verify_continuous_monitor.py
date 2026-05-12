"""Tests for ``scripts/verify_continuous_monitor.py`` (ALP-441).

The verify script exercises the continuous monitor's five responsibilities
against in-memory fixtures (no live websockets, no live broker, no live DB).
Per the story acceptance criteria these tests only confirm the runner-level
pass/fail aggregation; the per-scenario assertions are tested inline by the
per-story unit tests under ``tests/execution/continuous_monitor/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SHIM_PATH = Path(__file__).parents[2] / "scripts" / "verify_continuous_monitor.py"


def test_shim_exists() -> None:
    """The operator-facing shim at ``scripts/verify_continuous_monitor.py`` exists."""
    assert _SHIM_PATH.exists(), f"Shim not found at {_SHIM_PATH}"


def test_main_returns_zero_with_all_scenarios_passing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main([])`` runs the documented scenarios against in-memory fixtures
    and returns 0 with ``RESULT: PASS`` on stdout."""
    from alphamind.scripts.verify_continuous_monitor import main

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0, f"expected exit 0; got {rc}. stdout:\n{out}"
    assert "RESULT: PASS" in out


def test_main_returns_nonzero_when_a_scenario_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force one scenario to fail (the cascade-dispatcher path) and assert
    ``main()`` returns non-zero with ``RESULT: FAIL`` plus the failing
    scenario's label surfaced on stdout."""
    import alphamind.scripts.verify_continuous_monitor as module

    async def _failing_scenario() -> module.ScenarioResult:
        return module.ScenarioResult(
            label="(g) Immediate-action breach dispatch",
            ok=False,
            detail="forced failure for runner aggregation test",
        )

    monkeypatch.setattr(module, "run_scenario_g_immediate_breach_dispatch", _failing_scenario)

    rc = module.main([])
    out = capsys.readouterr().out
    assert rc != 0, "expected non-zero exit on forced FAIL"
    assert "RESULT: FAIL" in out
    assert "(g)" in out
    assert "forced failure" in out
