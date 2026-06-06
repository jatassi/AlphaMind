"""Pydantic model for ``config/replay_engine.yaml`` (ALP-555).

Carries the yaml-tunable knobs of the counterfactual replay engine (decision
(H) of ALP-129). No numeric defaults are baked into code — every value comes
from the yaml, so the operator-tunable surface lives in one place.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CounterfactualReplayEngineConfig(BaseModel):
    """Operator-tunable knobs for the counterfactual replay engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iv_lag_low_confidence_threshold_minutes: int = Field(gt=0)
    """Replays whose nearest IV snapshot at entry or exit is older than this
    lag are demoted to ``Confidence.LOW`` per Step 5 of the design doc.
    """

    strategist_default_forward_window_hours: int = Field(gt=0)
    """Default forward-simulation window for strategist position-action replays
    (CLOSE / REDUCE / ADJUST-BRACKET) whose horizon is not otherwise bounded by
    the proposal; consumed by ``compute_replay_window`` (stories 04 / 07).
    """


__all__ = ["CounterfactualReplayEngineConfig"]
