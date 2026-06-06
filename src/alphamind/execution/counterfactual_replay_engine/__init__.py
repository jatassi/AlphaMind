"""Counterfactual replay engine — records, enums, and config entry point.

Replays each PM-rejected and PM-modified proposal after its evaluation horizon
elapses, producing one immutable :class:`CounterfactualReplayRecord` per
attempt. This package hosts the algorithmic contract in
``docs/design/05-execution-layer/counterfactual-replay-engine.md``; later
stories add the table codec, eligibility, equity / option / strategist replay
paths, and the driver as flat siblings under this package.
"""

from __future__ import annotations

from alphamind.execution.counterfactual_replay_engine.config import (
    load_replay_engine_config,
)
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
    ReplayId,
)

__all__ = [
    "Confidence",
    "CounterfactualReplayRecord",
    "ExitLeg",
    "ReplayId",
    "ReplayKind",
    "ReplayStatus",
    "UnevaluableReason",
    "load_replay_engine_config",
]
