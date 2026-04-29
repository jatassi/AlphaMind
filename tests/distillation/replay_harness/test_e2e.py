"""End-to-end verification of the replay harness against committed fixtures.

The seven tests in this module exercise ``cli.main`` against canonical
SQLite slices under ``tests/fixtures/replay_harness/`` — no engine,
aggregator, or renderer mocks. Coverage:

* single-mode and diff-mode happy paths emit the documented report shape
* missing-regime curation aborts with the documented message
* mocked-now reruns produce byte-identical reports
* runtime DB env vars are ignored (fixture isolation)
* the committed fixture matches a freshly-regenerated one (drift canary)
* the report directory name matches the documented format

See ``docs/implementation/02-distillation-layer/replay-harness/09-end-to-end-verification.md``.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from alphamind.distillation.replay_harness.cli import EXIT_ERROR, EXIT_OK, main
from alphamind.distillation.replay_harness.version import HARNESS_VERSION
from tests.distillation.replay_harness.conftest import FrozenDatetime

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "replay_harness"
FIXTURE_STORE_ROOT = FIXTURES_ROOT / "fixture_store"
CANDIDATE_CONFIG = FIXTURES_ROOT / "candidate_distillation.yaml"
BASELINE_CONFIG = FIXTURES_ROOT / "baseline_distillation.yaml"

LOW_VOL_SLICE_ID = "slice_e2e_lowvol_0001"
NORMAL_SLICE_ID = "slice_e2e_normal_0001"

REPORT_ID_SINGLE_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{16}$")
REPORT_ID_DIFF_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{16}_[0-9a-f]{16}$")
SHA256_HEX_PATTERN = re.compile(r"\b[0-9a-f]{64}\b")


def _e2e_argv(
    *,
    report_root: Path,
    regimes: str,
    candidate_config: Path = CANDIDATE_CONFIG,
    baseline_config: Path | None = None,
) -> list[str]:
    """Build the standard --candidate-config/--fixture-store-root/--report-root argv."""
    argv = [
        "--candidate-config",
        str(candidate_config),
        "--fixture-store-root",
        str(FIXTURE_STORE_ROOT),
        "--report-root",
        str(report_root),
        "--regimes",
        regimes,
    ]
    if baseline_config is not None:
        argv.extend(["--baseline-config", str(baseline_config)])
    return argv


def _read_report(captured_stdout: str) -> tuple[Path, str]:
    """Extract the wrote-report path from stdout and return (path, body)."""
    written_path = Path(captured_stdout.removeprefix("wrote report: ").strip())
    return written_path, written_path.read_text(encoding="utf-8")


def test_e2e_single_mode_writes_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(_e2e_argv(report_root=tmp_path, regimes="low_vol,normal"))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err

    report_path, body = _read_report(captured.out)
    assert report_path.exists()
    assert report_path.name == "report.md"
    assert report_path.parent.parent == tmp_path.resolve()

    for required_section in (
        "# Replay harness report",
        "## Per-regime flag-rate table",
        "## Regime-label distribution",
        "## Class B baseline shape summary",
        "## Calibration-state breakdown",
    ):
        assert required_section in body, f"missing section: {required_section}"

    assert f"**harness_version:** {HARNESS_VERSION}" in body
    assert LOW_VOL_SLICE_ID in body
    assert NORMAL_SLICE_ID in body

    assert "**candidate_config_path:**" in body
    candidate_sha_line = next(
        line for line in body.splitlines() if line.startswith("**candidate_config_sha256:**")
    )
    assert SHA256_HEX_PATTERN.search(candidate_sha_line) is not None

    assert "**baseline_config_path:**" not in body


def test_e2e_diff_mode_writes_diff_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        _e2e_argv(
            report_root=tmp_path,
            regimes="low_vol,normal",
            baseline_config=BASELINE_CONFIG,
        )
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err

    _, body = _read_report(captured.out)
    assert "**baseline_config_path:**" in body
    assert "**baseline_config_sha256:**" in body

    # The diff-mode flag-rate header carries one Δ column per regime.
    assert "low_vol Δ" in body
    assert "normal Δ" in body

    # At least one delta cell must be non-zero. The candidate's
    # volume_anomaly_sigma=2.5 fires on the normal slice's planted spikes
    # while the baseline's 4.0 stays silent, producing a positive delta.
    delta_cells = re.findall(r"[+-]\d+\.\d+pp", body)
    assert delta_cells, "expected at least one delta cell in the flag-rate table"
    zero_deltas = {"+0.00pp", "-0.00pp"}
    assert any(cell not in zero_deltas for cell in delta_cells), (
        f"expected at least one non-zero delta; saw {delta_cells}"
    )


def test_e2e_curation_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(_e2e_argv(report_root=tmp_path, regimes="crisis"))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "no fixture slices for regime 'crisis'" in captured.err


def test_e2e_determinism(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixed_now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "alphamind.distillation.replay_harness.cli.datetime",
            FrozenDatetime(fixed_now),
        )
        code1 = main(_e2e_argv(report_root=first_root, regimes="low_vol,normal"))
        captured1 = capsys.readouterr()
        code2 = main(_e2e_argv(report_root=second_root, regimes="low_vol,normal"))
        captured2 = capsys.readouterr()

    assert code1 == EXIT_OK, captured1.err
    assert code2 == EXIT_OK, captured2.err

    first_path, _ = _read_report(captured1.out)
    second_path, _ = _read_report(captured2.out)
    assert first_path.read_bytes() == second_path.read_bytes()


def test_e2e_no_runtime_db_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bogus runtime-DB env vars must not affect a fixture-scoped replay."""
    bogus_path = tmp_path / "definitely_does_not_exist.sqlite"
    # ``DATABASE_PATH`` is the production env-var per
    # ``src/alphamind/persistence/session.py``. The harness's fixture-scoped
    # engine never opens this file; the test fails loudly if it ever does.
    monkeypatch.setenv("DATABASE_PATH", str(bogus_path))

    code = main(_e2e_argv(report_root=tmp_path / "reports", regimes="low_vol"))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert not bogus_path.exists()


def _table_names(db_path: Path) -> list[str]:
    """Return user-table names ordered alphabetically."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic_%' "
            "ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _per_table_row_crc(db_path: Path) -> dict[str, tuple[int, int]]:
    """Return ``{table: (row_count, content_hash)}`` for every user table.

    Catches schema drift (a new/dropped table changes the keyset) and content
    drift (different rows hash differently) without depending on byte-for-byte
    SQLite layout — which can shift across SQLite minor versions even when
    the data is identical.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        result: dict[str, tuple[int, int]] = {}
        for table in _table_names(db_path):
            # Table name comes from ``sqlite_master`` — not external input —
            # so the f-string interpolation is safe.
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY ROWID").fetchall()
            content_hash = hash(tuple(rows))
            result[table] = (len(rows), content_hash)
        return result
    finally:
        conn.close()


def _load_generator_module() -> ModuleType:
    """Import ``generate_fixtures.py`` by file path to avoid sys.path mutation.

    The module is registered in ``sys.modules`` before exec because
    :func:`dataclasses.dataclass` resolves the owning class's ``__module__``
    via ``sys.modules`` during ``InitVar`` introspection.
    """
    spec = importlib.util.spec_from_file_location(
        "alphamind_generate_fixtures",
        FIXTURES_ROOT / "generate_fixtures.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load generate_fixtures.py from {FIXTURES_ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generator_output_matches_committed_fixture(tmp_path: Path) -> None:
    """Re-run the operator-invoked generator into tmp_path; compare against the committed fixtures.

    The generator is operator-invoked when the runtime schema or fixture
    format changes (binary fixtures stay committed). When the operator
    forgets to re-run after a schema change, this test is the canary.
    """
    generator = _load_generator_module()
    try:
        fresh_root = tmp_path / "fresh"
        fresh_root.mkdir()
        for spec in generator.SLICE_SPECS:
            generator.generate_slice(spec, fixture_store_root=fresh_root)

        for spec in generator.SLICE_SPECS:
            committed_dir = FIXTURE_STORE_ROOT / spec.regime_label / spec.slice_id
            fresh_dir = fresh_root / spec.regime_label / spec.slice_id

            committed_manifest = (committed_dir / "manifest.json").read_bytes()
            fresh_manifest = (fresh_dir / "manifest.json").read_bytes()
            assert committed_manifest == fresh_manifest, (
                f"manifest drift for {spec.slice_id}; re-run "
                f"`python tests/fixtures/replay_harness/generate_fixtures.py`"
            )

            committed_db = committed_dir / "raw_inputs.sqlite"
            fresh_db = fresh_dir / "raw_inputs.sqlite"
            assert _per_table_row_crc(committed_db) == _per_table_row_crc(fresh_db), (
                f"raw_inputs.sqlite drift for {spec.slice_id}; re-run "
                f"`python tests/fixtures/replay_harness/generate_fixtures.py`"
            )
    finally:
        # Drop the sys.modules entry the lazy loader installed so this test
        # leaves no global state behind for sibling xdist workers.
        sys.modules.pop(generator.__name__, None)


def test_e2e_report_id_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Single-mode and diff-mode report directory names match the documented format."""
    single_root = tmp_path / "single"
    code = main(_e2e_argv(report_root=single_root, regimes="low_vol"))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    single_path, _ = _read_report(captured.out)
    assert REPORT_ID_SINGLE_PATTERN.match(single_path.parent.name) is not None, (
        f"single-mode report dir {single_path.parent.name!r} does not match "
        f"{REPORT_ID_SINGLE_PATTERN.pattern}"
    )

    diff_root = tmp_path / "diff"
    code = main(
        _e2e_argv(
            report_root=diff_root,
            regimes="low_vol",
            baseline_config=BASELINE_CONFIG,
        )
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    diff_path, _ = _read_report(captured.out)
    assert REPORT_ID_DIFF_PATTERN.match(diff_path.parent.name) is not None, (
        f"diff-mode report dir {diff_path.parent.name!r} does not match "
        f"{REPORT_ID_DIFF_PATTERN.pattern}"
    )
