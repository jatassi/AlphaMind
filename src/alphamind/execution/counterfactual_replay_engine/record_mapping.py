"""Pure result→record mapping for the engine driver (ALP-564, story 08).

Folds a per-proposal replay result — :class:`EquityReplayResult`,
:class:`OptionReplayResult`, or :class:`StrategistActionResult` — into a
:class:`CounterfactualReplayRecord`, building the :class:`ConfidenceSignals` and
calling :func:`classify_confidence` for EVALUATED records along the way. No
session, no clock: the driver supplies ``replay_timestamp`` and the window
bounds, and these functions are pure over plain values so the shape rules below
are unit-testable in isolation.

Shape rules (the persisted record's ``__post_init__`` enforces them; these
functions produce conforming inputs):

* **Equity not-entered** (``ENTRY_WINDOW_EXPIRED_UNFILLED``) — ``realized_pl``
  and entry/exit slippage/fees stay ``None``; they are NOT re-``Money(0)``-d.
* **Option / strategist ``data_missing``** — produces an UNEVALUABLE record with
  reason ``DATA_MISSING`` (no window, no confidence, no outcome fields). Such a
  result's outcome fields are ``None`` / ``False`` by design, so it never builds
  an EVALUATED record.
* **EVALUATED** — ``confidence`` is stamped and ``replay_engine_version`` is the
  ``"v2"`` constant from :mod:`.confidence`.

The record's ``entry_price`` / ``exit_price`` are :class:`Money` (non-negative,
may be zero). Option premium fields on the result are already :class:`Money` (a
worthless option at/after expiry is a legitimate zero premium) and pass straight
through; the equity result's :class:`Price` fields are widened to :class:`Money`
(a price is a non-negative money) via :func:`money`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind._kernel.money import Money, Price, money
from alphamind.execution.counterfactual_replay_engine.confidence import (
    ConfidenceSignals,
    classify_confidence,
    replay_engine_version,
)
from alphamind.state.tables.counterfactual_replays import (
    CounterfactualReplayRecord,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)

if TYPE_CHECKING:
    from alphamind.execution.counterfactual_replay_engine.equity_replay import (
        EquityReplayResult,
    )
    from alphamind.execution.counterfactual_replay_engine.option_replay import (
        OptionReplayResult,
    )
    from alphamind.execution.counterfactual_replay_engine.strategist_replay import (
        StrategistActionResult,
    )

__all__ = [
    "MappingContext",
    "record_from_equity_result",
    "record_from_option_result",
    "record_from_strategist_result",
    "unevaluable_record",
]


@dataclass(frozen=True, slots=True)
class MappingContext:
    """The per-proposal context the driver threads into the mapping functions.

    Bundles the envelope / kind identity, the data-quality envelope flags the
    confidence classifier reads (bar-coverage + paper-harness liquidity / spread
    envelopes; ``None`` flags are "unknown" and never demote on their own), the
    replay window bounds, and the ``replay_timestamp`` (``now(UTC)`` the driver
    captures). Carried as one value object so the mapping signatures stay small.
    """

    envelope_id: EnvelopeId
    replay_kind: ReplayKind
    bar_coverage_complete: bool
    liquidity_within_typical_envelope: bool | None
    spread_within_typical_envelope: bool | None
    window_start: datetime
    window_end: datetime
    replay_timestamp: datetime


def _replay_id(envelope_id: EnvelopeId, replay_kind: ReplayKind) -> ReplayId:
    """Deterministic replay id keyed by ``(envelope, kind)`` — matches the unevaluable writer."""
    return ReplayId(f"CFR-{envelope_id}-{replay_kind.value}")


def unevaluable_record(
    *,
    envelope_id: EnvelopeId,
    replay_kind: ReplayKind,
    reason: UnevaluableReason,
    replay_timestamp: datetime,
) -> CounterfactualReplayRecord:
    """Build an UNEVALUABLE :class:`CounterfactualReplayRecord` (no window / confidence)."""
    return CounterfactualReplayRecord(
        replay_id=_replay_id(envelope_id, replay_kind),
        pm_decision_envelope_id=envelope_id,
        replay_kind=replay_kind,
        replay_status=ReplayStatus.UNEVALUABLE,
        unevaluable_reason=reason,
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
        replay_timestamp=replay_timestamp,
        replay_data_window_start=None,
        replay_data_window_end=None,
        replay_engine_version=replay_engine_version,
    )


def _price_to_money(value: Price | None) -> Money | None:
    """Widen an equity :class:`Price` field to :class:`Money` (a price is non-negative)."""
    return None if value is None else money(value)


@dataclass(frozen=True, slots=True)
class _EvaluatedOutcome:
    """The entry/exit outcome fields shared by all three result shapes.

    Each public mapping function normalises its native result into this one
    shape — the option / strategist :class:`Money` premiums pass through, the
    equity :class:`Price` fields are widened to :class:`Money` — so
    :func:`_evaluated_record` builds the record from a single bundle.
    """

    entered: bool
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


def _evaluated_record(
    ctx: MappingContext,
    outcome: _EvaluatedOutcome,
    signals: ConfidenceSignals,
) -> CounterfactualReplayRecord:
    """Build the EVALUATED record common to all three result shapes."""
    return CounterfactualReplayRecord(
        replay_id=_replay_id(ctx.envelope_id, ctx.replay_kind),
        pm_decision_envelope_id=ctx.envelope_id,
        replay_kind=ctx.replay_kind,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=outcome.entered,
        entry_price=outcome.entry_price,
        entry_timestamp=outcome.entry_timestamp,
        entry_slippage=outcome.entry_slippage,
        entry_fees=outcome.entry_fees,
        exit_leg=outcome.exit_leg,
        exit_price=outcome.exit_price,
        exit_timestamp=outcome.exit_timestamp,
        exit_slippage=outcome.exit_slippage,
        exit_fees=outcome.exit_fees,
        realized_pl=outcome.realized_pl,
        confidence=classify_confidence(signals),
        replay_timestamp=ctx.replay_timestamp,
        replay_data_window_start=ctx.window_start,
        replay_data_window_end=ctx.window_end,
        replay_engine_version=replay_engine_version,
    )


def record_from_equity_result(
    result: EquityReplayResult,
    ctx: MappingContext,
) -> CounterfactualReplayRecord:
    """Fold an equity replay result into an EVALUATED record (equity never data-misses)."""
    signals = ConfidenceSignals(
        instrument_kind="equity",
        same_bar_ambiguity=result.same_bar_ambiguity,
        bar_coverage_complete=ctx.bar_coverage_complete,
        liquidity_within_typical_envelope=ctx.liquidity_within_typical_envelope,
        spread_within_typical_envelope=ctx.spread_within_typical_envelope,
        iv_lag_minutes_entry=None,
        iv_lag_minutes_exit=None,
        iv_lag_threshold_minutes=None,
    )
    outcome = _EvaluatedOutcome(
        entered=result.entered,
        entry_price=_price_to_money(result.entry_price),
        entry_timestamp=result.entry_timestamp,
        entry_slippage=result.entry_slippage,
        entry_fees=result.entry_fees,
        exit_leg=result.exit_leg,
        exit_price=_price_to_money(result.exit_price),
        exit_timestamp=result.exit_timestamp,
        exit_slippage=result.exit_slippage,
        exit_fees=result.exit_fees,
        realized_pl=result.realized_pl,
    )
    return _evaluated_record(ctx, outcome, signals)


def record_from_option_result(
    result: OptionReplayResult,
    ctx: MappingContext,
    *,
    iv_lag_threshold_minutes: float,
) -> CounterfactualReplayRecord:
    """Fold an option replay result into a record.

    A ``data_missing`` result becomes UNEVALUABLE / DATA_MISSING; otherwise it is
    an EVALUATED record with the IV-lag confidence signals.
    """
    if result.data_missing:
        return unevaluable_record(
            envelope_id=ctx.envelope_id,
            replay_kind=ctx.replay_kind,
            reason=UnevaluableReason.DATA_MISSING,
            replay_timestamp=ctx.replay_timestamp,
        )
    signals = ConfidenceSignals(
        instrument_kind="option",
        same_bar_ambiguity=result.same_bar_ambiguity,
        bar_coverage_complete=ctx.bar_coverage_complete,
        liquidity_within_typical_envelope=ctx.liquidity_within_typical_envelope,
        spread_within_typical_envelope=ctx.spread_within_typical_envelope,
        iv_lag_minutes_entry=result.entry_iv_lag_minutes,
        iv_lag_minutes_exit=result.exit_iv_lag_minutes,
        iv_lag_threshold_minutes=iv_lag_threshold_minutes,
    )
    outcome = _EvaluatedOutcome(
        entered=result.entered,
        entry_price=result.entry_price,
        entry_timestamp=result.entry_timestamp,
        entry_slippage=result.entry_slippage,
        entry_fees=result.entry_fees,
        exit_leg=result.exit_leg,
        exit_price=result.exit_price,
        exit_timestamp=result.exit_timestamp,
        exit_slippage=result.exit_slippage,
        exit_fees=result.exit_fees,
        realized_pl=result.realized_pl,
    )
    return _evaluated_record(ctx, outcome, signals)


def record_from_strategist_result(
    result: StrategistActionResult,
    ctx: MappingContext,
    *,
    instrument_kind: Literal["equity", "option"],
) -> CounterfactualReplayRecord:
    """Fold a strategist-action result into a record.

    A ``data_missing`` result (option-position IV miss) becomes UNEVALUABLE /
    DATA_MISSING; otherwise it is an EVALUATED record. The strategist path
    carries no per-IV-lag signal on the result, so the IV-lag confidence fields
    stay ``None`` (the confidence classifier never demotes a strategist record on
    IV staleness).
    """
    if result.data_missing:
        return unevaluable_record(
            envelope_id=ctx.envelope_id,
            replay_kind=ctx.replay_kind,
            reason=UnevaluableReason.DATA_MISSING,
            replay_timestamp=ctx.replay_timestamp,
        )
    signals = ConfidenceSignals(
        instrument_kind=instrument_kind,
        same_bar_ambiguity=result.same_bar_ambiguity,
        bar_coverage_complete=ctx.bar_coverage_complete,
        liquidity_within_typical_envelope=ctx.liquidity_within_typical_envelope,
        spread_within_typical_envelope=ctx.spread_within_typical_envelope,
        iv_lag_minutes_entry=None,
        iv_lag_minutes_exit=None,
        iv_lag_threshold_minutes=None,
    )
    outcome = _EvaluatedOutcome(
        entered=result.entered,
        entry_price=result.entry_price,
        entry_timestamp=result.entry_timestamp,
        entry_slippage=result.entry_slippage,
        entry_fees=result.entry_fees,
        exit_leg=result.exit_leg,
        exit_price=result.exit_price,
        exit_timestamp=result.exit_timestamp,
        exit_slippage=result.exit_slippage,
        exit_fees=result.exit_fees,
        realized_pl=result.realized_pl,
    )
    return _evaluated_record(ctx, outcome, signals)
