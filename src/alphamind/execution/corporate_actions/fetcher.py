"""v1beta1 corporate-actions fetcher (ALP-410).

``fetch_unprocessed_ca_activities`` is the function Phase 1 invokes to pull
new typed CA events from Alpaca's v1beta1 ``GET /v1beta1/corporate-actions``
endpoint, translate each into a :class:`CorporateActionActivity`, filter
against the integration ledger, and return them in ascending
``transaction_time`` order so Phase 1's chronological merge can interleave
them with fills.

Cursor derivation reads ``max(processing_timestamp)`` from
``corporate_action_integration_ledger``; the lookback window
(``config.fetcher_lookback_days``) absorbs Alpaca's late posts.  The per-event
UUID dedup against the ledger handles the resulting overlap.

Event-type → ``CorporateActionType`` discrimination follows the corporate-
actions design doc; for ``CashDividend`` the discriminator additionally reads
the matched position's ``direction`` (LONG → ``CASH_DIVIDEND_LONG`` /
SHORT → ``CASH_DIVIDEND_SHORT``).

Cash-impact arithmetic: the fetcher multiplies per-share rates (cash dividend
``rate``, cash merger ``rate``, stock-and-cash-merger ``cash_rate``) by the
matched position's quantity once so handlers can read absolute USD directly
from ``signed_cash_impact_usd``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, cast

from alpaca.data.enums import CorporateActionsType
from alpaca.data.models.corporate_actions import (
    CashDividend,
    CashMerger,
    CorporateAction,
    ForwardSplit,
    NameChange,
    Redemption,
    ReverseSplit,
    SpinOff,
    StockAndCashMerger,
    StockDividend,
    StockMerger,
    UnitSplit,
    WorthlessRemoval,
)
from sqlalchemy import func, select

from alphamind.portfolio_state.events.activity_log import CorporateActionType
from alphamind.portfolio_state.records.positions import Direction
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)

from .config import CorporateActionsConfig
from .types import CorporateActionActivity, PositionLookup


class _CorporateActionsQueriesProtocol(Protocol):
    """Structural shape of the v1beta1 query wrapper the fetcher consumes."""

    async def get_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...] | None = ...,
        start: date,
        end: date,
        types: tuple[CorporateActionsType, ...] = ...,
    ) -> tuple[CorporateAction, ...]: ...


# ---------------------------------------------------------------------------
# Cursor derivation
# ---------------------------------------------------------------------------


async def _derive_start_date(handle: InvocationHandle, *, lookback_days: int) -> date:
    """Compute the inclusive start date for the v1beta1 lookup window.

    Cold start (empty ledger): ``today - lookback_days``.

    Warm start: ``max(processing_timestamp).date() - lookback_days``.  The
    lookback absorbs Alpaca's late posts; UUID dedup against the ledger
    handles the overlap.
    """
    today = datetime.now(UTC).date()
    result = await handle.session.execute(
        select(func.max(CorporateActionIntegrationLedgerRow.processing_timestamp))
    )
    raw = result.scalar_one_or_none()
    if raw is None:
        return today - timedelta(days=lookback_days)
    anchor = datetime.fromisoformat(raw).date()
    return anchor - timedelta(days=lookback_days)


# ---------------------------------------------------------------------------
# Event-type dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Translated:
    """Per-event extracted fields the fetcher writes to ``CorporateActionActivity``.

    Mirrors ``CorporateActionActivity`` minus ``alpaca_activity_id``,
    ``position_id``, and ``transaction_time`` which the public translator
    fills in uniformly.  ``cash_factor`` is multiplied by the matched
    position's quantity to obtain absolute USD ``signed_cash_impact_usd``.
    """

    action_type: CorporateActionType
    ticker: str
    primary_date: date
    new_ticker: str | None = None
    ratio_or_amount: float = 0.0
    cash_factor: float = 0.0


def _from_forward_split(event: ForwardSplit, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.SPLIT,
        ticker=event.symbol,
        primary_date=event.ex_date,
        ratio_or_amount=event.new_rate / event.old_rate,
    )


def _from_reverse_split(event: ReverseSplit, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker=event.symbol,
        primary_date=event.ex_date,
        ratio_or_amount=event.new_rate / event.old_rate,
    )


def _from_stock_dividend(event: StockDividend, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=event.symbol,
        primary_date=event.ex_date,
        ratio_or_amount=event.rate,
    )


def _from_cash_dividend(event: CashDividend, position: PositionLookup) -> _Translated:
    is_long = position.direction == Direction.LONG
    return _Translated(
        action_type=(
            CorporateActionType.CASH_DIVIDEND_LONG
            if is_long
            else CorporateActionType.CASH_DIVIDEND_SHORT
        ),
        ticker=event.symbol,
        primary_date=event.ex_date,
        cash_factor=event.rate if is_long else -event.rate,
    )


def _from_spin_off(event: SpinOff, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.SPIN_OFF,
        ticker=event.source_symbol,
        primary_date=event.ex_date,
        new_ticker=event.new_symbol,
        ratio_or_amount=event.new_rate / event.source_rate,
    )


def _from_cash_merger(event: CashMerger, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.CASH_MERGER,
        ticker=event.acquiree_symbol,
        primary_date=event.effective_date,
        cash_factor=event.rate,
    )


def _from_stock_and_cash_merger(event: StockAndCashMerger, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.STOCK_MERGER,
        ticker=event.acquiree_symbol,
        primary_date=event.effective_date,
        new_ticker=event.acquirer_symbol,
        cash_factor=event.cash_rate,
    )


def _from_stock_merger(event: StockMerger, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.STOCK_MERGER,
        ticker=event.acquiree_symbol,
        primary_date=event.effective_date,
        new_ticker=event.acquirer_symbol,
    )


def _from_name_change(event: NameChange, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker=event.old_symbol,
        primary_date=event.process_date,
        new_ticker=event.new_symbol,
    )


def _from_worthless_removal(event: WorthlessRemoval, _: PositionLookup) -> _Translated:
    # Capture-only (no position-mutation math): the broker fact lands on the
    # event log; quantity / basis / cash are left to a later integration if ever
    # defined. ``ratio_or_amount`` / ``cash_factor`` stay zero.
    return _Translated(
        action_type=CorporateActionType.WORTHLESS_REMOVAL,
        ticker=event.symbol,
        primary_date=event.process_date,
    )


def _from_unit_split(event: UnitSplit, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.UNIT_SPLIT,
        ticker=event.old_symbol,
        primary_date=event.process_date,
    )


def _from_redemption(event: Redemption, _: PositionLookup) -> _Translated:
    return _Translated(
        action_type=CorporateActionType.REDEMPTION,
        ticker=event.symbol,
        primary_date=event.process_date,
    )


_TranslatorFn = Callable[[CorporateAction, PositionLookup], _Translated]


# StockAndCashMerger before StockMerger only as a defensive precaution
# against a future SDK change in which the former becomes a subclass of the
# latter; in alpaca-py 0.43 they are siblings and order doesn't matter.
# ``cast`` widens each per-type translator to the uniform tuple element type;
# the ``isinstance`` check in ``_translate`` reasserts the precondition at
# the call site.
_TRANSLATORS: tuple[tuple[type[CorporateAction], _TranslatorFn], ...] = (
    (ForwardSplit, cast(_TranslatorFn, _from_forward_split)),
    (ReverseSplit, cast(_TranslatorFn, _from_reverse_split)),
    (StockDividend, cast(_TranslatorFn, _from_stock_dividend)),
    (CashDividend, cast(_TranslatorFn, _from_cash_dividend)),
    (SpinOff, cast(_TranslatorFn, _from_spin_off)),
    (CashMerger, cast(_TranslatorFn, _from_cash_merger)),
    (StockAndCashMerger, cast(_TranslatorFn, _from_stock_and_cash_merger)),
    (StockMerger, cast(_TranslatorFn, _from_stock_merger)),
    (NameChange, cast(_TranslatorFn, _from_name_change)),
    # Capture-only types (ALP-849 / W1c): surfaced so Phase 1 appends them to the
    # event log, but with no position-mutation math (zero ratio / cash).
    (WorthlessRemoval, cast(_TranslatorFn, _from_worthless_removal)),
    (UnitSplit, cast(_TranslatorFn, _from_unit_split)),
    (Redemption, cast(_TranslatorFn, _from_redemption)),
)


def _translate(event: CorporateAction, position: PositionLookup) -> _Translated | None:
    """Map a v1beta1 typed event onto the local activity fields.

    Returns ``None`` only for ``RightsDistribution`` — the one v1beta1 type with
    no event-log CA vocabulary member; every other type (including the
    capture-only ``WorthlessRemoval`` / ``UnitSplit`` / ``Redemption``) translates.
    """
    for event_type, translator in _TRANSLATORS:
        if isinstance(event, event_type):
            return translator(event, position)
    return None


def _symbol_for(event: CorporateAction) -> str | None:
    """Return the ticker the fetcher matches against ``position_lookup_for_symbol``.

    Returns ``None`` only for ``RightsDistribution`` (no event-log CA vocabulary
    member); callers drop it silently. Mirrors the ``ticker`` field in
    :func:`_translate` so symbol resolution can short-circuit the position
    lookup before paying for translation.
    """
    match event:
        case ForwardSplit() | ReverseSplit() | StockDividend() | CashDividend():
            return event.symbol
        case SpinOff():
            return event.source_symbol
        case CashMerger() | StockMerger() | StockAndCashMerger():
            return event.acquiree_symbol
        case NameChange() | UnitSplit():
            return event.old_symbol
        case WorthlessRemoval() | Redemption():
            return event.symbol
        case _:
            return None


def _build_activity(
    event: CorporateAction,
    *,
    position: PositionLookup,
) -> CorporateActionActivity | None:
    """Translate a v1beta1 typed event into a ``CorporateActionActivity``.

    Returns ``None`` for events the fetcher does not handle natively.
    """
    translated = _translate(event, position)
    if translated is None:
        return None
    return CorporateActionActivity(
        alpaca_activity_id=str(event.id),
        action_type=translated.action_type,
        ticker=translated.ticker,
        new_ticker=translated.new_ticker,
        ratio_or_amount=translated.ratio_or_amount,
        position_id=position.position_id,
        signed_cash_impact_usd=translated.cash_factor * position.quantity,
        transaction_time=datetime.combine(translated.primary_date, time.min, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Ledger dedup
# ---------------------------------------------------------------------------


async def _already_processed(handle: InvocationHandle, alpaca_activity_id: str) -> bool:
    """Return ``True`` if the activity ID is already in the integration ledger."""
    row = await handle.session.get(CorporateActionIntegrationLedgerRow, alpaca_activity_id)
    return row is not None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def fetch_unprocessed_ca_activities(
    handle: InvocationHandle,
    queries: _CorporateActionsQueriesProtocol,
    *,
    config: CorporateActionsConfig,
    position_lookup_for_symbol: Callable[[str], PositionLookup | None],
    known_symbols: tuple[str, ...],
) -> tuple[CorporateActionActivity, ...]:
    """Fetch new v1beta1 CA events, translate them, and filter against the ledger.

    Args:
        handle: Open ``InvocationHandle`` from the surrounding
            ``InvocationContext``.
        queries: The :class:`CorporateActionsQueries` wrapper (or any object
            with the same async ``get_corporate_actions`` shape).
        config: Operator-tunable knobs; this story reads
            ``fetcher_lookback_days``.
        position_lookup_for_symbol: Maps a ticker to the local
            :class:`PositionLookup`.  Returns ``None`` for symbols AlphaMind
            has no exposure to; those events are filtered out.
        known_symbols: The set of tickers AlphaMind has local exposure to,
            built from the position-snapshot iteration. Passed to the v1beta1
            endpoint via ``symbols=`` so the broker narrows the response
            server-side; when empty, the call short-circuits to ``()``.

    Returns:
        Tuple of :class:`CorporateActionActivity` sorted ascending by
        ``transaction_time``.
    """
    if not known_symbols:
        return ()

    start = await _derive_start_date(handle, lookback_days=config.fetcher_lookback_days)
    end = datetime.now(UTC).date()

    events = await queries.get_corporate_actions(
        symbols=known_symbols,
        start=start,
        end=end,
    )

    activities: list[CorporateActionActivity] = []
    for event in events:
        symbol = _symbol_for(event)
        if symbol is None:
            continue
        position = position_lookup_for_symbol(symbol)
        if position is None:
            continue
        if await _already_processed(handle, str(event.id)):
            continue
        activity = _build_activity(event, position=position)
        if activity is None:
            continue
        activities.append(activity)

    activities.sort(key=lambda a: a.transaction_time)
    return tuple(activities)


__all__ = ["fetch_unprocessed_ca_activities"]
