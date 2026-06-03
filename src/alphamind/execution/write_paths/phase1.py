"""Phase 1 fill-integration write path (story 07 / ALP-365).

Drains every ``processing_status='unprocessed'`` fill record (and any
unprocessed CA activity), integrates each into state (orders / positions /
brackets / theses / cash_ledger / drawdown_state), emits the corresponding
activity-log entries, and marks the fills processed — all inside the open
``InvocationContext`` transaction.

The caller passes the ``InvocationHandle`` from the surrounding
``InvocationContext``; this module never opens its own transaction. On
exception, the surrounding context manager rolls back so fills remain
unprocessed for the next invocation.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.money import money, signed_money
from alphamind.execution.broker_adapter.queries import (
    OrderSnapshot,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.corporate_actions import integrate_ca_activity
from alphamind.execution.corporate_actions.reconciliation import (
    backfill_pending_submit_orders,
    reconcile,
)
from alphamind.execution.corporate_actions.types import (
    AlpacaPositionLookup,
    CorporateActionActivity,
)
from alphamind.execution.position_model import (
    compute_strategy_breakeven_levels,
    compute_strategy_max_loss_usd,
    compute_strategy_max_profit_usd,
    compute_strategy_net_premium_usd,
)
from alphamind.execution.regt_margin_attribution import (
    RegTMarginAttributionConfig,
    compute_attribution,
    load_regt_margin_attribution_config,
)
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    BracketActivatedDetail,
    BracketDissolvedDetail,
    BracketIncompleteWarningDetail,
    CapitalReleasedDetail,
    CashCreditedDetail,
    CashCreditReason,
    CashDebitedDetail,
    CashDebitReason,
    EventSource,
    EventType,
    OrderFilledDetail,
    PositionClosedDetail,
    PositionExitMethod,
    PositionOpenedDetail,
    PositionOpenMechanism,
    PositionReducedDetail,
    ReconciliationAlertDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegStatus,
    BracketStatus,
    OptionsInstrumentSpec,
    OrderClass,
    OrderRecord,
    OrderRole,
    OrderStatus,
    direction_to_side,
    order_direction,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
    position_direction,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import MarketInputs
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
    stamp_phase_completion,
)
from alphamind.state.invocation_id import mint_invocation_id
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.fill_records_codec import (
    row_to_record as fill_row_to_record,
)
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

log = logging.getLogger(__name__)

# Tolerance for "remaining quantity zero" comparisons after float arithmetic.
_QTY_EPSILON = 1e-9

# Position statuses a fill can never integrate against. Both are terminal: a
# CANCELLED position never opened (entry cancelled mid-fill, ALP-744/ALP-760) and
# a CLOSED position already exited. The per-instrument apply-fill helpers hard-raise
# on either, so a fill landing against one is an orphan — quarantine + alert it
# rather than letting a single poison-pill fill abort the whole batch (ALP-761).
_NON_INTEGRATABLE_STATUSES = frozenset({PositionStatus.CANCELLED, PositionStatus.CLOSED})


class StateInconsistencyError(RuntimeError):
    """Raised when a Tier-1 row references a parent FK target that is missing.

    The Phase 1 mutators (``_activate_bracket``, ``_dissolve_bracket``,
    ``_cancel_bracket_for_corporate_action``) expect every FK reference they
    read to point at a live row. A missing
    target would silently no-op and mask state corruption — we raise
    instead so the surrounding ``InvocationContext`` rolls back and the
    operator sees the violation.
    """


@dataclass(frozen=True)
class _FillIntegrationOutcome:
    """Per-fill diff handed to the activity-log emitter.

    Bundles the fields the per-fill emitter needs so its signature stays
    cohesive instead of spreading across positional / kw arguments.
    ``strategy_incomplete_legs``, when populated, marks an mleg cancel-mid-fill
    scenario surfaced by the strategy-fill handler — it triggers a
    ``BRACKET_INCOMPLETE_WARNING`` activity-log entry naming the unfilled
    siblings.
    """

    fill: FillRecord
    order: OrderRecord
    position_before: PositionRecord
    position_after: PositionRecord
    bracket_status_change: BracketStatus | None
    cash_delta_usd: Decimal
    direction_is_buy: bool
    strategy_incomplete_legs: tuple[str, ...] = ()


class _SnapshotLookup:
    """``AlpacaPositionLookup`` backed by the ``alpaca_positions`` tuple.

    The same positions feed reconciliation; constructing the lookup from them
    avoids a second Alpaca fetch and keeps handler-side reads consistent with
    the snapshot the reconciler will compare against.
    """

    def __init__(self, positions: tuple[PositionSnapshot, ...]) -> None:
        self._by_symbol: dict[str, PositionSnapshot] = {p.symbol: p for p in positions}

    def get_position(self, symbol: str) -> PositionSnapshot | None:
        return self._by_symbol.get(symbol)


@dataclass(frozen=True)
class Phase1Summary:
    """Outcome of one ``process_unprocessed_fills`` invocation.

    ``reconciliation_alerts`` counts every ``RECONCILIATION_ALERT`` activity-log
    entry this invocation emitted — the post-merge reconciliation step's
    (ALP-415) plus the per-fill quarantine alerts raised when a fill cannot be
    integrated (ALP-761). It is the total written to the activity log, so the
    summary cannot under-report orphan-fill alerts.

    ALP-619 — drift on an existing OPEN equity ``share_count`` and the
    singleton ``cash_ledger.current_cash_usd`` is now auto-corrected in the
    same Phase 1 transaction, with a paired ``RECONCILIATION_CORRECTION``
    activity-log row capturing the prior/applied scalars. Options drift
    and orphan positions still alert-only — see
    ``corporate_actions/reconciliation.py`` for the per-domain gating.
    Correction counts are not summarized here; the activity log is the
    source of truth.
    """

    fills_processed: int
    fills_quarantined: int
    ca_activities_processed: int
    reconciliation_alerts: int


async def process_unprocessed_fills(
    handle: InvocationHandle,
    ca_activities: tuple[CorporateActionActivity, ...] = (),
    alpaca_positions: tuple[PositionSnapshot, ...] = (),
    alpaca_account: TradeAccountSnapshot | None = None,
    *,
    market_inputs: MarketInputs,
    config: StatePersistenceConfig,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
    alpaca_orders: tuple[OrderSnapshot, ...] = (),
) -> Phase1Summary:
    """Drain every unprocessed fill + CA activity and integrate them atomically.

    Per ``corporate-actions.md § Phase 1 integration sequence``, fills and CA
    activities are interleaved by timestamp ascending so the post-state at
    each event reflects the chronologically correct quantity / cost basis. On
    exact-timestamp ties, fills resolve before CAs — fills are intra-day
    precise datetimes; CA activities are EOD-posted with midnight-UTC anchors
    on the ex-date; the ordering matches market reality and yields
    determinism. After all events apply, the reconciliation step compares
    local state to ``alpaca_positions`` / ``alpaca_account`` and emits one
    ``RECONCILIATION_ALERT`` per unexplained delta. ALP-619 — for OPEN
    equity ``share_count`` drift and ``cash_ledger.current_cash_usd``
    drift, the same step writes Alpaca's value back to local state and
    emits a paired ``RECONCILIATION_CORRECTION`` row. Options drift,
    orphans, direction-flips, and broker-degraded snapshots are alert-only
    (see ``corporate_actions/reconciliation.py``).

    Per-fill Reg T margin attribution (story 06a / ALP-428) is wedged into
    the merged-events loop: for every ``FillRecord`` event, snapshot all
    positions before integration, integrate the fill, snapshot all
    positions after, call :func:`compute_attribution`, and write the
    serialized attribution into the fill row's ``regt_attribution_json``
    column. Quarantined fills are excluded from the loop and retain
    ``regt_attribution_json IS NULL`` per parent decision (H). The Reg T
    attribution config is loaded once at function entry via
    :func:`load_regt_margin_attribution_config` (rather than threaded
    through the signature) so existing callers and tests do not need to
    construct it.

    All state mutations and activity-log emissions join the open
    ``handle.session`` transaction; the surrounding ``InvocationContext``
    commits on clean exit and rolls back on exception, leaving fills and CA
    activities unprocessed for the next invocation's Phase 1 to retry.

    Args:
        handle: Open ``InvocationHandle`` from the surrounding
            ``InvocationContext``.
        ca_activities: Tuple of typed CA activities to interleave with fills.
            Defaults to empty so pre-04 unit tests keep working unchanged;
            production callers always pass the fetcher's output.
        alpaca_positions: Tuple of typed Alpaca position snapshots for the
            reconciliation step. Defaults to empty (no positional alerts
            emitted); production callers pass ``AccountStateQueries.get_positions()``.
        alpaca_account: Typed Alpaca account snapshot for the cash-reconciliation
            step. Defaults to ``None`` (no cash alert emitted); production
            callers pass ``AccountStateQueries.get_account()``.
        market_inputs: Market data the per-fill attribution wedge reads —
            ``underlying_prices`` must cover every open-position underlying
            (``KeyError`` otherwise); ``iv_provider`` must serve every leg
            on a class group with options; ``risk_free_rate`` and ``as_of``
            anchor Black-Scholes pricing in the PM-equivalent path. Required
            keyword-only.
        config: State-persistence configuration knobs (currently unused; the
            signature is forward-shaped).
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.
    regt_config = load_regt_margin_attribution_config()

    fill_rows = await _read_unprocessed_fill_rows(handle)
    fills = tuple(fill_row_to_record(row) for row in fill_rows)
    rows_by_fill_id = {row.fill_id: row for row in fill_rows}

    valid_fills, quarantined_count = _quarantine_invalid(handle, fills, rows_by_fill_id)

    # Build a per-symbol lookup the handlers consult for post-adjustment Alpaca
    # state on the CA types that need it (STOCK_MERGER, SPIN_OFF, and the
    # options / strategy branches of REVERSE_SPLIT / STOCK_DIVIDEND). The same
    # positions feed reconciliation below, so the lookup is free.
    alpaca_lookup = _SnapshotLookup(alpaca_positions) if alpaca_positions else None

    # ALP-836 — recover any order whose post-submit ``alpaca_order_id`` backfill
    # was lost (durable PENDING_SUBMIT row + synthetic placeholder) by matching
    # Alpaca's orders on ``client_order_id``. Runs BEFORE fill integration so the
    # row is flipped PENDING_SUBMIT → PENDING + carries the real broker id before
    # its attributed fill advances it to FILLED. ``alpaca_orders`` is empty (and
    # this a no-op) unless the read-phase gatherer found local PENDING_SUBMIT rows.
    await backfill_pending_submit_orders(handle, alpaca_orders=alpaca_orders)

    fills_processed = 0
    quarantine_alerts = 0
    for event in _iter_merged_events(valid_fills, ca_activities):
        if isinstance(event, FillRecord):
            processed = await _integrate_or_quarantine_fill(
                handle,
                event,
                rows_by_fill_id[event.fill_id],
                market_inputs=market_inputs,
                regt_config=regt_config,
                borrow_cost_resolver=borrow_cost_resolver,
            )
            if processed:
                fills_processed += 1
            else:
                # Every in-loop quarantine emits exactly one reconciliation alert.
                quarantined_count += 1
                quarantine_alerts += 1
        else:
            await _integrate_one_ca_activity(handle, event, alpaca_lookup)

    reconciliation_alerts = await reconcile(
        handle,
        alpaca_positions=alpaca_positions,
        alpaca_account=alpaca_account,
    )

    await stamp_phase_completion(handle, column="phase1_completed_at")

    return Phase1Summary(
        fills_processed=fills_processed,
        fills_quarantined=quarantined_count,
        ca_activities_processed=len(ca_activities),
        # Count every RECONCILIATION_ALERT this invocation wrote — the post-merge
        # reconciler's plus the per-fill quarantine alerts (ALP-761) — so the
        # summary persisted into fill_collection_summary_json does not under-report
        # the orphan alerts an operator needs to see.
        reconciliation_alerts=reconciliation_alerts + quarantine_alerts,
    )


def _iter_merged_events(
    fills: tuple[FillRecord, ...],
    ca_activities: tuple[CorporateActionActivity, ...],
) -> tuple[FillRecord | CorporateActionActivity, ...]:
    """Return fills + CA activities interleaved by timestamp ascending.

    Per ``corporate-actions.md § Phase 1 integration sequence`` step 2:
    "Sort ascending. Ordering matters when a fill straddles an ex-date — fills
    before the CA reflect pre-action quantities, fills after reflect
    post-action."

    On exact-timestamp ties, fills come before CAs (sort-key 0 vs 1). CA
    activities are typically EOD-posted (Alpaca anchors them at midnight UTC
    on the ex-date); intra-day fills on the same calendar date carry precise
    sub-day timestamps and arrive strictly before midnight UTC of the next
    day. The tie-break preserves market reality (a 9:30 AM fill on the
    ex-date sees the pre-CA share count) and yields deterministic ordering.
    """
    events: list[tuple[datetime, int, FillRecord | CorporateActionActivity]] = [
        (fill.fill_timestamp, 0, fill) for fill in fills
    ]
    events.extend((activity.transaction_time, 1, activity) for activity in ca_activities)
    events.sort(key=lambda triple: (triple[0], triple[1]))
    return tuple(event for _ts, _kind, event in events)


# ---------------------------------------------------------------------------
# Fill-integration core
# ---------------------------------------------------------------------------


async def _read_all_positions(handle: InvocationHandle) -> tuple[PositionRecord, ...]:
    """Snapshot OPEN + PENDING ``positions`` rows, rehydrated to typed records.

    Used by the per-fill Reg T attribution wedge (story 06a / ALP-428) to
    capture pre- and post-fill state. CLOSED positions are filtered at the
    SQL boundary: their contribution to the attribution math is zero by
    construction (closed positions hold no contracts and no shares), so
    including them would be a no-op that scales linearly with the
    position-history table — a cost the table will pay every fill, every
    time. PENDING is retained because a fill can transition PENDING → OPEN
    inside the wedge (pre = PENDING, post = OPEN); excluding PENDING would
    drop the post-snapshot's just-opened row when the assembler downstream
    only inspects the attribution payload.
    """
    stmt = select(PositionRow).where(
        PositionRow.status.in_([PositionStatus.OPEN.value, PositionStatus.PENDING.value])
    )
    rows = (await handle.session.execute(stmt)).scalars()
    return tuple(position_row_to_record(row) for row in rows)


async def _read_unprocessed_fill_rows(
    handle: InvocationHandle,
) -> list[FillRecordRow]:
    """Pull every ``processing_status='unprocessed'`` row in fill-timestamp order.

    Backed by ``ix_fill_records_processing_status`` (story ALP-363) so the
    sweep stays cheap as the table grows.
    """
    stmt = (
        select(FillRecordRow)
        .where(FillRecordRow.processing_status == FillProcessingStatus.UNPROCESSED.value)
        .order_by(FillRecordRow.fill_timestamp.asc(), FillRecordRow.fill_id.asc())
    )
    return list((await handle.session.execute(stmt)).scalars())


def _quarantine_invalid(
    handle: InvocationHandle,
    fills: tuple[FillRecord, ...],
    rows_by_fill_id: dict[str, FillRecordRow],
) -> tuple[tuple[FillRecord, ...], int]:
    """Validate each fill and quarantine the malformed ones in place.

    Returns the surviving fills (in original order) plus the quarantine
    count. Validation rejects non-positive quantity; future revisions can
    extend this with price-plausibility and order-existence checks once the
    upstream contract surfaces them.
    """
    surviving: list[FillRecord] = []
    quarantined = 0
    for fill in fills:
        if fill.fill_quantity > 0:
            surviving.append(fill)
            continue
        _stamp_quarantined(rows_by_fill_id[fill.fill_id], handle.invocation_id)
        quarantined += 1
    return tuple(surviving), quarantined


def _stamp_quarantined(row: FillRecordRow, invocation_id: str) -> None:
    """Stamp a fill row's processing-status fields as QUARANTINED."""
    row.processing_status = FillProcessingStatus.QUARANTINED.value
    row.processing_invocation_id = invocation_id
    row.processing_timestamp = datetime.now(UTC).isoformat()


async def _integrate_or_quarantine_fill(
    handle: InvocationHandle,
    fill: FillRecord,
    row: FillRecordRow,
    *,
    market_inputs: MarketInputs | None,
    regt_config: RegTMarginAttributionConfig | None,
    borrow_cost_resolver: Callable[[str], float | None] | None,
) -> bool:
    """Integrate one fill, or quarantine it; return ``True`` iff it integrated.

    Two layers keep a single un-integratable fill from wedging the whole batch
    (ALP-761):

    * **Pre-integration gate.** Two checks run before the savepoint mutates
      anything: (1) a fill whose target position is in a terminal status
      (``CANCELLED`` / ``CLOSED``) is an orphan — quarantine + alert rather
      than letting it reach :func:`_apply_fill_to_equity_position`; (2) a fill
      whose ``fill_quantity`` exceeds the order's ``remaining_quantity`` (beyond
      ``_QTY_EPSILON``) is an over-fill — quarantine + alert rather than
      integrating it and double-counting the position (ALP-766). Both arms
      return ``False`` without entering the savepoint.

    * **Defense-in-depth.** Target resolution and the state mutation in
      :func:`_integrate_one_fill` both run under one try; the mutation runs
      inside a SAVEPOINT so any *unexpected* per-fill failure (a raise from an
      apply-fill helper, a codec error decoding a row, …) rolls back only this
      fill's partial mutations, and the fill is quarantined + alerted while the
      loop continues. Resolution runs inside the same try (it predates the
      savepoint and mutates nothing) so a resolution failure is isolated too,
      not propagated past the gate.

    Two failure classes deliberately escape this isolation and abort the
    invocation for retry rather than silently quarantining fill-by-fill:

    * :class:`StateInconsistencyError` — a Tier-1 row references a missing FK
      target. That is structural corruption, not a per-fill orphan; the
      operator must see it (re-raised below).
    * A ``compute_attribution`` failure — a missing-market-data gap that affects
      every open position. It runs *after* the savepoint commits, so it
      propagates ("surface the gap as a hard error"). A quarantined fill
      therefore retains ``regt_attribution_json IS NULL`` (parent decision (H)).
    """
    target: PositionRecord | None = None
    pre_positions: tuple[PositionRecord, ...] = ()
    if market_inputs is not None:
        pre_positions = await _read_all_positions(handle)
    try:
        target = await _resolve_target_position(handle, fill)
        if target.status in _NON_INTEGRATABLE_STATUSES:
            _quarantine_fill(
                handle,
                fill,
                row,
                position_id=target.position_id,
                delta_description=_orphan_quarantine_message(fill, target),
            )
            return False
        order = await _read_order(handle, fill.order_id)
        if fill.fill_quantity > order.remaining_quantity + _QTY_EPSILON:
            _quarantine_fill(
                handle,
                fill,
                row,
                position_id=target.position_id,
                delta_description=_over_fill_quarantine_message(fill, order),
            )
            return False
        async with handle.session.begin_nested():
            await _integrate_one_fill(handle, fill, borrow_cost_resolver=borrow_cost_resolver)
    except StateInconsistencyError:
        # Structural corruption (a Tier-1 row points at a missing FK target) is
        # not a per-fill orphan — surface it loudly and let the surrounding
        # InvocationContext roll the batch back, the same hard-stop contract as
        # a systemic attribution gap.
        raise
    except Exception as exc:
        # Broad by intent: the whole point of ALP-761 is that no single fill,
        # however its integration fails, can abort the batch. The savepoint above
        # has already rolled back this fill's partial mutations; quarantine +
        # alert surfaces it for manual reconciliation rather than swallowing it.
        _quarantine_fill(
            handle,
            fill,
            row,
            position_id=target.position_id if target is not None else None,
            delta_description=_failed_quarantine_message(fill, exc),
        )
        return False

    if market_inputs is not None:
        _config = regt_config if regt_config is not None else load_regt_margin_attribution_config()
        post_positions = await _read_all_positions(handle)
        attribution = compute_attribution(
            pre_fill_positions=pre_positions,
            post_fill_positions=post_positions,
            market_inputs=market_inputs,
            config=_config,
        )
        row.regt_attribution_json = attribution.model_dump_json()
    _mark_processed(row, handle.invocation_id)
    return True


async def _resolve_target_position(handle: InvocationHandle, fill: FillRecord) -> PositionRecord:
    """Resolve the position a fill targets, read-only, for the pre-integration gate.

    Reuses the same ``_read_order`` + ``_read_position_for_order`` lookups
    :func:`_integrate_one_fill` runs; within one transaction the second
    resolution there is an identity-map cache hit. The gate mutates nothing, so
    running it before the savepoint is safe. Raises (missing order / bracket /
    position) propagate to the caller's try, which isolates the fill — the gate
    never wedges the batch on its own.
    """
    order = await _read_order(handle, fill.order_id)
    _row, position = await _read_position_for_order(handle, order)
    return position


def _quarantine_fill(
    handle: InvocationHandle,
    fill: FillRecord,
    row: FillRecordRow,
    *,
    position_id: str | None,
    delta_description: str,
) -> None:
    """Stamp a fill ``QUARANTINED`` and emit the reconciliation alert surfacing it."""
    _stamp_quarantined(row, handle.invocation_id)
    _emit_quarantine_alert(
        handle, fill, position_id=position_id, delta_description=delta_description
    )


def _orphan_quarantine_message(fill: FillRecord, target: PositionRecord) -> str:
    """Operator-facing alert text for a fill against a terminal-status position."""
    return (
        f"Fill {fill.fill_id!r} (order {fill.order_id!r}, {fill.fill_quantity} @ "
        f"{fill.fill_price}) targets position {target.position_id!r} in terminal "
        f"status {target.status.value}; quarantined so the batch completes. The "
        "underlying broker position may still exist and needs separate "
        "reconciliation (ALP-760)."
    )


def _over_fill_quarantine_message(fill: FillRecord, order: OrderRecord) -> str:
    """Operator-facing alert text for a fill that exceeds the order's remaining quantity.

    Typically a recovery-sweep aggregate (cumulative qty @ avg price) landing on
    an already-fully-filled order whose genuine per-execution partials have all
    been integrated (ALP-766).
    """
    return (
        f"Fill {fill.fill_id!r} (order {fill.order_id!r}, qty {fill.fill_quantity} @ "
        f"{fill.fill_price}) exceeds order remaining quantity "
        f"{order.remaining_quantity}; quarantined to prevent double-count. Likely "
        "a recovery-sweep aggregate on a fully-filled order (ALP-766)."
    )


def _failed_quarantine_message(fill: FillRecord, exc: Exception) -> str:
    """Operator-facing alert text for a fill that failed to integrate unexpectedly.

    Deliberately does *not* assume a live broker-position orphan — the failure
    may be anywhere in integration (cash, drawdown, codec), and the savepoint has
    rolled the fill's mutations back, so the affected position's local state is
    unchanged.
    """
    return (
        f"Fill {fill.fill_id!r} (order {fill.order_id!r}, {fill.fill_quantity} @ "
        f"{fill.fill_price}) could not be integrated in Phase-1 "
        f"({type(exc).__name__}: {exc}); quarantined so the batch completes. Local "
        "state for the affected position is unchanged — review the fill and "
        "reconcile against the broker manually."
    )


def _emit_quarantine_alert(
    handle: InvocationHandle,
    fill: FillRecord,
    *,
    position_id: str | None,
    delta_description: str,
) -> None:
    """Emit a ``RECONCILIATION_ALERT`` activity-log entry for a quarantined fill.

    Routes through the same ``RECONCILIATION_ALERT`` channel the post-merge
    reconciler uses (ALP-415), so operator tooling already watching for
    reconciliation alerts surfaces the orphaned fill. ``local_value`` carries the
    orphaned fill quantity (the operationally salient scalar — e.g. the 19
    unguarded shares from the 2026-06-01 incident); ``alpaca_value`` is 0.0 (the
    fill never reached local state).
    """
    _emit(
        handle,
        event_type=EventType.RECONCILIATION_ALERT,
        order_id=fill.order_id,
        position_id=position_id,
        thesis_id=None,
        timestamp=fill.fill_timestamp,
        detail=ReconciliationAlertDetail(
            domain="position",
            field_name="processing_status",
            local_value=float(fill.fill_quantity),
            alpaca_value=0.0,
            delta_description=delta_description,
        ),
    )


async def _integrate_one_fill(
    handle: InvocationHandle,
    fill: FillRecord,
    *,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
) -> None:
    """Apply the design-doc nine-step sequence for one fill.

    Composes per-entity update helpers — each helper takes the typed
    record, returns the updated typed record, and then the row is
    re-projected via the matching codec. This keeps the per-step logic
    pure and testable through the public entry point.

    Strategy / mleg positions take a per-leg-aware path that consults
    sibling per-leg ``OrderRow`` statuses to gate the atomic PENDING → OPEN
    transition (per ``broker-adapter.md § Multi-leg fill events``).

    ``borrow_cost_resolver`` is forwarded to the equity dispatcher so a
    SHORT-equity PENDING → OPEN transition can stamp the four short-only
    fields (borrow_rate_pct, accrued_borrow_cost_usd, locate_status,
    margin_held_usd). LONG equity, single-leg options, and strategy paths
    ignore the resolver.
    """
    order = await _read_order(handle, fill.order_id)
    updated_order = _apply_fill_to_order(order, fill)
    await _persist_order_update(handle, updated_order)

    position_row, position = await _read_position_for_order(handle, order)
    # Strategy MLEG envelopes carry direction=None — the per-leg child orders
    # carry meaningful sides. The strategy branch below routes per-leg via
    # OrderRole rather than envelope direction, so this dispatch flag is only
    # consulted by the equity / single-leg options branch (ALP-614).
    envelope_direction = order_direction(order)
    direction_is_buy = envelope_direction is not None and (
        direction_to_side(envelope_direction) == "buy"
    )

    if isinstance(position.details, StrategyPositionDetails):
        updated_position, incomplete_legs = await _apply_strategy_fill_to_position(
            handle,
            position,
            position.details,
            fill,
            updated_order=updated_order,
        )
    else:
        updated_position = _apply_fill_to_position(
            position,
            fill,
            is_buy_side=direction_is_buy,
            borrow_cost_resolver=borrow_cost_resolver,
        )
        incomplete_legs = ()
    _persist_position_update(position_row, updated_position)

    bracket_status_change = await _maybe_update_bracket(handle, position, updated_position)
    if incomplete_legs:
        # Cancel-mid-fill: dissolve the bracket once (subsequent fills observe
        # the already-DISSOLVED bracket and skip a duplicate warning).
        bracket_status_change, incomplete_legs = await _dissolve_bracket_for_incomplete_strategy(
            handle,
            updated_position.bracket_id,
            bracket_status_change,
            incomplete_legs,
        )
    cash_delta = await _apply_cash_movement(handle, order, fill)

    if direction_is_buy:
        await _emit_capital_release(handle, order, fill)

    outcome = _FillIntegrationOutcome(
        fill=fill,
        order=updated_order,
        position_before=position,
        position_after=updated_position,
        bracket_status_change=bracket_status_change,
        cash_delta_usd=cash_delta,
        direction_is_buy=direction_is_buy,
        strategy_incomplete_legs=incomplete_legs,
    )
    await _emit_fill_activity_log_entries(handle, outcome)

    await _stamp_drawdown_state(handle)


def _mark_processed(row: FillRecordRow, invocation_id: str) -> None:
    """Stamp the fill row's processing-status fields per the design doc."""
    row.processing_status = FillProcessingStatus.PROCESSED.value
    row.processing_invocation_id = invocation_id
    row.processing_timestamp = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Order updates
# ---------------------------------------------------------------------------


async def _read_order(handle: InvocationHandle, order_id: str) -> OrderRecord:
    row = await handle.session.get(OrderRow, order_id)
    if row is None:
        msg = f"Phase 1 fill references missing order_id={order_id!r}"
        raise ValueError(msg)
    return order_row_to_record(row)


def _apply_fill_to_order(order: OrderRecord, fill: FillRecord) -> OrderRecord:
    """Increment filled_quantity, recompute weighted avg fill price, advance status."""
    new_filled = order.filled_quantity + fill.fill_quantity
    new_remaining = max(order.quantity - new_filled, 0.0)
    prior_avg = order.avg_fill_price if order.avg_fill_price is not None else 0.0
    # ALP-462 — ``fill.fill_price`` is ``Price`` (Decimal); coerce the float-typed
    # quantity through ``Decimal(str(...))`` so the weighted-avg arithmetic stays
    # exact, then surface as float for the legacy ``avg_fill_price`` field.
    fp_decimal = fill.fill_price
    fq_decimal = Decimal(str(fill.fill_quantity))
    prior_avg_decimal = Decimal(str(prior_avg))
    filled_qty_decimal = Decimal(str(order.filled_quantity))
    new_filled_decimal = Decimal(str(new_filled))
    new_avg = float(
        (prior_avg_decimal * filled_qty_decimal + fp_decimal * fq_decimal) / new_filled_decimal
    )
    new_status = (
        OrderStatus.FILLED if new_remaining <= _QTY_EPSILON else OrderStatus.PARTIALLY_FILLED
    )
    return dataclasses.replace(
        order,
        filled_quantity=new_filled,
        remaining_quantity=new_remaining,
        avg_fill_price=new_avg,
        status=new_status,
        last_update_timestamp=fill.fill_timestamp,
    )


async def _persist_order_update(handle: InvocationHandle, order: OrderRecord) -> None:
    # The order row is identity-mapped from the just-completed read, so this
    # is a cache hit — no extra round-trip.
    row = await handle.session.get(OrderRow, order.order_id)
    if row is None:  # pragma: no cover — read+write inside same transaction
        msg = f"order row {order.order_id!r} disappeared between read and write"
        raise RuntimeError(msg)
    row.status = order.status.value
    row.filled_quantity = order.filled_quantity
    row.average_fill_price = order.avg_fill_price
    row.remaining_quantity = order.remaining_quantity
    row.last_update_timestamp = order.last_update_timestamp.isoformat()


# ---------------------------------------------------------------------------
# Position updates
# ---------------------------------------------------------------------------


async def _read_position_for_order(
    handle: InvocationHandle, order: OrderRecord
) -> tuple[PositionRow, PositionRecord]:
    """Resolve the PositionRow this order acts on and decode it.

    Buy-side entries: ``order.position_id`` may be ``None`` (the OPEN
    command pre-creates the position with this order's bracket_id). Resolve
    via ``brackets.position_id``. Exit / add fills already carry
    ``position_id``.
    """
    position_id = order.position_id or await _resolve_position_id_via_bracket(handle, order)
    row = await handle.session.get(PositionRow, position_id)
    if row is None:
        msg = f"Phase 1 fill references missing position_id={position_id!r}"
        raise ValueError(msg)
    return row, position_row_to_record(row)


async def _resolve_position_id_via_bracket(handle: InvocationHandle, order: OrderRecord) -> str:
    bracket_row = await handle.session.get(BracketRow, order.bracket_id)
    if bracket_row is None:
        msg = (
            f"Phase 1 fill references missing bracket_id={order.bracket_id!r} "
            "while resolving position for entry fill"
        )
        raise ValueError(msg)
    return bracket_row.position_id


def _apply_fill_to_position(
    position: PositionRecord,
    fill: FillRecord,
    *,
    is_buy_side: bool,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
) -> PositionRecord:
    """Dispatch entry / add / exit handling based on instrument type, status, and direction.

    Strategy / mleg positions never reach this function: the caller
    (:func:`_integrate_one_fill`) routes them to
    :func:`_apply_strategy_fill_to_position` because the strategy path needs
    DB access to consult sibling per-leg order statuses.

    ``borrow_cost_resolver`` is consulted by the equity branch on a
    SHORT-PENDING entry-fill to stamp the four short-only fields. Options
    positions are direction-aware natively (SELL_TO_OPEN routes to the
    entry-fill helper without a borrow leg), so the resolver is not threaded
    into the options branch.
    """
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return _apply_fill_to_equity_position(
            position,
            details,
            fill,
            is_buy_side=is_buy_side,
            borrow_cost_resolver=borrow_cost_resolver,
        )
    if isinstance(details, OptionsPositionDetails):
        return _apply_fill_to_options_position(position, details, fill, is_buy_side=is_buy_side)
    # Strategy positions are intercepted upstream; this branch defends against
    # a future detail variant being added without updating the dispatcher.
    msg = f"Phase 1 fill integration: unhandled instrument type {details.instrument_type!r}"
    raise NotImplementedError(msg)


def _apply_fill_to_equity_position(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
    *,
    is_buy_side: bool,
    borrow_cost_resolver: Callable[[str], float | None] | None,
) -> PositionRecord:
    """Direction-aware dispatcher mirroring the options dispatcher.

    The 8 routing cases (status x direction x buy_side):

    +---------+-----------+----------+--------------------+
    | Status  | Direction | Buy side | Routing            |
    +=========+===========+==========+====================+
    | PENDING | LONG      | True     | _apply_entry_fill  |
    | PENDING | LONG      | False    | ValueError         |
    | PENDING | SHORT     | True     | ValueError         |
    | PENDING | SHORT     | False    | _apply_entry_fill  |
    | OPEN    | LONG      | True     | _apply_add_fill    |
    | OPEN    | LONG      | False    | _apply_exit_fill   |
    | OPEN    | SHORT     | True     | _apply_exit_fill   |
    | OPEN    | SHORT     | False    | _apply_add_fill    |
    +---------+-----------+----------+--------------------+

    The two defensive ValueError cases (PENDING LONG + SELL, PENDING SHORT +
    BUY) represent "a position cannot close before it opens" — the broker
    cannot fire an exit-side fill on a position that has not yet
    transitioned PENDING → OPEN.
    """
    direction = position_direction(position)
    # Equity positions always carry direction; strategy positions are
    # intercepted upstream by _integrate_one_fill.
    assert direction is not None
    if position.status == PositionStatus.PENDING:
        if _is_opening_fill(direction, is_buy_side):
            return _apply_entry_fill(
                position, details, fill, borrow_cost_resolver=borrow_cost_resolver
            )
        msg = (
            f"Phase 1 received closing fill on PENDING position "
            f"{position.position_id!r}; a position cannot close before it opens"
        )
        raise ValueError(msg)
    if position.status == PositionStatus.OPEN:
        if _is_opening_fill(direction, is_buy_side):
            return _apply_add_fill(position, details, fill)
        return _apply_exit_fill(position, details, fill)
    msg = f"Phase 1 cannot integrate fill against position status {position.status!r}"
    raise ValueError(msg)


def _apply_fill_to_options_position(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
    *,
    is_buy_side: bool,
) -> PositionRecord:
    """Options branch: dispatch entry / add / exit per status and direction.

    SHORT options entries (SELL_TO_OPEN) are first-class — unlike equity, where
    short entry requires a borrow leg ``OptionsPositionDetails`` doesn't model.
    The fill direction for an options entry always aligns with the position's
    direction (LONG ⇐ BUY_TO_OPEN, SHORT ⇐ SELL_TO_OPEN), so PENDING positions
    dispatch unconditionally to the entry-fill helper.

    On OPEN positions, an "opening" fill (one that grows the position) is
    same-sided as the position direction; an exit fill is opposite-sided.
    """
    if position.status == PositionStatus.PENDING:
        return _apply_options_entry_fill(position, details, fill)
    if position.status == PositionStatus.OPEN:
        # Strategy positions are intercepted upstream by _integrate_one_fill;
        # this helper only ever sees single-leg options, so position_direction()
        # is non-None here.
        direction = position_direction(position)
        assert direction is not None
        if _is_opening_fill(direction, is_buy_side):
            return _apply_options_add_fill(position, details, fill)
        return _apply_options_exit_fill(position, details, fill)
    msg = f"Phase 1 cannot integrate fill against position status {position.status!r}"
    raise ValueError(msg)


def _is_opening_fill(direction: Direction, is_buy_side: bool) -> bool:
    """Same-sided fills grow the position; opposite-sided fills exit it."""
    return is_buy_side == (direction == Direction.LONG)


def _strategy_fill_is_reducing(
    details: StrategyPositionDetails,
    order: OrderRecord,
    *,
    is_buy_side: bool,
) -> bool:
    """Return whether a fill against an OPEN strategy reduced (closed) it.

    A strategy has no position-level direction, so the opening-vs-reducing
    signal is per-leg: the fill targets one :class:`StrategyLeg`, and a
    same-sided fill (buy on a long leg / sell on a short leg) grows that leg
    while an opposite-sided fill reduces it. This mirrors the per-leg
    classification :func:`_apply_strategy_open_fill` uses to route the fill to
    the ADD vs CLOSE handler — an OPEN strategy can be grown via the ADD path,
    so a strategy OPEN→OPEN fill is *not* unconditionally reducing.
    """
    leg = _strategy_leg_for_order(details.legs, order)
    # Safe: an OPEN strategy's legs always carry an explicit direction (bracket_stops/wiring.py
    # raises ValueError on a None leg direction), so leg.direction is non-None here.
    leg_is_long = leg.direction != Direction.SHORT
    is_opening_for_leg = is_buy_side == leg_is_long
    return not is_opening_for_leg


def _fill_reduced_position(
    position: PositionRecord,
    order: OrderRecord,
    *,
    direction_is_buy: bool,
) -> bool:
    """Return whether a fill on an OPEN position reduced (closed) it.

    Equity / single-leg options have a position-level direction:
    :func:`position_direction` is non-None and an opposite-sided fill reduces
    the position. A multi-leg strategy has no position-level side, so the
    reducing signal is derived per-leg from the fill's order
    (:func:`_strategy_fill_is_reducing`).
    """
    if isinstance(position.details, StrategyPositionDetails):
        return _strategy_fill_is_reducing(position.details, order, is_buy_side=direction_is_buy)
    direction = position_direction(position)
    assert direction is not None
    return not _is_opening_fill(direction, direction_is_buy)


def _apply_entry_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
    *,
    borrow_cost_resolver: Callable[[str], float | None] | None,
) -> PositionRecord:
    """PENDING → OPEN: set entry timestamp, quantity, cost basis, history.

    For a SHORT position, also stamp the four short-only fields:
    ``borrow_rate_pct`` (resolver lookup on the ticker), ``accrued_borrow_cost_usd``
    (initialised to 0.0 — Story 04 increments it daily), ``locate_status``
    (LOCATED by construction; the AT_RISK_OF_RECALL transition is
    broker-driven and lands separately), and ``margin_held_usd`` (Reg T
    initial margin = ``fill_quantity * fill_price * 0.50``).

    A missing resolver (``None``) or a resolver returning ``None`` for the
    ticker is an upstream contract violation — the analyst's validation tool
    short-circuits with UNAVAILABLE on MISSING_BORROW_COST, so a missing rate
    at entry-fill time means a SHORT EQUITY OpenCommand reached Phase 1
    despite the rate being unavailable. The helper raises ``ValueError`` in
    both cases.

    ALP-462: ``fill.fill_price`` is ``Price`` (Decimal); the legacy
    ``average_cost_basis_per_share`` field on ``EquityPositionDetails`` is
    typed ``float``. Cast at this codec boundary so the dataclass stores a
    float — the pre-ALP-477 Pydantic record auto-coerced Decimal→float, the
    frozen dataclass does not.
    """
    new_details = dataclasses.replace(
        details,
        share_count=fill.fill_quantity,
        average_cost_basis_per_share=float(fill.fill_price),
    )
    if position.direction == Direction.SHORT:
        if borrow_cost_resolver is None:
            msg = (
                f"Phase 1 SHORT-equity entry on {details.ticker!r} requires a "
                "borrow_cost_resolver; got None"
            )
            raise ValueError(msg)
        annual_fee_pct = borrow_cost_resolver(details.ticker)
        if annual_fee_pct is None:
            msg = (
                f"Phase 1 SHORT-equity entry on {details.ticker!r}: "
                "borrow_cost_resolver returned None, but the analyst's validation "
                "tool short-circuits with UNAVAILABLE on MISSING_BORROW_COST. "
                "A missing rate at entry-fill time is an upstream contract violation."
            )
            raise ValueError(msg)
        margin_held_usd = fill.fill_quantity * float(fill.fill_price) * 0.50
        new_details = dataclasses.replace(
            new_details,
            borrow_rate_pct=annual_fee_pct,
            accrued_borrow_cost_usd=0.0,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=margin_held_usd,
        )
    return dataclasses.replace(
        position,
        status=PositionStatus.OPEN,
        entry_timestamp=fill.fill_timestamp,
        details=new_details,
        execution_history=(_position_fill_from_record(fill),),
    )


def _apply_add_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """ADD-side fill on an OPEN position: increment quantity, recompute cost basis."""
    new_qty = details.share_count + fill.fill_quantity
    # ALP-462 — fill_price is ``Price`` (Decimal); coerce float quantities so
    # the weighted-cost arithmetic stays exact, then surface as float for the
    # legacy ``average_cost_basis_per_share`` field on EquityPositionDetails.
    fp = fill.fill_price
    fq = Decimal(str(fill.fill_quantity))
    prior_avg = Decimal(str(details.average_cost_basis_per_share))
    prior_count = Decimal(str(details.share_count))
    new_qty_decimal = Decimal(str(new_qty))
    weighted_cost = float((prior_avg * prior_count + fp * fq) / new_qty_decimal)
    new_details = dataclasses.replace(
        details, share_count=new_qty, average_cost_basis_per_share=weighted_cost
    )
    return dataclasses.replace(
        position,
        details=new_details,
        execution_history=(*position.execution_history, _position_fill_from_record(fill)),
    )


def _apply_exit_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """Exit fill: decrement quantity, compute direction-signed realized P/L
    for the exited portion. On a cover-to-close fill (the cover that takes
    ``share_count`` to zero on a SHORT position with a non-zero borrow
    accumulator), additionally flush ``accrued_borrow_cost_usd`` into
    realized P/L — short P/L is borrow-net.

    Partial covers do NOT flush — the position stays OPEN and the
    accumulator continues against the reduced notional from the next tick
    onward. On the cover-to-close branch the accumulator on the new details
    is PRESERVED as the lifetime borrow total (audit-trail readers consume
    both ``accrued_borrow_cost_usd`` (lifetime borrow drag) and
    ``realized_pnl_to_date_usd`` (borrow-net realized P/L) from the closed
    position).
    """
    qty_after = details.share_count - fill.fill_quantity
    if qty_after < -_QTY_EPSILON:
        msg = (
            f"exit fill quantity ({fill.fill_quantity}) exceeds open share count "
            f"({details.share_count}) for position_id={position.position_id!r}"
        )
        raise ValueError(msg)
    # ALP-462 — fill_price is ``Price`` (Decimal); thread the P/L math through
    # Decimal arithmetic, then cast back to float for the legacy fields.
    fp = fill.fill_price
    avg_cost = Decimal(str(details.average_cost_basis_per_share))
    pnl_per_share = fp - avg_cost
    # Strategy positions never reach this equity helper (intercepted upstream),
    # so position_direction() is non-None here.
    direction = position_direction(position)
    assert direction is not None
    direction_sign = Decimal(-1) if direction == Direction.SHORT else Decimal(1)
    fq = Decimal(str(fill.fill_quantity))
    realized_delta = float(pnl_per_share * fq * direction_sign)

    closed = abs(qty_after) < _QTY_EPSILON
    if closed and direction == Direction.SHORT and details.accrued_borrow_cost_usd:
        # Cover-to-close on a SHORT with a non-zero accumulator: flush the
        # lifetime borrow cost from realized P/L. Falsy zero short-circuits
        # the branch — covers that close out a flat-borrow short skip the
        # subtraction.
        realized_delta -= details.accrued_borrow_cost_usd

    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta

    new_details = dataclasses.replace(details, share_count=0.0 if closed else qty_after)
    new_history = (*position.execution_history, _position_fill_from_record(fill))
    if closed:
        return dataclasses.replace(
            position,
            details=new_details,
            execution_history=new_history,
            realized_pnl_to_date_usd=cumulative_realized,
            status=PositionStatus.CLOSED,
        )
    return dataclasses.replace(
        position,
        details=new_details,
        execution_history=new_history,
        realized_pnl_to_date_usd=cumulative_realized,
    )


def _apply_options_entry_fill(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """PENDING → OPEN options entry: set premium, contract count, history.

    ``premium_paid_per_contract`` stores the absolute per-contract premium
    (positive for both long and short positions) — symmetric with equity's
    ``average_cost_basis_per_share`` and consistent with the snapshot
    assembler's price-like usage of the field. Direction sign is applied
    where total cost basis or P/L is reported.

    Greeks set at OPEN-validation time by the guardrail-evaluation library
    are preserved unchanged — refresh is the continuous monitor's job.
    """
    new_details = dataclasses.replace(
        details,
        contract_count=fill.fill_quantity,
        premium_paid_per_contract=float(fill.fill_price),
    )
    return dataclasses.replace(
        position,
        status=PositionStatus.OPEN,
        entry_timestamp=fill.fill_timestamp,
        details=new_details,
        execution_history=(_position_fill_from_record(fill),),
    )


def _apply_options_add_fill(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """ADD-side options fill on an OPEN position: increment quantity, recompute premium."""
    new_count = details.contract_count + fill.fill_quantity
    # ALP-462 — Decimal-arithmetic weighted-avg; cast back to float for the
    # legacy ``premium_paid_per_contract`` field on OptionsPositionDetails.
    fp = fill.fill_price
    fq = Decimal(str(fill.fill_quantity))
    prior_premium = Decimal(str(details.premium_paid_per_contract))
    prior_count = Decimal(str(details.contract_count))
    new_count_decimal = Decimal(str(new_count))
    weighted_premium = float((prior_premium * prior_count + fp * fq) / new_count_decimal)
    new_details = dataclasses.replace(
        details, contract_count=new_count, premium_paid_per_contract=weighted_premium
    )
    return dataclasses.replace(
        position,
        details=new_details,
        execution_history=(*position.execution_history, _position_fill_from_record(fill)),
    )


def _apply_options_exit_fill(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """Exit options fill: decrement quantity, accumulate realized P/L (multiplier-scaled).

    Realized P/L mirrors the equity formula: ``(exit - entry) * qty * dir_sign``,
    further scaled by ``contract_multiplier``. dir_sign is +1 for LONG (long
    closed for higher than paid is profit) and -1 for SHORT (short covered
    for less than received is profit).
    """
    qty_after = details.contract_count - fill.fill_quantity
    if qty_after < -_QTY_EPSILON:
        msg = (
            f"options exit fill quantity ({fill.fill_quantity}) exceeds open contract count "
            f"({details.contract_count}) for position_id={position.position_id!r}"
        )
        raise ValueError(msg)
    # ALP-462 — fill_price is ``Price`` (Decimal); thread the P/L math through
    # Decimal arithmetic, then cast back to float for the legacy field.
    fp = fill.fill_price
    paid_premium = Decimal(str(details.premium_paid_per_contract))
    pnl_per_contract = fp - paid_premium
    # Strategy positions never reach this single-leg options helper
    # (intercepted upstream), so position_direction() is non-None here.
    direction = position_direction(position)
    assert direction is not None
    direction_sign = Decimal(-1) if direction == Direction.SHORT else Decimal(1)
    fq = Decimal(str(fill.fill_quantity))
    multiplier = Decimal(str(details.contract_multiplier))
    realized_delta = float(pnl_per_contract * fq * multiplier * direction_sign)
    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta

    closed = abs(qty_after) < _QTY_EPSILON
    new_details = dataclasses.replace(details, contract_count=0.0 if closed else qty_after)
    new_history = (*position.execution_history, _position_fill_from_record(fill))
    if closed:
        return dataclasses.replace(
            position,
            details=new_details,
            execution_history=new_history,
            realized_pnl_to_date_usd=cumulative_realized,
            status=PositionStatus.CLOSED,
        )
    return dataclasses.replace(
        position,
        details=new_details,
        execution_history=new_history,
        realized_pnl_to_date_usd=cumulative_realized,
    )


# ---------------------------------------------------------------------------
# Strategy / mleg fill integration (story 04b / ALP-392)
# ---------------------------------------------------------------------------


async def _apply_strategy_fill_to_position(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Apply a per-leg fill to an mleg strategy position.

    Per ``broker-adapter.md § Multi-leg fill events``, a strategy position is
    not considered open until every leg has reached ``filled`` status. Each
    per-leg fill updates the matching ``StrategyLeg.options`` (contract_count,
    premium_paid_per_contract) and appends to ``execution_history``; the
    PENDING → OPEN transition fires only when the post-fill sibling-leg
    statuses say every leg is FILLED.

    The fill correlates to a leg via the order's ``OptionsInstrumentSpec``:
    legs are uniquely identified by ``(contract_type, strike, expiration)`` —
    the OCC identity that distinguishes one option contract from another in
    a multi-leg structure. Entry and close orders against the same leg carry
    different ``order_id`` values but the same option identity.

    Returns ``(updated_position, incomplete_leg_ids)``: ``incomplete_leg_ids``
    is non-empty only when the parent strategy was canceled mid-fill (one or
    more sibling legs are CANCELLED while not all legs are FILLED), signalling
    that :func:`_emit_fill_activity_log_entries` should write a
    ``BRACKET_INCOMPLETE_WARNING`` entry for operator follow-up.
    """
    leg = _strategy_leg_for_order(details.legs, updated_order)
    if position.status == PositionStatus.PENDING:
        return await _apply_strategy_entry_or_continuation(
            handle, position, details, fill, updated_order=updated_order, leg=leg
        )
    if position.status == PositionStatus.OPEN:
        return await _apply_strategy_open_fill(
            handle, position, details, fill, updated_order=updated_order, leg=leg
        )
    msg = f"Phase 1 cannot integrate strategy fill against position status {position.status!r}"
    raise ValueError(msg)


async def _apply_strategy_entry_or_continuation(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
    leg: StrategyLeg,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Update one leg's contract_count + premium and gate the atomic OPEN.

    Once the post-fill legs are known, the parent payoff metrics
    (``net_premium_usd`` / ``max_profit_usd`` / ``max_loss_usd`` /
    ``breakeven_levels``) are recomputed from them — a no-op until the atomic
    PENDING → OPEN transition fills the final zero-count leg.
    """
    new_legs = _set_leg_entry(details.legs, leg=leg, fill=fill)
    new_details = _recompute_strategy_payoff_metrics(dataclasses.replace(details, legs=new_legs))

    sibling_statuses = await _read_sibling_leg_statuses(
        handle,
        position_id=position.position_id,
        excluded_order_id=updated_order.order_id,
    )
    all_other_filled = all(status == OrderStatus.FILLED for status in sibling_statuses.values())
    any_canceled = any(status == OrderStatus.CANCELLED for status in sibling_statuses.values())
    this_leg_filled = updated_order.status == OrderStatus.FILLED

    new_history = (*position.execution_history, _position_fill_from_record(fill))
    incomplete: tuple[str, ...] = ()
    if this_leg_filled and all_other_filled and not any_canceled:
        # Atomic PENDING → OPEN at the last leg's filled event.
        return (
            dataclasses.replace(
                position,
                details=new_details,
                execution_history=new_history,
                status=PositionStatus.OPEN,
                entry_timestamp=fill.fill_timestamp,
            ),
            incomplete,
        )
    if any_canceled:
        # Cancel mid-fill: surface the unfilled sibling leg ids so the
        # surrounding integrator emits a BRACKET_INCOMPLETE_WARNING.
        incomplete = tuple(
            sorted(
                order_id
                for order_id, status in sibling_statuses.items()
                if status != OrderStatus.FILLED
            )
        )
    return (
        dataclasses.replace(position, details=new_details, execution_history=new_history),
        incomplete,
    )


async def _apply_strategy_open_fill(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
    leg: StrategyLeg,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Apply a fill against an OPEN strategy: ADD (same-side) or CLOSE (opposite-side).

    On the ADD branch the parent payoff metrics are recomputed from the
    post-add legs — every leg of an OPEN strategy is already positive, so the
    recompute always fires.

    Dispatches by ``updated_order.role`` as a positive list — only ENTRY
    and ADD_ENTRY are opening; CLOSE / TAKE_PROFIT / PRICE_STOP / TIME_STOP
    are all closing — rather than inferring an opening / closing side from
    ``updated_order.direction``. The prior direction-based inference
    miscategorised SHORT-leg fills on credit-spread ADDs as closes (they
    are opens); the positive list also catches the symmetric case where
    a protective-leg MLEG envelope fires and per-leg child fills carry the
    parent's TAKE_PROFIT / PRICE_STOP / TIME_STOP role — those route to the
    close branch correctly (ALP-614).
    """
    is_opening_for_leg = updated_order.role in {OrderRole.ENTRY, OrderRole.ADD_ENTRY}
    if is_opening_for_leg:
        new_legs = _add_to_leg(details.legs, leg=leg, fill=fill)
        new_details = _recompute_strategy_payoff_metrics(
            dataclasses.replace(details, legs=new_legs)
        )
        return (
            dataclasses.replace(
                position,
                details=new_details,
                execution_history=(
                    *position.execution_history,
                    _position_fill_from_record(fill),
                ),
            ),
            (),
        )
    return await _apply_strategy_close_fill(
        handle,
        position,
        details,
        fill,
        updated_order=updated_order,
        leg=leg,
    )


async def _apply_strategy_close_fill(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
    leg: StrategyLeg,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Apply a closing fill: decrement leg quantity, accumulate signed P/L,
    flip status to CLOSED only when the last leg fully closes."""
    leg_options = leg.options
    qty_after = leg_options.contract_count - fill.fill_quantity
    if qty_after < -_QTY_EPSILON:
        msg = (
            f"strategy exit fill quantity ({fill.fill_quantity}) exceeds open contract count "
            f"({leg_options.contract_count}) for position_id={position.position_id!r}, "
            f"leg_id={leg.leg_id!r}"
        )
        raise ValueError(msg)
    closed_for_this_leg = abs(qty_after) < _QTY_EPSILON
    leg_direction_sign = Decimal(-1) if leg.direction == Direction.SHORT else Decimal(1)
    # ALP-462 — fill_price is ``Price`` (Decimal); Decimal-thread the strategy
    # leg P/L computation, then cast back to float for the legacy field.
    fp = fill.fill_price
    paid_premium = Decimal(str(leg_options.premium_paid_per_contract))
    pnl_per_contract = fp - paid_premium
    fq = Decimal(str(fill.fill_quantity))
    multiplier = Decimal(str(leg_options.contract_multiplier))
    realized_delta = float(pnl_per_contract * fq * multiplier * leg_direction_sign)
    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta

    new_options = dataclasses.replace(
        leg.options, contract_count=0.0 if closed_for_this_leg else qty_after
    )
    new_legs = _replace_leg(details.legs, leg_id=leg.leg_id, new_options=new_options)
    new_details = dataclasses.replace(details, legs=new_legs)

    new_history = (*position.execution_history, _position_fill_from_record(fill))
    sibling_statuses = await _read_sibling_leg_statuses(
        handle,
        position_id=position.position_id,
        excluded_order_id=updated_order.order_id,
    )
    all_others_terminal = all(
        status in (OrderStatus.FILLED, OrderStatus.CANCELLED)
        for status in sibling_statuses.values()
    )
    if (
        closed_for_this_leg
        and updated_order.status == OrderStatus.FILLED
        and all_others_terminal
        and _every_leg_closed(new_legs)
    ):
        return (
            dataclasses.replace(
                position,
                details=new_details,
                execution_history=new_history,
                realized_pnl_to_date_usd=cumulative_realized,
                status=PositionStatus.CLOSED,
            ),
            (),
        )
    return (
        dataclasses.replace(
            position,
            details=new_details,
            execution_history=new_history,
            realized_pnl_to_date_usd=cumulative_realized,
        ),
        (),
    )


def _recompute_strategy_payoff_metrics(
    details: StrategyPositionDetails,
) -> StrategyPositionDetails:
    """Return ``details`` with parent payoff metrics recomputed from its legs.

    Recomputes ``net_premium_usd`` / ``max_profit_usd`` / ``max_loss_usd`` /
    ``breakeven_levels`` from the (already-updated) legs so the parent metrics
    always agree with the legs once their counts and premiums are known.
    ``strategy_greeks`` is left untouched — a fill carries no greeks and per-leg
    greeks are still zero/stale at fill time; greek refresh is the continuous
    monitor's job.

    Recompute is gated on every leg having a positive ``contract_count`` — the
    ``strategy_payoff`` primitives raise ``ValueError`` on a zero-count leg.
    While any leg is still a zero-count skeleton (an entry not yet fully
    filled), the details are returned unchanged so the parent metrics stay at
    their skeleton zeros. The entry path first satisfies this at the atomic
    PENDING → OPEN transition (a strategy enters all legs simultaneously); an
    ADD on an already-OPEN strategy has every leg positive already.
    """
    if any(leg.options.contract_count <= 0 for leg in details.legs):
        return details
    net_premium_usd = compute_strategy_net_premium_usd(details.legs)
    return dataclasses.replace(
        details,
        net_premium_usd=net_premium_usd,
        max_profit_usd=compute_strategy_max_profit_usd(details.legs, net_premium_usd),
        max_loss_usd=compute_strategy_max_loss_usd(details.legs, net_premium_usd),
        breakeven_levels=compute_strategy_breakeven_levels(details.legs, net_premium_usd),
    )


def _strategy_leg_for_order(legs: tuple[StrategyLeg, ...], order: OrderRecord) -> StrategyLeg:
    """Return the strategy leg the order targets, matched on OCC option identity.

    Per-leg orders carry an ``OptionsInstrumentSpec``; the matching
    ``StrategyLeg.options`` agrees on ``(contract_type, strike,
    expiration_date)`` because a strategy is structurally a fixed set of
    option contracts. Entry / ADD / CLOSE orders against the same leg share
    the option identity but carry distinct ``order_id`` values.
    """
    spec = order.instrument_spec
    if not isinstance(spec, OptionsInstrumentSpec):
        msg = (
            f"strategy fill: order {order.order_id!r} carries non-options "
            f"instrument_spec {type(spec).__name__}; per-leg orders must use "
            "OptionsInstrumentSpec"
        )
        raise TypeError(msg)
    for leg in legs:
        opts = leg.options
        if (
            opts.contract_type == spec.contract_type
            and opts.strike_price == spec.strike
            and opts.expiration_date == spec.expiration
        ):
            return leg
    msg = (
        f"strategy fill: no leg matches order {order.order_id!r} on OCC identity "
        f"({spec.contract_type!r}, strike={spec.strike}, expiration={spec.expiration})"
    )
    raise ValueError(msg)


def _set_leg_entry(
    legs: tuple[StrategyLeg, ...],
    *,
    leg: StrategyLeg,
    fill: FillRecord,
) -> tuple[StrategyLeg, ...]:
    """Set the matching leg's contract_count and premium from a PENDING entry fill.

    A strategy enters all legs simultaneously at OPEN time, so each leg's
    pre-fill ``contract_count`` is zero — the entry fill establishes the
    initial size and premium.
    """
    new_options = dataclasses.replace(
        leg.options,
        contract_count=fill.fill_quantity,
        premium_paid_per_contract=float(fill.fill_price),
    )
    return _replace_leg(legs, leg_id=leg.leg_id, new_options=new_options)


def _add_to_leg(
    legs: tuple[StrategyLeg, ...],
    *,
    leg: StrategyLeg,
    fill: FillRecord,
) -> tuple[StrategyLeg, ...]:
    """Increment a leg's contract_count and recompute weighted-average premium."""
    prior_count = leg.options.contract_count
    new_count = prior_count + fill.fill_quantity
    # ALP-462 — fill_price is ``Price`` (Decimal); thread weighted-avg math
    # through Decimal arithmetic, then cast back to float for the legacy field.
    fp = fill.fill_price
    fq = Decimal(str(fill.fill_quantity))
    prior_premium = Decimal(str(leg.options.premium_paid_per_contract))
    prior_count_decimal = Decimal(str(prior_count))
    new_count_decimal = Decimal(str(new_count))
    weighted_premium = float((prior_premium * prior_count_decimal + fp * fq) / new_count_decimal)
    new_options = dataclasses.replace(
        leg.options, contract_count=new_count, premium_paid_per_contract=weighted_premium
    )
    return _replace_leg(legs, leg_id=leg.leg_id, new_options=new_options)


def _replace_leg(
    legs: tuple[StrategyLeg, ...],
    *,
    leg_id: str,
    new_options: OptionsPositionDetails,
) -> tuple[StrategyLeg, ...]:
    return tuple(
        StrategyLeg(
            leg_id=existing.leg_id,
            direction=existing.direction,
            options=new_options,
        )
        if existing.leg_id == leg_id
        else existing
        for existing in legs
    )


def _every_leg_closed(legs: tuple[StrategyLeg, ...]) -> bool:
    return all(abs(leg.options.contract_count) < _QTY_EPSILON for leg in legs)


async def _read_sibling_leg_statuses(
    handle: InvocationHandle,
    *,
    position_id: str,
    excluded_order_id: str,
) -> dict[str, OrderStatus]:
    """Return ``{leg_order_id: OrderStatus}`` for all per-leg orders on a strategy.

    Excludes the parent strategy order (``order_class=MLEG``) and the leg the
    current fill just updated — the caller has the latter's post-fill status
    in hand and gates the OPEN transition by combining it with the sibling
    statuses.
    """
    stmt = (
        select(OrderRow.order_id, OrderRow.status)
        .where(OrderRow.position_id == position_id)
        .where(OrderRow.order_class != OrderClass.MLEG.value)
        .where(OrderRow.order_id != excluded_order_id)
    )
    rows = (await handle.session.execute(stmt)).all()
    return {row.order_id: OrderStatus(row.status) for row in rows}


async def _dissolve_bracket_for_incomplete_strategy(
    handle: InvocationHandle,
    bracket_id: str | None,
    prior_change: BracketStatus | None,
    incomplete_legs: tuple[str, ...],
) -> tuple[BracketStatus | None, tuple[str, ...]]:
    """Dissolve the strategy bracket on cancel-mid-fill.

    The strategist resolves the partial residual on its next invocation per
    ``broker-adapter.md § Multi-leg fill events § Cancellation``; dissolving
    here surfaces the broken contract immediately rather than leaving the
    bracket in an inconsistent PENDING_ENTRY / ACTIVE state.

    Returns the (possibly updated) bracket status change plus the
    ``incomplete_legs`` tuple. The leg list is cleared when the bracket is
    already DISSOLVED — a prior fill in the same Phase 1 invocation has
    already emitted the warning, so subsequent fills suppress duplicates.
    """
    if bracket_id is None:
        return prior_change, incomplete_legs
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = f"position references bracket {bracket_id!r}, but bracket row is missing"
        raise StateInconsistencyError(msg)
    if bracket_row.status == BracketStatus.DISSOLVED.value:
        # A prior fill in this invocation already dissolved the bracket and
        # emitted the warning; suppress the duplicate.
        return prior_change, ()
    bracket_row.status = BracketStatus.DISSOLVED.value
    for leg_row in await _read_bracket_legs(handle, bracket_id):
        leg_row.leg_status = BracketLegStatus.CANCELLED.value
    return BracketStatus.DISSOLVED, incomplete_legs


def _persist_position_update(row: PositionRow, position: PositionRecord) -> None:
    """Project the updated record back onto the existing ``PositionRow``."""
    new_row = position_record_to_row(position)
    row.status = new_row.status
    row.entry_timestamp = new_row.entry_timestamp
    row.details_json = new_row.details_json
    row.execution_history_json = new_row.execution_history_json
    row.realized_pnl_to_date_usd = new_row.realized_pnl_to_date_usd
    row.corporate_action_adjustment_needed = new_row.corporate_action_adjustment_needed


def _position_fill_from_record(fill: FillRecord) -> PositionFill:
    return PositionFill(
        fill_timestamp=fill.fill_timestamp,
        fill_price=fill.fill_price,
        fill_quantity=fill.fill_quantity,
        slippage=fill.slippage_usd if fill.slippage_usd is not None else signed_money(0),
        fees=fill.fees_usd,
        live_execution_estimate=fill.live_execution_estimate,
    )


# ---------------------------------------------------------------------------
# Bracket updates
# ---------------------------------------------------------------------------


async def _maybe_update_bracket(
    handle: InvocationHandle,
    position_before: PositionRecord,
    position_after: PositionRecord,
) -> BracketStatus | None:
    """Advance the bracket state machine based on the position transition.

    * PENDING → OPEN: bracket PENDING_ENTRY → ACTIVE, legs ACTIVE.
    * OPEN → CLOSED: bracket → DISSOLVED, legs CANCELLED.
    """
    bracket_id = position_after.bracket_id
    if bracket_id is None:
        return None
    transition = (position_before.status, position_after.status)
    if transition == (PositionStatus.PENDING, PositionStatus.OPEN):
        await _activate_bracket(handle, bracket_id)
        return BracketStatus.ACTIVE
    if transition == (PositionStatus.OPEN, PositionStatus.CLOSED):
        await _dissolve_bracket(handle, bracket_id)
        return BracketStatus.DISSOLVED
    return None


async def _activate_bracket(handle: InvocationHandle, bracket_id: str) -> None:
    """Flip a PENDING_ENTRY bracket to ACTIVE with all legs ACTIVE."""
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = f"position references bracket {bracket_id!r}, but bracket row is missing"
        raise StateInconsistencyError(msg)
    bracket_row.status = BracketStatus.ACTIVE.value
    for leg_row in await _read_bracket_legs(handle, bracket_id):
        leg_row.leg_status = BracketLegStatus.ACTIVE.value


async def _dissolve_bracket(handle: InvocationHandle, bracket_id: str) -> None:
    """Flip an ACTIVE bracket to DISSOLVED with all legs CANCELLED."""
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = f"position references bracket {bracket_id!r}, but bracket row is missing"
        raise StateInconsistencyError(msg)
    bracket_row.status = BracketStatus.DISSOLVED.value
    for leg_row in await _read_bracket_legs(handle, bracket_id):
        leg_row.leg_status = BracketLegStatus.CANCELLED.value


async def _read_bracket_legs(handle: InvocationHandle, bracket_id: str) -> list[BracketLegRow]:
    leg_stmt = (
        select(BracketLegRow)
        .where(BracketLegRow.bracket_id == bracket_id)
        .order_by(BracketLegRow.leg_index.asc())
    )
    return list((await handle.session.execute(leg_stmt)).scalars())


async def _bracket_leg_order_ids(handle: InvocationHandle, bracket_id: str) -> tuple[str, ...]:
    """Return the bracket's leg order_ids — empty for legs without an Alpaca order."""
    return tuple(
        row.order_id for row in await _read_bracket_legs(handle, bracket_id) if row.order_id
    )


# ---------------------------------------------------------------------------
# Cash + drawdown
# ---------------------------------------------------------------------------


async def _apply_cash_movement(
    handle: InvocationHandle, order: OrderRecord, fill: FillRecord
) -> Decimal:
    """Debit / credit the cash ledger by the consideration of this fill.

    Options consideration scales by the contract multiplier from the order's
    ``OptionsInstrumentSpec`` (typically 100). Equity consideration is
    ``fill_price * fill_quantity`` directly.

    Buy-side fills additionally release the capital Phase 2 reserved for this
    entry order — by the order's *reserved notional* attributable to the filled
    quantity (``_fill_reservation_release_usd``), NOT the fill consideration.
    Reserve and release share the ``reservation_price * quantity`` basis (ALP-741)
    so cumulative releases over partial fills sum to exactly what was reserved
    and ``reserved_capital_usd`` returns to its pre-reservation level once the
    entry is fully filled — the limit-vs-fill price gap never strands in the
    reservation pool. The decrement still floors at zero (defensive — rounding
    or mid-flight reprice could leave the running reservation just under the
    release).

    Returns the *signed cash delta* — positive for credits (sell-side
    proceeds), negative for debits (buy-side consideration). Decimal-typed
    so callers can thread the value into the activity-log without rounding;
    the cash-ledger column is ``Numeric`` after the ALP-462 migration.
    """
    consideration = _fill_consideration_usd(order, fill)
    fees = max(Decimal(str(fill.fees_usd)), Decimal(0))
    # Cash movement runs against the per-leg / single-leg order whose
    # direction is always populated. MLEG-envelope parents emit their
    # cash movement via per-leg child fills, so this path never sees one
    # (ALP-614).
    envelope_direction = order_direction(order)
    if envelope_direction is None:
        msg = (
            f"_apply_cash_movement reached MLEG envelope order_id={order.order_id!r}; "
            "expected per-leg or single-leg order"
        )
        raise ValueError(msg)
    is_buy = direction_to_side(envelope_direction) == "buy"
    delta = -(consideration + fees) if is_buy else (consideration - fees)
    cash_row = await _read_cash_row_or_raise(handle)
    cash_row.current_cash_usd = cash_row.current_cash_usd + delta
    # ALP-778: settled tracks current (no T+2 lag modelled in paper trading).
    cash_row.settled_cash_usd = cash_row.current_cash_usd
    if is_buy:
        release = _fill_reservation_release_usd(order, fill)
        # Floor at zero (defensive) and wrap in money() — the same non-negative
        # invariant _release_capital enforces on the Phase 2 side (ALP-741).
        cash_row.reserved_capital_usd = money(
            max(cash_row.reserved_capital_usd - release, Decimal(0))
        )
    cash_row.last_updated_at = datetime.now(UTC).isoformat()
    return delta


def _fill_consideration_usd(order: OrderRecord, fill: FillRecord) -> Decimal:
    """USD notional moved by the fill — multiplier-scaled for options.

    Decimal-typed; ``fill.fill_price`` / ``fill.fill_quantity`` are coerced
    through ``str`` so binary-float drift on legacy float-typed fields cannot
    enter the cash-ledger accumulator.
    """
    base = Decimal(str(fill.fill_price)) * Decimal(str(fill.fill_quantity))
    spec = order.instrument_spec
    if isinstance(spec, OptionsInstrumentSpec):
        return base * Decimal(str(spec.contract_multiplier))
    return base


def _fill_reservation_release_usd(order: OrderRecord, fill: FillRecord) -> Decimal:
    """Reserved capital released by a buy-side fill (ALP-741).

    The order's reservation price (``limit_price`` preferred, ``stop_trigger_price``
    fallback) times *this fill's* quantity — the same ``reservation_price *
    quantity`` basis Phase 2 reserves at OPEN / ADD
    (``_shared._order_reserved_notional``) and adjusts on reprice, scoped to the
    filled quantity so partial-fill releases sum to the order's full reservation.
    A market entry carries no price → ``Decimal(0)`` (it reserved nothing). NOT
    multiplier-scaled for options: the reservation basis is per-contract price,
    so the release must match it (the *consideration* keeps the multiplier — that
    is the real cash that moves, a separate ledger field).
    """
    pp = order.price_parameters
    px = pp.limit_price if pp.limit_price is not None else pp.stop_trigger_price
    if px is None:
        return Decimal(0)
    return Decimal(str(px)) * Decimal(str(fill.fill_quantity))


async def _stamp_drawdown_state(handle: InvocationHandle) -> None:
    """Touch the drawdown_state singleton's ``last_updated_at`` after each fill.

    The full drawdown recomputation (HWM + current drawdown vs. live equity)
    requires market-price context the snapshot assembler owns; for the
    persistence layer this story leaves the running fields untouched and
    simply stamps the row so observers see Phase 1 ran. The richer
    recomputation lands in the snapshot-assembler / breach-behavior wiring.
    """
    row = await handle.session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
    if row is None:
        msg = "drawdown_state singleton missing — Phase 1 cannot integrate fill"
        raise ValueError(msg)
    row.last_updated_at = datetime.now(UTC).isoformat()


async def _read_cash_row_or_raise(handle: InvocationHandle) -> CashLedgerRow:
    row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if row is None:
        msg = "cash_ledger singleton missing — Phase 1 cannot integrate fill"
        raise ValueError(msg)
    return row


# ---------------------------------------------------------------------------
# Activity-log emission
# ---------------------------------------------------------------------------


async def _emit_capital_release(
    handle: InvocationHandle, order: OrderRecord, fill: FillRecord
) -> None:
    """Buy-side fills release the capital reservation Phase 2 staked.

    The emitted amount is the order's reserved notional for the filled quantity
    (``_fill_reservation_release_usd``) — the same basis
    :func:`_apply_cash_movement` drains the reservation pool by, so the activity
    log and the ledger agree (ALP-741). A market entry reserved nothing, so a
    market-entry fill releases nothing and emits no entry (symmetric with OPEN /
    ADD, which emit no ``capital_reserved`` for a market entry).
    """
    amount = _fill_reservation_release_usd(order, fill)
    if amount == 0:
        return
    _emit(
        handle,
        event_type=EventType.CAPITAL_RELEASED,
        order_id=order.order_id,
        position_id=order.position_id,
        thesis_id=order.originating_thesis_id,
        timestamp=fill.fill_timestamp,
        detail=CapitalReleasedDetail(order_id=order.order_id, amount_usd=money(amount)),
    )


async def _emit_fill_activity_log_entries(
    handle: InvocationHandle,
    outcome: _FillIntegrationOutcome,
) -> None:
    """Emit one entry per state mutation produced by this fill."""
    fill = outcome.fill
    order = outcome.order
    position_before = outcome.position_before
    position_after = outcome.position_after
    bracket_status_change = outcome.bracket_status_change
    cash_delta_usd = outcome.cash_delta_usd
    direction_is_buy = outcome.direction_is_buy
    bracket_id = position_after.bracket_id
    pos_id = position_after.position_id
    thesis_id = position_after.thesis_id

    _emit(
        handle,
        event_type=EventType.ORDER_FILLED,
        order_id=order.order_id,
        position_id=pos_id,
        thesis_id=thesis_id,
        timestamp=fill.fill_timestamp,
        detail=OrderFilledDetail(
            fill_price=fill.fill_price,
            fill_quantity=fill.fill_quantity,
            slippage=fill.slippage_usd if fill.slippage_usd is not None else signed_money(0),
            fees=fill.fees_usd,
        ),
    )

    if (position_before.status, position_after.status) == (
        PositionStatus.PENDING,
        PositionStatus.OPEN,
    ):
        _emit(
            handle,
            event_type=EventType.POSITION_OPENED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=PositionOpenedDetail(
                ticker=_ticker_of(position_after),
                # position_direction() yields None for a strategy — it has no
                # position-level side; PositionOpenedDetail.direction is
                # str | None and round-trips the None faithfully (ALP-607).
                direction=(
                    pos_dir.value
                    if (pos_dir := position_direction(position_after)) is not None
                    else None
                ),
                fill_price=fill.fill_price,
                quantity=fill.fill_quantity,
                thesis_id=thesis_id,
                bracket_id=bracket_id,
                mechanism=PositionOpenMechanism.ORDER_FILL,
                parent_position_id=None,
            ),
        )

    if bracket_status_change == BracketStatus.ACTIVE and bracket_id is not None:
        _emit(
            handle,
            event_type=EventType.BRACKET_ACTIVATED,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=BracketActivatedDetail(
                bracket_id=bracket_id,
                protective_leg_order_ids=await _bracket_leg_order_ids(handle, bracket_id),
            ),
        )

    open_to_open = (position_before.status, position_after.status) == (
        PositionStatus.OPEN,
        PositionStatus.OPEN,
    )
    partial_close = open_to_open and _fill_reduced_position(
        position_after, order, direction_is_buy=direction_is_buy
    )
    if partial_close:
        partial_pnl = (position_after.realized_pnl_to_date_usd or 0.0) - (
            position_before.realized_pnl_to_date_usd or 0.0
        )
        _emit(
            handle,
            event_type=EventType.POSITION_REDUCED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=PositionReducedDetail(
                reduced_quantity=fill.fill_quantity,
                partial_realized_pnl_usd=signed_money(partial_pnl),
                close_rationale_classification="",
            ),
        )

    if (position_before.status, position_after.status) == (
        PositionStatus.OPEN,
        PositionStatus.CLOSED,
    ):
        _emit(
            handle,
            event_type=EventType.POSITION_CLOSED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=PositionClosedDetail(
                exit_method=PositionExitMethod.PM_DECISION,
                exit_price=money(fill.fill_price),
                realized_pnl_usd=signed_money(position_after.realized_pnl_to_date_usd or 0.0),
                thesis_resolution_category="",
            ),
        )

    if bracket_status_change == BracketStatus.DISSOLVED and bracket_id is not None:
        _emit(
            handle,
            event_type=EventType.BRACKET_DISSOLVED,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=BracketDissolvedDetail(
                cancelled_leg_order_ids=await _bracket_leg_order_ids(handle, bracket_id),
            ),
        )

    if outcome.strategy_incomplete_legs:
        _emit(
            handle,
            event_type=EventType.BRACKET_INCOMPLETE_WARNING,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=BracketIncompleteWarningDetail(
                missing_leg_types=outcome.strategy_incomplete_legs,
                expected_resolution=(
                    "strategist re-evaluates the partial mleg residual on its next "
                    "invocation per broker-adapter.md § Multi-leg fill events § Cancellation"
                ),
            ),
        )

    new_balance = await _current_cash_balance(handle)
    if direction_is_buy:
        _emit(
            handle,
            event_type=EventType.CASH_DEBITED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=CashDebitedDetail(
                amount_usd=money(abs(cash_delta_usd)),
                reason=CashDebitReason.ENTRY_FILL,
                new_balance_usd=signed_money(new_balance),
            ),
        )
    else:
        _emit(
            handle,
            event_type=EventType.CASH_CREDITED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=CashCreditedDetail(
                amount_usd=money(abs(cash_delta_usd)),
                reason=CashCreditReason.EXIT_FILL,
                new_balance_usd=signed_money(new_balance),
            ),
        )


async def _current_cash_balance(handle: InvocationHandle) -> float:
    # ALP-462 — the cash_ledger column is ``DecimalText`` (Decimal-backed); cast
    # to float at the activity-log boundary.
    return float((await _read_cash_row_or_raise(handle)).current_cash_usd)


def _emit(
    handle: InvocationHandle,
    *,
    event_type: EventType,
    order_id: str | None,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource = EventSource.FILL_PROCESSOR,
) -> None:
    """Construct + persist one ``ActivityLogEntry`` joined to the current handle."""
    entry = ActivityLogEntry(
        entry_id=f"{handle.invocation_id}-{event_type.value}-{uuid.uuid4().hex}",
        invocation_id=handle.invocation_id,
        timestamp=timestamp,
        event_type=event_type,
        event_group=EVENT_TYPE_TO_GROUP[event_type],
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=detail,
    )
    append_activity_log_entry(handle, entry)


def _ticker_of(position: PositionRecord) -> str:
    """Best-effort ticker extraction across instrument variants for log details."""
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    return getattr(details, "underlying_ticker", "")


# ---------------------------------------------------------------------------
# Corporate-action integration
# ---------------------------------------------------------------------------


async def _integrate_one_ca_activity(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    alpaca_position_lookup: AlpacaPositionLookup | None,
) -> None:
    """Thin shim: delegate to the ``corporate_actions`` package dispatcher.

    The ``corporate_actions`` package owns the full implementation; this shim
    preserves the existing call site in ``process_unprocessed_fills`` without
    change. ``alpaca_position_lookup`` is built from the same
    ``alpaca_positions`` snapshot that feeds the reconciliation step (see
    :class:`_SnapshotLookup`); it is ``None`` only when the caller supplied an
    empty ``alpaca_positions`` tuple.
    """
    await integrate_ca_activity(handle, activity, alpaca_position_lookup=alpaca_position_lookup)


# ---------------------------------------------------------------------------
# Fast-fill recovery integration (ALP-767)
# ---------------------------------------------------------------------------


def _build_recovery_invocation_row(
    *,
    invocation_id: str,
    process_lifetime_id: str,
    now: datetime,
) -> InvocationRow:
    """Minimal InvocationRow for a fill-recovery Phase-1 pass (ALP-767).

    Mirrors the borrow-accrual tick pattern: only the execution-scaffolding
    columns are populated; snapshot / composition columns get inert sentinels
    (``""`` or ``"{}"``) because this pass does not run an agent pipeline and
    has no resolved-config snapshot to reference.
    """
    iso = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        start_at=iso,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="continuous_monitor",
        trigger_reason="fill_stream_recovery - fast-fill convergence (ALP-767)",
        git_sha_at_invocation="",
        active_profile="",
        active_regime="",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="",
        resolved_config_snapshot_path="",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def integrate_recovered_fills(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    process_lifetime_id: str,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
) -> int:
    """Integrate all unprocessed fills immediately after fill recovery (ALP-767).

    Called by the fill-stream consumer's drain right after persisting a
    recovered fill so the PENDING→OPEN transition and ``_activate_bracket``
    run within seconds rather than waiting for the next scheduled pipeline
    run.

    Skips Reg T attribution (``regt_attribution_json`` stays NULL, the same
    as quarantined fills), CA activities, and reconciliation — those remain
    the scheduled pipeline's responsibility. The resulting invocation row is
    tagged ``trigger_source="continuous_monitor"`` (same vocabulary as the
    borrow-accrual tick and other monitor-originated invocations).

    SHORT equity ENTRY fills are skipped when ``borrow_cost_resolver`` is
    ``None``: ``_apply_entry_fill`` raises for those, the broad
    ``except Exception`` handler would permanently quarantine them, and
    QUARANTINED rows are invisible to the scheduled Phase-1's
    ``_read_unprocessed_fill_rows``. Skipping here leaves them UNPROCESSED
    so the scheduled pipeline (which always has a resolver) can integrate them.
    """
    now = datetime.now(UTC)
    invocation_id = mint_invocation_id(now)

    async with session_factory() as db:
        db.add(
            _build_recovery_invocation_row(
                invocation_id=invocation_id,
                process_lifetime_id=process_lifetime_id,
                now=now,
            )
        )
        await db.commit()

    fills_processed = 0
    async with session_factory() as session:
        handle = InvocationHandle(session=session, invocation_id=invocation_id)
        fill_rows = await _read_unprocessed_fill_rows(handle)
        fills = tuple(fill_row_to_record(row) for row in fill_rows)
        rows_by_fill_id = {row.fill_id: row for row in fill_rows}

        valid_fills, _ = _quarantine_invalid(handle, fills, rows_by_fill_id)

        for fill in valid_fills:
            if borrow_cost_resolver is None:
                _order = await _read_order(handle, fill.order_id)
                if _order.role == OrderRole.ENTRY:
                    _, _pos = await _read_position_for_order(handle, _order)
                    if _pos.direction == Direction.SHORT:
                        log.info(
                            "recovery skip: SHORT entry fill %s deferred to scheduled Phase-1"
                            " (no borrow_cost_resolver)",
                            fill.fill_id,
                        )
                        continue
            processed = await _integrate_or_quarantine_fill(
                handle,
                fill,
                rows_by_fill_id[fill.fill_id],
                market_inputs=None,
                regt_config=None,
                borrow_cost_resolver=borrow_cost_resolver,
            )
            if processed:
                fills_processed += 1

        await stamp_phase_completion(handle, column="phase1_completed_at")
        await session.commit()

    return fills_processed


__all__ = [
    "CorporateActionActivity",
    "Phase1Summary",
    "StateInconsistencyError",
    "integrate_recovered_fills",
    "process_unprocessed_fills",
]
