"""Verify HARNESS_VERSION is a non-empty string and re-exported from the package."""

from __future__ import annotations

from alphamind.distillation.replay_harness import HARNESS_VERSION as PKG_HARNESS_VERSION
from alphamind.distillation.replay_harness.version import HARNESS_VERSION


def test_harness_version_is_non_empty_string() -> None:
    assert isinstance(HARNESS_VERSION, str)
    assert HARNESS_VERSION


def test_package_reexport_matches_module_constant() -> None:
    assert PKG_HARNESS_VERSION == HARNESS_VERSION
