"""Tests for alphamind.scheduler.debug_e2e.phase_outputs.

Covers:
- SDK_PHASE_NAMES frozenset equality
- phase_output_path path computation + validation
- write_phase_output / read_phase_output round-trip
- write_phase_output creates directory when absent
- write_phase_output atomicity (no partial file on mid-write failure)
- read_phase_output raises FileNotFoundError when absent
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from alphamind.scheduler.debug_e2e.phase_outputs import (
    SDK_PHASE_NAMES,
    phase_output_path,
    read_phase_output,
    write_phase_output,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SampleModel(BaseModel):
    x: int
    label: str


# ---------------------------------------------------------------------------
# SDK_PHASE_NAMES whitelist
# ---------------------------------------------------------------------------


def test_sdk_phase_names_exact_set() -> None:
    """SDK_PHASE_NAMES contains exactly the 9 SDK phases — no more, no less."""
    expected = frozenset(
        {
            "tech_semis",
            "financials",
            "energy",
            "qualitative",
            "adaptive",
            "synthesizer",
            "analyst",
            "strategist",
            "pm",
        }
    )
    assert expected == SDK_PHASE_NAMES


def test_sdk_phase_names_is_frozenset() -> None:
    """SDK_PHASE_NAMES is a frozenset for O(1) membership checks."""
    assert isinstance(SDK_PHASE_NAMES, frozenset)


# ---------------------------------------------------------------------------
# phase_output_path
# ---------------------------------------------------------------------------


def test_phase_output_path_valid(tmp_path: Path) -> None:
    """Valid phase returns <archive_dir>/phase_outputs/<phase>.json."""
    result = phase_output_path(tmp_path, "analyst")
    assert result == tmp_path / "phase_outputs" / "analyst.json"


def test_phase_output_path_all_valid_phases(tmp_path: Path) -> None:
    """Each of the 9 SDK phases resolves without raising."""
    for phase in SDK_PHASE_NAMES:
        path = phase_output_path(tmp_path, phase)
        assert path == tmp_path / "phase_outputs" / f"{phase}.json"


def test_phase_output_path_invalid_raises_value_error(tmp_path: Path) -> None:
    """An unknown phase raises ValueError naming the invalid phase."""
    with pytest.raises(ValueError, match="snapshot_assembly"):
        phase_output_path(tmp_path, "snapshot_assembly")


def test_phase_output_path_invalid_names_phase_in_message(tmp_path: Path) -> None:
    """ValueError message includes the rejected phase name."""
    bad_phase = "not_a_real_phase"
    with pytest.raises(ValueError, match=bad_phase):
        phase_output_path(tmp_path, bad_phase)


# ---------------------------------------------------------------------------
# write_phase_output + read_phase_output
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    """write then read returns an equal model instance."""
    original = _SampleModel(x=42, label="hello")
    write_phase_output(archive_dir=tmp_path, phase="analyst", model=original)
    recovered = read_phase_output(archive_dir=tmp_path, phase="analyst", model_cls=_SampleModel)
    assert recovered == original


def test_write_creates_phase_outputs_dir(tmp_path: Path) -> None:
    """write_phase_output creates phase_outputs/ if it doesn't exist."""
    phase_outputs_dir = tmp_path / "phase_outputs"
    assert not phase_outputs_dir.exists()
    write_phase_output(archive_dir=tmp_path, phase="pm", model=_SampleModel(x=1, label="a"))
    assert phase_outputs_dir.is_dir()


def test_write_atomicity_no_partial_file(tmp_path: Path) -> None:
    """If atomic_write_text raises, no .json file is left in phase_outputs/."""
    phase_outputs_dir = tmp_path / "phase_outputs"
    target = phase_outputs_dir / "analyst.json"

    with (
        patch(
            "alphamind.scheduler.debug_e2e.phase_outputs.atomic_write_text",
            side_effect=OSError("simulated mid-write failure"),
        ),
        pytest.raises(OSError, match="simulated mid-write failure"),
    ):
        write_phase_output(
            archive_dir=tmp_path,
            phase="analyst",
            model=_SampleModel(x=99, label="oops"),
        )

    # No partial .json file must exist after the failure
    assert not target.exists()


def test_read_raises_file_not_found_when_absent(tmp_path: Path) -> None:
    """read_phase_output raises FileNotFoundError when the file doesn't exist."""
    with pytest.raises(FileNotFoundError):
        read_phase_output(archive_dir=tmp_path, phase="analyst", model_cls=_SampleModel)


def test_read_raises_validation_error_on_schema_mismatch(tmp_path: Path) -> None:
    """read_phase_output propagates Pydantic ValidationError on bad JSON."""
    from pydantic import ValidationError

    # Write raw JSON that doesn't match _SampleModel
    phase_outputs_dir = tmp_path / "phase_outputs"
    phase_outputs_dir.mkdir()
    (phase_outputs_dir / "analyst.json").write_text('{"wrong_field": true}', encoding="utf-8")

    with pytest.raises(ValidationError):
        read_phase_output(archive_dir=tmp_path, phase="analyst", model_cls=_SampleModel)
