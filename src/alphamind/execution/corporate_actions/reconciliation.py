"""Post-Phase-1 reconciliation step (ALP-415 / story 04; ALP-619 auto-correct).

After all fills + CA activities are integrated, :func:`reconcile` compares
local state to Alpaca's authoritative ``GET /v2/positions`` /
``GET /v2/account`` snapshot (the caller-supplied
:class:`PositionSnapshot` / :class:`TradeAccountSnapshot` records) and emits
one ``RECONCILIATION_ALERT`` activity-log entry per unexplained delta beyond
the documented tolerance.

Tolerances:

* ``_QTY_EPSILON`` (1e-9) for position quantities — mirrors the
  ``phase1`` write-path's existing constant. Same threshold gates alert
  emission AND auto-correction (ALP-619).
* ``_CASH_EPSILON`` (0.01 USD) for cash-balance comparisons.

Auto-correction (ALP-619). For drift on an existing OPEN equity position
(``share_count``) and the singleton ``cash_ledger`` row
(``current_cash_usd``), reconcile writes Alpaca's authoritative value back
to local state in the same transaction AND emits a paired
``RECONCILIATION_CORRECTION`` entry capturing the prior local value, the
applied Alpaca value, the field, and the domain.

Alpaca's ``PositionSnapshot.qty`` is signed (negative for shorts; ``side``
carries the boolean direction). Local ``EquityPositionDetails.share_count``
is unsigned-by-convention with ``PositionRecord.direction`` tracking sign
separately — see ``positions/computations.py:compute_market_value_usd``.
Reconcile compares unsigned magnitudes and writes back ``abs(alpaca.qty)``
on drift. When ``alpaca.side`` disagrees with ``record.direction``, the
delta is surfaced as a direction-flip ALERT only — no auto-correct, since a
flipped side is semantically a different position (stop-out, assignment, or
broker error) that warrants operator review.

Scope narrowed in ALP-619:

* Equity-only auto-correct. Options positions still emit ALERTs through
  the same comparator but skip the writeback — Alpaca returns each options
  contract under its OCC symbol (e.g. ``"AAPL250620C00200000"``) while the
  local options record carries ``underlying_ticker`` (e.g. ``"AAPL"``), so
  the existing ALERT comparator is structurally broken for real Alpaca
  options snapshots and writeback would zero out every options
  ``contract_count`` on every invocation. The OCC-matching fix is a
  follow-up.
* OPEN-only auto-correct. PENDING positions carry ``share_count=0`` by the
  status-rule invariant; comparing against an Alpaca-side missing entry
  produces no drift in practice, but we exclude PENDING from the writeback
  unconditionally as a defensive narrow.
* Positive-evidence gate. When ``alpaca_positions=()`` the caller could
  mean either "Alpaca holds zero positions" or "positions fetch degraded"
  — we can't disambiguate from inside reconcile. The writeback is skipped
  in that case so a transient broker failure does not silently wipe the
  local book; alerts still emit so the operator gets visibility. The cash
  side gates separately on ``alpaca_account is None``.

Alpaca-only orphans continue as ALERT-only — synthesizing a thesis_id,
cost basis, and execution history from the snapshot isn't honest enough to
auto-materialize a local row. Operator triage handles those out of band.

See ``docs/design/05-execution-layer/corporate-actions.md`` § Phase 1
integration sequence step 4 and ``broker-adapter.md`` § Account state
queries — "Alpaca's positions and account endpoints are the source of
truth. On disagreement, Alpaca wins."
"""

from __future__ import annotations

import dataclasses
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
    ReconciliationCorrectionDetail,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
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
# Same threshold gates ALERT emission and ALP-619 auto-correction.
_QTY_EPSILON = 1e-9

# Sub-cent cash differences are floating-point noise; anything larger
# warrants operator attention. Same threshold gates ALERT and CORRECTION.
_CASH_EPSILON = 0.01

# Position statuses the reconciler sweeps. Auto-correct narrows further to
# OPEN-only in the equity branch (PENDING by invariant has share_count=0,
# so no drift fires; the narrow is defensive against future invariant
# changes).
_LIVE_STATUSES = (PositionStatus.OPEN.value, PositionStatus.PENDING.value)


def _direction_from_side(side: str) -> Direction:
    """Map Alpaca's ``side`` literal to the local ``Direction`` enum."""
    return Direction.LONG if side == "long" else Direction.SHORT


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
            ``EquityPositionDetails.ticker``; options positions are not
            auto-corrected (see module docstring).
        alpaca_account: Typed Alpaca account snapshot, or ``None`` when the
            caller has not yet wired the account fetch (the pre-04 default).
            When ``None`` the cash comparison is skipped.

    Returns:
        Count of ``RECONCILIATION_ALERT`` entries emitted. Corrections are
        counted separately at activity-log read time — the existing summary
        consumer in ``InvocationSummary`` reads alerts only.
    """
    # ALP-619 — broker-fully-degraded short-circuit. When the Phase 1 input
    # gatherer sets ``staleness_flag=True`` after both broker fetches fail,
    # the orchestrator hands ``alpaca_positions=()`` AND
    # ``alpaca_account=None``. Without this gate the equity comparator
    # would interpret every local OPEN row as "Alpaca says zero" and emit
    # spurious alerts; the writeback gate below stops the destructive half,
    # but the alert noise is still useless. Skip the entire pass when both
    # signals are absent — the operator already sees the staleness flag on
    # the invocation row.
    if not alpaca_positions and alpaca_account is None:
        return 0

    # ALP-619 — positive-evidence gate for position writeback. When
    # ``alpaca_positions=()`` (account fetch succeeded but positions
    # endpoint either failed or genuinely returned empty), we cannot tell
    # the cases apart. Emit alerts so the operator gets visibility, but
    # suppress writeback to avoid wiping the book on a transient
    # positions-fetch failure.
    autocorrect_positions = bool(alpaca_positions)

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
                local_direction=record.direction,
                alpaca=alpaca_by_symbol.get(details.ticker),
                autocorrect=autocorrect_positions and record.status == PositionStatus.OPEN,
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
    local_direction: Direction | None,
    alpaca: PositionSnapshot | None,
    autocorrect: bool,
) -> int:
    """Emit alert(s) and (optionally) auto-correct ``share_count`` on drift.

    Alpaca's ``qty`` is signed (negative for shorts) and ``side`` carries
    the direction independently. Local ``share_count`` is unsigned with
    ``PositionRecord.direction`` tracking sign. We compare unsigned
    magnitudes and write back ``abs(alpaca.qty)`` on quantity drift.

    A direction mismatch (local LONG vs Alpaca short, or vice versa) is
    surfaced as a separate ALERT and skips auto-correction — a flipped side
    is semantically distinct from a quantity drift and demands operator
    review.
    """
    alerts = 0

    # Vanished local position (no Alpaca match) — compare against zero.
    if alpaca is None:
        alpaca_qty_unsigned = 0.0
        direction_mismatch = False
    else:
        alpaca_qty_unsigned = abs(alpaca.qty)
        alpaca_direction = _direction_from_side(alpaca.side)
        direction_mismatch = local_direction is not None and local_direction != alpaca_direction

    # Direction flip — alert, no auto-correct.
    if direction_mismatch:
        assert alpaca is not None  # narrowed by direction_mismatch
        local_is_long = 0.0 if local_direction is None else float(local_direction == Direction.LONG)
        await _emit_alert(
            handle,
            position_id=position_row.position_id,
            domain="position",
            field_name="direction",
            local_value=local_is_long,
            alpaca_value=float(alpaca.side == "long"),
            delta_description=(
                f"{ticker}: local direction={local_direction} vs Alpaca side={alpaca.side!r}"
            ),
        )
        alerts += 1
        # Fall through to quantity comparison too — both deltas warrant alerts.

    if abs(local_qty - alpaca_qty_unsigned) > _QTY_EPSILON:
        await _emit_alert(
            handle,
            position_id=position_row.position_id,
            domain="position",
            field_name="share_count",
            local_value=local_qty,
            alpaca_value=alpaca_qty_unsigned,
            delta_description=(
                f"{ticker}: local share_count={local_qty} vs Alpaca qty={alpaca_qty_unsigned}"
            ),
        )
        alerts += 1
        if autocorrect and not direction_mismatch:
            _rewrite_equity_share_count(position_row, new_share_count=alpaca_qty_unsigned)
            await _emit_correction(
                handle,
                position_id=position_row.position_id,
                domain="position",
                field_name="share_count",
                prior_local_value=local_qty,
                applied_alpaca_value=alpaca_qty_unsigned,
            )

    return alerts


async def _reconcile_options(
    handle: InvocationHandle,
    *,
    position_row: PositionRow,
    underlying_ticker: str,
    local_count: float,
    alpaca: PositionSnapshot | None,
) -> int:
    """Emit one alert on options ``contract_count`` drift; never auto-correct.

    Alpaca's options positions are keyed by OCC contract symbol (e.g.
    ``"AAPL250620C00200000"``), not the underlying ticker. Until the
    OCC-matching fix lands (follow-up issue), the alert here may fire
    spuriously for any held options position, and auto-correction would
    silently zero out every ``contract_count`` on every invocation. Skip
    writeback unconditionally.
    """
    alpaca_qty = abs(alpaca.qty) if alpaca is not None else 0.0
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
    # ``Money = NewType("Money", Decimal)`` — at runtime the value is
    # already a Decimal, assign directly into the ``DecimalText`` column.
    cash_row.current_cash_usd = alpaca_account.cash
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
    if not isinstance(record.details, EquityPositionDetails):
        # Fail-closed: bare assert is silently stripped under ``python -O``,
        # which would let a wrong-type details payload reach
        # ``dataclasses.replace`` and silently corrupt ``details_json``.
        msg = (
            f"_rewrite_equity_share_count: expected EquityPositionDetails, "
            f"got {type(record.details).__name__} for position_id={row.position_id!r}"
        )
        raise TypeError(msg)
    new_details = dataclasses.replace(record.details, share_count=new_share_count)
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
