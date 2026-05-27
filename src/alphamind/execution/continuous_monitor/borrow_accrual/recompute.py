"""Pure recompute kernel for the daily borrow-accrual tick (ALP-719).

For every OPEN SHORT EQUITY position the kernel:

1. Resolves the live closing print from ``close_prices``.
2. Resolves the live annualized borrow fee from ``fee_rates``.
3. Computes today's per-position cost via
   :func:`alphamind.risk_guardrails.borrow_cost.daily_borrow_cost_usd`.
4. Returns a new :class:`PositionRecord` whose
   :attr:`EquityPositionDetails.accrued_borrow_cost_usd` accumulator has
   advanced by today's cost.
5. Returns one :class:`ActivityLogEntry` (``BORROW_COST_ACCRUED``) per
   position carrying the per-tick :class:`BorrowCostAccruedDetail`.

Positions that are not OPEN SHORT EQUITY (LONG equity, options, strategy,
CLOSED, PENDING) are silently skipped — the imperative shell does not
need to filter them out before the call.

The kernel is **fail-fast**: a missing close or missing fee for any
in-scope ticker raises :class:`ValueError` naming the ticker. The shell
catches the raise at the surrounding transaction boundary and rolls back
the whole tick — partial accruals would silently underaccount P/L. Per
the pre-resolved decision on ALP-715, a tick that fails for any reason
stays lost and the next trading day's tick catches up.

The kernel is pure: no clock, no DB, no resolver — every input is passed
in by the shell. Tests exercise it without any pytest-asyncio or SQLAlchemy
machinery.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import money
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    BorrowCostAccruedDetail,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    PositionRecord,
    is_open_short_equity,
)
from alphamind.risk_guardrails.borrow_cost import daily_borrow_cost_usd

# The accrual date the activity-log entry stamps follows the configured
# US/Eastern tick wall clock — the trading day the tick covers.
_US_EASTERN = ZoneInfo("US/Eastern")


@dataclass(frozen=True, slots=True)
class AccrualTickResult:
    """Per-tick result.

    The shell consumes this in one transaction: every
    :class:`PositionRecord` in ``updated_positions`` is round-tripped
    through the codec and UPDATEd; every :class:`ActivityLogEntry` in
    ``activity_log_entries`` is inserted via
    :func:`append_activity_log_entry`. Both lists are aligned by index —
    ``activity_log_entries[i]`` carries the ``BORROW_COST_ACCRUED`` event
    for ``updated_positions[i]``.

    ``total_accrued_usd`` is the sum of today's accruals across every
    in-scope position; useful for the supervisor's log line.
    """

    updated_positions: tuple[PositionRecord, ...]
    activity_log_entries: tuple[ActivityLogEntry, ...]
    total_accrued_usd: float


def compute_tick(
    *,
    positions: tuple[PositionRecord, ...],
    close_prices: Mapping[Symbol, float],
    fee_rates: Mapping[Symbol, float | None],
    now: datetime,
    invocation_id: str,
) -> AccrualTickResult:
    """Compute per-position accruals + activity-log entries for one tick.

    ``positions`` is the unfiltered set the shell read from the DB. The
    kernel filters in-place to OPEN SHORT EQUITY only; other variants are
    silently skipped (they have no borrow obligation).

    ``close_prices`` maps every in-scope ticker to its session closing
    print. ``fee_rates`` maps every in-scope ticker to its current
    annualized borrow fee (``%/yr``) or ``None`` if the resolver had no
    row. A missing close or a ``None`` fee for any in-scope ticker raises
    :class:`ValueError` — the shell rolls the transaction back.
    """
    updated_positions: list[PositionRecord] = []
    activity_log_entries: list[ActivityLogEntry] = []
    total_accrued = 0.0

    for position in positions:
        if not _is_open_short_equity(position):
            continue
        # Narrowed by the predicate above.
        details = position.details
        assert isinstance(details, EquityPositionDetails)  # type narrowing for mypy
        ticker = details.ticker

        close = close_prices.get(ticker)
        if close is None:
            msg = (
                f"borrow-accrual tick: missing closing print for ticker {ticker!r} "
                f"(position_id={position.position_id!r}); rolling tick back"
            )
            raise ValueError(msg)

        fee_rate = fee_rates.get(ticker)
        if fee_rate is None:
            msg = (
                f"borrow-accrual tick: borrow_cost_resolver returned no rate for "
                f"ticker {ticker!r} (position_id={position.position_id!r}); "
                f"rolling tick back"
            )
            raise ValueError(msg)

        # ``share_count`` is wrapped in ``abs`` so the formula is sign-agnostic.
        # The SHORT-equity codec stores ``share_count`` as a positive value
        # today, but the math survives a future sign-convention change
        # without silently inverting the cost.
        notional_usd = abs(details.share_count * close)
        today = daily_borrow_cost_usd(
            notional_usd=notional_usd,
            annual_fee_pct=fee_rate,
        )
        prior_accrued = details.accrued_borrow_cost_usd or 0.0
        new_accrued = prior_accrued + today
        new_details = dataclasses.replace(details, accrued_borrow_cost_usd=new_accrued)
        new_record = dataclasses.replace(position, details=new_details)
        updated_positions.append(new_record)

        entry = _build_activity_log_entry(
            position=position,
            invocation_id=invocation_id,
            now=now,
            today_cost=today,
            cumulative=new_accrued,
            annual_fee_pct=fee_rate,
            notional_usd=notional_usd,
        )
        activity_log_entries.append(entry)
        total_accrued += today

    return AccrualTickResult(
        updated_positions=tuple(updated_positions),
        activity_log_entries=tuple(activity_log_entries),
        total_accrued_usd=total_accrued,
    )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


# Backwards-compatible alias retained within this module's own call sites.
# The canonical predicate lives at
# :func:`alphamind.portfolio_state.records.positions.is_open_short_equity`;
# re-exported through ``__all__`` below so existing importers (and the shell
# at ``task.py``) keep working.
_is_open_short_equity = is_open_short_equity


def _build_activity_log_entry(
    *,
    position: PositionRecord,
    invocation_id: str,
    now: datetime,
    today_cost: float,
    cumulative: float,
    annual_fee_pct: float,
    notional_usd: float,
) -> ActivityLogEntry:
    """Compose the ``BORROW_COST_ACCRUED`` entry for *position* this tick.

    ``entry_id`` carries the timestamp + an 8-hex random suffix so two
    positions accruing in the same tick do not collide. ``accrual_date``
    is the US/Eastern wall-clock date the tick covers (per the design's
    *Off-tick reads* note: the persisted accrual is a discrete EOD
    quantity keyed off the trading day).

    Source is :attr:`EventSource.BORROW_ACCRUAL_MONITOR` — the continuous
    monitor's dedicated borrow-accrual sub-task. Using a dedicated source
    keeps the strategist's source-classification logic from conflating
    daily borrow accrual with margin-call signals
    (``EventSource.MARGIN_MONITOR``).
    """
    suffix = secrets.token_hex(4)
    entry_id = f"mon-bra-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{suffix}"
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=now,
        event_type=EventType.BORROW_COST_ACCRUED,
        event_group=EventGroup.CASH_AND_MARGIN,
        position_id=position.position_id,
        order_id=None,
        thesis_id=position.thesis_id,
        source=EventSource.BORROW_ACCRUAL_MONITOR,
        detail=BorrowCostAccruedDetail(
            accrued_amount_usd=money(today_cost),
            cumulative_accrued_usd=money(cumulative),
            annual_fee_pct_used=annual_fee_pct,
            notional_usd_used=money(notional_usd),
            accrual_date=now.astimezone(_US_EASTERN).date(),
        ),
    )


__all__ = [
    "AccrualTickResult",
    "compute_tick",
    "is_open_short_equity",
]
