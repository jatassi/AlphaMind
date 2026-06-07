"""Load + validation tests for the replay-engine config (ALP-555).

Covers the happy-path load from a yaml file, the ``extra="forbid"`` rejection
of unknown keys, and the ``gt=0`` rejection of each knob. The loader reads from
a ``config_dir`` so tests build a throwaway dir rather than depending on the
repo's ``config/replay_engine.yaml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
from alphamind.execution.counterfactual_replay_engine.config import (
    load_replay_engine_config,
)

_VALID_YAML = (
    "iv_lag_low_confidence_threshold_minutes: 60\nstrategist_default_forward_window_hours: 24\n"
)


def _write_config(config_dir: Path, body: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "replay_engine.yaml").write_text(body, encoding="utf-8")
    return config_dir


def test_load_happy_path(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path, _VALID_YAML)
    config = load_replay_engine_config(config_dir)
    assert config.iv_lag_low_confidence_threshold_minutes == 60
    assert config.strategist_default_forward_window_hours == 24


def test_extra_key_rejected() -> None:
    with pytest.raises(ValidationError):
        CounterfactualReplayEngineConfig.model_validate(
            {
                "iv_lag_low_confidence_threshold_minutes": 60,
                "strategist_default_forward_window_hours": 24,
                "unexpected_knob": 1,
            }
        )


def test_iv_lag_threshold_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        CounterfactualReplayEngineConfig.model_validate(
            {
                "iv_lag_low_confidence_threshold_minutes": 0,
                "strategist_default_forward_window_hours": 24,
            }
        )


def test_forward_window_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        CounterfactualReplayEngineConfig.model_validate(
            {
                "iv_lag_low_confidence_threshold_minutes": 60,
                "strategist_default_forward_window_hours": 0,
            }
        )
