# Broker boundary redesign — implementation specification

**Feature:** Broker boundary redesign
**Status:** Designed (2026-06-03), not yet built. Cutover via fresh account + fresh DB.
**Decisions (why):** [ADR-0001…0005](../../adr/) · **Glossary:** [CONTEXT.md](../../../CONTEXT.md) · **Cutover:** [genesis-cutover runbook](../../runbooks/genesis-cutover.md)

This is the build spec and work decomposition. It is written to be sliced into Linear
user stories — §5 workstreams are story candidates, §6 is the dependency graph. Read the
ADRs for rationale; this document does not re-argue it.

---

## 1. Goal

Make position / order / PnL reconciliation issues **structurally impossible**, not rarer.
The production cluster (ALP-841 monitor wedge, ALP-824 DB contention, ALP-836/838/834/761/
760/619 reconciliation husks, plus the unfiled options-lifecycle husk) is one root cause:
AlphaMind maintained a **writeable, co-equal Mirror** of broker-owned facts, written by two
processes and reconciled after the fact. The redesign replaces the Mirror with a
**Projection + Intent** split, carries attribution out to the broker, puts a broker-enforced
floor under every exit, and gives every fact one writer.

## 2. Target architecture (components & ownership)

| Component | Ownership / authority | New? |
|---|---|---|
| **Broker-event log** (fills ∪ activities ∪ CAs ∪ terminal order status) | append-only, idempotent; written by capturers | **new** |
| **Projection** (positions, qty, cash) | derived read-model; single writer = pipeline; rebuildable | restructured |
| **Intent** (theses, reservations, order→thesis link, per-thesis cost basis + realized PnL) | authored by pipeline; never overwritten by broker | restructured |
| **Broker-carried link** (thesis + invocation FK in `client_order_id`) | the System of Record carries it; echoed on every fill | **new** |
| **Protective leg = Intent** + typed **broker-/monitor-enforced** binding | Intent; no synthetic broker id | restructured |
| **Greeks side table** | single writer (monitor proper) | **new** |
| **Safety core** (breach + price-staleness) | isolated process; reads broker snapshot + price stream; **writes nothing** | **new (split out)** |
| **Monitor proper** (fill stream, bracket-stops, greeks, entry-window, recovery sweep) | precision/data, fail-safe under the broker floor | restructured |
| **Out-of-process watchdog** | supervises the safety core | **new** |

## 3. Current-state touchpoints (what changes)

- Attribution / ids: `oms/command_ids.py`, `oms/broker_dispatch.py`, `broker_adapter/order_{equity,options,mleg}.py`, `decision/portfolio_manager/submit_envelope/`.
- Capture: `continuous_monitor/fill_stream_consumer/persistence.py` (`_resolve_oms_order_id`, `_quarantine_unattributed_fill`), `continuous_monitor/activities_backfill/`, `broker_adapter/queries.py` (`get_account_activities:491` — **dead, to wire**; `get_orders`, `get_positions`, `get_account`), `corporate_actions/fetcher.py` (CA v1beta1 endpoint — `WorthlessRemoval`/`UnitSplit`/etc. unhandled). **`OPASN`/`OPEXC`/`OPEXP` are account-activities types, absent from the codebase entirely** (the activities endpoint is never polled).
- Projection / reconcile: `corporate_actions/reconciliation.py` (**delete the adjudication path**), `execution/write_paths/phase1.py` (`process_unprocessed_fills`), `scheduler/phase1_inputs.py`, `scheduler/orchestrator.py`.
- Brackets: `write_paths/phase2/_shared.py:305` (**remove `alp-{order_id}` synthetic id**), `write_paths/phase2/open.py`, `continuous_monitor/bracket_stops/`, `state/tables/{brackets,bracket_legs}.py`.
- Monitor / writers: `continuous_monitor/{__main__,supervisor}.py`, `continuous_monitor/{breach_loop,greeks_refresh,borrow_accrual,emergency_trigger}/`, `persistence/session.py` (`BEGIN IMMEDIATE`/retry → cheap insurance).
- Schema: `state/tables/*`, new Alembic baseline under `persistence/migrations/`.
- Decision layer: `commands/` + OMS command schema (thesis-nature tag, thesis-shaped exits, capital floor), `decision/{portfolio_manager,strategist}/`.
- Cutover: `scheduler/fresh_start.py` (extend precondition).
- Operator docs: `scripts/RUNBOOK_production.md` (service inventory, first-run bootstrap, monitor-wedge §8.9, reconciliation notes) — updated in lockstep per its own living-document rule (`RUNBOOK_production.md:26`); siblings `RUNBOOK_command_center.md` / `RUNBOOK_end_to_end_verification.md` where they enumerate services.

---

## 4. Invariants the build must hold (test targets)

1. **One writer per fact.** Every shared mutable row has exactly one writer (pipeline); the only cross-process writes are append-only-idempotent (the event log). No `SQLITE_BUSY_SNAPSHOT` is reachable.
2. **Self-attributing fills.** Any fill from an AlphaMind-submitted order resolves to thesis/position via the broker-carried link with **no** local order row required. `unattributed_fills` is reachable only for out-of-band orders.
3. **PnL from the log.** Per-thesis realized PnL is derivable from the broker-event log alone (fills ∪ activities), never a join to a lose-able order table.
4. **Fail-safe.** With the safety core/monitor stopped, every open position retains a broker-enforced exit (equity bracket or options capital `stop_limit`). Monitor death degrades precision, never protection.
5. **No synthetic broker ids.** No `alp-…` placeholder exists anywhere; a leg with no broker order has no broker id.
6. **Genesis is clean.** First invocation on a fresh account emits zero reconciliation alerts.

---

## 5. Work decomposition (story candidates)

Naming is parallelism-aware (letters = parallelizable within a wave). Each lists **Depends
on**, **Scope**, **Key files**, **Done when**.

### W0 — Schema baseline & the broker-carried id (foundation)

**W0a — New Alembic baseline + new-design models**
- Depends on: none.
- Scope: Define SQLAlchemy models for the broker-event log (append-only, idempotent keys), the Intent store (order→thesis link, reservations, per-thesis PnL/cost-basis ledger), the greeks side table, and the projection tables; author one fresh baseline migration. Build CHECK-constraint enum vocabularies fully up front.
- Key files: `state/tables/*`, `persistence/migrations/`.
- Done when: `alembic upgrade head` on an empty DB yields the full new schema; model round-trip tests pass; migration test green.

**W0b — Broker-carried link in `client_order_id`**
- Depends on: none (id format is independent of storage).
- Scope: Extend `command_ids.py` derive/parse/validate so `client_order_id` encodes the **thesis + invocation** FK within the 128-char budget; keep uniqueness/`attempt_seq` for replay-idempotency. Update engine-originated ids likewise.
- Key files: `oms/command_ids.py`.
- Done when: derive→parse round-trips thesis+invocation; length ≤128 asserted; collision/replay-idempotency tests pass.

### W1 — Capture: the broker-event log fills up (parallel after W0)

**W1a — Fill stream → event log, self-attributing**
- Depends on: W0a, W0b.
- Scope: Rewrite fill persistence to append to the broker-event log and resolve attribution via the broker-carried link (order row optional). Retire the lost-order-row strand path; `unattributed_fills` only for out-of-band.
- Key files: `continuous_monitor/fill_stream_consumer/persistence.py`, `broker_adapter/fill_stream.py`.
- Done when: a fill with no local order row attributes to its thesis/position; fast-fill (ALP-763) regression green; no strand for AlphaMind-submitted orders.

**W1b — Activity poll + lifecycle handlers (the dead-code gap)**
- Depends on: W0a.
- Scope: Wire `get_account_activities` (OPEXP/OPEXC/OPASN/OPTRD) into a periodic poll that appends to the event log; add handlers that **book realized PnL** (worthless = −premium; assignment/exercise = strike-based via the paired OPTRD) and **open the assignment/exercise equity position** with cost basis + an Intent stub linked to the originating thesis.
- Key files: `broker_adapter/queries.py:491`, `corporate_actions/fetcher.py`, new activity-handler module.
- Done when: a simulated OTM expiry books −premium and closes the option (no husk); a simulated assignment opens the equity at strike linked to the option's thesis.

**W1c — CA & terminal order status → event log**
- Depends on: W0a.
- Scope: Route corporate-actions and zero-fill terminal order-status events through the append-only event log (stop the monitor RMW on `orders.status`).
- Key files: `corporate_actions/`, `continuous_monitor/fill_stream_consumer/` (terminal sync), `state/tables/orders`.
- Done when: terminal status and CA events are events, not in-place RMWs; no second-writer on `orders`.

**W1d — Recovery sweep = fill-log completeness**
- Depends on: W1a.
- Scope: Refocus the periodic REST sweep as the gap-free guarantee for the event log (not quantity reconciliation); offload all sync REST off any event loop (`to_thread` + timeouts — the ALP-841 lesson).
- Key files: `continuous_monitor/activities_backfill/`, `broker_adapter/recovery.py`, `broker_adapter/client_factory.py`.
- Done when: a dropped fill is recovered exactly once; no bare-sync REST on a loop.

### W2 — Projection & Intent derive from the log (after W1)

**W2a — Projection rebuild + delete reconcile-adjudication**
- Depends on: W1a, W1c.
- Scope: Make positions/cash a derived read-model rebuilt from the event log + the snapshot **checkpoint**. **Delete** `reconcile()`'s adjudication/auto-correct path and the `RECONCILIATION_ALERT/CORRECTION` emitters. A broker position with no Intent becomes a first-class projection state (attach/flag), not an alert.
- Key files: `corporate_actions/reconciliation.py` (remove), `execution/write_paths/phase1.py`, `scheduler/phase1_inputs.py`.
- Done when: a snapshot/projection mismatch triggers a rebuild (no adjudication, no alert insert); the DVN-style orphan surfaces as "broker fact, no Intent."

**W2b — Per-thesis PnL/cost-basis in Intent, from the log**
- Depends on: W1a, W1b.
- Scope: Derive per-thesis realized PnL + cost basis in the Intent store from the broker-event log; never overwritten by a snapshot.
- Key files: Intent ledger module, `execution/position_model/`, `thesis_model/`.
- Done when: realized PnL reproduces from the event log alone; a quantity checkpoint never alters per-thesis PnL.

### W3 — Brackets & thesis-shaped exits (after W0; parallel with W2)

**W3a — Protective leg = Intent; kill synthetic ids; close ALP-837**
- Depends on: W0a.
- Scope: Model a protective leg as an Intent record (no broker id); remove `alp-{order_id}` minting; type the enforcement binding (broker-enforced vs monitor-enforced). "Cancel" of a monitor-enforced leg is a local state change.
- Key files: `write_paths/phase2/_shared.py:305`, `write_paths/phase2/open.py`, `state/tables/{brackets,bracket_legs}.py`, `oms/broker_dispatch.py`.
- Done when: no `alp-…` exists; CANCEL/ADJUST never targets a non-existent broker order (ALP-837 unrepresentable).

**W3b — Thesis-nature tag + thesis-shaped trigger selection**
- Depends on: W3a; decision-layer (W6).
- Scope: Carry a directional/non-directional tag on the thesis; select the thesis-invalidation trigger accordingly (underlying for directional; option-price/net-mark for non-directional).
- Key files: `commands/` (OMS command schema), `decision/{strategist,portfolio_manager}/`, `continuous_monitor/bracket_stops/`.
- Done when: a directional thesis triggers on the underlying; a spread thesis triggers on net-mark.

**W3c — Broker-enforced options capital floor (`stop_limit`)**
- Depends on: W3a.
- Scope: Submit an always-on single-leg GTC `stop_limit` capital floor per options position (broker-enforced, wedge-survivable); reconcile it with monitor action (cancel-on-monitor-fire; absorb-on-broker-fire).
- Key files: `broker_adapter/order_options.py`, `continuous_monitor/bracket_stops/`.
- Done when: every open options position has a resting broker-side floor; a monitor stop and the floor never double-close.

**W3d — Monitor-enforced legs fire self-attributing closes**
- Depends on: W0b, W3a.
- Scope: When a monitor-enforced leg fires, submit a fresh close carrying the broker-carried link (self-attributing); no synthetic order, no engine-envelope detour.
- Key files: `continuous_monitor/bracket_stops/wiring.py`, `oms/broker_dispatch.py`.
- Done when: a fired leg's close self-attributes end-to-end.

### W4 — Monitor topology & single-writer (after W1/W2/W3)

**W4a — Single-writer enforcement (remove cross-process RMW)**
- Depends on: W0a, W1c.
- Scope: Move greeks to the side table; relocate borrow accrual + realized-vol + activity poll out of the always-on monitor (to scheduled/pipeline); ensure the only cross-process writes are append-only. `BEGIN IMMEDIATE`/retry kept as cheap insurance on the append path.
- Key files: `continuous_monitor/{greeks_refresh,borrow_accrual,__main__}.py`, `persistence/session.py`.
- Done when: a contention test cannot produce `BUSY_SNAPSHOT`; no monitor RMW on positions/orders.

**W4b — Isolate the safety core + out-of-process watchdog**
- Depends on: W2a (snapshot reads), W3c (broker floor in place).
- Scope: Extract breach detection + price-staleness into its own non-blocking process that reads the **broker snapshot** + price stream and writes nothing; add an out-of-process watchdog (or NSSM/parent liveness probe) that restarts on heartbeat staleness.
- Key files: new `continuous_monitor/safety_core/` (or top-level service), `continuous_monitor/breach_loop/`, `supervisor.py`, `scripts/install_*_service.ps1`.
- Done when: a deliberately frozen monitor proper does not freeze the safety core; the out-of-process watchdog restarts a wedged safety core; positions stay broker-protected throughout.

### W5 — Cutover

**W5a — Extend `fresh_start` precondition + genesis verification**
- Depends on: W0–W4 built & green.
- Scope: Extend the bootstrap precondition to assert open-orders-empty **and** positions-empty; add the genesis assertions (zero reconciliation alerts; canary self-attribution) as a runnable check.
- Key files: `scheduler/fresh_start.py`, a genesis-verify script.
- Done when: bootstrap refuses a non-flat account; genesis-verify passes on a fresh account.

**W5b — Execute the cutover** (operational, per the runbook) — new paper account (Level 3), fresh DB baseline, carry over collector tables, bootstrap, canary. Not a code story; tracked as the rollout.

### W6 — Decision-layer support (cross-cutting; upstream of W3b)

**W6a — OMS command schema: thesis nature + thesis-shaped exits + capital floor**
- Depends on: W0b.
- Scope: Extend the OMS command/envelope schema so the PM expresses thesis nature, a thesis-shaped invalidation, and an always-on capital floor; strategist/PM populate them.
- Key files: `commands/`, `decision/portfolio_manager/`, `decision/strategist/`.
- Done when: a PM OPEN carries thesis-nature + floor; schema validation enforces the mandatory floor.

---

## 6. Dependency graph (blockedBy wiring)

```
W0a ──┬─> W1a ──> W1d
      ├─> W1b
      ├─> W1c ──┐
      ├─> W3a ──┼─> W3b ──(needs W6a)
      │         ├─> W3c ──┐
      │         └─> W3d   │
W0b ──┼─> W1a              │
      ├─> W3d              │
      └─> W6a ──> W3b      │
W1a,W1c ─> W2a ────────────┼─> W4b
W1a,W1b ─> W2b             │
W0a,W1c ─> W4a             │
W2a,W3c ─────────────────> W4b
W0–W4 (all) ─> W5a ─> W5b
```

Critical path: **W0 → W1 → W2 → W4b → W5**. Brackets (W3) + decision-layer (W6) run in
parallel off W0 and rejoin at W4b/W5.

## 7. Cross-cutting

- **Schema:** one fresh Alembic baseline (no incremental chain — cutover is fresh-DB). Full CHECK-vocab up front (migration-vocab trap). New-table migration tests.
- **Tests:** mock only at the four sanctioned boundaries (LLM, broker, clock, DB). Each invariant in §4 gets a failing-for-a-unique-reason test. CI (Windows, `-n auto`) is the gate; the begin-mode/schema changes touch transaction emission broadly, so full-suite is authoritative.
- **Lint/imports:** new modules respect the downward-only `.importlinter` layering (capture → projection/Intent → decision; safety core depends on neither pipeline nor monitor internals).
- **Cutover:** [genesis-cutover runbook](../../runbooks/genesis-cutover.md).
- **Operator runbooks (living-document rule, `RUNBOOK_production.md:26`).** The redesign invalidates concrete operator procedures; each workstream updates the affected sections **in the same change** (no story lands prod-behavior changes leaving the runbook stale):

  | Redesign change | `RUNBOOK_production.md` sections to update |
  |---|---|
  | New safety-core service + out-of-process watchdog; monitor sheds crons (W4b) | service table (intro `:11`), stop/start order (§1.4/§1.6), install/start (§2.5–2.6), restart table + valid names (§7), ports/paths (§9) |
  | `alp-` ids deleted (W3a) | bootstrap `synthetic_id_count must be 0` check + rationale (§2.4) |
  | reconcile-adjudication deleted (W2a) | §2.4 auto-correct note, §8.9 reconciliation-lag / `_reconcile_cash`, §5.9 `RECONCILIATION_*` watcher lines |
  | in-process → out-of-process watchdog, fail-safe (W4b) | §7 "Running ≠ healthy", §8.9 post-ALP-825 hardening model |
  | fresh-DB genesis cutover (W5) | §2.2 "do not delete the DB / migrations additive" (reconcile with the genesis-cutover runbook — the new-design first-run supersedes §2), §2.1 account swap, §2.4 `--fresh-start` precondition |

  W5 owns the final end-to-end coherence pass so the runbook reads as one consistent operator workflow post-cutover.

## 8. Out of scope / deferred

- Selling spreads / iron condors / naked shorts (exceed Alpaca options **Level 3**) — a separate decision-layer constraint + strategy whitelist.
- Proactive option exercise (the exercise endpoint) — auto-expiry/assignment handled; manual exercise deferred.
- Postgres / message broker — explicitly rejected (ADR-0005); SQLite stays.

## 9. Definition of done (feature)

All six §4 invariants hold under test; the genesis-cutover runbook executes clean on a
fresh account; first live week produces zero husks, zero `BUSY_SNAPSHOT` aborts, and no
silent monitor wedge (a frozen monitor leaves positions broker-protected and is restarted
by the out-of-process watchdog).

## 10. Current-state claim verification (2026-06-03)

The current-code assertions in §3/§4 were adversarially verified against post-merge `main`
(ALP-824 #292 and ALP-836 #300 both merged) by four independent verifiers. All
load-bearing claims **confirmed**: the husk mechanism (dead `get_account_activities`, no
expiry/assignment handler, `_reconcile_options` drift-to-zero booking no PnL), the
synthetic-`alp-` mint (`_shared.py:305`), the **unguarded ALP-837 cancel path** (`_is_synthetic`
guard present only in `entry_window/canceller.py`, absent in `broker_dispatch.py`), the
bare-sync `get_orders` on the event loop (`queries.py:480`) with the watchdog on the same
loop, the monitor as a second writer (greeks RMW on the positions row), and the
single-writer doc claim it violates.

Corrections folded in: `BEGIN IMMEDIATE` + retry is **no longer Phase-1-only** — ALP-836
(#300) extended it to Phase-2 (`write_paths/phase2/atomic.py`), so the primitives manage
contention on both phases (the redesign still removes the need). The post-#300 durable
order row is a **required pre-condition** before broker submit (the broker-carried link is
what *demotes* it to an optional cache). Only the **first** equity price-stop + take-profit
are broker-native; secondary equity stops, all options legs, and time/event legs are
monitor-enforced. `OPASN`/`OPEXC` are account-activities types absent from the codebase
(not listed in `fetcher.py`, which is the CA endpoint).
