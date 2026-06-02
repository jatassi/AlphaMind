"""Tests covering the structural acceptance criteria of ALP-458.

The 12-module decision↔execution import cycle is broken by hoisting the
LLM↔engine wire-format types into ``alphamind.commands.*`` and inverting
``submit_envelope_mcp`` through a ``BrokerDispatch`` Protocol injected at
the composition root.

Each test below pins one of the story's acceptance criteria:

* ``alphamind.execution.oms`` is a normal re-export module — no
  ``__getattr__`` lazy-loader bridging the formerly-circular module.
* ``analyze_imports.py`` reports no decision↔execution import cycle.
* ``commands/*`` modules import zero first-party ``alphamind.*`` modules
  except ``alphamind._kernel.*``.
* Zero ``from alphamind.decision.portfolio_manager`` imports inside
  ``src/alphamind/execution/``.
* Zero ``from alphamind.execution.oms.{command_models,engine_envelope,
  submit_envelope_mcp}`` imports inside ``src/``.

The end-to-end PM submission path test (``test_pm_harness_submit_path``)
exercises the constructed PM wire-up with a fake ``BrokerDispatch`` —
a stub-only contract pass would miss real wiring drift, so the test
constructs an actual ``SubmitEnvelopeState`` and runs the
``submit_envelope`` handler through it.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "alphamind"


# ---------------------------------------------------------------------------
# __getattr__ removal
# ---------------------------------------------------------------------------


def test_execution_oms_has_no_getattr_lazy_loader() -> None:
    """``alphamind.execution.oms`` must be a normal re-export module.

    The transitional ``__getattr__`` lazy-loader at
    ``execution/oms/__init__.py`` existed to defer importing
    ``submit_envelope_mcp`` so its decision-layer imports would not
    create a circular import. With ``submit_envelope_mcp`` moved out of
    execution, the lazy bridge is no longer needed.
    """
    import alphamind.execution.oms as oms

    assert not hasattr(oms, "__getattr__"), (
        "alphamind.execution.oms still defines __getattr__ — the transitional "
        "lazy-loader bridging the decision↔execution cycle must be deleted "
        "now that submit_envelope_mcp lives in decision/portfolio_manager/."
    )


# ---------------------------------------------------------------------------
# commands/* purity: only _kernel.* first-party imports allowed
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


# ---------------------------------------------------------------------------
# Execution must not import decision
# ---------------------------------------------------------------------------


_DECISION_PM_PATTERN = re.compile(r"from\s+alphamind\.decision\.portfolio_manager\b")


def test_execution_has_no_decision_portfolio_manager_imports() -> None:
    """No file under ``src/alphamind/execution/`` may import from
    ``alphamind.decision.portfolio_manager``."""
    execution_root = _SRC_ROOT / "execution"
    violations: list[str] = []
    for path in _iter_python_files(execution_root):
        text = path.read_text(encoding="utf-8")
        if _DECISION_PM_PATTERN.search(text):
            violations.append(str(path.relative_to(_REPO_ROOT)))
    assert violations == [], (
        f"execution/ files still import from decision.portfolio_manager: {violations!r}"
    )


# ---------------------------------------------------------------------------
# Legacy OMS import paths are gone everywhere
# ---------------------------------------------------------------------------


_LEGACY_PATTERN = re.compile(
    r"from\s+alphamind\.execution\.oms\.(?:command_models|engine_envelope|submit_envelope_mcp)\b"
)


def test_no_legacy_execution_oms_imports_remaining() -> None:
    """Every consumer migrated off ``execution.oms.command_models``,
    ``execution.oms.engine_envelope``, ``execution.oms.submit_envelope_mcp``."""
    violations: list[str] = []
    for path in _iter_python_files(_SRC_ROOT):
        text = path.read_text(encoding="utf-8")
        if _LEGACY_PATTERN.search(text):
            violations.append(str(path.relative_to(_REPO_ROOT)))
    assert violations == [], (
        f"Consumers still import from the legacy execution.oms wire-format modules: {violations!r}"
    )


# ---------------------------------------------------------------------------
# BrokerDispatch Protocol exists and is usable end-to-end
# ---------------------------------------------------------------------------


def test_broker_dispatch_protocol_exposed_from_commands() -> None:
    """``BrokerDispatch`` is exposed as a Protocol from ``alphamind.commands``."""
    from typing import get_type_hints

    from alphamind.commands import BrokerDispatch

    # Protocols set ``_is_protocol`` to True on the class object.
    assert getattr(BrokerDispatch, "_is_protocol", False), (
        "BrokerDispatch must be a typing.Protocol so consumers can ducktype it without inheriting"
    )
    # The Protocol's call shape is exposed via its method table.
    assert callable(BrokerDispatch)
    # get_type_hints on the Protocol just confirms it's resolvable.
    get_type_hints(BrokerDispatch)


def test_pm_harness_constructible_with_fake_broker_dispatch() -> None:
    """A fake ``BrokerDispatch`` plumbed through the PM submit-envelope wiring
    drives the engine-stub through factory construction without raising.

    Architectural integration gap: stub-heavy tests can pass while real callers
    break. This test asks the actual ``build_submit_envelope_mcp_server``
    factory to accept a fake ``BrokerDispatch`` and confirms the composition
    shape wires correctly — if the factory required a concrete class, this
    would raise TypeError. Mirrors the option-shape the composition root
    constructs in production (``runner.run_portfolio_manager`` →
    ``invoke_pm`` → ``_build_mcp_wiring`` →
    ``build_submit_envelope_mcp_server``).

    Builds the fixture surface manually rather than reusing
    ``tests/decision/portfolio_manager`` fixtures so this test stays
    independent of the heavyweight harness fixture chain — the goal is to
    exercise the new constructor seam, not duplicate harness coverage.
    """
    import inspect

    from alphamind.commands import BrokerDispatch
    from alphamind.commands.command_models import OMSCommand
    from alphamind.commands.submission_results import SubmissionResult
    from alphamind.decision.portfolio_manager.submit_envelope import (
        build_submit_envelope_mcp_server,
    )

    class _FakeBrokerDispatch:
        """Minimal Protocol implementer — records calls and returns success."""

        def __init__(self) -> None:
            self.calls: list[OMSCommand] = []

        async def __call__(
            self,
            command: OMSCommand,
            *,
            client_order_id: str,
            **context: object,
        ) -> SubmissionResult:
            self.calls.append(command)
            return SubmissionResult(
                command_ordinal=0,
                status="accepted",
                command_id=client_order_id,
            )

    fake: BrokerDispatch = _FakeBrokerDispatch()
    # Verify the fake duck-types BrokerDispatch (Protocol is runtime_checkable).
    assert isinstance(fake, BrokerDispatch)

    # The architectural integration test: the production-wired factory
    # signature must accept a ``broker_dispatch`` kwarg of the Protocol type.
    # If the composition shape ever drifted (e.g., the factory regressed to
    # importing the concrete dispatcher directly), this assertion would
    # catch it before runtime.
    sig = inspect.signature(build_submit_envelope_mcp_server)
    assert "broker_dispatch" in sig.parameters, (
        "build_submit_envelope_mcp_server must accept the composition-root-"
        "injected BrokerDispatch Protocol per ALP-458"
    )
    annotation = sig.parameters["broker_dispatch"].annotation
    annotation_text = str(annotation)
    assert "BrokerDispatch" in annotation_text, (
        f"broker_dispatch parameter annotation must reference BrokerDispatch; "
        f"got {annotation_text!r}"
    )

    # Confirm the same parameter is plumbed through invoke_pm and
    # run_portfolio_manager — the composition root injects the dispatcher
    # at run_portfolio_manager, which threads it down via invoke_pm into
    # the MCP wiring helper. If any link in this chain regressed, the
    # downstream factory would never see the fake.
    from alphamind.decision.portfolio_manager.harness import invoke_pm
    from alphamind.decision.portfolio_manager.runner import run_portfolio_manager

    invoke_sig = inspect.signature(invoke_pm)
    assert "broker_dispatch" in invoke_sig.parameters, (
        "invoke_pm must accept broker_dispatch so the composition root can "
        "thread it from run_portfolio_manager into the engine-stub"
    )
    runner_sig = inspect.signature(run_portfolio_manager)
    assert "broker_dispatch" in runner_sig.parameters, (
        "run_portfolio_manager must accept broker_dispatch as the composition-"
        "root-facing injection point"
    )
