# SHORT-equity write path — design

The SHORT-equity write path covers OPEN through cover-to-close for equity
positions held with a borrow obligation. Borrow cost accrues daily against
the live position notional and resolves into realized P/L when the position
closes. The path mirrors the existing LONG-equity write path with directional
sign flips and adds a continuous-monitor daily-accrual tick for the borrow
obligation.

Pairs with [architecture.md § 4](architecture.md#4-continuous-monitor) for
monitor responsibilities, [state-persistence.md](state-persistence.md) for
the fill collection fill-integration semantics, and
[regt-margin-attribution.md](regt-margin-attribution.md) for per-fill margin
attribution. Borrow-cost resolution at validation time is the
[risk-guardrails layer's responsibility](../06-risk-guardrails/guardrail-evaluation.md);
this doc covers the execution-side persistence and the lifecycle that
attributes borrow cost into realized P/L.

---

## Why this exists

Fill collection today rejects SHORT-equity entry at two layers:

* The OMS command boundary (`OpenCommand._validate_equity_direction` at
  `src/alphamind/commands/command_models.py:488-506`) raises on every SHORT
  EQUITY `OpenCommand`.
* The fill collection dispatcher
  (`_apply_fill_to_equity_position` at
  `src/alphamind/execution/write_paths/fill_collection.py:611-626`) raises
  `NotImplementedError` on `(is_buy_side=False, status=PENDING)`.

Both guards were placeholders to keep an unfinished write path from leaking
silent bugs into production. The analyst, validation tool, pre-processor,
strategist, and PM already produce SHORT EQUITY proposals end-to-end —
attempt-4 of the 2026-05-27 bootstrap confirmed a CRWD conviction-3 SHORT
reached the PM with all five envelope criteria PASSing — but the PM cannot
construct the `OpenCommand` and the run ends with `commands_submitted: 0`.
Production scheduler, monitor, and command-center services are held in the
Stopped state pending this path.

The single design decision behind the rest of this doc:

**Borrow cost accrues into realized P/L.** A SHORT position carries a daily
borrow obligation against its live notional. The obligation accumulates on
a per-position field and resolves into the realized P/L delta at cover-to-close.
The guardrail layer's resolver-driven snapshot projection (which already
gates SHORT proposals on borrow headroom) continues unchanged; this doc adds
the persistence and attribution side.

---

## Contracts

After this work lands:

* A SHORT EQUITY `OpenCommand` constructs without raising. The OMS dispatches
  it to the broker, which submits a SELL_TO_OPEN entry order via the existing
  `submit_equity_open` path (already direction-aware at
  `src/alphamind/execution/broker_adapter/order_equity.py:245-247`).
* The fill collection integrator routes the SELL_TO_OPEN fill into a PENDING →
  OPEN transition on the SHORT position, stamps the entry-time borrow rate
  from the resolver, and initializes the accrued-borrow accumulator at zero.
* On every trading day's close, the continuous monitor advances every OPEN
  SHORT-equity position's accrued-borrow accumulator by one day's worth of
  borrow cost against the live notional, and emits a `BORROW_COST_ACCRUED`
  activity-log event per position.
* On cover-to-close (the closing fill that takes `share_count` to zero), the
  position's accumulated borrow cost subtracts from the realized P/L delta
  atomically with the close, the position transitions to CLOSED, and the
  thesis resolves.
* Reg T per-leg margin attribution stamps the 150%-MV initial margin on the
  SHORT entry's `FillRecord.regt_attribution_json` via the existing
  `compute_attribution` path. (Already supported — no new work.)
* The existing borrow-cost guardrail rule continues to gate SHORT proposals
  against portfolio-level borrow headroom via the snapshot-time
  resolver-driven projection at `risk_guardrails/library_snapshot.py:342-362`.
  (Already supported — no new work.)

---

## Data model

### `EquityPositionDetails`

The persistent dataclass at
`src/alphamind/portfolio_state/records/positions.py:146-155` carries the
short-only state. The contract:

```python
@dataclass(frozen=True, slots=True)
class EquityPositionDetails:
    ticker: Symbol
    share_count: float
    average_cost_basis_per_share: float

    # Short-only fields. All non-None for direction=SHORT; all None for direction=LONG.
    borrow_rate_pct: float | None = None
    accrued_borrow_cost_usd: float | None = None  # NEW
    locate_status: LocateStatus | None = None
    margin_held_usd: float | None = None

    instrument_type: InstrumentType = field(default=InstrumentType.EQUITY, init=False)
```

* `borrow_rate_pct` carries the **entry-time annualized borrow fee rate**
  (e.g., `15.0` for 15%/yr) — a snapshot stamped from
  `borrow_cost_resolver(ticker)` at the entry-fill helper. Informational; not
  consulted by live computations (those re-resolve via the resolver at every
  snapshot build).
* `accrued_borrow_cost_usd` is the **running USD accumulator** of borrow
  cost incurred since entry. Initialized to `0.0` at entry-fill. Incremented
  by the continuous monitor's daily tick. Flushed into realized P/L on
  cover-to-close.
* `locate_status` records the borrow-locate status (`LOCATED` or
  `AT_RISK_OF_RECALL`); stamped at entry-fill from the locate metadata that
  travels with the borrow-cost resolver row.
* `margin_held_usd` records the entry-time Reg T initial margin held against
  the position (`share_count × entry_price × 0.50` for SHORT equity); stamped
  at entry-fill. Informational; the live aggregate is the
  broker-reconciliation-fed `cash_ledger.margin_held_usd`.

Storage is JSON-on-row in `PositionRow.details_json`; adding
`accrued_borrow_cost_usd` requires no Alembic migration. The codec at
`src/alphamind/state/tables/positions_codec.py:120-173` round-trips the new
field through `_details_to_dict` and `_equity_from_dict`.

### `_check_equity_direction_fields` validator

The `PositionRecord` validator at
`src/alphamind/portfolio_state/records/positions.py:309-328` extends to
include `accrued_borrow_cost_usd` in the short-only set:

* `direction == Direction.SHORT` → all four short fields
  (`borrow_rate_pct`, `accrued_borrow_cost_usd`, `locate_status`,
  `margin_held_usd`) **must** be non-`None`.
* `direction == Direction.LONG` → all four short fields **must** be `None`.

### `BorrowCostAccruedDetail` (new activity-log event detail)

A new event type `BORROW_COST_ACCRUED` (group `OPERATIONS`) lands in
`src/alphamind/portfolio_state/events/activity_log.py`. Per the *one schema
per producer* discipline, the detail is its own dataclass:

```python
@dataclass(frozen=True, slots=True)
class BorrowCostAccruedDetail:
    accrued_amount_usd: Money         # today's increment
    cumulative_accrued_usd: Money     # running total after this tick
    annual_fee_pct_used: float        # the live resolver rate consumed
    notional_usd_used: Money          # the live notional the accrual was computed against
    accrual_date: date                # the trading day this tick covers
```

`EVENT_TYPE_TO_GROUP` gains the corresponding mapping; the event-emitter
sequence in fill collection's `_emit` helper is reused via the monitor's own
`append_activity_log_entry` call.

### Cash ledger

No change. Borrow fees physically charge to the cash account via the
broker's monthly statement; the broker-reconciliation step at
`src/alphamind/execution/corporate_actions/reconciliation.py` will reflect
the actual cash debit when Alpaca posts it. The accrual lifecycle in this
doc is a **P/L attribution mechanism**, not a cash-movement mechanism — it
makes the position's running P/L reflect the borrow drag without
double-counting against actual cash debits.

---

## Lifecycle

### Entry-fill (PENDING → OPEN)

Triggered by the SELL_TO_OPEN fill against a PENDING SHORT-equity position.

`_apply_fill_to_equity_position` dispatches via direction-aware routing
(see § Fill collection dispatcher below) to `_apply_entry_fill`. The entry helper
extends to stamp the four short-only fields when the position direction is
SHORT:

```python
def _apply_entry_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
    *,
    borrow_cost_resolver: Callable[[str], float | None] | None,
) -> PositionRecord:
    new_details = dataclasses.replace(
        details,
        share_count=fill.fill_quantity,
        average_cost_basis_per_share=float(fill.fill_price),
    )
    if position.direction == Direction.SHORT:
        annual_fee_pct = borrow_cost_resolver(details.ticker)  # non-None by contract
        margin_held_usd = (
            fill.fill_quantity * float(fill.fill_price) * 0.50  # Reg T 150% MV - notional
        )
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
```

The resolver is threaded through `process_unprocessed_fills`. A SHORT entry
fill arriving without a resolvable rate is an upstream contract violation —
the analyst's validation tool returns `UNAVAILABLE` for tickers the resolver
cannot price — so the entry-fill helper raises `ValueError` rather than
attempting to stub. The PENDING position the OMS pre-created carries
direction=SHORT; resolver coverage was confirmed at validation time; the
intervening time window between validation and fill is bounded by the
broker round-trip and the next invocation's snapshot rebuild.

`locate_status` is stamped at `LOCATED` by construction; the
`AT_RISK_OF_RECALL` transition lands separately (broker-driven; outside
this doc's scope).

### ADD on a SHORT position

Same-side SELL fill against an OPEN SHORT position. Routes through
`_apply_add_fill`, which weighted-averages `share_count` and
`average_cost_basis_per_share`. `borrow_rate_pct` is **not** re-stamped on
ADD — the field is the entry-time snapshot; live rate is consumed from the
resolver at every snapshot build and at every daily accrual tick.
`accrued_borrow_cost_usd` is **not** reset on ADD — the accumulator
continues against the new combined notional from the next accrual tick
onward. The pre-ADD borrow accrual stays attributed to the position's
combined history.

### Daily accrual tick (continuous monitor)

A new continuous-monitor responsibility (§ 4f below, paired with
architecture.md § 4). Fires once per trading day at session close.

For every OPEN SHORT-equity position:

1. Read the position's current `share_count` and `ticker`.
2. Resolve the live annualized fee from `borrow_cost_resolver(ticker)`.
3. Compute today's notional as `share_count × close_price` where
   `close_price` is the session's closing print from the latest
   `equity_bars` row for the ticker.
4. Compute today's accrual:
   `today_cost_usd = abs(notional) × annual_fee_pct / 100 / 252`.
5. In an `InvocationContext`-equivalent transaction:
   * Update `position.details.accrued_borrow_cost_usd += today_cost_usd`.
   * Emit a `BORROW_COST_ACCRUED` activity-log entry with the
     `BorrowCostAccruedDetail` payload.

Trading-day calendar follows the existing market-hours convention
([architecture.md § 4](architecture.md#4-continuous-monitor)). The accrual
tick fires only on trading days (no weekend / holiday accrual — borrow fees
typically charge over weekends but at a per-day rate that the
single-business-day-equivalent annualization built into `_TRADING_DAYS_PER_YEAR`
already approximates). A position opened mid-day still accrues a full day
at the next close; this is the broker's convention.

Position closures within the same trading day (intraday cover) skip the
accrual tick for that day — the position is no longer OPEN at session
close. This is a minor underestimation against actual broker billing
(borrow fees sometimes charge for the day if the position was held at any
point), priced into the conservative direction.

A resolver that returns `None` for a ticker mid-lifecycle (the
`borrow_cost_daily` row went missing post-entry) raises `ValueError` from
the monitor's accrual tick — the position cannot be silently abandoned to
zero accrual. Recovery is a data-source repair, not a runtime fallback.

### Cover-to-close (OPEN → CLOSED)

Triggered by the BUY (cover) fill that takes `share_count` to zero on an
OPEN SHORT position.

`_apply_fill_to_equity_position` routes via the direction-aware dispatcher
to `_apply_exit_fill`. The exit helper already direction-signs the realized
P/L delta (`fill_collection.py:765-808`); it extends to flush the accumulated
borrow cost into the realized P/L on the closing-side branch:

```python
def _apply_exit_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    qty_after = details.share_count - fill.fill_quantity
    ...
    pnl_per_share = fp - avg_cost
    direction = position_direction(position)
    direction_sign = Decimal(-1) if direction == Direction.SHORT else Decimal(1)
    realized_delta = float(pnl_per_share * fq * direction_sign)

    closed = abs(qty_after) < _QTY_EPSILON
    if closed and direction == Direction.SHORT and details.accrued_borrow_cost_usd:
        # Flush accumulated borrow cost into realized P/L at cover-to-close.
        realized_delta -= details.accrued_borrow_cost_usd

    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta
    ...
```

Partial covers do **not** flush the accumulator; the position remains OPEN
and the accumulator continues to grow against the reduced notional from
the next tick onward. Only the cover that takes `share_count` to zero
flushes.

The flushed accumulator does not zero out post-close — the position is
CLOSED and immutable. Audit-trail tools that read the closed position's
`accrued_borrow_cost_usd` see the lifetime total. Equally,
`realized_pnl_to_date_usd` now includes the lifetime borrow drag.

### Activity log emission

The `BORROW_COST_ACCRUED` event lands one per position per tick. The
existing fill-side activity-log emission contract for cover-to-close
(`PositionClosedDetail.realized_pnl_usd`) automatically reflects the
flushed accumulator since `realized_pnl_to_date_usd` is post-flush at the
point `_emit_fill_activity_log_entries` reads it
(`fill_collection.py:1670-1687`). No new emission point is required at close.

---

## Architecture

### Fill collection dispatcher

`_apply_fill_to_equity_position` mirrors the options dispatcher's
direction-aware routing via `_is_opening_fill(direction, is_buy_side)`
(already defined at `fill_collection.py:662-664`):

```python
def _apply_fill_to_equity_position(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
    *,
    is_buy_side: bool,
    borrow_cost_resolver: Callable[[str], float | None] | None,
) -> PositionRecord:
    direction = position_direction(position)
    assert direction is not None  # equity positions always have direction
    if position.status == PositionStatus.PENDING:
        if _is_opening_fill(direction, is_buy_side):
            return _apply_entry_fill(
                position, details, fill,
                borrow_cost_resolver=borrow_cost_resolver,
            )
        msg = (
            f"Fill collection received closing fill on PENDING position {position.position_id!r}; "
            "a position cannot close before it opens"
        )
        raise ValueError(msg)
    if position.status == PositionStatus.OPEN:
        if _is_opening_fill(direction, is_buy_side):
            return _apply_add_fill(position, details, fill)
        return _apply_exit_fill(position, details, fill)
    msg = f"Fill collection cannot integrate fill against position status {position.status!r}"
    raise ValueError(msg)
```

The `is_buy_side` flag retains its meaning at the call site
(`_integrate_one_fill` at `fill_collection.py:441-443`) — the dispatch interpretation
shifts to "opening" via `_is_opening_fill`. The four cases:

| Status   | Direction | Buy side | Routing             |
|----------|-----------|----------|---------------------|
| PENDING  | LONG      | True     | `_apply_entry_fill` |
| PENDING  | LONG      | False    | `ValueError`        |
| PENDING  | SHORT     | True     | `ValueError`        |
| PENDING  | SHORT     | False    | `_apply_entry_fill` |
| OPEN     | LONG      | True     | `_apply_add_fill`   |
| OPEN     | LONG      | False    | `_apply_exit_fill`  |
| OPEN     | SHORT     | True     | `_apply_exit_fill`  |
| OPEN     | SHORT     | False    | `_apply_add_fill`   |

The resolver is threaded from `process_unprocessed_fills` through
`_integrate_one_fill` to the dispatcher to `_apply_entry_fill`. The
orchestrator already builds the resolver per invocation at
`src/alphamind/scheduler/orchestrator.py:629-630`; an additional kwarg
on `process_unprocessed_fills` carries it into fill collection.

### Continuous monitor — § 4f daily borrow accrual

A new continuous-monitor responsibility paired into
[architecture.md § 4](architecture.md#4-continuous-monitor) and
[continuous-monitor-runtime.md](continuous-monitor-runtime.md).

**Trigger.** Once per trading day at session close. Configurable cadence
in `config/continuous_monitor.yaml` via a new knob:

```yaml
borrow_accrual_tick_local_time: "16:00"  # ET, post-close
```

The supervisor's asyncio loop schedules a daily timer against the
US/Eastern wall clock; off-hours and weekends, the timer sleeps to the
next trading-day close. Holidays follow the same market-hours calendar
the rest of the monitor consumes.

**Action per tick.** Iterate every OPEN SHORT-equity position via the
existing `_read_all_positions` query shape (`fill_collection.py:352-370`,
filtered to `direction=SHORT` and `instrument_type=EQUITY`). For each:

1. Read the live `share_count` and the latest closing print from
   `equity_bars`.
2. Resolve the annualized fee via `borrow_cost_resolver(ticker)` —
   the monitor builds its own resolver instance per tick from the
   same `build_borrow_cost_resolver` helper the orchestrator uses
   (`src/alphamind/risk_guardrails/borrow_cost.py:63-99`).
3. Compute `today_cost_usd = abs(share_count × close_price) × annual_fee_pct / 100 / 252`.
4. Update `position.details.accrued_borrow_cost_usd += today_cost_usd`
   via the position codec's record_to_row round-trip.
5. Emit a `BORROW_COST_ACCRUED` activity-log entry.

All five steps run inside one `InvocationContext`-equivalent transaction
per tick so the cross-position update and the activity-log emissions
commit atomically. The monitor opens this transaction via the same
`InvocationHandle` plumbing fill collection uses; an invocation row is inserted
with `invocation_kind=ENGINE_BORROW_ACCRUAL` (new enum variant) so the
audit trail keeps tick-level groupability.

**Failure semantics.** A resolver miss raises and propagates to the
supervisor; NSSM restarts the process on its policy. A missing closing
print for a ticker (e.g., the `equity_bars` collector backlog) raises and
propagates the same way — silently skipping a tick would underaccrue P/L
without a trace. Operators repair the data source and the next tick
catches up (with a brief comment in the docstring noting that the
missing tick's accrual remains lost; this is the price of fail-fast
integrity in v1).

**Off-hours.** The accrual tick fires only on trading days. Weekend and
holiday borrow accrual is approximated into the 252-day annualization
denominator (broker convention is consistent on this point).

**Off-tick reads.** The persisted `accrued_borrow_cost_usd` is a discrete
end-of-day quantity. Mid-session snapshot reads (analyst, strategist, PM)
see the prior-day-close value; the resolver-driven snapshot projection
covers intraday borrow projection independently for guardrail purposes.

### Borrow resolver — call sites

The `borrow_cost_resolver` callable lands in four production call sites:

| Caller                                  | Path                                                                              | Existing? |
|-----------------------------------------|-----------------------------------------------------------------------------------|-----------|
| Analyst validation tool                 | `risk_guardrails/state_delivery/validation_tool.py:832-866`                       | yes       |
| Library snapshot assembler              | `risk_guardrails/library_snapshot.py:342-362`                                     | yes       |
| Proposal pre-processor (translator)     | `decision/proposal_pre_processor/translator.py:282-348`                           | yes       |
| Fill collection entry-fill helper       | `execution/write_paths/fill_collection.py::_apply_entry_fill`                     | **new**   |
| Continuous monitor § 4f accrual tick    | new module under `execution/continuous_monitor/borrow_accrual.py` (or similar)    | **new**   |

The two new call sites consume the same `Callable[[str], float | None]`
contract; both build their instance via `build_borrow_cost_resolver` from
`risk_guardrails/borrow_cost.py`.

### What stays the same

* `EquityInstrument` (`commands/command_models.py:131-138`) — already
  carries `direction: Direction` (the union literal type
  `Literal["long", "short"]`).
* `EquityPositionDetails.borrow_rate_pct` / `locate_status` /
  `margin_held_usd` — schema unchanged.
* `_apply_add_fill` (`fill_collection.py:739-762`) — direction-agnostic
  arithmetic.
* `_apply_exit_fill` (`fill_collection.py:765-808`) — direction-signed realized
  P/L; gains only the borrow-flush branch.
* Command execution OPEN write path (`execution/write_paths/command_execution/open.py:555-564`) —
  already builds a valid SHORT-equity PENDING skeleton.
* Broker submit (`broker_adapter/order_equity.py:89-132`,
  `_entry_side` at `:245-247`) — already maps `short → SELL`.
* Reg T per-leg attribution (`execution/regt_margin_attribution/regt_margin.py:38-48`) —
  already uses the 150%-MV formula for SHORT equity.
* `risk_guardrails/library_snapshot.py:342-362` — already resolves
  daily borrow cost at snapshot-build time.
* `risk_guardrails/state_delivery/validation_tool.py:700-720` — already
  short-circuits with `UNAVAILABLE` on `MISSING_BORROW_COST`.
* `risk_guardrails/state_delivery/validation_tool.py:643-644` — single
  `short_selling_enabled` profile flag covers both SHORT equity and SHORT
  options; no split.

---

## Validation and tests

### Removed

* `OpenCommand._validate_equity_direction` and its comment block at
  `commands/command_models.py:487-507`.
* `_apply_fill_to_equity_position`'s `not is_buy_side and PENDING`
  branch at `fill_collection.py:623-625` (the `NotImplementedError`).

### Existing tests that flip

Three tests assert the rejected state and flip direction:

1. `tests/execution/oms/test_command_models.py::TestOpenCommandStrategyTargetType::test_rejects_short_equity_instrument`
   (lines 437-458) flips to `test_allows_short_equity_instrument`,
   mirroring the existing `test_allows_short_option_instrument`.
2. `tests/execution/state_persistence/test_fill_collection_write_path.py::test_short_entry_fill_raises_explicit_not_implemented`
   (lines 1450-1491) flips to assert PENDING → OPEN with the four
   short-only fields stamped from a stub resolver.
3. `tests/execution/state_persistence/test_fill_collection_write_path.py::test_atomicity_exception_rolls_back_fills_and_log`
   (lines 1091-1145) re-grounds around a different deliberate fault.
   The replacement: seed a fill referencing a missing `order_id` to hit
   the `_read_order` `ValueError` path. Rollback assertions stay the
   same.

### New positive coverage

* OMS command boundary: `OpenCommand` constructs cleanly for SHORT EQUITY.
* Fill collection dispatcher: each of the eight (status × direction × buy_side)
  cases routes correctly, including the two defensive `ValueError`
  cases (PENDING-LONG+SELL, PENDING-SHORT+BUY).
* Fill collection entry-fill (SHORT): the four short-only fields stamp from the
  resolver; the codec round-trips through `details_json` and the
  validator accepts.
* Fill collection entry-fill (SHORT, missing resolver row): raises `ValueError`
  with a message naming the upstream contract violation.
* Fill collection ADD on a SHORT position: weighted-avg cost basis is correct;
  `borrow_rate_pct` / `accrued_borrow_cost_usd` unchanged on the post-ADD
  position.
* Fill collection cover-to-close (SHORT): realized P/L sign is correct (entry $100,
  exit $90 → +$10/share net of borrow flush); `accrued_borrow_cost_usd`
  subtracts from the realized delta; position transitions to CLOSED.
* Fill collection partial cover (SHORT): no borrow flush; position stays OPEN;
  `accrued_borrow_cost_usd` unchanged.
* Reg T attribution: SHORT entry stamps 150%-MV initial margin via
  `compute_attribution`; the existing tests under
  `tests/execution/state_persistence/test_fill_collection_regt_attribution.py`
  add a SHORT-equity case.
* Codec round-trip: `EquityPositionDetails` with SHORT short-only fields
  including `accrued_borrow_cost_usd` round-trips through
  `record_to_row` and `row_to_record`.
* Validator: `_check_equity_direction_fields` rejects LONG positions with
  any short-only field set and SHORT positions with any short-only field
  unset, including the new `accrued_borrow_cost_usd`.
* Continuous monitor § 4f: a single accrual tick against a synthetic
  OPEN SHORT position updates `accrued_borrow_cost_usd` by the expected
  daily amount, emits exactly one `BORROW_COST_ACCRUED` activity-log
  entry, and commits atomically.
* Continuous monitor § 4f: a resolver miss on an OPEN SHORT position
  raises and rolls the tick's transaction back; the
  `accrued_borrow_cost_usd` on every position in scope remains at its
  pre-tick value.
* Continuous monitor § 4f: a position that closes intraday is excluded
  from the next end-of-day tick (no accrual emitted, no field updated).

---

## Implementation plan — feature work-tree

Two sub-stories under one Linear parent issue. The dependency direction:
Story 2 builds on Story 1's `accrued_borrow_cost_usd` field; Story 1
unblocks production immediately, with daily borrow drag landing in
realized P/L only after Story 2 lands and the first tick fires.

### Parent issue

**Title**: SHORT-equity write path end-to-end (option-A with full borrow
accrual)
**Project**: Execution layer
**Priority**: P1
**Reading**: this doc;
[architecture.md § 4](architecture.md#4-continuous-monitor);
[state-persistence.md](state-persistence.md);
[regt-margin-attribution.md](regt-margin-attribution.md);
[continuous-monitor-runtime.md](continuous-monitor-runtime.md).

### Story 01 — Fill collection SHORT-equity write path

Unblocks production. After this story lands, SHORT EQUITY proposals
dispatch end-to-end with correctly-signed realized P/L on cover-to-close.
Borrow accrual is wired structurally but the accumulator stays at zero
until Story 02 lands its daily tick.

**Scope.**

* Delete `_validate_equity_direction` from `commands/command_models.py:487-507`.
* Add `accrued_borrow_cost_usd: float | None = None` to
  `EquityPositionDetails` at `portfolio_state/records/positions.py:146-155`.
* Extend `_check_equity_direction_fields` at
  `portfolio_state/records/positions.py:309-328` to include
  `accrued_borrow_cost_usd` in the short-only set.
* Extend the positions codec at
  `state/tables/positions_codec.py:120-173` to round-trip the new field
  (`_details_to_dict` + `_equity_from_dict`).
* Replace `_apply_fill_to_equity_position` at
  `execution/write_paths/fill_collection.py:611-626` with the direction-aware
  dispatcher above, threading `borrow_cost_resolver` to
  `_apply_entry_fill`.
* Extend `_apply_entry_fill` at
  `execution/write_paths/fill_collection.py:712-736` to stamp the four short-only
  fields when `direction == Direction.SHORT`, sourced from the
  resolver and the fill itself (margin from Reg T arithmetic).
* Extend `_apply_exit_fill` at
  `execution/write_paths/fill_collection.py:765-808` with the borrow-flush
  branch on the closing fill that takes `share_count` to zero. Reword
  the helper's docstring from "Sell-side fill" to "Exit fill" and make
  the direction-sign source explicit.
* Thread `borrow_cost_resolver` through `process_unprocessed_fills` at
  `execution/write_paths/fill_collection.py:207-271` (new kwarg) and through
  `_integrate_one_fill` at `:416-487` (new kwarg) to the dispatcher.
* Thread the resolver into the call from the orchestrator
  (`scheduler/orchestrator.py` — the call site that wires fill collection into
  the post-decision step); the resolver is already built earlier in the
  orchestrator at `:629-630`.
* Update command execution open at
  `execution/write_paths/command_execution/open.py:555-564` so the PENDING
  skeleton's `accrued_borrow_cost_usd` is `0.0` (not None) for SHORT,
  to satisfy the extended validator at PENDING construction time.
* Flip three existing tests (per § Validation and tests above).
* Add positive fill collection SHORT entry, ADD, partial-cover, and
  cover-to-close coverage.
* Add codec round-trip coverage for the new field.
* Add Reg T attribution coverage for the SHORT-entry case.

**Acceptance.** Scoped pytest green on the touched files, full local lint
chain green, CI green. A manual invocation against attempt-4's CRWD
SHORT thesis produces `commands_submitted >= 1`.

### Story 02 — Continuous monitor § 4f daily borrow accrual

Lands the daily accrual lifecycle. After this story lands, borrow drag
attributes into `realized_pnl_to_date_usd` on cover-to-close via the
accumulator that Story 01 ships.

**Scope.**

* New `BORROW_COST_ACCRUED` event type and `BorrowCostAccruedDetail`
  dataclass in `portfolio_state/events/activity_log.py`. Wire into
  `EVENT_TYPE_TO_GROUP`.
* New `ENGINE_BORROW_ACCRUAL` invocation-kind variant (existing
  enum location TBD by the implementer; the analogue for engine-triggered
  invocations already exists).
* New module under
  `src/alphamind/execution/continuous_monitor/borrow_accrual.py` (path
  to be finalized against the existing `continuous_monitor/` layout)
  implementing the tick: read OPEN SHORT positions, resolve rates,
  compute today's cost, update accumulators, emit activity-log entries —
  all inside one transactional `InvocationHandle`.
* Wire the tick into the asyncio supervisor in
  `continuous_monitor/runtime.py` (or the per-existing-runtime path)
  with a daily timer at the configured local time.
* New config knob `borrow_accrual_tick_local_time` in
  `config/continuous_monitor.yaml`, parsed into `ContinuousMonitorConfig`.
* Update [architecture.md § 4](architecture.md#4-continuous-monitor) with
  the new responsibility 4f. Update
  [continuous-monitor-runtime.md](continuous-monitor-runtime.md) with the
  new config knob and the tick's failure-mode semantics.
* Tests per § Validation and tests above: happy-path tick, resolver-miss
  rollback, intraday-close exclusion.

**Acceptance.** Scoped pytest green on the new module and the monitor's
existing test files, full local lint chain green, CI green. A staged
end-to-end run with a seeded OPEN SHORT position confirms the tick fires
once per close, the accumulator advances by the expected amount, the
activity-log entry lands, and the next cover-to-close subtracts the
accumulator from realized P/L.

---

## Verification post-landing

After Story 01 lands and the operator restarts `alphamind-scheduler`,
`alphamind-monitor`, and `AlphaMindCommandCenter`:

* The first pre-open cycle that surfaces a SHORT EQUITY thesis (the CRWD
  and SCHW shapes from attempt 4 are the proximate candidates) dispatches
  the `OpenCommand` to Alpaca; the next invocation's fill collection integrates
  the SELL_TO_OPEN fill into an OPEN SHORT position with the four
  short-only fields stamped.
* `commands_submitted` on the PM result record advances above zero on
  the cycles that produce SHORT proposals.

After Story 02 lands and the monitor restarts:

* The first post-close tick advances every OPEN SHORT position's
  `accrued_borrow_cost_usd` by one day's worth of borrow cost and emits
  one `BORROW_COST_ACCRUED` activity-log entry per position.
* The activity-log entries surface in the command-center's audit pane via
  the existing event-feed projection.
* The next cover-to-close on each SHORT position reflects the accumulated
  borrow drag in `realized_pnl_to_date_usd`.
