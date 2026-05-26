"""Tests for the ``--resume-from`` plumbing + ``check_deterministic_prefix``
in ``scripts/verify_debug_e2e.py`` (story ALP-696).

Exercises the four user-visible surfaces this story adds:

1. ``--resume-from`` argparse flag forwards verbatim to the subprocess CLI
   command line (story ALP-696 acceptance criterion 1).
2. ``check_deterministic_prefix`` PASSes when the source and new
   distillation directories hash byte-identical (criterion 2).
3. ``check_deterministic_prefix`` FAILs naming the first mismatched file
   on a divergence (criterion 3).
4. ``main()`` does NOT emit a ``deterministic_prefix`` line on a
   non-resume run; summary stays at ``7/7 checks passed`` (criterion 4).
5. ``main()`` emits the PASS line on a resume targeting ``pm`` (all
   upstream phases replayed); distillation comparison is unconditional
   on resume (criterion 5).

The script is loaded via ``importlib.util.spec_from_file_location`` —
same pattern as ``test_verify_debug_e2e.py`` — because it lives under
``scripts/`` not ``src/``.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_debug_e2e.py"


@pytest.fixture(scope="module")
def verify_module() -> ModuleType:
    """Load ``scripts/verify_debug_e2e.py`` as an importable module.

    Mirrors :func:`tests.scripts.test_verify_debug_e2e.verify_module`
    so the two suites can co-exist under the same conftest without
    fighting over the ``verify_debug_e2e`` entry in ``sys.modules``.
    """
    spec = importlib.util.spec_from_file_location("verify_debug_e2e", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_debug_e2e"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers — build a minimal date-partitioned archive layout for the check
# ---------------------------------------------------------------------------


def _seed_distillation(
    *,
    archive_root: Path,
    invocation_id: str,
    date_part: str = "2026-05-26",
    files: dict[str, bytes] | None = None,
) -> Path:
    """Create ``<archive_root>/<date>/<invocation_id>/distillation/`` with
    a stable set of files.

    Default file set mirrors the real distillation orchestrator's output
    shape (3 sector .md docs + correlation-regime brief + regime label)
    so the check exercises the same file-count + per-file naming the
    operator runbook describes.
    """
    distillation_dir = archive_root / date_part / invocation_id / "distillation"
    distillation_dir.mkdir(parents=True, exist_ok=True)
    payload = files or {
        "tech_semis_sector.md": b"# Tech semis sector\n\nseeded content\n",
        "financials_sector.md": b"# Financials sector\n\nseeded content\n",
        "energy_sector.md": b"# Energy sector\n\nseeded content\n",
        "correlation_regime_brief.md": b"# CR brief\n\nseeded content\n",
        "regime.md": b"# Regime\n\nseeded content\n",
    }
    for name, blob in payload.items():
        (distillation_dir / name).write_bytes(blob)
    return distillation_dir


# ---------------------------------------------------------------------------
# 1. --resume-from forwards to subprocess command line
# ---------------------------------------------------------------------------


def test_resume_from_forwards_verbatim_to_subprocess(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--resume-from <id>:<phase>`` lands in the subprocess command
    line verbatim (story ALP-696 § 1).

    The wrapper does no validation — the underlying CLI (ALP-693) is
    the source of truth on validity. So the test pins the EXACT
    positional shape: the ``--resume-from`` flag immediately followed
    by the value the operator passed.
    """
    captured: dict[str, Any] = {}

    def _stub_run(cmd: list[str], **_kwargs: Any) -> Any:
        captured["cmd"] = cmd

        class _Completed:
            returncode = 0
            stdout = '{"invocation_id": "inv-20260526T025612Z-a8a954ec"}'
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    args = verify_module._parse_args(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--resume-from",
            "inv-20260525T231625Z-b9f1a7aa:analyst",
        ]
    )
    result, _output = verify_module._drive_debug_e2e_subprocess(args)

    assert result.passed is True
    assert "--resume-from" in captured["cmd"]
    flag_idx = captured["cmd"].index("--resume-from")
    assert captured["cmd"][flag_idx + 1] == "inv-20260525T231625Z-b9f1a7aa:analyst"


def test_resume_from_omitted_when_not_set(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--resume-from`` on the verify wrapper, the subprocess
    command line carries no ``--resume-from`` token at all (parallels
    the ``--fresh-start`` omission test).
    """
    captured: dict[str, Any] = {}

    def _stub_run(cmd: list[str], **_kwargs: Any) -> Any:
        captured["cmd"] = cmd

        class _Completed:
            returncode = 0
            stdout = '{"invocation_id": "iid"}'
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    args = verify_module._parse_args(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )
    verify_module._drive_debug_e2e_subprocess(args)

    assert "--resume-from" not in captured["cmd"]


# ---------------------------------------------------------------------------
# 2. check_deterministic_prefix — PASS on byte-identical archives
# ---------------------------------------------------------------------------


def test_check_deterministic_prefix_passes_on_byte_identical(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """PASS when every distillation file in source has a byte-identical
    counterpart in the new archive (story ALP-696 § 2 — happy path).
    """
    source_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-source")
    new_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-new")

    result = verify_module.check_deterministic_prefix(
        source_distillation_dir=source_dir,
        new_distillation_dir=new_dir,
    )

    assert result.passed is True
    assert result.label == "deterministic_prefix"
    # Message names the file count + cumulative bytes; the operator reads
    # this to confirm the check actually ran something.
    assert "5 distillation file(s) byte-identical" in result.message


# ---------------------------------------------------------------------------
# 3. check_deterministic_prefix — FAIL naming the first mismatched file
# ---------------------------------------------------------------------------


def test_check_deterministic_prefix_fails_on_mutated_file(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL naming the first mismatched file when one distillation
    output diverges (story ALP-696 § 2 — mismatch arm).
    """
    source_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-source")
    new_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-new")
    # Mutate one file in the source archive so the source vs new
    # hashes diverge — the operator-runbook story arm: "someone edited
    # the synthetic portfolio fixture between runs".
    (source_dir / "energy_sector.md").write_bytes(b"# Energy sector\n\nMUTATED\n")

    result = verify_module.check_deterministic_prefix(
        source_distillation_dir=source_dir,
        new_distillation_dir=new_dir,
    )

    assert result.passed is False
    assert result.label == "deterministic_prefix"
    # First-mismatched-file naming is load-bearing for the runbook
    # triage row — the operator uses it to scope the regression search.
    assert "energy_sector.md" in result.message
    assert "differs" in result.message


def test_check_deterministic_prefix_fails_on_missing_file(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when a distillation file is present in source but absent
    from the new archive (a different shape of mismatch worth its own
    test arm).
    """
    source_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-source")
    new_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-new")
    (new_dir / "regime.md").unlink()

    result = verify_module.check_deterministic_prefix(
        source_distillation_dir=source_dir,
        new_distillation_dir=new_dir,
    )

    assert result.passed is False
    assert "regime.md" in result.message
    assert "missing" in result.message


def test_check_deterministic_prefix_fails_on_missing_source_dir(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when the source distillation directory is absent entirely."""
    new_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-new")
    missing_source = tmp_path / "ghost" / "distillation"

    result = verify_module.check_deterministic_prefix(
        source_distillation_dir=missing_source,
        new_distillation_dir=new_dir,
    )

    assert result.passed is False
    assert "source distillation directory missing" in result.message


def test_check_deterministic_prefix_fails_on_missing_new_dir(
    verify_module: ModuleType, tmp_path: Path
) -> None:
    """FAIL when the new distillation directory is absent entirely."""
    source_dir = _seed_distillation(archive_root=tmp_path, invocation_id="inv-source")
    missing_new = tmp_path / "ghost" / "distillation"

    result = verify_module.check_deterministic_prefix(
        source_distillation_dir=source_dir,
        new_distillation_dir=missing_new,
    )

    assert result.passed is False
    assert "new distillation directory missing" in result.message


# ---------------------------------------------------------------------------
# Helpers — main() integration tests
# ---------------------------------------------------------------------------


def _stub_main_for_resume(
    verify_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    new_invocation_id: str,
) -> None:
    """Stub subprocess + DB-dependent checks so ``main()`` runs to
    completion in-process.

    Mirrors :func:`tests.scripts.test_verify_debug_e2e._stub_main_dependencies`
    but with the ``new_invocation_id`` plumbed in via the stubbed
    stdout payload so the caller can pre-seed the new-archive's
    distillation directory at the right path before invoking main.
    """
    stdout_payload = (
        f'{{"invocation_id": "{new_invocation_id}", "staleness_flag": false, '
        f'"trigger_source": "debug_e2e_cli", "commands_submitted": 0}}'
    )

    def _stub_run(_cmd: list[str], **_kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stdout = stdout_payload
            stderr = ""

        return _Completed()

    monkeypatch.setattr(verify_module.subprocess, "run", _stub_run)

    def _passing_archive(*, archive_root: Path, invocation_id: str) -> Any:
        inv_dir = archive_root / "invocations" / invocation_id
        inv_dir.mkdir(parents=True, exist_ok=True)
        (inv_dir / "progress.jsonl").touch()
        return verify_module.CheckResult(label="archive_directory", passed=True, message="stub")

    monkeypatch.setattr(verify_module, "check_archive_directory", _passing_archive)
    monkeypatch.setattr(
        verify_module,
        "check_jsonl_ordering",
        lambda _path: verify_module.CheckResult(
            label="jsonl_ordering", passed=True, message="stub"
        ),
    )
    monkeypatch.setattr(
        verify_module,
        "check_synthetic_portfolio_visibility",
        lambda _engine, *, expected: verify_module.CheckResult(
            label="synthetic_portfolio", passed=True, message="stub"
        ),
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stub-token")


def _capture_stdout(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Redirect ``sys.stdout`` to an in-memory buffer for assertion."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


# ---------------------------------------------------------------------------
# 4. Non-resume run: no deterministic_prefix line; summary 7/7
# ---------------------------------------------------------------------------


def test_main_non_resume_run_omits_deterministic_prefix_line(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a fresh (non-resume) debug-e2e invocation, the
    ``deterministic_prefix`` check does NOT fire and the summary line
    stays at ``7/7 checks passed`` (story ALP-696 acceptance crit 4).
    """
    new_id = "inv-20260526T100000Z-12345678"
    _stub_main_for_resume(verify_module, monkeypatch, new_invocation_id=new_id)
    stdout = _capture_stdout(monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
        ]
    )

    out = stdout.getvalue()
    assert exit_code == 0
    assert "deterministic_prefix" not in out
    assert "7/7 checks passed" in out


# ---------------------------------------------------------------------------
# 2 (cont.) Resume run: PASS line lands; summary 8/8
# ---------------------------------------------------------------------------


def test_main_resume_run_emits_deterministic_prefix_pass(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a clean resume run the wrapper emits ``PASS:
    deterministic_prefix`` and the summary becomes ``8/8 checks
    passed`` (story ALP-696 acceptance crit 2).
    """
    source_id = "inv-20260525T231625Z-b9f1a7aa"
    new_id = "inv-20260526T100000Z-12345678"
    archive_root = tmp_path / "archive"
    _seed_distillation(archive_root=archive_root, invocation_id=source_id)
    _seed_distillation(archive_root=archive_root, invocation_id=new_id)

    _stub_main_for_resume(verify_module, monkeypatch, new_invocation_id=new_id)
    stdout = _capture_stdout(monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(archive_root),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--resume-from",
            f"{source_id}:analyst",
        ]
    )

    out = stdout.getvalue()
    assert exit_code == 0
    assert "PASS: deterministic_prefix" in out
    assert "8/8 checks passed" in out


# ---------------------------------------------------------------------------
# 3 (cont.) Resume run with mutated source: FAIL line; summary 7/8
# ---------------------------------------------------------------------------


def test_main_resume_run_with_mutated_source_emits_fail(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a resume run whose source distillation has a mutated file,
    the wrapper emits ``FAIL: deterministic_prefix`` naming the file
    and the summary becomes ``7/8 checks passed`` (story ALP-696
    acceptance crit 3).
    """
    source_id = "inv-20260525T231625Z-b9f1a7aa"
    new_id = "inv-20260526T100000Z-12345678"
    archive_root = tmp_path / "archive"
    source_dir = _seed_distillation(archive_root=archive_root, invocation_id=source_id)
    _seed_distillation(archive_root=archive_root, invocation_id=new_id)
    # Mutate one source file so the hashes diverge — the FAIL arm.
    (source_dir / "tech_semis_sector.md").write_bytes(b"# Tech semis\n\nMUTATED\n")

    _stub_main_for_resume(verify_module, monkeypatch, new_invocation_id=new_id)
    stdout = _capture_stdout(monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(archive_root),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--resume-from",
            f"{source_id}:analyst",
        ]
    )

    out = stdout.getvalue()
    assert exit_code == 1  # any FAIL → exit non-zero
    assert "FAIL: deterministic_prefix" in out
    assert "tech_semis_sector.md" in out
    assert "7/8 checks passed" in out


# ---------------------------------------------------------------------------
# 5. Resume target = pm (all upstream replayed): check still PASSes
# ---------------------------------------------------------------------------


def test_main_resume_target_pm_still_runs_deterministic_prefix(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the resume target is ``pm`` (all upstream SDK phases
    replayed, no shared phase_outputs beyond distillation), the check
    still computes and passes — distillation comparison is
    unconditional on resume (story ALP-696 acceptance crit 5).

    Functionally this is the same call shape as the ``analyst``-target
    case above — distinguishing the two would require richer per-phase
    plumbing (e.g. introspecting which phases were replayed vs re-run).
    The story carves out distillation as the *single* unconditional
    comparison; this test pins that the resume-target value doesn't
    short-circuit the check.
    """
    source_id = "inv-20260525T231625Z-b9f1a7aa"
    new_id = "inv-20260526T100000Z-12345678"
    archive_root = tmp_path / "archive"
    _seed_distillation(archive_root=archive_root, invocation_id=source_id)
    _seed_distillation(archive_root=archive_root, invocation_id=new_id)

    _stub_main_for_resume(verify_module, monkeypatch, new_invocation_id=new_id)
    stdout = _capture_stdout(monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(archive_root),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--resume-from",
            f"{source_id}:pm",
        ]
    )

    out = stdout.getvalue()
    assert exit_code == 0
    assert "PASS: deterministic_prefix" in out
    assert "8/8 checks passed" in out


# ---------------------------------------------------------------------------
# Resume run missing source distillation: FAIL with a useful message
# ---------------------------------------------------------------------------


def test_main_resume_run_with_missing_source_distillation_fails(
    verify_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the source archive's distillation directory cannot be
    located under the archive root, the wrapper FAILs the
    ``deterministic_prefix`` check with the missing path / invocation
    id — surfacing the configuration error rather than passing trivially.
    """
    source_id = "inv-20260525T231625Z-b9f1a7aa"
    new_id = "inv-20260526T100000Z-12345678"
    archive_root = tmp_path / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    # Seed only the NEW archive's distillation; source is absent.
    _seed_distillation(archive_root=archive_root, invocation_id=new_id)

    _stub_main_for_resume(verify_module, monkeypatch, new_invocation_id=new_id)
    stdout = _capture_stdout(monkeypatch)

    exit_code = verify_module.main(
        [
            "--archive-root",
            str(archive_root),
            "--db-path",
            str(tmp_path / "alphamind-debug-e2e.db"),
            "--resume-from",
            f"{source_id}:analyst",
        ]
    )

    out = stdout.getvalue()
    assert exit_code == 1
    assert "FAIL: deterministic_prefix" in out
    assert source_id in out
    assert "7/8 checks passed" in out
