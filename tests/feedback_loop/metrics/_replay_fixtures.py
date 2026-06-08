"""Hand-built replay-observation fixtures for the PM-accuracy metric tests (ALP-887).

The PM-accuracy / modification-effectiveness metrics read ``ReplayObservation``s —
the loader-assembled join of a ``counterfactual_replays`` record to its originating
PM envelope. Counterfactual replays do not exist in production until the ALP-129
engine populates them, so the pure cores are driven entirely by hand-built fixtures
carrying the joined facts the cores read (verdict, sizing flag, anti-pattern tags,
the counterfactual realized P/L, and the actual modified-form outcome).
"""

from __future__ import annotations

from alphamind.feedback_loop.dataset import ReplayObservation
from alphamind.state.tables.counterfactual_replays import (
    Confidence,
    ReplayKind,
    ReplayStatus,
)


def make_replay_observation(
    *,
    envelope_id: str = "ENV-REC-1",
    replay_kind: ReplayKind = ReplayKind.REJECTION,
    replay_status: ReplayStatus = ReplayStatus.EVALUATED,
    confidence: Confidence | None = Confidence.HIGH,
    counterfactual_pnl: float | None = 0.0,
    actual_modified_pnl: float | None = None,
    is_sizing_modification: bool = False,
    anti_patterns: tuple[str, ...] = (),
) -> ReplayObservation:
    """One joined replay observation with the facts the PM-accuracy cores read."""
    return ReplayObservation(
        envelope_id=envelope_id,
        replay_kind=replay_kind,
        replay_status=replay_status,
        confidence=confidence,
        counterfactual_pnl=counterfactual_pnl,
        actual_modified_pnl=actual_modified_pnl,
        is_sizing_modification=is_sizing_modification,
        anti_patterns=anti_patterns,
    )
