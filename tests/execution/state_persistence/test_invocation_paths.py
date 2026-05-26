"""Tests for the shared snapshot-filename constants in ``alphamind._kernel.archive_layout``."""

from __future__ import annotations


def test_constants_are_canonical_strings() -> None:
    """The shared filename constants carry their documented values."""
    from alphamind._kernel.archive_layout import (
        CALIBRATION_SNAPSHOT_FILENAME,
        RESOLVED_CONFIG_FILENAME,
    )

    assert CALIBRATION_SNAPSHOT_FILENAME == "data_calibration_state.json"
    assert RESOLVED_CONFIG_FILENAME == "resolved_config.json"
