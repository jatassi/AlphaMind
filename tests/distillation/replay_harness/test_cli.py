"""End-to-end tests for the replay-harness CLI runner (story 08).

The CLI orchestrates the primitives implemented in stories 03-07. These tests
exercise the wired-up runner against synthesized fixtures under ``tmp_path``
and verify each acceptance criterion from story 08 produces the documented
exit code and operator-facing output.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from alphamind.distillation.replay_harness.cli import (
    EXIT_ERROR,
    EXIT_OK,
    GIT_SHA_FALLBACK,
    _resolve_git_sha,
    main,
)
from tests.distillation.replay_harness.conftest import (
    FrozenDatetime,
    build_synthesized_slice,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_CONFIG_PATH = REPO_ROOT / "config" / "distillation.yaml"


def _cli_args(
    *,
    tmp_path: Path,
    candidate_config: Path | str = CANONICAL_CONFIG_PATH,
    baseline_config: Path | str | None = None,
    regimes: str = "low_vol",
) -> list[str]:
    """Build a CLI argv list with the standard tmp_path-rooted layout."""
    args = [
        "--candidate-config",
        str(candidate_config),
        "--fixture-store-root",
        str(tmp_path / "fixtures"),
        "--report-root",
        str(tmp_path / "reports"),
        "--regimes",
        regimes,
    ]
    if baseline_config is not None:
        args.extend(["--baseline-config", str(baseline_config)])
    return args


# ---------------------------------------------------------------------------
# Argument-validation tests — no fixture seeding required
# ---------------------------------------------------------------------------


def test_unknown_regime_label_exits_one_with_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate_yaml = tmp_path / "cand.yaml"
    candidate_yaml.write_text("not validated yet")
    code = main(
        _cli_args(tmp_path=tmp_path, candidate_config=candidate_yaml, regimes="definitely_unknown")
    )
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "definitely_unknown" in captured.err
    assert "low_vol" in captured.err


def test_missing_candidate_config_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_yaml = tmp_path / "does_not_exist.yaml"
    code = main(_cli_args(tmp_path=tmp_path, candidate_config=missing_yaml))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "does_not_exist.yaml" in captured.err


def test_malformed_candidate_config_exits_one_with_validation_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    # Valid YAML but missing the required nested distillation-config sections
    # — Pydantic's validator rejects with a field-by-field error message.
    bad_yaml.write_text("anomaly_detection:\n  volume_anomaly_sigma: 3.0\n")
    code = main(_cli_args(tmp_path=tmp_path, candidate_config=bad_yaml))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "invalid distillation config" in captured.err


def test_curation_required_when_requested_regime_has_no_fixtures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Empty fixture store directory — discovery succeeds with zero slices.
    (tmp_path / "fixtures").mkdir()
    code = main(_cli_args(tmp_path=tmp_path, regimes="crisis"))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "no fixture slices for regime 'crisis'" in captured.err
    assert "curate one before replay" in captured.err


# ---------------------------------------------------------------------------
# Happy-path tests — full single-mode end-to-end through replay/aggregate/render
# ---------------------------------------------------------------------------


def _seed_low_vol_slice(tmp_path: Path) -> None:
    """Build the synthetic slice every happy-path test reuses."""
    build_synthesized_slice(
        tmp_path,
        invocation_count=1,
        days_of_history=30,
        slice_id="synthetic_low_vol_a",
        regime_label="low_vol",
    )


def _write_looser_config(tmp_path: Path) -> Path:
    """Return a path to a candidate config with a looser volume-anomaly sigma."""
    raw = yaml.safe_load(CANONICAL_CONFIG_PATH.read_text())
    raw["anomaly_detection"]["volume_anomaly_sigma"] = 5.0
    looser_path = tmp_path / "looser_distillation.yaml"
    looser_path.write_text(yaml.safe_dump(raw))
    return looser_path


def test_single_mode_writes_report_prints_path_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_low_vol_slice(tmp_path)
    code = main(_cli_args(tmp_path=tmp_path))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert captured.out.startswith("wrote report: ")
    written_path = Path(captured.out.removeprefix("wrote report: ").strip())
    assert written_path.exists()
    assert written_path.name == "report.md"
    assert written_path.parent.parent == (tmp_path / "reports").resolve()
    body = written_path.read_text()
    assert written_path.parent.name in body  # report_id appears in header


def test_diff_mode_renders_baseline_config_header_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_low_vol_slice(tmp_path)
    looser_yaml = _write_looser_config(tmp_path)
    code = main(_cli_args(tmp_path=tmp_path, baseline_config=looser_yaml))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    written_path = Path(captured.out.removeprefix("wrote report: ").strip())
    body = written_path.read_text()
    assert "**baseline_config_path:**" in body
    assert "**baseline_config_sha256:**" in body


# ---------------------------------------------------------------------------
# Failure mode tests
# ---------------------------------------------------------------------------


def test_existing_report_directory_exits_one_without_overwriting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two invocations within the same UTC second collide on the report directory.

    The CLI captures ``datetime.now(UTC)`` once per call; mocking it returns
    the same value across two calls so the second invocation's
    ``compute_report_id`` produces the same directory name.
    """
    _seed_low_vol_slice(tmp_path)
    args = _cli_args(tmp_path=tmp_path)

    fixed_now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "alphamind.distillation.replay_harness.cli.datetime",
            FrozenDatetime(fixed_now),
        )
        first_code = main(args)
        capsys.readouterr()  # discard
        second_code = main(args)
    second_captured = capsys.readouterr()
    assert first_code == EXIT_OK
    assert second_code == EXIT_ERROR
    assert "already exists" in second_captured.err
    assert "refusing to overwrite" in second_captured.err


def test_replay_failure_exits_one_without_writing_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay-time exception aborts before write_report is reached."""
    _seed_low_vol_slice(tmp_path)

    def _raising_replay_slice(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated replay engine crash")

    monkeypatch.setattr(
        "alphamind.distillation.replay_harness.cli.replay_slice",
        _raising_replay_slice,
    )
    code = main(_cli_args(tmp_path=tmp_path))
    captured = capsys.readouterr()
    report_root = tmp_path / "reports"
    assert code == EXIT_ERROR
    assert "simulated replay engine crash" in captured.err
    assert not report_root.exists() or list(report_root.iterdir()) == []


# ---------------------------------------------------------------------------
# Discovery warning tests
# ---------------------------------------------------------------------------


def test_discovery_warnings_appear_on_stderr_but_run_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed slice generates a stderr warning; the run completes."""
    _seed_low_vol_slice(tmp_path)
    # Inject a malformed slice (manifest exists but is invalid JSON).
    bad_slice = tmp_path / "fixtures" / "low_vol" / "broken_slice"
    bad_slice.mkdir(parents=True)
    (bad_slice / "manifest.json").write_text("{ this is not valid json")

    code = main(_cli_args(tmp_path=tmp_path))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert "warning:" in captured.err
    assert "broken_slice" in captured.err


# ---------------------------------------------------------------------------
# Git SHA fallback tests
# ---------------------------------------------------------------------------


def test_git_sha_fallback_when_subprocess_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer receives the literal sentinel when ``git rev-parse`` fails."""
    _seed_low_vol_slice(tmp_path)

    def _raising_subprocess_run(*_args: object, **_kwargs: object) -> object:
        raise OSError("git binary not available")

    monkeypatch.setattr(
        "alphamind.distillation.replay_harness.cli.subprocess.run",
        _raising_subprocess_run,
    )
    code = main(_cli_args(tmp_path=tmp_path))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    written_path = Path(captured.out.removeprefix("wrote report: ").strip())
    body = written_path.read_text()
    assert "(not in a git checkout)" in body


def test_resolve_git_sha_returns_sentinel_when_returncode_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CompletedNonZero:
        returncode = 128
        stdout = ""

    def _failing_run(*_args: object, **_kwargs: object) -> object:
        return _CompletedNonZero()

    monkeypatch.setattr(
        "alphamind.distillation.replay_harness.cli.subprocess.run",
        _failing_run,
    )
    assert _resolve_git_sha() == GIT_SHA_FALLBACK


def test_resolve_git_sha_returns_sentinel_when_stdout_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CompletedEmpty:
        returncode = 0
        stdout = "   \n"

    def _empty_run(*_args: object, **_kwargs: object) -> object:
        return _CompletedEmpty()

    monkeypatch.setattr(
        "alphamind.distillation.replay_harness.cli.subprocess.run",
        _empty_run,
    )
    assert _resolve_git_sha() == GIT_SHA_FALLBACK


# ---------------------------------------------------------------------------
# Module-invocation smoke test (replaces the deleted test_main_module.py)
# ---------------------------------------------------------------------------


def test_module_help_renders_all_arguments() -> None:
    """`python -m alphamind.distillation.replay_harness --help` lists every argument."""
    completed = subprocess.run(
        [sys.executable, "-m", "alphamind.distillation.replay_harness", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    for flag in (
        "--candidate-config",
        "--baseline-config",
        "--regimes",
        "--fixture-store-root",
        "--report-root",
    ):
        assert flag in completed.stdout, f"{flag} missing from --help output"
