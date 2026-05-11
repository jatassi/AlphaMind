"""Tests for ``alphamind.execution.state_persistence.invocation_paths`` (ALP-450 item 4)."""

from __future__ import annotations

from pathlib import Path


def test_constants_are_canonical_strings() -> None:
    """The two shared filename constants carry their documented values."""
    from alphamind.execution.state_persistence.invocation_paths import (
        CALIBRATION_SNAPSHOT_FILENAME,
        INVOCATIONS_DIRNAME,
    )

    assert CALIBRATION_SNAPSHOT_FILENAME == "data_calibration_state.json"
    assert INVOCATIONS_DIRNAME == "invocations"


def test_scheduler_invocation_imports_from_shared_module() -> None:
    """``scheduler.invocation`` reads the constants from the shared module.

    Guards against re-introducing the duplicate constant definition in
    ``scheduler/invocation.py`` (the original ALP-450 item 4 condition).
    """
    from alphamind.execution.state_persistence import invocation_paths
    from alphamind.scheduler import invocation as scheduler_invocation

    src = scheduler_invocation.__file__
    assert src is not None
    text = Path(src).read_text()
    assert "from alphamind.execution.state_persistence.invocation_paths import" in text
    # And the private duplicates should be gone.
    assert "_CALIBRATION_SNAPSHOT_FILENAME = " not in text
    # The shared module is reachable.
    assert invocation_paths.INVOCATIONS_DIRNAME == "invocations"


def test_distillation_calibration_snapshot_imports_from_shared_module() -> None:
    """``distillation.calibration_snapshot`` reads from the shared module.

    Same purpose as the scheduler-side guard but for the second duplicate
    site flagged in ALP-450 item 4.
    """
    from alphamind.distillation import calibration_snapshot

    src = calibration_snapshot.__file__
    assert src is not None
    text = Path(src).read_text()
    assert "from alphamind.execution.state_persistence.invocation_paths import" in text
    assert "_SNAPSHOT_FILENAME: str = " not in text
    assert "_INVOCATIONS_DIRNAME: str = " not in text
