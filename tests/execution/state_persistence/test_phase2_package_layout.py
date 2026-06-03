"""Structural invariants for the decomposed ``phase2/`` package (story 06c).

The package was carved out of a single 2.3k-LOC ``phase2.py`` god module
into one file per OMS command kind (OPEN / CLOSE / ADJUST / CANCEL / ADD)
plus ``_shared.py``. These tests pin the layout so a future contributor
cannot silently re-introduce the regressions the decomposition fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PHASE2 = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "alphamind"
    / "execution"
    / "write_paths"
    / "phase2"
)


def test_phase2_is_a_package_with_prescribed_files() -> None:
    """phase2/ has the files prescribed by ALP-465, plus reprice.py (ALP-740) and
    atomic.py (ALP-836).

    ALP-740 added ``reprice.py`` — the non-terminal entry-window reprice
    writeback that contrasts with the terminal ``cancel.py``. ALP-836 added
    ``atomic.py`` — the atomicity-first per-command pre-commit / backfill /
    teardown the broker-active dispatch loop interleaves with broker submission.
    """
    assert PHASE2.is_dir()
    names = sorted(p.name for p in PHASE2.iterdir() if p.suffix == ".py")
    assert names == [
        "__init__.py",
        "_shared.py",
        "add.py",
        "adjust.py",
        "atomic.py",
        "cancel.py",
        "close.py",
        "open.py",
        "reprice.py",
    ]


def test_public_entrypoints_are_importable_from_the_package_root() -> None:
    """``submit_envelope/persist.py`` and friends import these by name."""
    from alphamind.execution.write_paths.phase2 import (
        persist_command_abandoned,
        persist_engine_envelope_outcome,
        persist_envelope_outcome,
        persist_envelope_parse_failure,
        persist_envelope_rejection,
    )

    assert callable(persist_envelope_outcome)
    assert callable(persist_envelope_parse_failure)
    assert callable(persist_envelope_rejection)
    assert callable(persist_engine_envelope_outcome)
    assert callable(persist_command_abandoned)


@pytest.mark.parametrize(
    "module_name",
    ["open", "close", "adjust", "cancel", "add"],
)
def test_each_command_kind_module_exports_its_writeback(module_name: str) -> None:
    """Each command-kind submodule defines its writeback function."""
    module = __import__(
        f"alphamind.execution.write_paths.phase2.{module_name}",
        fromlist=["*"],
    )
    writeback = getattr(module, f"_writeback_{module_name}")
    assert callable(writeback)


def test_no_function_local_imports_of_alphamind_inside_phase2() -> None:
    """Acceptance criterion: no inline / function-local ``from alphamind.``
    imports anywhere in ``phase2/``; all imports live at the top of the file.

    The original monolith carried function-local imports as cycle workarounds
    around the decision ↔ execution cycle. Story 02b broke that cycle; the
    workarounds are unnecessary now and would re-establish import-time fragility.
    """
    indented_alphamind_import = re.compile(r"^[ \t]+from alphamind\.")
    offenders: list[tuple[Path, int, str]] = []
    for py in PHASE2.glob("*.py"):
        for lineno, raw in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if indented_alphamind_import.match(raw):
                offenders.append((py, lineno, raw))
    assert not offenders, "Function-local alphamind imports re-introduced in phase2/: " + ", ".join(
        f"{p.name}:{n} → {line.strip()}" for p, n, line in offenders
    )
