"""Structural invariants for the decomposed ``command_execution/`` package (story 06c).

The package was carved out of a single 2.3k-LOC monolith (``fill_collection.py`` /
``command_execution/``)
into one file per OMS command kind (OPEN / CLOSE / ADJUST / CANCEL / ADD)
plus ``_shared.py``. These tests pin the layout so a future contributor
cannot silently re-introduce the regressions the decomposition fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COMMAND_EXECUTION = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "alphamind"
    / "execution"
    / "write_paths"
    / "command_execution"
)


def test_command_execution_is_a_package_with_prescribed_files() -> None:
    """command_execution/ has the files prescribed by ALP-465, plus reprice.py (ALP-740) and
    atomic.py (ALP-836).

    ALP-740 added ``reprice.py`` — the non-terminal entry-window reprice
    writeback that contrasts with the terminal ``cancel.py``. ALP-836 added
    ``atomic.py`` — the atomicity-first per-command pre-commit / backfill /
    teardown the broker-active dispatch loop interleaves with broker submission.
    """
    assert COMMAND_EXECUTION.is_dir()
    names = sorted(p.name for p in COMMAND_EXECUTION.iterdir() if p.suffix == ".py")
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
    from alphamind.execution.write_paths.command_execution import (
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
        f"alphamind.execution.write_paths.command_execution.{module_name}",
        fromlist=["*"],
    )
    writeback = getattr(module, f"_writeback_{module_name}")
    assert callable(writeback)


def test_no_function_local_imports_of_alphamind_inside_command_execution() -> None:
    """Acceptance criterion: no inline / function-local ``from alphamind.``
    imports anywhere in ``command_execution/``; all imports live at the top of the file.

    The original monolith carried function-local imports as cycle workarounds
    around the decision ↔ execution cycle. Story 02b broke that cycle; the
    workarounds are unnecessary now and would re-establish import-time fragility.
    """
    indented_alphamind_import = re.compile(r"^[ \t]+from alphamind\.")
    offenders: list[tuple[Path, int, str]] = []
    for py in COMMAND_EXECUTION.glob("*.py"):
        for lineno, raw in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if indented_alphamind_import.match(raw):
                offenders.append((py, lineno, raw))
    msg = "Function-local alphamind imports re-introduced in command_execution/: " + ", ".join(
        f"{p.name}:{n} → {line.strip()}" for p, n, line in offenders
    )
    assert not offenders, msg
