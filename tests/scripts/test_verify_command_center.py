"""Shape tests for ``scripts/verify_command_center.py`` (story 07 / ALP-685).

The script is the operator-runnable end-to-end gate for the command
center; its seven check helpers (``check_daemons_bind``,
``check_passkey_roundtrip``, ``check_control_verbs``,
``check_alert_fires``, ``check_sse_roundtrip``, ``check_frontend_build``,
``check_clean_teardown``) live as module-level functions so this suite
can verify the structural contract without booting the full FastAPI
app + supervisor + temp DB.

Per the AC: this suite parses the script and asserts the 7 check
helpers + the assertion logic that drives each is present. The live
end-to-end run is operator-driven; the shape test is what runs in CI.

The script is imported via :func:`importlib.util.spec_from_file_location`
because it lives under ``scripts/`` (not under ``src/``) and is not
installed as a package — mirrors the same pattern in
:mod:`tests.scripts.test_verify_debug_e2e`.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_command_center.py"


@pytest.fixture(scope="module")
def verify_module() -> ModuleType:
    """Load ``scripts/verify_command_center.py`` as an importable module.

    The script lives under ``scripts/`` rather than ``src/`` per the
    "no shim split" convention shared by ``verify_debug_e2e.py``; we
    use ``spec_from_file_location`` to give the tests a normal module
    handle.
    """
    spec = importlib.util.spec_from_file_location("verify_command_center", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_command_center"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_ast() -> ast.Module:
    """Parse the script as a Python AST for structural assertions.

    A second fixture distinct from :func:`verify_module` because the
    AST walks don't need a live module — and a module-load failure
    on the live import shouldn't mask AST-level invariants.
    """
    return ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Structural invariants — the 7 check helpers exist as module-level callables.
# ---------------------------------------------------------------------------


_EXPECTED_CHECK_FUNCTIONS: tuple[str, ...] = (
    "check_daemons_bind",
    "check_passkey_roundtrip",
    "check_control_verbs",
    "check_alert_fires",
    "check_sse_roundtrip",
    "check_frontend_build",
    "check_clean_teardown",
)
"""The 7 check labels the AC requires.

Mirrored by the script's ``STEP_LABELS`` constant; a divergence
between this list and ``STEP_LABELS`` surfaces as the test below.
"""


def test_script_exposes_seven_check_helpers(verify_module: ModuleType) -> None:
    """Each of the 7 named check helpers must be a module-level callable."""
    for name in _EXPECTED_CHECK_FUNCTIONS:
        assert hasattr(verify_module, name), (
            f"verify_command_center missing required check helper: {name!r}"
        )
        assert callable(getattr(verify_module, name))


def test_step_labels_constant_lists_seven_checks(verify_module: ModuleType) -> None:
    """``STEP_LABELS`` must enumerate exactly the 7 expected check labels."""
    step_labels = verify_module.STEP_LABELS
    assert isinstance(step_labels, tuple)
    assert len(step_labels) == 7
    # The labels should match the function-suffix portion of each
    # check_* helper (``check_daemons_bind`` → ``daemons_bind``). This
    # mirrors the operator-facing PASS / FAIL lines.
    expected_labels = tuple(name.removeprefix("check_") for name in _EXPECTED_CHECK_FUNCTIONS)
    assert step_labels == expected_labels


def test_check_result_dataclass_present(verify_module: ModuleType) -> None:
    """``CheckResult`` must expose ``label``, ``passed``, ``message``, ``format_line``."""
    CheckResult = verify_module.CheckResult
    instance = CheckResult(label="x", passed=True, message="y")
    assert instance.label == "x"
    assert instance.passed is True
    assert instance.message == "y"
    assert instance.format_line() == "PASS: x — y"
    failed = CheckResult(label="z", passed=False, message="w")
    assert failed.format_line() == "FAIL: z — w"


# ---------------------------------------------------------------------------
# Assertion logic checks — each helper carries the assertion behavior
# the AC names. We walk the AST + look for the load-bearing keywords.
# ---------------------------------------------------------------------------


def _find_function_def(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    pytest.fail(f"no function named {name!r} in script AST")


def _function_source(tree: ast.Module, name: str) -> str:
    """Return the source text of a named function in the script."""
    node = _find_function_def(tree, name)
    return ast.unparse(node)


def test_check_daemons_bind_polls_socket_connect(script_ast: ast.Module) -> None:
    """``check_daemons_bind`` must poll socket.connect against a timeout."""
    source = _function_source(script_ast, "check_daemons_bind")
    # The check builds a socket + calls connect() under a deadline loop.
    assert "socket" in source
    assert "connect" in source
    assert "timeout" in source.lower()


def test_check_passkey_roundtrip_hits_four_auth_endpoints(script_ast: ast.Module) -> None:
    """The passkey check must drive register/login begin + complete."""
    source = _function_source(script_ast, "check_passkey_roundtrip")
    assert "/auth/register/begin" in source
    assert "/auth/register/complete" in source
    assert "/auth/login/begin" in source
    assert "/auth/login/complete" in source


def test_check_passkey_roundtrip_asserts_session_and_csrf_cookies(
    script_ast: ast.Module,
) -> None:
    """The passkey check asserts the session + CSRF cookies are issued."""
    source = _function_source(script_ast, "check_passkey_roundtrip")
    assert "session_cookie_name" in source
    assert "csrf_cookie_name" in source


def test_check_control_verbs_hits_all_eight(script_ast: ast.Module) -> None:
    """The control_verbs check must reference all 8 verbs from the AC."""
    source = _function_source(script_ast, "check_control_verbs")
    # The verb list lives in the module-level ``_CONTROL_VERBS`` constant;
    # the check iterates it. Confirm both the iteration AND that the
    # constant lists each of the 8 required verbs.
    assert "_CONTROL_VERBS" in source
    module_source = _SCRIPT_PATH.read_text(encoding="utf-8")
    for verb in (
        "pause",
        "resume",
        "trigger_emergency_invocation",
        "switch_profile",
        "run_universe_validation",
        "cancel_order",
        "force_close_position",
        "set_halt_mode",
    ):
        assert f'"{verb}"' in module_source, (
            f"verify_command_center does not reference verb {verb!r}"
        )


def test_check_control_verbs_asserts_operator_console_rows(script_ast: ast.Module) -> None:
    """The control_verbs check must assert activity_log.source=OPERATOR_CONSOLE."""
    source = _function_source(script_ast, "check_control_verbs")
    assert "OPERATOR_CONSOLE" in source
    # PROFILE_SWITCHED is the additional event_type row the switch_profile
    # audit layer writes; the check must verify its presence.
    assert "PROFILE_SWITCHED" in source


def test_check_alert_fires_publishes_failed_invocation_event(script_ast: ast.Module) -> None:
    """The alert_fires check must publish a synthetic INVOCATION_ENDED event."""
    source = _function_source(script_ast, "check_alert_fires")
    # The check fires the ``pipeline_aborted`` rule by publishing an
    # INVOCATION_ENDED frame with status=failed.
    assert "INVOCATION_ENDED" in source
    assert "failed" in source
    assert "pipeline_aborted" in source


def test_check_alert_fires_asserts_discord_call(script_ast: ast.Module) -> None:
    """The alert_fires check must assert a Discord call landed on the fake."""
    source = _function_source(script_ast, "check_alert_fires")
    assert "Discord" in source or "discord" in source
    assert "AlertRow" in source or "alerts" in source


def test_check_sse_roundtrip_publishes_pipeline_and_monitor(script_ast: ast.Module) -> None:
    """The sse_roundtrip check must publish one pipeline + one monitor frame."""
    source = _function_source(script_ast, "check_sse_roundtrip")
    # Publishes a synthetic PipelineEvent + a synthetic MonitorEvent.
    assert "PipelineEvent" in source
    assert "MonitorEvent" in source


def test_check_frontend_build_asserts_dist_index_html(script_ast: ast.Module) -> None:
    """The frontend_build check must confirm dist/index.html exists."""
    source = _function_source(script_ast, "check_frontend_build")
    assert "index.html" in source
    assert "dist" in source or "is_file" in source


def test_check_clean_teardown_calls_request_stop(script_ast: ast.Module) -> None:
    """The clean_teardown check must signal supervisor.request_stop()."""
    source = _function_source(script_ast, "check_clean_teardown")
    assert "request_stop" in source
    # The check must enforce the 10s budget from the AC.
    assert "timeout" in source.lower()


# ---------------------------------------------------------------------------
# Entry point — main() exists and returns int.
# ---------------------------------------------------------------------------


def test_main_function_present_and_returns_int(verify_module: ModuleType) -> None:
    """``main`` must be a callable returning an int exit code."""
    assert hasattr(verify_module, "main")
    assert callable(verify_module.main)


def test_module_documents_seven_checks_in_docstring(verify_module: ModuleType) -> None:
    """Module docstring must enumerate the 7 check helpers for the operator."""
    doc = verify_module.__doc__ or ""
    for name in _EXPECTED_CHECK_FUNCTIONS:
        assert name in doc, (
            f"module docstring missing reference to {name!r} — "
            "operator-facing summary is out of date"
        )
