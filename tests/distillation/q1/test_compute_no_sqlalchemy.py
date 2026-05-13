"""Static check: every q*/<sub>_compute.py module is sqlalchemy-free.

Implements the verification gate from ALP-467: "grep -rn 'from sqlalchemy\\|session.'
src/alphamind/distillation/q1/*_compute.py src/alphamind/distillation/q3/*_compute.py
returns zero hits". The check is structural — if any current or future
compute module accidentally imports the ORM, the test fails and the bug
surfaces immediately rather than waiting for the next lint-imports run.

The import-linter contract ``distillation-compute-no-sqlalchemy`` codifies
the same invariant for CI; this test gives a fast in-pytest readback that
runs in milliseconds without invoking the linter's full graph build.

The check inspects AST-level imports rather than raw text so docstring
mentions of "sqlalchemy" (in module docstrings explaining the convention)
do not produce false positives.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2].parent
_COMPUTE_GLOBS = (
    "src/alphamind/distillation/q1/*_compute.py",
    "src/alphamind/distillation/q3/*_compute.py",
)


def _gather_compute_modules() -> list[Path]:
    paths: list[Path] = []
    for pattern in _COMPUTE_GLOBS:
        paths.extend(_REPO_ROOT.glob(pattern))
    return sorted(paths)


def _imported_module_names(source: str) -> set[str]:
    """Return every top-level imported module name in ``source`` via AST."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module.split(".", maxsplit=1)[0])
    return names


@pytest.mark.parametrize("module_path", _gather_compute_modules(), ids=lambda p: p.name)
def test_compute_module_has_no_sqlalchemy_edges(module_path: Path) -> None:
    """Each ``*_compute.py`` must not import sqlalchemy in any form."""
    source = module_path.read_text()
    imported = _imported_module_names(source)
    assert "sqlalchemy" not in imported, (
        f"{module_path.relative_to(_REPO_ROOT)} imports sqlalchemy — "
        "compute modules must consume the DistillationRepository protocol "
        "instead of the ORM directly."
    )
    # The legacy assemble-time helpers named their session parameter ``session``
    # so a compute module that references ``session.execute`` / ``session.add``
    # is likely smuggling in IO. Scan the AST for ``session`` attribute access.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "session"
        ):
            pytest.fail(
                f"{module_path.relative_to(_REPO_ROOT)} accesses ``session.{node.attr}`` "
                "— compute modules must operate on frozen inputs only."
            )


def test_at_least_one_compute_module_exists() -> None:
    """Sanity guard so a missing-pattern bug doesn't silently pass the matrix."""
    assert _gather_compute_modules(), (
        "Expected at least one ``*_compute.py`` under distillation/q1/ or "
        "distillation/q3/; the pilot must produce one before the test passes."
    )
