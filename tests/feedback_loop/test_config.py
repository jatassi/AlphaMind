"""Tests for FeedbackLoopConfig — the operator-tunable sample-size thresholds (ALP-872).

The thresholds gate outcome-tier metrics: below the per-tier minimum resolved-thesis
count, a metric reads "needs N more observations" rather than a (noise-dominated) value.
Only sample-size thresholds are YAML-tunable; windows and posterior bands are definitional
(see docs/design/feedback-loop.md § Cadence, § Process metrics vs. outcome metrics).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from alphamind.config.models.feedback import FeedbackLoopConfig

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read_feedback_yaml() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / "feedback.yaml").read_text()))


def test_loads_from_shipped_feedback_yaml() -> None:
    config = FeedbackLoopConfig.model_validate(_read_feedback_yaml())

    assert config.min_resolved_theses_monthly >= 1
    assert config.min_resolved_theses_quarterly >= 1


def test_is_frozen() -> None:
    config = FeedbackLoopConfig.model_validate(_read_feedback_yaml())

    with pytest.raises(ValidationError):
        config.min_resolved_theses_monthly = 99  # type: ignore[misc]


def test_rejects_non_positive_threshold() -> None:
    raw = _read_feedback_yaml()
    raw["min_resolved_theses_monthly"] = 0

    with pytest.raises(ValidationError):
        FeedbackLoopConfig.model_validate(raw)


def test_rejects_missing_threshold() -> None:
    raw = _read_feedback_yaml()
    del raw["min_resolved_theses_quarterly"]

    with pytest.raises(ValidationError):
        FeedbackLoopConfig.model_validate(raw)
