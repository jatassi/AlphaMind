"""Tests for the per-invocation filesystem snapshot writers (story 02b).

Two narrowly-scoped helpers ship the on-disk side of invocation provenance:
``write_pip_freeze_snapshot`` lays down ``pip_freeze.txt`` under the per-
process-lifetime directory; ``write_invocation_provenance_snapshots`` lays
down ``resolved_config.json`` and ``data_calibration_state.json`` under the
per-invocation directory.

The directory layout mirrors the design doc at
``docs/design/05-execution-layer/state-persistence.md`` § Process lifetimes
and § Invocation records.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.state.invocation_context.snapshots import (
    write_invocation_provenance_snapshots,
    write_pip_freeze_snapshot,
)

# Canonical test invocation timestamp; date partition is "2026-01-15".
_AS_OF = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


class TestWritePipFreezeSnapshot:
    def test_writes_to_expected_path_under_root(self, tmp_path: Path) -> None:
        root = tmp_path / "provenance"
        path_str = write_pip_freeze_snapshot(
            process_lifetime_id="proc-1",
            pip_freeze_text="alphamind==0.1.0\nsqlalchemy==2.0.49\n",
            root=str(root),
        )

        path = Path(path_str)
        assert path == root / "process_lifetimes" / "proc-1" / "pip_freeze.txt"
        assert path.read_text() == "alphamind==0.1.0\nsqlalchemy==2.0.49\n"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        # Nest the root inside another non-existent directory to confirm
        # parent creation reaches all the way up.
        root = tmp_path / "deep" / "nest" / "provenance"
        write_pip_freeze_snapshot(
            process_lifetime_id="proc-2",
            pip_freeze_text="x",
            root=str(root),
        )
        assert (root / "process_lifetimes" / "proc-2" / "pip_freeze.txt").exists()

    def test_overwrites_when_path_already_exists(self, tmp_path: Path) -> None:
        root = tmp_path / "provenance"
        path_str = write_pip_freeze_snapshot(
            process_lifetime_id="proc-3",
            pip_freeze_text="first",
            root=str(root),
        )
        write_pip_freeze_snapshot(
            process_lifetime_id="proc-3",
            pip_freeze_text="second",
            root=str(root),
        )
        assert Path(path_str).read_text() == "second"


class TestWriteInvocationProvenanceSnapshots:
    def test_writes_both_files_under_invocation_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "provenance"
        config_path, calibration_path = write_invocation_provenance_snapshots(
            invocation_id="inv-A",
            as_of=_AS_OF,
            resolved_config={"profile": "medium", "regime": "normal"},
            data_calibration_state={"vix_baseline": 18.5},
            root=str(root),
        )

        config_p = Path(config_path)
        calibration_p = Path(calibration_path)
        assert config_p == root / "2026-01-15" / "inv-A" / "resolved_config.json"
        assert calibration_p == root / "2026-01-15" / "inv-A" / "data_calibration_state.json"

        assert json.loads(config_p.read_text()) == {"profile": "medium", "regime": "normal"}
        assert json.loads(calibration_p.read_text()) == {"vix_baseline": 18.5}

    def test_creates_invocation_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "provenance"
        write_invocation_provenance_snapshots(
            invocation_id="inv-B",
            as_of=_AS_OF,
            resolved_config={},
            data_calibration_state={},
            root=str(root),
        )
        invocation_dir = root / "2026-01-15" / "inv-B"
        assert invocation_dir.is_dir()
        assert (invocation_dir / "resolved_config.json").exists()
        assert (invocation_dir / "data_calibration_state.json").exists()

    def test_overwrites_when_files_already_exist(self, tmp_path: Path) -> None:
        root = tmp_path / "provenance"
        write_invocation_provenance_snapshots(
            invocation_id="inv-C",
            as_of=_AS_OF,
            resolved_config={"old": True},
            data_calibration_state={"old": True},
            root=str(root),
        )
        config_path, calibration_path = write_invocation_provenance_snapshots(
            invocation_id="inv-C",
            as_of=_AS_OF,
            resolved_config={"new": True},
            data_calibration_state={"new": True},
            root=str(root),
        )
        assert json.loads(Path(config_path).read_text()) == {"new": True}
        assert json.loads(Path(calibration_path).read_text()) == {"new": True}

    def test_rejects_non_serializable_payload(self, tmp_path: Path) -> None:
        root = tmp_path / "provenance"
        with pytest.raises(TypeError):
            write_invocation_provenance_snapshots(
                invocation_id="inv-bad",
                as_of=_AS_OF,
                resolved_config={"set": {1, 2, 3}},
                data_calibration_state={},
                root=str(root),
            )
