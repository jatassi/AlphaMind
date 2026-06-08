"""Counterfactual replay engine — config entry point.

Replays each PM-rejected and PM-modified proposal after its evaluation horizon
elapses, producing one immutable
:class:`~alphamind.state.tables.counterfactual_replays.CounterfactualReplayRecord`
per attempt. This package hosts the algorithmic contract in
``docs/design/05-execution-layer/counterfactual-replay-engine.md``; the record
type and enums live in ``state/tables/counterfactual_replays.py`` (ALP-913).
"""

from __future__ import annotations

from alphamind.execution.counterfactual_replay_engine.config import (
    load_replay_engine_config,
)

__all__ = [
    "load_replay_engine_config",
]
