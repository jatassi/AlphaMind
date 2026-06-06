"""Behavior tests for the confidence classifier (ALP-560).

Drives :func:`classify_confidence` through each demotion path documented in
``docs/design/05-execution-layer/counterfactual-replay-engine.md`` § Step 5,
encoding parent decision (D): at 15-minute production resolution, same-bar
target-and-stop ambiguity demotes to ``Confidence.MEDIUM`` baseline rather than
the design's minute-bar ``Confidence.LOW``.
"""

from __future__ import annotations

from alphamind.execution.counterfactual_replay_engine.confidence import (
    ConfidenceSignals,
    classify_confidence,
)
from alphamind.execution.counterfactual_replay_engine.enums import Confidence


def _clean_equity_signals() -> ConfidenceSignals:
    """All-clean equity signals — the HIGH baseline each test perturbs."""
    return ConfidenceSignals(
        instrument_kind="equity",
        same_bar_ambiguity=False,
        bar_coverage_complete=True,
        liquidity_within_typical_envelope=True,
        spread_within_typical_envelope=True,
        iv_lag_minutes_entry=None,
        iv_lag_minutes_exit=None,
        iv_lag_threshold_minutes=None,
    )


def test_equity_happy_path_is_high() -> None:
    assert classify_confidence(_clean_equity_signals()) is Confidence.HIGH
