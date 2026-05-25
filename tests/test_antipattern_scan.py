"""Regression tests for ``.claude/skills/python-architecture/scripts/antipattern_scan.py``.

The scanner's L9 (internal Pydantic) boundary-directory filter used a
forward-slash ``split("/")`` against the stringified absolute path, which
silently broke on Windows where ``str(Path)`` returns backslash-separated
paths. Surfaced when PR #170 first ran the full suite on a Windows runner
and the L9 count jumped from 193 (POSIX) to 264 (Windows) because every
``BaseModel`` under a boundary directory was incorrectly flagged.

These tests pin the function-level contract: regardless of how the caller
stringifies the absolute path, files under a boundary directory must not
be flagged as L9.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

SCANNER_PATH = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "python-architecture"
    / "scripts"
    / "antipattern_scan.py"
)


def _load_scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("antipattern_scan", SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load scanner spec from {SCANNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["antipattern_scan"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scanner() -> ModuleType:
    return _load_scanner()


_BASEMODEL_SRC = "from pydantic import BaseModel\nclass Foo(BaseModel):\n    x: int = 0\n"


@pytest.mark.parametrize(
    "abs_path",
    [
        pytest.param(
            "/home/runner/work/AlphaMind/src/alphamind/config/models.py",
            id="posix-style",
        ),
        pytest.param(
            r"C:\Users\runner\work\AlphaMind\src\alphamind\config\models.py",
            id="windows-style",
        ),
        pytest.param(
            r"C:/Users/runner/work/AlphaMind/src/alphamind/config/models.py",
            id="windows-style-forward-slashes",
        ),
    ],
)
def test_scan_pydantic_internal_skips_boundary_dirs(scanner: ModuleType, abs_path: str) -> None:
    """BaseModel under a boundary dir must not be flagged regardless of path separator."""
    tree = ast.parse(_BASEMODEL_SRC)
    findings = scanner.scan_pydantic_internal(tree, _BASEMODEL_SRC, "config/models.py", abs_path)
    assert findings == [], (
        f"BaseModel under boundary dir 'config' should not be L9-flagged for "
        f"{abs_path!r}; got {findings}"
    )


@pytest.mark.parametrize(
    "abs_path",
    [
        pytest.param(
            "/home/runner/work/AlphaMind/src/alphamind/decision/portfolio_manager/types.py",
            id="posix-style",
        ),
        pytest.param(
            r"C:\Users\runner\work\AlphaMind\src\alphamind\decision\portfolio_manager\types.py",
            id="windows-style",
        ),
    ],
)
def test_scan_pydantic_internal_flags_internal_basemodel(
    scanner: ModuleType, abs_path: str
) -> None:
    """BaseModel outside boundary dirs must be flagged on both POSIX and Windows paths."""
    tree = ast.parse(_BASEMODEL_SRC)
    findings = scanner.scan_pydantic_internal(
        tree, _BASEMODEL_SRC, "decision/portfolio_manager/types.py", abs_path
    )
    assert len(findings) == 1, (
        f"Internal BaseModel 'Foo' should be L9-flagged for {abs_path!r}; got {findings}"
    )
    assert findings[0]["antipattern_id"] == "L9"
    assert cast(str, findings[0]["note"]).startswith("Pydantic BaseModel 'Foo'")


# ---------------------------------------------------------------------------
# L25 (print in non-CLI) — the prior `"/cli/" in path` substring check missed
# top-level ``cli/`` and ``scripts/`` directories (no leading slash in ``rel``)
# and broke entirely on Windows backslash paths.
# ---------------------------------------------------------------------------


_PRINT_SRC = "def main():\n    print('hello')\n"


@pytest.mark.parametrize(
    "rel_path",
    [
        pytest.param("cli/main.py", id="top-level-cli"),
        pytest.param("scripts/backfill.py", id="top-level-scripts"),
        pytest.param("distillation/cli/main.py", id="nested-cli"),
        pytest.param("foo/__main__.py", id="dunder-main"),
        pytest.param(r"cli\main.py", id="windows-top-level-cli"),
        pytest.param(r"scripts\backfill.py", id="windows-top-level-scripts"),
        pytest.param(r"distillation\cli\main.py", id="windows-nested-cli"),
    ],
)
def test_scan_print_skips_cli_and_scripts(scanner: ModuleType, rel_path: str) -> None:
    """print() calls under cli/, scripts/, or in __main__.py must not be flagged."""
    tree = ast.parse(_PRINT_SRC)
    findings = scanner.scan_print(tree, _PRINT_SRC, rel_path)
    assert findings == [], (
        f"print() under CLI-ish path {rel_path!r} should not be L25-flagged; got {findings}"
    )


@pytest.mark.parametrize(
    "rel_path",
    [
        pytest.param("decision/pm.py", id="decision-module"),
        pytest.param("distillation/replay_harness/cli.py", id="file-named-cli-py"),
        pytest.param(r"decision\pm.py", id="windows-decision-module"),
    ],
)
def test_scan_print_flags_non_cli_directories(scanner: ModuleType, rel_path: str) -> None:
    """print() outside cli/scripts directories must be flagged (file named cli.py is NOT exempt)."""
    tree = ast.parse(_PRINT_SRC)
    findings = scanner.scan_print(tree, _PRINT_SRC, rel_path)
    assert len(findings) == 1, (
        f"print() in non-CLI module {rel_path!r} should produce one L25 finding; got {findings}"
    )
    assert findings[0]["antipattern_id"] == "L25"
