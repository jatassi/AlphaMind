"""Tests for ``StatePersistenceConfig`` and its loader."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from alphamind.state import (
    StatePersistenceConfig,
    load_state_persistence_config,
)

_REPO_ROOT = pathlib.Path(__file__).parents[2]
_MAIN_YAML = _REPO_ROOT / "config" / "main.yaml"


def _valid_payload() -> dict[str, Any]:
    return {
        "pm_decision_log_sliding_window_invocations": 2,
        "snapshot_read_timeout_seconds": 30.0,
        "pip_freeze_snapshot_root": "%USERPROFILE%/AlphaMind/data/provenance/process_lifetimes",
        "invocation_provenance_root": "%USERPROFILE%/AlphaMind/data/provenance/invocations",
    }


def test_load_state_persistence_config_returns_validated_config_from_main_yaml() -> None:
    raw = yaml.safe_load(_MAIN_YAML.read_text())

    cfg = load_state_persistence_config(raw)

    assert isinstance(cfg, StatePersistenceConfig)
    assert cfg.snapshot_read_timeout_seconds == 30.0
    assert "process_lifetimes" in cfg.pip_freeze_snapshot_root
    assert "invocations" in cfg.invocation_provenance_root


def test_load_state_persistence_config_raises_value_error_when_section_missing() -> None:
    raw_without_section: dict[str, Any] = {
        "active_profile": "medium",
        "execution_mode": "paper",
        "paths": {},
    }

    with pytest.raises(ValueError, match="state_persistence") as exc_info:
        load_state_persistence_config(raw_without_section)

    assert "state_persistence" in str(exc_info.value)


def test_state_persistence_config_is_frozen_and_rejects_mutation() -> None:
    cfg = StatePersistenceConfig.model_validate(_valid_payload())

    with pytest.raises(ValidationError):
        cfg.pm_decision_log_sliding_window_invocations = 5


def test_default_pm_decision_log_sliding_window_in_main_yaml_matches_design_doc() -> None:
    """The design doc says the sliding window is "typically 2-3"; the story
    pins the definitional default at 2 so downstream stories consume a stable
    knob without re-deciding it. This test guards against silent drift in
    config/main.yaml."""
    raw = yaml.safe_load(_MAIN_YAML.read_text())

    cfg = load_state_persistence_config(raw)

    assert cfg.pm_decision_log_sliding_window_invocations == 2


def test_state_persistence_config_rejects_zero_window_size() -> None:
    payload = _valid_payload()
    payload["pm_decision_log_sliding_window_invocations"] = 0

    with pytest.raises(ValidationError):
        StatePersistenceConfig.model_validate(payload)


def test_state_persistence_config_rejects_non_positive_snapshot_timeout() -> None:
    payload = _valid_payload()
    payload["snapshot_read_timeout_seconds"] = 0.0

    with pytest.raises(ValidationError):
        StatePersistenceConfig.model_validate(payload)
