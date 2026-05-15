"""Post-Phase-1 reconciliation step (ALP-415 / story 04).

After all fills + CA activities are integrated, :func:`reconcile` compares
local state to Alpaca's authoritative ``GET /v2/positions`` /
``GET /v2/account`` snapshot (the caller-supplied
:class:`PositionSnapshot` / :class:`TradeAccountSnapshot` records) and emits
one ``RECONCILIATION_ALERT`` activity-log entry per unexplained delta
beyond the documented tolerance.

Tolerances:

* ``_QTY_EPSILON`` (1e-9) for position quantities — mirrors the
  ``phase1`` write-path's existing constant.
* ``_CASH_EPSILON`` (0.01 USD) for cash-balance comparisons.

Auto-correction is *not* performed here — local state is preserved as-is and
the operator handles divergences out-of-band per the parent issue's
pre-resolved decision (A). Deferred to ALP-123 (continuous monitor's
reconciliation pass).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select

from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    ReconciliationAlertDetail,
)
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionStatus,
)
from alphamind.state.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

# Mirrors phase1.py's tolerance for "quantities are effectively equal".
_QTY_EPSILON = 1e-9

# Sub-cent cash differences are floating-point noise; anything larger
# warrants operator attention.
_CASH_EPSILON = 0.01

# Position statuses the reconciler compares — closed positions are out of
# scope (Alpaca won't report them under ``GET /v2/positions``).
_LIVE_STATUSES = (PositionStatus.OPEN.value, PositionStatus.PENDING.value)


async def reconcile(
    handle: InvocationHandle,
    *,
    alpaca_positions: tuple[PositionSnapshot, ...],
    alpaca_account: TradeAccountSnapshot | None,
) -> int:
    """Compare local state to Alpaca and emit one alert per unexplained delta.

    Args:
        handle: Open ``InvocationHandle`` from the surrounding
            ``InvocationContext``. Alerts are appended to the open session
            transaction; the surrounding context commits on clean exit.
        alpaca_positions: Tuple of typed Alpaca position snapshots, keyed by
            ``symbol``. Equity positions match against the local
            ``EquityPositionDetails.ticker``; options positions match against
            ``OptionsPositionDetails.underlying_ticker``.
        alpaca_account: Typed Alpaca account snapshot, or ``None`` when the
            caller has not yet wired the account fetch (the pre-04 default).
            When ``None`` the cash comparison is skipped.

    Returns:
        Count of ``RECONCILIATION_ALERT`` entries emitted.
    """
    alpaca_by_symbol = {snapshot.symbol: snapshot for snapshot in alpaca_positions}
    matched_symbols: set[str] = set()
    alert_count = 0

    for position_row in await _read_live_position_rows(handle):
        record = position_row_to_record(position_row)
        details = record.details
        if isinstance(details, EquityPositionDetails):
            matched_symbols.add(details.ticker)
            alert_count += await _reconcile_equity(
                handle,
                position_id=position_row.position_id,
                ticker=details.ticker,
                local_qty=details.share_count,
                alpaca=alpaca_by_symbol.get(details.ticker),
            )
        elif isinstance(details, OptionsPositionDetails):
            matched_symbols.add(details.underlying_ticker)
            alert_count += await _reconcile_options(
                handle,
                position_id=position_row.position_id,
                underlying_ticker=details.underlying_ticker,
                local_count=details.contract_count,
                alpaca=alpaca_by_symbol.get(details.underlying_ticker),
            )
        # Strategy positions are intentionally not reconciled at this layer:
        # Alpaca reports each leg as its own row keyed by OCC symbol, and the
        # local strategy carries multiple legs — the comparison is
        # leg-by-leg through the continuous monitor (ALP-123), not this
        # post-Phase-1 sweep.

    # Alpaca-only orphans: positions present in Alpaca but with no matching
    # local OPEN/PENDING record (e.g., a SPIN_OFF child stranded by a prior
    # invocation crash, or any unexpected broker-side holding). Surface one
    # alert per orphan so the operator can investigate. ``sorted`` keeps the
    # emission order deterministic.
    for symbol in sorted(alpaca_by_symbol.keys() - matched_symbols):
        snapshot = alpaca_by_symbol[symbol]
        await _emit_alert(
            handle,
            position_id=None,
            domain="position",
            field_name="alpaca_only_position",
            local_value=0.0,
            alpaca_value=snapshot.qty,
            delta_description=(
                f"Alpaca holds {symbol} qty={snapshot.qty}; no matching local position"
            ),
        )
        alert_count += 1

    if alpaca_account is not None:
        alert_count += await _reconcile_cash(handle, alpaca_account=alpaca_account)

    return alert_count


# ---------------------------------------------------------------------------
# Per-domain comparators
# ---------------------------------------------------------------------------


async def _reconcile_equity(
    handle: InvocationHandle,
    *,
    position_id: str,
    ticker: str,
    local_qty: float,
    alpaca: PositionSnapshot | None,
) -> int:
    """Emit one alert if local share_count diverges from Alpaca's qty."""
    alpaca_qty = alpaca.qty if alpaca is not None else 0.0
    if abs(local_qty - alpaca_qty) <= _QTY_EPSILON:
        return 0
    await _emit_alert(
        handle,
        position_id=position_id,
        domain="position",
        field_name="share_count",
        local_value=local_qty,
        alpaca_value=alpaca_qty,
        delta_description=(f"{ticker}: local share_count={local_qty} vs Alpaca qty={alpaca_qty}"),
    )
    return 1


async def _reconcile_options(
    handle: InvocationHandle,
    *,
    position_id: str,
    underlying_ticker: str,
    local_count: float,
    alpaca: PositionSnapshot | None,
) -> int:
    """Emit one alert if local contract_count diverges from Alpaca's qty."""
    alpaca_qty = alpaca.qty if alpaca is not None else 0.0
    if abs(local_count - alpaca_qty) <= _QTY_EPSILON:
        return 0
    await _emit_alert(
        handle,
        position_id=position_id,
        domain="position",
        field_name="contract_count",
        local_value=local_count,
        alpaca_value=alpaca_qty,
        delta_description=(
            f"{underlying_ticker}: local contract_count={local_count} vs Alpaca qty={alpaca_qty}"
        ),
    )
    return 1


async def _reconcile_cash(
    handle: InvocationHandle,
    *,
    alpaca_account: TradeAccountSnapshot,
) -> int:
    """Emit one alert if local current_cash_usd diverges from Alpaca's cash.

    ``buying_power`` reconciliation is intentionally skipped at this layer:
    AlphaMind's ``cash_ledger.reserved_capital_usd`` tracks per-order capital
    reservations made by Phase 2 submissions, which has no clean mapping to
    Alpaca's ``buying_power`` (a derived metric incorporating margin rules,
    PDT status, and overnight tiers). Surfacing it as an "alert" would
    produce false positives every invocation.
    """
    cash_row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if cash_row is None:
        return 0
    if abs(cash_row.current_cash_usd - alpaca_account.cash) <= _CASH_EPSILON:
        return 0
    # ALP-462 — both sides are ``Decimal`` after the migration; cast to float
    # at the activity-log boundary (06a migrates ReconciliationAlertDetail).
    await _emit_alert(
        handle,
        position_id=None,
        domain="cash",
        field_name="current_cash_usd",
        local_value=float(cash_row.current_cash_usd),
        alpaca_value=float(alpaca_account.cash),
        delta_description=(
            f"cash_ledger.current_cash_usd={cash_row.current_cash_usd} "
            f"vs Alpaca account.cash={alpaca_account.cash}"
        ),
    )
    return 1


# ---------------------------------------------------------------------------
# Activity-log emission
# ---------------------------------------------------------------------------


async def _emit_alert(
    handle: InvocationHandle,
    *,
    position_id: str | None,
    domain: Literal["position", "cash", "buying_power"],
    field_name: str,
    local_value: float,
    alpaca_value: float,
    delta_description: str,
) -> None:
    entry = ActivityLogEntry(
        entry_id=f"{handle.invocation_id}-{EventType.RECONCILIATION_ALERT.value}-{uuid.uuid4().hex}",
        invocation_id=handle.invocation_id,
        timestamp=datetime.now(UTC),
        event_type=EventType.RECONCILIATION_ALERT,
        event_group=EventGroup.RECONCILIATION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
        detail=ReconciliationAlertDetail(
            domain=domain,
            field_name=field_name,
            local_value=local_value,
            alpaca_value=alpaca_value,
            delta_description=delta_description,
        ),
    )
    append_activity_log_entry(handle, entry)


async def _read_live_position_rows(handle: InvocationHandle) -> list[PositionRow]:
    """Pull every OPEN/PENDING ``PositionRow`` for reconciliation."""
    stmt = select(PositionRow).where(PositionRow.status.in_(_LIVE_STATUSES))
    return list((await handle.session.execute(stmt)).scalars())


__all__ = ["reconcile"]
