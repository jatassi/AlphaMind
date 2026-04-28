"""Verify the not-yet-implemented sentinel behavior of the replay harness CLI.

Story 02 lands a sentinel exit; story 08 replaces it with the real runner
and removes this test file.
"""

from __future__ import annotations

import pytest

from alphamind.distillation.replay_harness.cli import (
    NOT_YET_IMPLEMENTED_EXIT_CODE,
    NOT_YET_IMPLEMENTED_MESSAGE,
    main,
)


def test_main_emits_sentinel_exit_code_and_stderr_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--candidate-config", "/nonexistent.yaml"])
    captured = capsys.readouterr()
    assert code == NOT_YET_IMPLEMENTED_EXIT_CODE
    assert NOT_YET_IMPLEMENTED_MESSAGE in captured.err
    assert captured.out == ""
