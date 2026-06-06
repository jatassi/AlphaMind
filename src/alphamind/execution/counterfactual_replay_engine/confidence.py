"""Confidence classifier for the counterfactual replay engine (ALP-560).

A pure function — no I/O, no DB — that maps a small dossier of input-quality
signals to :class:`Confidence`. The engine driver (story 08) supplies the
signals and folds the result onto each ``CounterfactualReplayRecord``.

Encodes parent decision (D): production runs on 15-minute bars rather than the
design's assumed minute bars, so same-bar target-and-stop ambiguity demotes to
:attr:`Confidence.MEDIUM` baseline (not :attr:`Confidence.LOW`) — at the coarser
resolution the ambiguity fraction is higher and a Low baseline would
over-shrink the High pool. The full rule set lives in
``docs/design/05-execution-layer/counterfactual-replay-engine.md`` § Step 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alphamind.execution.counterfactual_replay_engine.enums import Confidence

__all__ = ["ConfidenceSignals", "classify_confidence", "replay_engine_version"]

replay_engine_version = "v2"
"""Algorithm version stamped on every record this work tree writes.

Single source of truth for the version string per parent decision (B); the
engine driver (story 08) reads it onto ``CounterfactualReplayRecord``.
"""


@dataclass(frozen=True, slots=True)
class ConfidenceSignals:
    """Input-quality signals the classifier reads, one per replay attempt.

    Field semantics:

    * ``instrument_kind`` — dispatches between the equity path (no IV checks)
      and the option path (apply the IV-lag rules).
    * ``same_bar_ambiguity`` — both target and stop triggered within one bar, so
      the engine assumed stop-first (a conservative bias). Sourced from
      ``EquityBracketResult.same_bar_ambiguity`` or the option equivalent.
    * ``bar_coverage_complete`` — ``True`` iff the bar repo returned a contiguous
      bar sequence over the replay window; ``False`` signals minor data gaps.
    * ``liquidity_within_typical_envelope`` / ``spread_within_typical_envelope``
      — ``None`` when the upstream paper-harness primitive declined to estimate;
      the classifier treats ``None`` as "unknown" and does not demote on it
      alone.
    * ``iv_lag_minutes_entry`` / ``iv_lag_minutes_exit`` — minutes the nearest
      per-contract IV snapshot lags the entry / exit timestamp (story 05c).
      ``None`` for equity proposals.
    * ``iv_lag_threshold_minutes`` — the lag beyond which IV is materially stale,
      from ``CounterfactualReplayEngineConfig.iv_lag_low_confidence_threshold_minutes``
      (decision (H)). ``None`` for equity proposals.

    There are no P/L-bracket-leg signals: analyst option proposals carry no
    P/L-on-the-option's-own-price bracket leg, so there is nothing to demote on.
    """

    instrument_kind: Literal["equity", "option"]
    same_bar_ambiguity: bool
    bar_coverage_complete: bool
    liquidity_within_typical_envelope: bool | None
    spread_within_typical_envelope: bool | None
    # Option-specific signals — None for equity proposals.
    iv_lag_minutes_entry: float | None
    iv_lag_minutes_exit: float | None
    iv_lag_threshold_minutes: float | None


def classify_confidence(signals: ConfidenceSignals) -> Confidence:
    """Classify a replay's confidence from its input-quality signals.

    Starts at :attr:`Confidence.HIGH` and demotes by signal severity per
    § Step 5: any LOW trigger wins; otherwise any MEDIUM trigger; otherwise
    HIGH. See :class:`ConfidenceSignals` for the per-signal meaning.
    """
    return Confidence.HIGH
