"""Tests for ``alphamind.execution.state_persistence.invocation_paths``."""

from __future__ import annotations


def test_constants_are_canonical_strings() -> None:
    """The two shared filename constants carry their documented values."""
    from alphamind.execution.state_persistence.invocation_paths import (
        CALIBRATION_SNAPSHOT_FILENAME,
        INVOCATIONS_DIRNAME,
    )

    assert CALIBRATION_SNAPSHOT_FILENAME == "data_calibration_state.json"
    assert INVOCATIONS_DIRNAME == "invocations"
