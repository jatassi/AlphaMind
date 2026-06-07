"""Persistent typed record for one counterfactual replay attempt (ALP-129).

``CounterfactualReplayRecord`` mirrors
``docs/design/05-execution-layer/state-persistence.md`` § Counterfactual
replays field-for-field. One record per PM-rejected or PM-modified proposal,
produced after the proposal's evaluation horizon elapses. Records are
immutable (``frozen=True``); the engine never silently overwrites.

``ReplayId`` is the kernel-level NewType for replay identifiers, re-exported
here so downstream stories import the record and its ID type from one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind._kernel.money import Money
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)

__all__ = ["CounterfactualReplayRecord", "ReplayId"]


@dataclass(frozen=True, slots=True)
class CounterfactualReplayRecord:
    """One counterfactual replay attempt for a PM-rejected or PM-modified proposal.

    The field set mirrors the persisted record in state-persistence.md. The
    cross-field invariants in :meth:`__post_init__` encode the design's "set
    only when …" clauses as type-level guarantees so downstream readers can
    trust the shape without re-checking it.
    """

    replay_id: ReplayId
    pm_decision_envelope_id: EnvelopeId
    replay_kind: ReplayKind
    replay_status: ReplayStatus
    unevaluable_reason: UnevaluableReason | None
    entered: bool | None
    entry_price: Money | None
    entry_timestamp: datetime | None
    entry_slippage: Money | None
    entry_fees: Money | None
    exit_leg: ExitLeg | None
    exit_price: Money | None
    exit_timestamp: datetime | None
    exit_slippage: Money | None
    exit_fees: Money | None
    realized_pl: Money | None
    confidence: Confidence | None
    replay_timestamp: datetime
    replay_data_window_start: datetime | None
    replay_data_window_end: datetime | None
    replay_engine_version: str

    def __post_init__(self) -> None:
        self._check_datetimes_utc()
        if self.replay_status is ReplayStatus.UNEVALUABLE:
            self._check_unevaluable()
        else:
            self._check_evaluated()

    def _check_datetimes_utc(self) -> None:
        """Every non-``None`` datetime field must be tz-aware UTC."""
        fields = (
            ("entry_timestamp", self.entry_timestamp),
            ("exit_timestamp", self.exit_timestamp),
            ("replay_timestamp", self.replay_timestamp),
            ("replay_data_window_start", self.replay_data_window_start),
            ("replay_data_window_end", self.replay_data_window_end),
        )
        for name, value in fields:
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                msg = f"{name} must be tz-aware when not None"
                raise ValueError(msg)

    def _check_unevaluable(self) -> None:
        """UNEVALUABLE: reason required; all outcome/simulation fields ``None``."""
        if self.unevaluable_reason is None:
            msg = "unevaluable_reason is required when replay_status is UNEVALUABLE"
            raise ValueError(msg)
        forbidden = (
            ("entered", self.entered),
            ("entry_price", self.entry_price),
            ("entry_timestamp", self.entry_timestamp),
            ("entry_slippage", self.entry_slippage),
            ("entry_fees", self.entry_fees),
            ("exit_leg", self.exit_leg),
            ("exit_price", self.exit_price),
            ("exit_timestamp", self.exit_timestamp),
            ("exit_slippage", self.exit_slippage),
            ("exit_fees", self.exit_fees),
            ("realized_pl", self.realized_pl),
            ("confidence", self.confidence),
            ("replay_data_window_start", self.replay_data_window_start),
            ("replay_data_window_end", self.replay_data_window_end),
        )
        offenders = [name for name, value in forbidden if value is not None]
        if offenders:
            msg = f"{', '.join(offenders)} must be None when replay_status is UNEVALUABLE"
            raise ValueError(msg)

    def _check_evaluated(self) -> None:
        """EVALUATED: no reason; ``entered`` + ``confidence`` set; entry/exit gated."""
        if self.unevaluable_reason is not None:
            msg = "unevaluable_reason must be None when replay_status is EVALUATED"
            raise ValueError(msg)
        if self.entered is None:
            msg = "entered is required when replay_status is EVALUATED"
            raise ValueError(msg)
        if self.confidence is None:
            msg = "confidence is required when replay_status is EVALUATED"
            raise ValueError(msg)
        if self.entered:
            self._check_entered()
        else:
            self._check_not_entered()

    def _check_entered(self) -> None:
        """``entered`` True: every entry field is non-``None``."""
        entry_fields = (
            ("entry_price", self.entry_price),
            ("entry_timestamp", self.entry_timestamp),
            ("entry_slippage", self.entry_slippage),
            ("entry_fees", self.entry_fees),
        )
        missing = [name for name, value in entry_fields if value is None]
        if missing:
            msg = f"{', '.join(missing)} must be non-None when entered is True"
            raise ValueError(msg)

    def _check_not_entered(self) -> None:
        """``entered`` False: window-expired leg; no exit fields or realized P/L."""
        if self.exit_leg is not ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED:
            msg = "exit_leg must be ENTRY_WINDOW_EXPIRED_UNFILLED when entered is False"
            raise ValueError(msg)
        forbidden = (
            ("exit_price", self.exit_price),
            ("exit_timestamp", self.exit_timestamp),
            ("exit_slippage", self.exit_slippage),
            ("exit_fees", self.exit_fees),
            ("realized_pl", self.realized_pl),
        )
        offenders = [name for name, value in forbidden if value is not None]
        if offenders:
            msg = f"{', '.join(offenders)} must be None when entered is False"
            raise ValueError(msg)
