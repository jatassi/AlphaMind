"""Cross-field invariant tests for ``CounterfactualReplayRecord`` (ALP-555).

One test per ``__post_init__`` branch. The fixtures build minimal valid
records per status and mutate a single field per test so each failure isolates
the invariant it exercises.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
)

_NOW = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
_ENTRY_TS = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
_EXIT_TS = datetime(2026, 1, 3, 16, 0, tzinfo=UTC)


def _evaluated_entered() -> CounterfactualReplayRecord:
    """A valid EVALUATED record where the entry filled and a bracket exited."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId("RPL-1"),
        pm_decision_envelope_id=EnvelopeId("ENV-REC-1"),
        replay_kind=ReplayKind.REJECTION,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=True,
        entry_price=price("100.00"),
        entry_timestamp=_ENTRY_TS,
        entry_slippage=money("0.05"),
        entry_fees=money("0.01"),
        exit_leg=ExitLeg.TARGET_HIT,
        exit_price=price("110.00"),
        exit_timestamp=_EXIT_TS,
        exit_slippage=money("0.05"),
        exit_fees=money("0.01"),
        realized_pl=signed_money("9.88"),
        confidence=Confidence.HIGH,
        replay_timestamp=_NOW,
        replay_data_window_start=_ENTRY_TS,
        replay_data_window_end=_EXIT_TS,
        replay_engine_version="v2.0.0",
    )


def _evaluated_not_entered() -> CounterfactualReplayRecord:
    """A valid EVALUATED record where the entry window expired unfilled."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId("RPL-2"),
        pm_decision_envelope_id=EnvelopeId("ENV-REC-2"),
        replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=False,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        confidence=Confidence.MEDIUM,
        replay_timestamp=_NOW,
        replay_data_window_start=_ENTRY_TS,
        replay_data_window_end=None,
        replay_engine_version="v2.0.0",
    )


def _unevaluable() -> CounterfactualReplayRecord:
    """A valid UNEVALUABLE record."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId("RPL-3"),
        pm_decision_envelope_id=EnvelopeId("ENV-REC-3"),
        replay_kind=ReplayKind.REJECTION,
        replay_status=ReplayStatus.UNEVALUABLE,
        unevaluable_reason=UnevaluableReason.UNSUPPORTED_INSTRUMENT,
        entered=None,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=None,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        confidence=None,
        replay_timestamp=_NOW,
        replay_data_window_start=None,
        replay_data_window_end=None,
        replay_engine_version="v2.0.0",
    )


def test_evaluated_entered_record_is_valid() -> None:
    record = _evaluated_entered()
    assert record.replay_status is ReplayStatus.EVALUATED
    assert record.entered is True


def test_evaluated_not_entered_record_is_valid() -> None:
    record = _evaluated_not_entered()
    assert record.entered is False
    assert record.exit_leg is ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED


def test_unevaluable_record_is_valid() -> None:
    record = _unevaluable()
    assert record.replay_status is ReplayStatus.UNEVALUABLE
    assert record.unevaluable_reason is UnevaluableReason.UNSUPPORTED_INSTRUMENT


# --- _check_datetimes_utc -------------------------------------------------


def test_naive_datetime_rejected() -> None:
    base = _evaluated_entered()
    with pytest.raises(ValueError, match="entry_timestamp must be tz-aware"):
        replace(base, entry_timestamp=datetime(2026, 1, 2, 16, 0))  # noqa: DTZ001


# --- _check_unevaluable ---------------------------------------------------


def test_unevaluable_without_reason_rejected() -> None:
    base = _unevaluable()
    with pytest.raises(ValueError, match="unevaluable_reason is required"):
        replace(base, unevaluable_reason=None)


def test_unevaluable_with_outcome_field_rejected() -> None:
    base = _unevaluable()
    with pytest.raises(ValueError, match="must be None when replay_status is UNEVALUABLE"):
        replace(base, confidence=Confidence.HIGH)


# --- _check_evaluated -----------------------------------------------------


def test_evaluated_with_reason_rejected() -> None:
    base = _evaluated_entered()
    with pytest.raises(ValueError, match="unevaluable_reason must be None"):
        replace(base, unevaluable_reason=UnevaluableReason.DATA_MISSING)


def test_evaluated_without_entered_rejected() -> None:
    base = _evaluated_entered()
    with pytest.raises(ValueError, match="entered is required"):
        replace(base, entered=None)


def test_evaluated_without_confidence_rejected() -> None:
    base = _evaluated_entered()
    with pytest.raises(ValueError, match="confidence is required"):
        replace(base, confidence=None)


# --- _check_entered -------------------------------------------------------


def test_entered_without_entry_field_rejected() -> None:
    base = _evaluated_entered()
    with pytest.raises(ValueError, match="must be non-None when entered is True"):
        replace(base, entry_price=None)


# --- _check_not_entered ---------------------------------------------------


def test_not_entered_with_wrong_exit_leg_rejected() -> None:
    base = _evaluated_not_entered()
    with pytest.raises(ValueError, match="exit_leg must be ENTRY_WINDOW_EXPIRED_UNFILLED"):
        replace(base, exit_leg=ExitLeg.TARGET_HIT)


def test_not_entered_with_exit_field_rejected() -> None:
    base = _evaluated_not_entered()
    with pytest.raises(ValueError, match="must be None when entered is False"):
        replace(base, realized_pl=signed_money("5.00"))
