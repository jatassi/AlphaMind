"""Orchestrator provenance-root derivation for agent_calls telemetry (ALP-907).

The scheduler derives the agent_calls provenance root from the loaded
``StatePersistenceConfig`` and threads it through the analysis + decision
pipelines into the subprocess worker's per-call telemetry session. These tests
pin the no-regression invariant: the derived value is the parent of
``invocation_provenance_root`` and equals
``distillation/orchestrator._default_provenance_root()`` so distillation's own
provenance artifacts continue to land at the same path.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from alphamind.distillation.orchestrator import _default_provenance_root
from alphamind.scheduler.orchestrator import _resolve_provenance_root
from alphamind.state.config import StatePersistenceConfig


def _config(invocation_provenance_root: str) -> StatePersistenceConfig:
    return StatePersistenceConfig(
        pm_decision_log_sliding_window_invocations=10,
        snapshot_read_timeout_seconds=5.0,
        pip_freeze_snapshot_root="%USERPROFILE%/AlphaMind/data/provenance/process_lifetimes",
        invocation_provenance_root=invocation_provenance_root,
    )


def test_resolved_root_equals_default_provenance_root_for_canonical_config() -> None:
    """The production config value (``%USERPROFILE%\\…\\data\\provenance\\invocations``)
    resolves to exactly ``_default_provenance_root()`` — so threading it through the
    pipelines leaves distillation provenance artifacts at the same path (no regression)."""
    config = _config(r"%USERPROFILE%\AlphaMind\data\provenance\invocations")
    assert _resolve_provenance_root(config) == _default_provenance_root()


def test_resolved_root_is_parent_of_expanded_invocation_provenance_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved root is the parent of the expanded
    ``invocation_provenance_root`` (the ``…/invocations`` segment dropped)."""
    monkeypatch.setenv("USERPROFILE", "/home/agent")
    config = _config(r"%USERPROFILE%\AlphaMind\data\provenance\invocations")

    resolved = _resolve_provenance_root(config)

    expanded = PureWindowsPath(r"/home/agent\AlphaMind\data\provenance\invocations")
    assert resolved == Path(expanded.parent)
    assert resolved.name == "provenance"
    assert resolved == Path("/home/agent/AlphaMind/data/provenance")
