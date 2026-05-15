"""Tests for ``alphamind._kernel.atomic_io`` — ALP-473.

The kernel atomic-write helpers replace the three duplicate ``_atomic_write``
implementations previously living in ``config/snapshot.py``,
``distillation/calibration_snapshot.py``, and
``config/control_handlers/profile_switch.py``.
"""

from __future__ import annotations

from pathlib import Path


class TestAtomicWriteText:
    def test_round_trips_contents_to_path(self, tmp_path: Path) -> None:
        from alphamind._kernel.atomic_io import atomic_write_text

        target = tmp_path / "snapshot.json"
        atomic_write_text(target, "hello")

        assert target.read_text(encoding="utf-8") == "hello"

    def test_idempotent_on_re_run(self, tmp_path: Path) -> None:
        from alphamind._kernel.atomic_io import atomic_write_text

        target = tmp_path / "snapshot.json"
        atomic_write_text(target, "first")
        atomic_write_text(target, "second")

        assert target.read_text(encoding="utf-8") == "second"

    def test_creates_parent_directories_when_missing(self, tmp_path: Path) -> None:
        from alphamind._kernel.atomic_io import atomic_write_text

        target = tmp_path / "nested" / "dir" / "snapshot.json"
        atomic_write_text(target, "payload")

        assert target.read_text(encoding="utf-8") == "payload"

    def test_no_tmp_residue_after_success(self, tmp_path: Path) -> None:
        from alphamind._kernel.atomic_io import atomic_write_text

        target = tmp_path / "snapshot.json"
        atomic_write_text(target, "payload")

        # the ``.tmp`` sibling used during atomic write must not remain
        tmp_sibling = target.with_suffix(target.suffix + ".tmp")
        assert not tmp_sibling.exists()


class TestAtomicWriteBytes:
    def test_round_trips_bytes_to_path(self, tmp_path: Path) -> None:
        from alphamind._kernel.atomic_io import atomic_write_bytes

        target = tmp_path / "snapshot.bin"
        payload = b"\x00\x01\x02\xff"
        atomic_write_bytes(target, payload)

        assert target.read_bytes() == payload

    def test_creates_parent_directories_when_missing(self, tmp_path: Path) -> None:
        from alphamind._kernel.atomic_io import atomic_write_bytes

        target = tmp_path / "nested" / "snapshot.bin"
        atomic_write_bytes(target, b"payload")

        assert target.read_bytes() == b"payload"
