"""Package-layout guarantees for the feedback_loop scaffolding (ALP-872).

The later stories (05/06*/07*/08*) import these subpackages; the foundation must
ship them as real (importable, empty) namespaces, not NotImplementedError stubs.
"""

from __future__ import annotations

import importlib

import pytest

_SUBPACKAGES = ("metrics", "citation", "digest", "validation", "retrospective")
_REMOVED = ("discovery", "analytics")


def test_top_level_package_imports() -> None:
    module = importlib.import_module("alphamind.feedback_loop")
    assert hasattr(module, "__path__"), "feedback_loop must be a package"


@pytest.mark.parametrize("name", _SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    module = importlib.import_module(f"alphamind.feedback_loop.{name}")
    assert hasattr(module, "__path__"), f"{name} must be a package"


@pytest.mark.parametrize("name", _REMOVED)
def test_removed_placeholders_are_gone(name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"alphamind.feedback_loop.{name}")
