# Runbook — Genesis cutover (fresh Alpaca account + fresh DB)

One-time procedure to cut AlphaMind over to the redesigned broker boundary
([ADR-0001…0005](../adr/)) by swapping in a **new Alpaca paper account** and a **fresh DB
file**, rather than migrating the corrupt legacy state.

**Run this on the Windows prod box** (the DB is WAL-mode SQLite; writes only run on prod;
`.env` is not auto-sourced). Do it **outside RTH**.

---

## The one invariant

> **Genesis = broker flat ∧ local empty**, established atomically with every service
> stopped.

A brand-new paper account gives `broker flat` for free. The work is: point credentials at
the new account **before** bootstrap, and start the projection from an **empty** DB. The
trap this avoids: wiping local state while the broker still holds positions/orders
re-creates a husk at genesis (a Broker-Owned Fact with no Intent).

---

## Prerequisite (do NOT start until true)

- [ ] **The new design is built, tested, lint + CI green.** This runbook is the *cutover*
      — the last step. Nuking onto the old code just resets the symptoms to zero and
      reproduces them.
- [ ] `fresh_start` precondition extended to assert **open-orders-empty** *and*
      positions-empty (a resting bracket/entry with no Intent is the husk in order form).
- [ ] New-design service inventory ready to install: `alphamind-collector` (unchanged),
      `alphamind-scheduler` (projection + Intent writer, entry point
      `python -m alphamind.scheduler run`), `alphamind-monitor` (precision/data — fill
      stream, bracket-stops, greeks, entry-window, recovery sweep),
      `alphamind-safety-core` (**new** — isolated breach + price-staleness), and the
      **out-of-process watchdog** `alphamind-safety-core-watchdog` for the safety core
      (per ADR-0004). Install scripts: `scripts/install_pipeline_scheduler_service.ps1`,
      `scripts/install_monitor_service.ps1`, `scripts/install_safety_core_service.ps1`
      (installs both safety-core + watchdog).

## New account checklist (provision + verify before cutover)

- [ ] New Alpaca **paper** account created.
- [ ] **Options Level 3 approved and verified** on the new account. *(A fresh paper account
      starts at level 0 — without this, every options thesis is rejected at submit on day
      one.)*
- [ ] Starting **cash** set to the intended value.
- [ ] **Margin / PDT** settings match intent.
- [ ] New **API key + secret** obtained; base URL is the paper endpoint.

---

## Procedure

### 0. Pre-flight
- [ ] Confirm the new-account checklist above is fully green.
- [ ] Announce the cutover window; ensure no scheduled invocation will fire mid-procedure.

### 1. Stop everything (reverse-dependency order)
Stop so nothing initiates new state while you tear down: **pipeline → monitor →
safety-core → collector** (NSSM stop each service).
- ✅ **Verify:** all four (five incl. watchdog) services `Stopped`; no process holds the
  DB file open.

### 2. Freeze the legacy artifacts (forensic — do NOT delete)
- [ ] Checkpoint the old DB so the copy is consistent: with all services stopped, run
      `PRAGMA wal_checkpoint(TRUNCATE);` against the old DB.
- [ ] Copy the old DB aside, read-only: `alphamind.db` → `archive/alphamind.OLD-<UTC>.db`.
- [ ] Dump the **old account** end-state for the RCA record (`get_positions`,
      `get_orders(status=all)`) — this preserves the GS husk / DVN strand evidence.
- ✅ **Verify:** the `.OLD-<UTC>.db` copy opens read-only and contains the legacy
  portfolio rows; old-account dump saved.

### 3. Swap credentials to the new account
- [ ] Update `.env` (+ any config) on the prod box with the **new** account's key/secret.
- [ ] Ensure **every** Alpaca-trading service will read the new creds — no split-brain
      (one service on the old account, one on the new).
- ✅ **Verify (broker-flat-∧-right-account gate):** a one-shot `get_account` with the new
  creds returns the **new** account number, **Level 3**, expected starting cash, and
  `get_positions` empty **and** `get_orders(status=open)` empty.

### 4. Stand up the fresh DB from the new Alembic baseline
- [ ] Point config at the **new** DB path (do not reuse the old file).
- [ ] `alembic upgrade head` to create the new-design schema — append-only **broker-event
      log**, separated **Intent**, **greeks side table**, link-carrying `client_order_id`
      (ADR-0002/0003/0005).
- ⚠️ When authoring the baseline's CHECK constraints, include the **full intended enum
      vocabulary** up front (see the "migration CHECK-vocab" trap) so later enum additions
      don't retroactively differ on fresh DBs.
- ✅ **Verify:** schema at head; new tables exist; all portfolio/Intent tables empty.

### 5. Carry over collector market-data + reference tables (only)
Copy from the old DB **only** the schema-stable, expensive-to-recollect tables the
redesign does not touch:
`equity_bars`, `options_chains`, `ohlcv_bars`, `corporate_actions`,
`ticker_realized_vol`, `borrow_cost_daily` (verify the exact set against the schema).
- ⚠️ Copy only if their schema is **unchanged** under the redesign (it should be — the
      redesign is execution-layer only). If any changed, **re-collect** instead.
- ✅ **Verify:** copied-table row counts match the old DB; **no** portfolio/execution/Intent
  rows came across.

### 6. Bootstrap from the new (flat) account
- [ ] Run `fresh_start` (with `source .env`). It seeds `cash_ledger` + `drawdown_state`
      from `get_account` on the new account; the extended precondition (positions empty ∧
      open-orders empty) passes trivially.
- ✅ **Verify:** `cash_ledger.current_cash_usd` == new-account cash; drawdown HWM ==
  starting cash; `positions / orders / theses / fill_records / unattributed_fills` empty; a
  projection rebuild from the (flat) snapshot yields empty.

### 7. Start services (dependency order)
Bring up data + safety **before** the thing that can open positions:
**collector → safety-core (+ watchdog) → monitor → pipeline.**
- ✅ **Verify:** all services healthy; the **out-of-process** watchdog is seeing safety-core
  heartbeats; control-surface heartbeat live.

### 8. First-run genesis assertions
- ✅ First pipeline invocation completes with **zero reconciliation alerts** (nothing to
  reconcile — by construction); `unattributed_fills` empty; no husks.
- ✅ **Canary trade:** let it open one position, then confirm end-to-end:
  - the broker `client_order_id` carries the **thesis + invocation** link (ADR-0002);
  - the entry fill **self-attributes** (no `unattributed_fills` row, no local-order-row
    dependency);
  - **projection** (quantity) and **Intent** (thesis, reservation, cost basis) both reflect
    it;
  - greeks land in the **side table**, not on the positions row (ADR-0005);
  - the options **capital-protection floor** `stop_limit` is resting at the broker
    (ADR-0003);
  - a monitor stop, if it fires, submits a fresh self-attributing close (no `alp-…`
    synthetic id anywhere).

---

## Rollback

If cutover fails: stop services → restore `archive/alphamind.OLD-<UTC>.db` → swap creds
back to the old account → restart on the old design. Clean because both the old account and
old DB are frozen, untouched. **Note:** rollback returns you to the *buggy* legacy design —
an escape hatch, not a destination.

## Post-cutover

- [ ] Hold a stability window, then disable/retire the old account.
- [ ] Keep `archive/alphamind.OLD-<UTC>.db` + the old-account dump as the forensic / RCA
      artifact.
- [ ] The **go-live evaluation clock restarts** from this clean genesis — the gate now
      measures the new design, not the buggy week.

## Genesis gate (the checklist that must all be ✅ before declaring cutover done)

- [ ] New account: Level 3 verified, expected cash, **flat** (no positions, no open orders).
- [ ] Creds swapped everywhere — no split-brain.
- [ ] Fresh DB at schema head; portfolio/Intent tables empty; collector tables carried over.
- [ ] Bootstrap seeded cash/drawdown from the new account.
- [ ] First invocation: **zero reconciliation alerts**, no unattributed fills.
- [ ] Canary trade self-attributes end-to-end; broker-side capital floor resting; no `alp-…`
      synthetic ids.
