"""Public-surface re-export contract for the ``submit_envelope`` package — ALP-464.

After story 06b decomposes the single-file ``submit_envelope.py`` into a five-
submodule package, every symbol pre-existing consumers (PM harness, PM runner,
state-persistence tests, etc.) imported from
``alphamind.decision.portfolio_manager.submit_envelope`` must remain importable
from the same path via the package's ``__init__.py`` re-export.

These tests assert the curated public surface stays unchanged across the
refactor; they belong to the structural contract, not behavior, so they live
adjacent to the package they pin (rather than in ``test_submit_envelope_mcp``
which exercises behavior).
"""

from __future__ import annotations

import importlib


def test_init_reexports_public_surface() -> None:
    """Every previously-importable public symbol is reachable from the package."""
    module = importlib.import_module("alphamind.decision.portfolio_manager.submit_envelope")
    expected = {
        "Acknowledgment",
        "FailedSubmissionEntry",
        "RejectionPayload",
        "SubmissionLogEntry",
        "SubmissionResult",
        "SubmitEnvelopeState",
        "build_initial_submit_envelope_state",
        "build_submit_envelope_mcp_server",
        "get_failed_submission_log",
        "get_submission_log",
    }
    missing = sorted(name for name in expected if not hasattr(module, name))
    assert not missing, f"missing public re-exports: {missing}"


def test_init_reexports_private_handler_and_helpers() -> None:
    """Tests still import a handful of underscore-prefixed helpers; preserve them.

    ``_handle_submit_envelope`` is exercised directly by the phase-2 write-path
    and broker-routing test suites; ``_validate_envelope_payload`` by the
    state-persistence verify script; ``_adjust_command_context`` by the
    engine-stub broker-routing tests. Keeping them importable from the package
    root avoids touching unrelated test modules in this refactor.
    """
    module = importlib.import_module("alphamind.decision.portfolio_manager.submit_envelope")
    expected = {
        "_handle_submit_envelope",
        "_validate_envelope_payload",
        "_adjust_command_context",
    }
    missing = sorted(name for name in expected if not hasattr(module, name))
    assert not missing, f"missing private-but-tested re-exports: {missing}"


def test_submodules_are_focused() -> None:
    """Each submodule imports cleanly and exposes its declared subsystem."""
    types_mod = importlib.import_module(
        "alphamind.decision.portfolio_manager.submit_envelope.types"
    )
    process_mod = importlib.import_module(
        "alphamind.decision.portfolio_manager.submit_envelope.process"
    )
    dispatch_mod = importlib.import_module(
        "alphamind.decision.portfolio_manager.submit_envelope.dispatch"
    )
    persist_mod = importlib.import_module(
        "alphamind.decision.portfolio_manager.submit_envelope.persist"
    )
    server_mod = importlib.import_module(
        "alphamind.decision.portfolio_manager.submit_envelope.server"
    )

    # types.py owns the state cell + Pydantic boundary types.
    assert hasattr(types_mod, "SubmitEnvelopeState")
    assert hasattr(types_mod, "SubmissionLogEntry")
    assert hasattr(types_mod, "FailedSubmissionEntry")

    # process.py owns the per-command pipeline.
    assert hasattr(process_mod, "_process_commands")
    assert hasattr(process_mod, "_process_one_command")

    # dispatch.py owns broker routing.
    assert hasattr(dispatch_mod, "_route_through_broker")
    assert hasattr(dispatch_mod, "_adjust_command_context")

    # persist.py owns the phase-2 persistence helpers and imports phase2 at top.
    assert hasattr(persist_mod, "_persist_envelope_outcome_via_phase2")
    assert hasattr(persist_mod, "_persist_envelope_parse_failure_via_phase2")

    # server.py owns the MCP factory.
    assert hasattr(server_mod, "build_submit_envelope_mcp_server")
    assert hasattr(server_mod, "_handle_submit_envelope")
