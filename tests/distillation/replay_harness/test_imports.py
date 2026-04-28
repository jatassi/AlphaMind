"""Smoke test: importing the replay_harness package and its cli module succeeds."""

from __future__ import annotations

import importlib


def test_package_imports() -> None:
    module = importlib.import_module("alphamind.distillation.replay_harness")
    assert module is not None


def test_cli_module_imports() -> None:
    module = importlib.import_module("alphamind.distillation.replay_harness.cli")
    assert module is not None
