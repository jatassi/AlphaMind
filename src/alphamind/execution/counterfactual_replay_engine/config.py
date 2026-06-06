"""Config-load entry point for the counterfactual replay engine (ALP-555).

The thin I/O shell that reads ``config/replay_engine.yaml`` and parses it into
the frozen :class:`CounterfactualReplayEngineConfig` boundary model. Downstream
stories (04 / 07) call :func:`load_replay_engine_config` at the composition
root and thread the validated config inward.
"""

from __future__ import annotations

from pathlib import Path

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig

__all__ = ["load_replay_engine_config"]


def load_replay_engine_config(config_dir: Path) -> CounterfactualReplayEngineConfig:
    """Load and validate ``config_dir / 'replay_engine.yaml'``.

    Raises ``FileNotFoundError`` if the file is missing (fails closed at
    invocation start, matching the other bundle loaders) and
    ``pydantic.ValidationError`` if a knob violates its constraint.
    """
    payload = read_yaml_file(config_dir / "replay_engine.yaml")
    return CounterfactualReplayEngineConfig.model_validate(payload)
