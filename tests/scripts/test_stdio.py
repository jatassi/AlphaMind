"""Tests for the UTF-8 stdio bootstrap helper.

The helper is called at the top of every ``verify_*`` ``main()`` so the
scripts behave identically on Windows (cp1252 by default) and POSIX.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
from typing import Any

import pytest

from alphamind.scripts._stdio import configure_utf8_stdio


class _Recorder:
    """Test substitute for a text stream that captures reconfigure() calls."""

    def __init__(self) -> None:
        self.reconfigure_calls: list[dict[str, Any]] = []

    def reconfigure(self, **kwargs: Any) -> None:
        self.reconfigure_calls.append(kwargs)


def test_configure_utf8_stdio_reconfigures_both_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_stdout = _Recorder()
    fake_stderr = _Recorder()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_utf8_stdio()

    assert fake_stdout.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert fake_stderr.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_configure_utf8_stdio_skips_streams_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    configure_utf8_stdio()


_VERIFY_MODULES = (
    "verify_adaptive_researcher",
    "verify_analyst",
    "verify_bootstrap_calibration_mix",
    "verify_broker_adapter",
    "verify_continuous_monitor",
    "verify_corporate_actions",
    "verify_decision_pipeline",
    "verify_distillation",
    "verify_domain_researcher_failure_modes",
    "verify_domain_researchers",
    "verify_guardrail_enforcement",
    "verify_oms_commands",
    "verify_pipeline_scheduler",
    "verify_pm",
    "verify_proposal_pre_processor",
    "verify_qualitative_researcher",
    "verify_regime_transition",
    "verify_regt_margin_attribution",
    "verify_state_persistence",
    "verify_strategist",
    "verify_synthesizer",
)


@pytest.mark.parametrize("module_name", _VERIFY_MODULES)
def test_verify_main_calls_configure_utf8_stdio(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``verify_*`` ``main()`` must invoke the stdio bootstrap.

    ``main(["--help"])`` exits via :class:`SystemExit` after argparse
    finishes; running it with the bootstrap monkey-patched to a spy
    confirms the helper fires.
    """
    module = importlib.import_module(f"alphamind.scripts.{module_name}")
    calls: list[None] = []

    def _spy() -> None:
        calls.append(None)

    monkeypatch.setattr(f"alphamind.scripts.{module_name}.configure_utf8_stdio", _spy)

    with contextlib.suppress(SystemExit):
        module.main(["--help"])

    assert calls, f"{module_name}.main() did not call configure_utf8_stdio()"
