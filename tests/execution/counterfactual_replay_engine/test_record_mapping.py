"""Tests for the pure result→record mapping layer (ALP-564, story 08).

``record_mapping`` folds a per-proposal replay result into a
``CounterfactualReplayRecord``. The mapping is pure (no session, no clock beyond
the ``replay_timestamp`` the driver supplies) and carries the gotcha-laden
shape rules:

* equity not-entered (ENTRY_WINDOW_EXPIRED_UNFILLED) keeps realized_pl AND
  entry/exit slippage/fees as ``None`` — they are not re-``Money(0)``-d;
* an option / strategist result with ``data_missing=True`` produces an
  UNEVALUABLE record (reason DATA_MISSING), never an EVALUATED one;
* a ``data_missing=False`` result maps to an EVALUATED record with confidence
  stamped and ``replay_engine_version == "v2"``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from alphamind._kernel.ids import EnvelopeId
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.counterfactual_replay_engine.confidence import replay_engine_version
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.equity_replay import EquityReplayResult
from alphamind.execution.counterfactual_replay_engine.option_replay import OptionReplayResult
from alphamind.execution.counterfactual_replay_engine.record_mapping import (
    MappingContext,
    record_from_equity_result,
    record_from_option_result,
    record_from_strategist_result,
)
from alphamind.execution.counterfactual_replay_engine.strategist_replay import (
    StrategistActionResult,
)

_ENVELOPE = EnvelopeId("env-1")
_WINDOW_START = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
_WINDOW_END = datetime(2026, 6, 2, 14, 0, tzinfo=UTC)
_ENTRY_TS = datetime(2026, 6, 1, 14, 15, tzinfo=UTC)
_EXIT_TS = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
_REPLAY_TS = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)


def _equity_entered() -> EquityReplayResult:
    return EquityReplayResult(
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
        same_bar_ambiguity=False,
    )


def _equity_not_entered() -> EquityReplayResult:
    return EquityReplayResult(
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
        same_bar_ambiguity=False,
    )


def _option_evaluated() -> OptionReplayResult:
    return OptionReplayResult(
        entered=True,
        entry_price=money("5.00"),
        entry_timestamp=_ENTRY_TS,
        entry_slippage=money("0.05"),
        entry_fees=money("0.02"),
        entry_iv_lag_minutes=2.0,
        exit_leg=ExitLeg.TARGET_HIT,
        exit_underlying_price=110.0,
        exit_price=money("8.00"),
        exit_timestamp=_EXIT_TS,
        exit_slippage=money("0.05"),
        exit_fees=money("0.02"),
        exit_iv_lag_minutes=3.0,
        realized_pl=signed_money("294.86"),
        data_missing=False,
        same_bar_ambiguity=False,
    )


def _option_data_missing() -> OptionReplayResult:
    return OptionReplayResult(
        entered=False,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        entry_iv_lag_minutes=None,
        exit_leg=None,
        exit_underlying_price=None,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        exit_iv_lag_minutes=None,
        realized_pl=None,
        data_missing=True,
        same_bar_ambiguity=False,
    )


def _strategist_evaluated() -> StrategistActionResult:
    return StrategistActionResult(
        entered=True,
        entry_price=money("100.00"),
        entry_timestamp=_ENTRY_TS,
        entry_slippage=money("0"),
        entry_fees=money("0"),
        exit_leg=ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
        exit_price=money("110.00"),
        exit_timestamp=_EXIT_TS,
        exit_slippage=money("0.05"),
        exit_fees=money("0.01"),
        realized_pl=signed_money("499.94"),
        data_missing=False,
        same_bar_ambiguity=False,
    )


def _strategist_data_missing() -> StrategistActionResult:
    return StrategistActionResult(
        entered=False,
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
        data_missing=True,
        same_bar_ambiguity=False,
    )


def _ctx(
    *,
    bar_coverage_complete: bool = True,
    liquidity: bool | None = True,
    spread: bool | None = True,
) -> MappingContext:
    return MappingContext(
        envelope_id=_ENVELOPE,
        replay_kind=ReplayKind.REJECTION,
        bar_coverage_complete=bar_coverage_complete,
        liquidity_within_typical_envelope=liquidity,
        spread_within_typical_envelope=spread,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        replay_timestamp=_REPLAY_TS,
    )


class TestEquityMapping:
    def test_entered_maps_to_evaluated_record(self) -> None:
        record = record_from_equity_result(_equity_entered(), _ctx())
        assert record.replay_status is ReplayStatus.EVALUATED
        assert record.entered is True
        assert record.realized_pl == signed_money("9.88")
        assert record.confidence is Confidence.HIGH
        assert record.replay_engine_version == replay_engine_version
        assert record.replay_data_window_start == _WINDOW_START
        assert record.replay_data_window_end == _WINDOW_END

    def test_not_entered_keeps_none_monetary_fields(self) -> None:
        record = record_from_equity_result(_equity_not_entered(), _ctx())
        assert record.replay_status is ReplayStatus.EVALUATED
        assert record.entered is False
        assert record.exit_leg is ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED
        # The not-entered fields stay None — NOT re-Money(0)-d.
        assert record.realized_pl is None
        assert record.entry_slippage is None
        assert record.entry_fees is None
        assert record.exit_slippage is None
        assert record.exit_fees is None

    def test_same_bar_ambiguity_demotes_to_medium(self) -> None:
        ambiguous = replace(_equity_entered(), same_bar_ambiguity=True)
        record = record_from_equity_result(ambiguous, _ctx())
        assert record.confidence is Confidence.MEDIUM


class TestOptionMapping:
    def test_evaluated_maps_to_evaluated_record(self) -> None:
        record = record_from_option_result(
            _option_evaluated(), _ctx(), iv_lag_threshold_minutes=30.0
        )
        assert record.replay_status is ReplayStatus.EVALUATED
        assert record.entered is True
        assert record.entry_price == price("5.00")
        assert record.realized_pl == signed_money("294.86")
        assert record.confidence is Confidence.HIGH

    def test_data_missing_maps_to_unevaluable(self) -> None:
        record = record_from_option_result(
            _option_data_missing(), _ctx(), iv_lag_threshold_minutes=30.0
        )
        assert record.replay_status is ReplayStatus.UNEVALUABLE
        assert record.unevaluable_reason is UnevaluableReason.DATA_MISSING
        assert record.entered is None
        assert record.realized_pl is None
        # UNEVALUABLE records carry no window.
        assert record.replay_data_window_start is None
        assert record.replay_data_window_end is None

    def test_stale_iv_demotes_to_low(self) -> None:
        stale = replace(_option_evaluated(), exit_iv_lag_minutes=90.0)
        record = record_from_option_result(stale, _ctx(), iv_lag_threshold_minutes=30.0)
        assert record.confidence is Confidence.LOW


class TestStrategistMapping:
    def test_evaluated_maps_to_evaluated_record(self) -> None:
        record = record_from_strategist_result(
            _strategist_evaluated(), _ctx(), instrument_kind="equity"
        )
        assert record.replay_status is ReplayStatus.EVALUATED
        assert record.exit_leg is ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL
        assert record.entry_price == price("100.00")
        assert record.realized_pl == signed_money("499.94")

    def test_data_missing_maps_to_unevaluable(self) -> None:
        record = record_from_strategist_result(
            _strategist_data_missing(), _ctx(), instrument_kind="option"
        )
        assert record.replay_status is ReplayStatus.UNEVALUABLE
        assert record.unevaluable_reason is UnevaluableReason.DATA_MISSING
