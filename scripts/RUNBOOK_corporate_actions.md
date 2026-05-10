# Corporate-Actions Verification Runbook

Operator workflow for the ALP-416 verification artifact that proves the
corporate-actions integration work tree (ALP-124) is internally consistent:
all nine `CorporateActionType` handlers mutate state per
`docs/design/05-execution-layer/corporate-actions.md`'s per-action-type
matrix, write one `corporate_action_integration_ledger` row per
`alpaca_activity_id`, and the post-Phase-1 reconciliation step emits exactly
the alerts the verify script's synthetic divergence variant warrants.

Run after any change to `src/alphamind/execution/corporate_actions/`,
`src/alphamind/execution/state_persistence/write_paths/phase1.py`, or any
schema migration that touches the corporate-action integration ledger or the
position / cash-ledger codecs.

## Purpose

`alphamind.scripts.verify_corporate_actions` is a pure in-process integration
check against a freshly-migrated SQLite DB. No SDK calls; no live broker
contact (the script supplies synthetic `PositionSnapshot` /
`TradeAccountSnapshot` records to stand in for Alpaca). Sub-second runtime.

It exercises a single Phase 1 pass through `process_unprocessed_fills` that
drains nine synthetic `CorporateActionActivity` records — one per
`CorporateActionType` member — and asserts:

- **Quantity** matches the design matrix per type (× ratio for SPLIT, ÷ ratio
  for REVERSE_SPLIT, × (1+rate) for STOCK_DIVIDEND, unchanged for cash
  dividends and SYMBOL_CHANGE, zeroed for CASH_MERGER, Alpaca-projected for
  STOCK_MERGER, parent-unchanged-plus-child-created for SPIN_OFF).
- **Cost basis** matches the design matrix (inverse scaling for the
  quantity-mutating types; unchanged for cash dividends and SYMBOL_CHANGE).
- **Ticker** matches (`new_ticker` for SYMBOL_CHANGE and STOCK_MERGER;
  parent unchanged + new child ticker for SPIN_OFF; unchanged everywhere
  else).
- **Status** matches (OPEN for all except CASH_MERGER which transitions to
  CLOSED).
- **Cash impact** matches via the per-CA cash credits/debits expected by the
  design matrix (REVERSE_SPLIT residual; CASH_DIVIDEND_LONG credit;
  CASH_DIVIDEND_SHORT debit; CASH_MERGER proceeds; STOCK_MERGER optional
  partial credit).
- **`corporate_action_adjustment_needed`** is set on every position that
  remains OPEN post-CA.
- **CA integration ledger** has exactly one row per `alpaca_activity_id`
  with `processing_status='processed'`.

A tenth check covers the post-merge reconciliation step: one of the supplied
`PositionSnapshot` records is deliberately offset by one share against the
expected post-CA local state, so the reconciler emits exactly one
`RECONCILIATION_ALERT` activity-log entry.

## Prerequisites

1. **`uv sync` completed** — `uv run` is the entry point.
2. The work tree's integration branch (`jackson/alp-124-corporate-actions`)
   must be reachable from HEAD (all six predecessor stories landed: ALP-409
   through ALP-415).
3. **No live Alpaca calls** — the script supplies synthetic
   `PositionSnapshot` and `TradeAccountSnapshot` records via the same
   tuples Phase 1's reconciliation step consumes; live broker connectivity
   is **not** exercised here. Live integration verification belongs with
   ALP-123 (continuous monitor).
4. A freshly-migrated SQLite DB. The script will create one in a
   `tempfile.mkdtemp` directory if `--db-path` is omitted. For a stable
   on-disk DB, produce one with:

   ```python
   from pathlib import Path
   import alphamind.execution.state_persistence.tables  # noqa: F401
   from alphamind.persistence.models import Base
   from alphamind.persistence.session import make_engine

   db_path = Path("/tmp/alphamind-verify-corporate-actions.db")
   engine = make_engine(str(db_path))
   Base.metadata.create_all(engine)
   engine.dispose()
   ```

## Invocation

```bash
uv run python -m alphamind.scripts.verify_corporate_actions [--db-path PATH]
```

CLI flags:

- `--db-path PATH` — Path to a freshly-migrated SQLite DB. When omitted,
  the script creates a temp directory and migrates a fresh DB to head.

Exit code: `0` on full pass, `1` on any failure.

## Expected output

A clean run prints (exit code 0):

```
======================================================================
AlphaMind Corporate-Actions Verification
======================================================================
  SPLIT                     PASS
  REVERSE_SPLIT             PASS
  STOCK_DIVIDEND            PASS
  CASH_DIVIDEND_LONG        PASS
  CASH_DIVIDEND_SHORT       PASS
  CASH_MERGER               PASS
  STOCK_MERGER              PASS
  SPIN_OFF                  PASS
  SYMBOL_CHANGE             PASS
----------------------------------------------------------------------
  Reconciliation             PASS (alerts emitted=1, expected=1)
======================================================================
ALL ACTION TYPES PASS
======================================================================
```

On any failure, the script exits 1, the failed row prints `FAIL` followed
by an indented diagnostic line (e.g., `share_count=42.0, expected 40.0`),
and the trailer reads `FAIL: N check(s) failed: <labels>`.

## Failure-mode triage

| Action row | Typical failure | Likely root cause | Likely fix |
|---|---|---|---|
| any | `missing ledger row for activity X` | Handler returned without calling `mark_ca_activity_processed`. | Re-read the handler's tail — every handler must end with the `mark_ca_activity_processed` call. The shared `finalize_ca_handler` wraps this for handlers that use the helper. |
| SPLIT / REVERSE_SPLIT / STOCK_DIVIDEND | `share_count=...` or `basis=...` mismatch | The equity-mutation path regressed (wrong factor, wrong direction). | Re-read `handlers/splits.py` (forward) or `handlers/reverse_splits.py` / `handlers/stock_dividends.py` via `apply_options_position_mutation` in `_handler_base.py`; the `equity_quantity_factor` / `equity_basis_factor` arguments are the multiplicative knobs. |
| CASH_DIVIDEND_LONG / CASH_DIVIDEND_SHORT | `basis changed (should be unchanged)` | Cash-dividend handler accidentally mutated `share_count` / `average_cost_basis_per_share`. | Cash dividends only adjust the cash ledger and flip `corporate_action_adjustment_needed`. Re-read `handlers/cash_dividends.py::_apply_cash_dividend`. |
| CASH_MERGER | `share_count=..., expected 0` or `status=..., expected CLOSED` | The cash-merger handler is not zeroing quantity or transitioning status. | Re-read `handlers/mergers.py::handle_cash_merger` — it must `model_copy(update={"share_count": 0.0})` and set `status=PositionStatus.CLOSED`. |
| CASH_MERGER | `realized_pnl=...` mismatch | The realized P/L formula drifted (should be `proceeds - pre_qty * pre_basis` for equity). | Re-read `_close_for_cash_merger` in `handlers/mergers.py`. |
| STOCK_MERGER | `ticker=...` or `share_count=...` or `basis=...` mismatch | The handler is not reading the post-merger snapshot from `AlpacaPositionLookup`. | Re-read `handlers/mergers.py::handle_stock_merger`; confirm `lookup.get_position(activity.new_ticker)` returns the synthetic snapshot the verify script seeded. |
| SPIN_OFF | `expected 1 child, got 0` | The handler is not inserting the child `PositionRecord` row. | Re-read `handlers/spin_offs.py::handle_spin_off`; confirm `session.add(position_record_to_row(child))` runs. |
| SPIN_OFF | `child origin=...` mismatch or `_check_spinoff_invariant` raised | The (origin, parent_position_id, ca_adjustment_needed) triplet on the child is malformed. | Re-read `handlers/spin_offs.py::_build_spin_off_child`. The triplet is validated by `PositionRecord._check_spinoff_invariant` — all three fields must be coherent (origin set, parent set, flag True). |
| SYMBOL_CHANGE | `underlying_ticker=...` mismatch | The ticker-only-mutation helper is not renaming the underlying. | Re-read `_handler_base.py::apply_ticker_only_mutation`; the options branch updates `underlying_ticker`. |
| any | unexpected `RECONCILIATION_ALERT` (alerts emitted > 1) | The handler's post-CA local state diverged from the synthetic Alpaca snapshot the verify script supplied. | Compare local state for the diverging position against `_alpaca_positions_aligned()` in the verify script. The CASH_DIVIDEND_LONG position is the script's intentional divergence anchor — all other tickers should match Alpaca within the reconciler's tolerance (1e-9 for qty, 0.01 USD for cash). |
| Reconciliation | `alerts emitted=0, expected=1` | The deliberate-divergence row reconciled cleanly — either the off-by-one offset in `_alpaca_positions_aligned()` for `_TICKER_CD_LONG` was reverted, or the equity-reconciliation comparator regressed. | Re-read `corporate_actions/reconciliation.py::_reconcile_equity`; confirm it emits when `abs(local - alpaca) > _QTY_EPSILON`. |

## Operational caveats

**The reconciler intentionally skips `buying_power`.** Per
`corporate_actions/reconciliation.py::_reconcile_cash`'s docstring,
`reserved_capital_usd` is a per-order reservation and has no clean mapping
to Alpaca's margin-aware derived `buying_power`. The verify script does not
expect a buying-power alert on the cash-aligned account snapshot.

**Strategy-position reconciliation is deferred to ALP-123.** The script
seeds one strategy position (vertical call spread) to satisfy the
seed-coverage acceptance criterion, but no CA targets it and the
post-Phase-1 reconciler does not compare strategy positions leg-by-leg at
this layer (the continuous monitor will, per ALP-123).

**Greeks staleness uses `OptionGreeks.refresh_failed=True`**, not literal
`None`. The greeks fields (delta/gamma/theta/vega) are non-nullable floats;
handlers that touch options positions flag them stale via the freshness
metadata field so downstream consumers know to re-derive at the next 4d
refresh (`architecture.md § 4d`).

**The verify script uses synthetic `CorporateActionActivity` records.** It
does not exercise the v1beta1 fetcher (`fetch_unprocessed_ca_activities`) —
that path requires a `CorporateActionsClient` and is tested separately under
`tests/execution/corporate_actions/test_fetcher.py`. The verify script
asserts the handler dispatch / state-mutation / reconciliation contract;
the fetcher's Alpaca-typed-event-to-`CorporateActionActivity` translation
is asserted at the unit-test layer.

**`process_unprocessed_fills` builds the `AlpacaPositionLookup` from
`alpaca_positions`.** The same tuple of `PositionSnapshot` records feeds
both the reconciliation step and the handler-side lookups for STOCK_MERGER,
SPIN_OFF, and the options / strategy branches of REVERSE_SPLIT and
STOCK_DIVIDEND. Production wiring (ALP-123) will supply a coherent batch of
Alpaca positions for both purposes via a single `get_positions` call.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook; this
  script runs as the corporate-actions phase after state-persistence and
  broker-adapter (Phases 1b + 1d) and before the continuous monitor
  (ALP-123) when it lands.
- `docs/design/05-execution-layer/corporate-actions.md` — design doc the
  verification asserts conformance to. The per-action-type matrix at the
  top is the script's test list.
- ALP-124 parent issue — work-tree overview, dependency graph, pre-resolved
  decisions.
- ALP-409 through ALP-415 — predecessor stories whose deliverables this
  script integrates (package skeleton, per-action-type handlers, v1beta1
  fetcher, chronological merge, reconciliation alerts).
- `src/alphamind/execution/corporate_actions/` — package implementing the
  handlers, dispatch, fetcher, and reconciliation.
- `src/alphamind/execution/state_persistence/write_paths/phase1.py` —
  `process_unprocessed_fills` entry point and the merged-event loop.
- `tests/execution/corporate_actions/` — per-handler unit tests; the verify
  script asserts the integration contract these unit tests' per-handler
  expectations sum to.
