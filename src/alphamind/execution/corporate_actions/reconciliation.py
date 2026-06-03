"""Post-Phase-1 reconciliation step (ALP-415 / story 04; ALP-619 / ALP-637 auto-correct).

After all fills + CA activities are integrated, :func:`reconcile` compares
local state to Alpaca's authoritative ``GET /v2/positions`` /
``GET /v2/account`` snapshot (the caller-supplied
:class:`PositionSnapshot` / :class:`TradeAccountSnapshot` records) and emits
one ``RECONCILIATION_ALERT`` activity-log entry per unexplained delta beyond
the documented tolerance.

Tolerances:

* ``_QTY_EPSILON`` (1e-9) for position quantities — mirrors the
  ``phase1`` write-path's existing constant. Same threshold gates alert
  emission AND auto-correction.
* ``_CASH_EPSILON`` (0.01 USD) for cash-balance comparisons.

Auto-correction. For drift on an existing OPEN equity position
(``share_count``), OPEN options position (``contract_count``), and the
singleton ``cash_ledger`` row (``current_cash_usd``), reconcile writes
Alpaca's authoritative value back to local state in the same transaction
AND emits a paired ``RECONCILIATION_CORRECTION`` entry capturing the prior
local value, the applied Alpaca value, the field, and the domain.

Position-matching keys:

* Equity — local ``EquityPositionDetails.ticker`` matches Alpaca's
  ``PositionSnapshot.symbol`` (both bare underlying for ``us_equity``).
* Options — local ``OptionsPositionDetails`` matches Alpaca's snapshot by
  the bare OCC contract symbol (e.g. ``"AAPL250620C00200000"``) built via
  :func:`_alpaca_occ_symbol`. Multiple held contracts on the same
  underlying reconcile independently (ALP-637).

Alpaca's ``PositionSnapshot.qty`` is signed (negative for shorts; ``side``
carries the boolean direction). Local ``EquityPositionDetails.share_count``
is unsigned-by-convention with ``PositionRecord.direction`` tracking sign
separately — see ``positions/computations.py:compute_market_value_usd``.
Reconcile compares unsigned magnitudes and writes back ``abs(alpaca.qty)``
on drift. When ``alpaca.side`` disagrees with ``record.direction``, the
delta is surfaced as a direction-flip ALERT only — no auto-correct, since a
flipped side is semantically a different position (stop-out, assignment, or
broker error) that warrants operator review. Direction-flip handling for
options is not implemented — Alpaca options always carry ``side="long"``
since short options behave as covered/cash-secured writes at the broker
layer and are represented locally via ``StrategyPositionDetails`` legs that
this reconciler skips.

Scope narrows:

* OPEN-only ``share_count`` / ``contract_count`` writeback. PENDING
  positions carry quantity = 0 by the status-rule invariant, so the standard
  drift writeback is OPEN-only. A PENDING equity position whose Alpaca holding
  is nonzero is a likely dropped/unintegrated entry fill; the reconciler
  ESCALATES it as a distinct ``pending_with_broker_holding`` drift alert and
  does nothing else — see :func:`_escalate_pending_equity`. The reconciler's
  job is drift DETECTION, not authoring portfolio state: recovery is owned by
  the fill drain/periodic backfill, which writes the real fill into
  ``fill_records`` so the next Phase-1 integrates it and flips the position
  PENDING → OPEN with the correct basis. The reconciler never flips status,
  writes share_count, synthesizes a fill, or emits a correction for this case.
  Options carry no PENDING escalation.
* Positive-evidence gate, split per asset class (ALP-662). The
  positions endpoint can legitimately hand back a snapshot tuple that
  is missing an entire asset class — either the operator truly holds
  nothing in that class, or the response was incomplete / truncated /
  a downstream filter dropped that class. We can't disambiguate from
  inside reconcile, so writeback for each asset class gates on the
  presence of at least one snapshot of THAT class: equity writeback
  requires a ``us_equity`` snapshot, options writeback requires a
  ``us_option`` snapshot. The fully-degraded case
  (``alpaca_positions=()``) closes both gates as before. Alerts still
  emit on both sides so the operator sees the drift; only the
  destructive writeback half is gated. The cash side gates separately
  on ``alpaca_account is None``.

Alpaca-only orphans continue as ALERT-only — synthesizing a thesis_id,
cost basis, and execution history from the snapshot isn't honest enough to
auto-materialize a local row. Operator triage handles those out of band.

Multi-leg strategy positions are intentionally skipped at this layer (the
``StrategyPositionDetails`` branch of the position-record discriminated
union is a no-op). Alpaca reports each leg as its own ``us_option`` row
keyed by OCC symbol; those legs will therefore surface as
``alpaca_only_position`` orphan alerts on every invocation for any held
strategy. Leg-level reconciliation lives in the continuous monitor
(ALP-123), not this post-Phase-1 sweep — the orphan-alert noise is
expected and known.

See ``docs/design/05-execution-layer/corporate-actions.md`` § Phase 1
integration sequence step 4 and ``broker-adapter.md`` § Account state
queries — "Alpaca's positions and account endpoints are the source of
truth. On disagreement, Alpaca wins."
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select

from alphamind.execution.broker_adapter.queries import (
    OrderSnapshot,
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
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
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
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

log = logging.getLogger(__name__)

# ALP-836 — Alpaca order status → local terminal OrderStatus for the order-link
# backfill. A non-terminal Alpaca status (new / accepted / partially_filled /
# filled / …) maps to local PENDING: the order is now broker-known, and Phase-1
# fill integration (driven by the attributed fill_records row) advances it to
# FILLED. Only the terminal no-fill dispositions need an explicit local terminal.
_ALPACA_TERMINAL_TO_LOCAL: dict[str, OrderStatus] = {
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}

# Mirrors phase1.py's tolerance for "quantities are effectively equal".
# Same threshold gates ALERT emission and ALP-619 auto-correction.
_QTY_EPSILON = 1e-9

# Sub-cent cash differences are floating-point noise; anything larger
# warrants operator attention. Same threshold gates ALERT and CORRECTION.
_CASH_EPSILON = 0.01

# Position statuses the reconciler sweeps. Auto-correct is OPEN-only (PENDING
# by invariant has quantity == 0, so no drift fires). PENDING rows are still
# swept so the equity branch can ESCALATE a PENDING-with-nonzero-Alpaca-holding
# as a distinct drift alert (ALP-763) — a likely dropped entry fill.
_LIVE_STATUSES = (PositionStatus.OPEN.value, PositionStatus.PENDING.value)


def _direction_from_side(side: str) -> Direction:
    """Map Alpaca's ``side`` literal to the local ``Direction`` enum."""
    return Direction.LONG if side == "long" else Direction.SHORT


def _alpaca_occ_symbol(details: OptionsPositionDetails) -> str:
    """Build the bare OCC contract symbol Alpaca returns for an options position.

    Alpaca's ``GET /v2/positions`` keys ``asset_class="us_option"`` entries by
    the compact OCC symbol (e.g. ``"AAPL250620C00200000"``), with no Polygon
    ``O:`` prefix and no space-padding on the underlying root. Share-class
    tickers (``BRK.B``, ``BF.B``) are encoded WITHOUT the dot per OCC
    convention — mirrors ``build_occ_symbol`` in
    ``broker_adapter.order_options`` (which strips the dot for order
    submission, so Alpaca's position-fetch returns the same dot-stripped
    form). This differs from :func:`occ_symbol_for_options` in
    ``portfolio_state.records.positions``, which prepends ``O:`` and is the
    canonical Polygon key for greeks/price lookups.
    """
    underlying = details.underlying_ticker.replace(".", "")
    expiry = details.expiration_date.strftime("%y%m%d")
    cp = "C" if details.contract_type is OptionContractType.CALL else "P"
    strike_milli = round(details.strike_price * 1000)
    return f"{underlying}{expiry}{cp}{strike_milli:08d}"


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
            caller has not wired the account fetch. When ``None`` the cash
            comparison is skipped.

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

    # ALP-619 / ALP-662 — per-asset-class positive-evidence gate for
    # writeback; see module docstring for the rationale.
    autocorrect_equity = any(s.asset_class == "us_equity" for s in alpaca_positions)
    autocorrect_options = any(s.asset_class == "us_option" for s in alpaca_positions)

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
                local_status=record.status,
                alpaca=alpaca_by_symbol.get(details.ticker),
                equity_evidence=autocorrect_equity,
            )
        elif isinstance(details, OptionsPositionDetails):
            occ_symbol = _alpaca_occ_symbol(details)
            matched_symbols.add(occ_symbol)
            alert_count += await _reconcile_options(
                handle,
                position_row=position_row,
                occ_symbol=occ_symbol,
                local_count=details.contract_count,
                alpaca=alpaca_by_symbol.get(occ_symbol),
                autocorrect=autocorrect_options and record.status == PositionStatus.OPEN,
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
    local_status: PositionStatus,
    alpaca: PositionSnapshot | None,
    equity_evidence: bool,
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

    Auto-correction is gated on positive equity evidence (``equity_evidence``;
    a ``us_equity`` snapshot is present) per the ALP-619 / ALP-662 writeback
    rationale. Within that gate the status drives the behavior:

    * OPEN — ``share_count`` drift writes back ``abs(alpaca.qty)`` (the
      standard ALP-619 path).
    * PENDING with a nonzero Alpaca holding — ALP-763 drift escalation: a
      likely dropped/unintegrated entry fill. Emit a distinct
      ``pending_with_broker_holding`` alert and nothing else. See
      :func:`_escalate_pending_equity`.
    """
    # ALP-763 — PENDING-with-broker-holding escalation. A PENDING row carries
    # share_count=0 by invariant; a nonzero Alpaca holding means the entry fill
    # was likely dropped before integration. The reconciler's job is drift
    # DETECTION, not authoring portfolio state — so it escalates this as a
    # distinct alert and returns. Recovery is owned by the fill drain/periodic
    # backfill, which writes the real fill into ``fill_records`` so the next
    # Phase-1 integrates it and flips the position PENDING → OPEN with the
    # correct basis. Firing this branch before the direction/quantity
    # comparison also keeps a wrong-direction PENDING row from being treated as
    # ordinary quantity drift.
    if (
        local_status == PositionStatus.PENDING
        and equity_evidence
        and alpaca is not None
        and abs(alpaca.qty) > _QTY_EPSILON
    ):
        return await _escalate_pending_equity(
            handle,
            position_row=position_row,
            ticker=ticker,
            alpaca=alpaca,
        )

    # OPEN-only auto-correct from here down (PENDING with no/zero Alpaca
    # holding has share_count=0 by invariant, so no drift fires anyway).
    autocorrect = equity_evidence and local_status == PositionStatus.OPEN

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


async def _escalate_pending_equity(
    handle: InvocationHandle,
    *,
    position_row: PositionRow,
    ticker: str,
    alpaca: PositionSnapshot,
) -> int:
    """ALP-763 — escalate a PENDING equity row with a nonzero Alpaca holding.

    A PENDING row carries ``share_count=0`` by invariant; a nonzero Alpaca
    holding means the entry fill was likely dropped/unintegrated. The
    reconciler only DETECTS drift, so it emits a single distinct
    ``pending_with_broker_holding`` alert and mutates nothing — no status flip,
    no share_count write, no synthesized fill, no correction. The honest
    recovery is the fill drain/periodic backfill: it writes the real fill into
    ``fill_records`` so the next Phase-1 integrates it and flips the position
    PENDING → OPEN with the correct basis. Returns the alert count (always 1).
    """
    alpaca_qty_unsigned = abs(alpaca.qty)
    await _emit_alert(
        handle,
        position_id=position_row.position_id,
        domain="position",
        field_name="pending_with_broker_holding",
        local_value=0.0,
        alpaca_value=alpaca_qty_unsigned,
        delta_description=(
            f"{ticker}: PENDING local position has nonzero Alpaca holding "
            f"qty={alpaca_qty_unsigned} — likely a dropped/unintegrated entry "
            f"fill awaiting recovery"
        ),
    )
    return 1


async def _reconcile_options(
    handle: InvocationHandle,
    *,
    position_row: PositionRow,
    occ_symbol: str,
    local_count: float,
    alpaca: PositionSnapshot | None,
    autocorrect: bool,
) -> int:
    """Emit alert and (optionally) auto-correct ``contract_count`` on drift.

    Alpaca's options positions are keyed by OCC contract symbol (e.g.
    ``"AAPL250620C00200000"``) — the caller builds the matching key via
    :func:`_alpaca_occ_symbol` and looks the snapshot up by it, so multiple
    held contracts on the same underlying reconcile independently.

    Mirrors the equity write-back path: on quantity drift, alert, then write
    Alpaca's ``abs(qty)`` back into ``contract_count`` and emit a paired
    ``RECONCILIATION_CORRECTION``. The caller gates ``autocorrect`` on
    OPEN-only + positive Alpaca evidence (ALP-619 invariants).
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
            f"{occ_symbol}: local contract_count={local_count} vs Alpaca qty={alpaca_qty}"
        ),
    )
    if autocorrect:
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

    Also corrects ``settled_cash_usd`` to the same Alpaca value (ALP-778).
    No T+2 settlement lag is modelled in paper trading, so settled tracks
    reconciled current.

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
    # ALP-778: settled tracks current (no T+2 lag modelled in paper trading).
    cash_row.settled_cash_usd = alpaca_account.cash
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


def _rewrite_options_contract_count(row: PositionRow, *, new_contract_count: float) -> None:
    """Update ``contract_count`` in ``row.details_json`` via the codec.

    Mirrors :func:`_rewrite_equity_share_count` for the options payload —
    same round-trip discipline so the codec stays the single source of truth
    for JSON layout.
    """
    record = position_row_to_record(row)
    if not isinstance(record.details, OptionsPositionDetails):
        msg = (
            f"_rewrite_options_contract_count: expected OptionsPositionDetails, "
            f"got {type(record.details).__name__} for position_id={row.position_id!r}"
        )
        raise TypeError(msg)
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


# ---------------------------------------------------------------------------
# ALP-836 — order→broker-link backfill (recovers a lost post-submit commit)
# ---------------------------------------------------------------------------


async def backfill_pending_submit_orders(
    handle: InvocationHandle,
    *,
    alpaca_orders: tuple[OrderSnapshot, ...],
) -> int:
    """Recover orders whose post-submit ``alpaca_order_id`` backfill was lost.

    Atomicity-first persistence commits a durable ``orders`` row in
    ``PENDING_SUBMIT`` (carrying the deterministic ``client_order_id`` and a
    synthetic ``alp-{order_id}`` placeholder) BEFORE the broker dispatch, then
    backfills the real ``alpaca_order_id`` + flips to ``PENDING`` after. If that
    post-submit commit is lost, the row survives in ``PENDING_SUBMIT`` with the
    synthetic placeholder — a live broker order whose local row has no broker id.

    This pass matches each such row against Alpaca's orders by
    ``client_order_id`` (Alpaca ``order.client_order_id == command_id``) and
    backfills the real ``alpaca_order_id`` + chain, flipping ``PENDING_SUBMIT`` →
    ``PENDING`` (or the broker's terminal disposition). The attributed fill — if
    any — was never stranded (the fill stream resolves it by ``client_order_id``);
    this repairs the order→broker link so reconciliation and the fill path see a
    consistent row.

    A ``PENDING_SUBMIT`` row with NO matching Alpaca order is left untouched and
    logged: the pre-commit may have landed but the broker dispatch never did
    (process death between the durable write and the submit) — there is no live
    broker order to link, and an operator/cleanup resolves it out of band.

    Runs inside the Phase-1 write transaction; the caller commits. Returns the
    count of rows backfilled. ``alpaca_orders`` is fetched in the read-phase
    gatherer (never inside the write lock) and is empty when no local
    ``PENDING_SUBMIT`` rows exist (the common case), so this is a no-op then.
    """
    if not alpaca_orders:
        return 0
    rows = list(
        (
            await handle.session.execute(
                select(OrderRow).where(
                    OrderRow.status == OrderStatus.PENDING_SUBMIT.value,
                    OrderRow.client_order_id.is_not(None),
                )
            )
        ).scalars()
    )
    if not rows:
        return 0
    by_client_order_id = {o.client_order_id: o for o in alpaca_orders}
    now = datetime.now(UTC).isoformat()
    backfilled = 0
    for row in rows:
        snapshot = by_client_order_id.get(row.client_order_id or "")
        if snapshot is None:
            log.warning(
                "order-link backfill: PENDING_SUBMIT order_id=%s client_order_id=%s has no "
                "matching Alpaca order — the broker dispatch likely never landed; leaving "
                "for operator review",
                row.order_id,
                row.client_order_id,
            )
            continue
        new_status = _ALPACA_TERMINAL_TO_LOCAL.get(snapshot.status.lower(), OrderStatus.PENDING)
        row.alpaca_order_id = snapshot.order_id
        row.alpaca_order_id_chain_json = json.dumps([snapshot.order_id])
        row.status = new_status.value
        row.last_update_timestamp = now
        backfilled += 1
        log.warning(
            "order-link backfill: recovered lost post-submit commit — order_id=%s "
            "client_order_id=%s alpaca_order_id=%s status=PENDING_SUBMIT→%s",
            row.order_id,
            row.client_order_id,
            snapshot.order_id,
            new_status.value,
        )
    return backfilled


__all__ = ["backfill_pending_submit_orders", "reconcile"]
