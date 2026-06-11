"""Tests for ``scripts/ops/reconcile_alpaca_state.py`` (ALP-818).

The operator helper compares the live Alpaca paper account against local
SQLite portfolio state and prints a ``<<< DIVERGENCE`` flag per symbol. The
bug under test: it compared a *signed* Alpaca quantity (``-6`` for a short)
against an *unsigned* local share magnitude (``6``), so every open short
tripped a false divergence. The fix signs the local quantity by position
``direction`` before comparing.

The script lives in repo-root ``scripts/`` (not an importable package), so it
is loaded by file path. The Alpaca position objects are faked at the broker
boundary (a sanctioned mock point); the local state is a plain dict matching
``load_local``'s shape.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "reconcile_alpaca_state.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("reconcile_alpaca_state", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RECON = _load_script()
_report_positions = _RECON._report_positions


@dataclass(frozen=True)
class _FakeAlpacaPos:
    """Stand-in for an Alpaca ``Position`` (broker boundary).

    Mirrors the string-typed quantity fields the real client returns; the
    script does ``float(a.qty)`` for the comparison and prints the rest.
    """

    qty: str
    avg_entry_price: str = "100.0"
    unrealized_pl: str = "0.0"


def _local(legs: list[tuple[str, str, str | None, float]]) -> dict[str, Any]:
    """Build the ``load_local``-shaped dict from ``(sym, status, dir, shares)`` legs."""
    positions = {
        f"POS-{i}": {"sym": sym, "status": status, "dir": direction, "shares": shares}
        for i, (sym, status, direction, shares) in enumerate(legs)
    }
    return {"orders": {}, "positions": positions, "cash": {}}


def _line_for(symbol: str, output: str) -> str:
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == symbol:  # exact symbol, not a prefix of another
            return line
    raise AssertionError(f"no position line for {symbol!r} in:\n{output}")


@pytest.mark.parametrize(
    ("alpaca_qty", "leg", "expect_divergence"),
    [
        # Open short: Alpaca -6 vs local SHORT 6 — the same position. Must NOT flag
        # (this is the bug: unsigned compare gave abs(-6 - 6) = 12).
        pytest.param("-6", ("OPEN", "SHORT", 6.0), False, id="short_reconciles_clean"),
        # Open long: Alpaca 50 vs local LONG 50 — regression guard, must stay clean.
        pytest.param("50", ("OPEN", "LONG", 50.0), False, id="long_reconciles_clean"),
        # Direction divergence: Alpaca short -6 vs local LONG 6 — genuinely wrong, must flag.
        pytest.param("-6", ("OPEN", "LONG", 6.0), True, id="direction_mismatch_flags"),
        # Magnitude divergence: Alpaca 50 vs local LONG 40 — must still flag.
        pytest.param("50", ("OPEN", "LONG", 40.0), True, id="magnitude_mismatch_flags"),
        # NULL direction (a strategy/legacy row) signs as long-like (positive), so
        # Alpaca +6 reconciles clean — exercises the `or ""` fallback in _signed_shares.
        pytest.param("6", ("OPEN", None, 6.0), False, id="null_direction_treated_long"),
    ],
)
def test_report_positions_signs_local_quantity(
    alpaca_qty: str,
    leg: tuple[str, str | None, float],
    expect_divergence: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, direction, shares = leg
    apos: dict[str, Any] = {"SYM": _FakeAlpacaPos(qty=alpaca_qty)}
    local = _local([("SYM", status, direction, shares)])

    _report_positions(apos, local)

    line = _line_for("SYM", capsys.readouterr().out)
    assert ("<<< DIVERGENCE" in line) is expect_divergence


def test_cancelled_leg_excluded_from_comparison(capsys: pytest.CaptureFixture[str]) -> None:
    # Alpaca flat (no position), local CANCELLED SHORT 6 — cancelled legs are ignored,
    # so signed local nets to 0 and the row must not flag.
    local = _local([("SYM", "CANCELLED", "SHORT", 6.0)])

    _report_positions({}, local)

    line = _line_for("SYM", capsys.readouterr().out)
    assert "<<< DIVERGENCE" not in line


def test_leg_label_renders_status_and_direction(capsys: pytest.CaptureFixture[str]) -> None:
    # The local side is self-describing — status/direction/magnitude — so the printed row
    # explains itself next to Alpaca's signed qty (e.g. `alpaca=-6 ... local=[OPEN/SHORT:6.0sh]`).
    local = _local([("SYM", "OPEN", "SHORT", 6.0)])

    _report_positions({"SYM": _FakeAlpacaPos(qty="-6")}, local)

    assert "local=[OPEN/SHORT:6.0sh]" in _line_for("SYM", capsys.readouterr().out)
