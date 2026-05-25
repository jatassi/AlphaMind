"""Post-Phase-1 reconciliation step (ALP-415 / story 04; ALP-619 auto-correct).

After all fills + CA activities are integrated, :func:`reconcile` compares
local state to Alpaca's authoritative ``GET /v2/positions`` /
``GET /v2/account`` snapshot (the caller-supplied
:class:`PositionSnapshot` / :class:`TradeAccountSnapshot` records) and emits
one ``RECONCILIATION_ALERT`` activity-log entry per unexplained delta beyond
the documented tolerance.

Tolerances:

* ``_QTY_EPSILON`` (1e-9) for position quantities — mirrors the
  ``phase1`` write-path's existing constant.
* ``_CASH_EPSILON`` (0.01 USD) for cash-balance comparisons.

Auto-correction (ALP-619). For drift on an existing OPEN equity/options
position (``share_count`` / ``contract_count``) and the singleton
``cash_ledger`` row (``current_cash_usd``), reconcile now writes Alpaca's
value back to local state in the same transaction AND emits a paired
``RECONCILIATION_CORRECTION`` entry capturing the prior local value, the
applied Alpaca value, the field, and the domain. Alerts continue to fire for
every drift — the operator's forensic trail is preserved and the strategist's
existing reconciliation-alert consumer (`input_bundle.filter_by_event_type`)
still sees the same `RECONCILIATION_ALERT` shape.

Auto-correction is deliberately narrowed to drift on existing rows. Alpaca-
only orphans (a symbol present in ``GET /v2/positions`` with no matching
local OPEN/PENDING row) continue to surface as ALERT-only — materializing a
synthetic ``PositionRecord`` requires a thesis_id, a cost basis, and an
execution history that can't be honestly synthesized from the Alpaca
snapshot. Operator triage handles those out of band.

See ``docs/design/05-execution-layer/corporate-actions.md`` § Phase 1
integration sequence step 4 and ``broker-adapter.md`` § Account state queries
— "Alpaca's positions and account endpoints are the source of truth. On
disagreement, Alpaca wins."
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
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
    ReconciliationCorrectionDetail,
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
    record_to_row as position_record_to_row,
)
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
    """Compare local state to Alpaca, write Alpaca's truth back on drift.

    Args:
        handle: Open ``InvocationHandle`` from the surrounding
            ``InvocationContext``. Both alerts and corrections are appended
            to the open session transaction; the surrounding context commits
            on clean exit.
        alpaca_positions: Tuple of typed Alpaca position snapshots, keyed by
            ``symbol``. Equity positions match against the local
            ``EquityPositionDetails.ticker``; options positions match against
            ``OptionsPositionDetails.underlying_ticker``.
        alpaca_account: Typed Alpaca account snapshot, or ``None`` when the
            caller has not yet wired the account fetch (the pre-04 default).
            When ``None`` the cash comparison is skipped.

    Returns:
        Count of ``RECONCILIATION_ALERT`` entries emitted. Corrections are
        counted separately at activity-log read time — the existing summary
        consumer in ``InvocationSummary`` reads alerts only.
    """
    # ALP-619 — broker-degraded short-circuit. When the Phase 1 input
    # gatherer sets ``staleness_flag=True`` after a broker fetch failure, the
    # orchestrator hands ``alpaca_positions=()`` AND ``alpaca_account=None``
    # to ``process_unprocessed_fills``. With auto-correction wired in, running
    # the comparators against an empty Alpaca snapshot would interpret every
    # local OPEN position as "Alpaca says zero" and wipe the book to zero.
    # Treat the dual-empty signal as "Alpaca state unavailable; skip
    # reconciliation entirely" — the operator already sees the staleness flag
    # on the invocation row.
    if not alpaca_positions and alpaca_account is None:
        return 0
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
                position_row=position_row,
                ticker=details.ticker,
                local_qty=details.share_count,
                alpaca=alpaca_by_symbol.get(details.ticker),
            )
        elif isinstance(details, OptionsPositionDetails):
            matched_symbols.add(details.underlying_ticker)
            alert_count += await _reconcile_options(
                handle,
                position_row=position_row,
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
    # alert per orphan so the operator can investigate. Auto-correction is
    # deliberately skipped here — materializing a synthetic ``PositionRecord``
    # demands a thesis_id, cost basis, and execution history that can't be
    # honestly synthesized from the Alpaca snapshot (ALP-619). ``sorted`` keeps
    # the emission order deterministic.
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
    position_row: PositionRow,
    ticker: str,
    local_qty: float,
    alpaca: PositionSnapshot | None,
) -> int:
    """Emit one alert and auto-correct ``share_count`` on drift."""
    alpaca_qty = alpaca.qty if alpaca is not None else 0.0
    if abs(local_qty - alpaca_qty) <= _QTY_EPSILON:
        return 0
    await _emit_alert(
        handle,
        position_id=position_row.position_id,
        domain="position",
        field_name="share_count",
        local_value=local_qty,
        alpaca_value=alpaca_qty,
        delta_description=(f"{ticker}: local share_count={local_qty} vs Alpaca qty={alpaca_qty}"),
    )
    _rewrite_equity_share_count(position_row, new_share_count=alpaca_qty)
    await _emit_correction(
        handle,
        position_id=position_row.position_id,
        domain="position",
        field_name="share_count",
        prior_local_value=local_qty,
        applied_alpaca_value=alpaca_qty,
    )
    return 1


async def _reconcile_options(
    handle: InvocationHandle,
    *,
    position_row: PositionRow,
    underlying_ticker: str,
    local_count: float,
    alpaca: PositionSnapshot | None,
) -> int:
    """Emit one alert and auto-correct ``contract_count`` on drift."""
    alpaca_qty = alpaca.qty if alpaca is not None else 0.0
    if abs(local_count - alpaca_qty) <= _QTY_EPSILON:
        return 0
    await _emit_alert(
        handle,
        position_id=position_row.position_id,
        domain="position",
        field_name="contract_count",
        local_value=local_count,
        alpaca_value=alpaca_qty,
        delta_description=(
            f"{underlying_ticker}: local contract_count={local_count} vs Alpaca qty={alpaca_qty}"
        ),
    )
    _rewrite_options_contract_count(position_row, new_contract_count=alpaca_qty)
    await _emit_correction(
        handle,
        position_id=position_row.position_id,
        domain="position",
        field_name="contract_count",
        prior_local_value=local_count,
        applied_alpaca_value=alpaca_qty,
    )
    return 1


async def _reconcile_cash(
    handle: InvocationHandle,
    *,
    alpaca_account: TradeAccountSnapshot,
) -> int:
    """Emit one alert and auto-correct ``current_cash_usd`` on drift.

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
    prior_cash = cash_row.current_cash_usd
    # ALP-462 — both sides are ``Decimal`` after the migration; cast to float
    # at the activity-log boundary (06a migrates ReconciliationAlertDetail).
    await _emit_alert(
        handle,
        position_id=None,
        domain="cash",
        field_name="current_cash_usd",
        local_value=float(prior_cash),
        alpaca_value=float(alpaca_account.cash),
        delta_description=(
            f"cash_ledger.current_cash_usd={prior_cash} "
            f"vs Alpaca account.cash={alpaca_account.cash}"
        ),
    )
    # Alpaca's ``cash`` is ``Money`` (Decimal-backed); assign through ``Decimal``
    # to satisfy ``CashLedgerRow.current_cash_usd``'s declared type. The Money
    # NewType narrows to Decimal at runtime.
    cash_row.current_cash_usd = Decimal(alpaca_account.cash)
    await _emit_correction(
        handle,
        position_id=None,
        domain="cash",
        field_name="current_cash_usd",
        prior_local_value=float(prior_cash),
        applied_alpaca_value=float(alpaca_account.cash),
    )
    return 1


# ---------------------------------------------------------------------------
# In-place row rewrite — used by the auto-correct paths above.
# ---------------------------------------------------------------------------


def _rewrite_equity_share_count(row: PositionRow, *, new_share_count: float) -> None:
    """Update ``share_count`` in ``row.details_json`` via the codec.

    Round-trips the row through ``row_to_record`` → ``dataclasses.replace`` →
    ``record_to_row`` so the codec stays the single source of truth for
    JSON layout. Mutates the existing row in place — the surrounding session
    transaction commits the change.
    """
    record = position_row_to_record(row)
    assert isinstance(record.details, EquityPositionDetails)
    new_details = dataclasses.replace(record.details, share_count=new_share_count)
    new_record = dataclasses.replace(record, details=new_details)
    row.details_json = position_record_to_row(new_record).details_json


def _rewrite_options_contract_count(row: PositionRow, *, new_contract_count: float) -> None:
    """Update ``contract_count`` in ``row.details_json`` via the codec."""
    record = position_row_to_record(row)
    assert isinstance(record.details, OptionsPositionDetails)
    new_details = dataclasses.replace(record.details, contract_count=new_contract_count)
    new_record = dataclasses.replace(record, details=new_details)
    row.details_json = position_record_to_row(new_record).details_json


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


async def _emit_correction(
    handle: InvocationHandle,
    *,
    position_id: str | None,
    domain: Literal["position", "cash"],
    field_name: str,
    prior_local_value: float,
    applied_alpaca_value: float,
) -> None:
    entry = ActivityLogEntry(
        entry_id=(
            f"{handle.invocation_id}-{EventType.RECONCILIATION_CORRECTION.value}-{uuid.uuid4().hex}"
        ),
        invocation_id=handle.invocation_id,
        timestamp=datetime.now(UTC),
        event_type=EventType.RECONCILIATION_CORRECTION,
        event_group=EventGroup.RECONCILIATION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
        detail=ReconciliationCorrectionDetail(
            domain=domain,
            field_name=field_name,
            prior_local_value=prior_local_value,
            applied_alpaca_value=applied_alpaca_value,
        ),
    )
    append_activity_log_entry(handle, entry)


async def _read_live_position_rows(handle: InvocationHandle) -> list[PositionRow]:
    """Pull every OPEN/PENDING ``PositionRow`` for reconciliation."""
    stmt = select(PositionRow).where(PositionRow.status.in_(_LIVE_STATUSES))
    return list((await handle.session.execute(stmt)).scalars())


__all__ = ["reconcile"]
