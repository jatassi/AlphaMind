# Single-writer by construction — append-only event log + single-owner projection/Intent

Status: Accepted — 2026-06-03

## Context

Two processes did deferred **read→modify→write** on the same mutable rows (positions,
cash, orders), producing immediate `SQLITE_BUSY_SNAPSHOT` aborts when one committed
between the other's read snapshot and its write-upgrade (ALP-824). `busy_timeout` does not
cover a stale-snapshot upgrade. The shipped fix (`BEGIN IMMEDIATE` + bounded retry)
*manages* the contention rather than removing it, and covered Phase-1 only — leaving the
Phase-2 gap that produced the ALP-836 lost commit. The single-writer invariant
`state-persistence.md` always *claimed* — *"all other entities have exactly one writer
(the OMS)"* — was already violated by monitor writes to `orders.status`, `positions`
(greeks/borrow), `unattributed_fills`, and `invocations`.

## Decision

Give every write a single owner:

- **Broker-event log** (fills, activities, corporate-actions, terminal order status) —
  **append-only, idempotent**, written by the capturers.
- **Projection** (positions / quantity / cash) — derived and rebuildable, **pipeline
  only**.
- **Intent** (theses, reservations, per-thesis PnL, order→thesis) — **pipeline only**.
- **Safety core** — writes nothing to the shared DB.
- **Greeks** (the lone computed decoration) — a **single-writer monitor-owned side
  table**, never an RMW on the positions row.

The only cross-process writes are append-only-idempotent.

## Why

`SQLITE_BUSY_SNAPSHOT` *requires* cross-process read-modify-write on shared rows; removing
that pattern makes the race *unrepresentable* rather than retried. This generalizes the
fill-capture design (append-only capture + single-owner integration) that was always
correct, reversing the drift (`orders.status` sync, greeks, borrow, reconciliation) that
introduced second writers. The contention was self-inflicted, not a database limit, so
**SQLite stays** — no Postgres, no message broker (respects the existing
`data-and-state.md` rationale and the "simplify" instinct).

## Consequences

- ALP-824 dissolves; `state-persistence.md`'s single-writer invariant becomes true *by
  construction*.
- `BEGIN IMMEDIATE` + retry drops from load-bearing to cheap belt on the append path.
- Append-only-idempotent broker-event capture is also what makes
  [0002](0002-attribution-rides-a-broker-carried-link.md)'s gap-free log achievable.
- Depends on [0001](0001-broker-facts-are-a-projection-not-a-mirror.md) and
  [0004](0004-monitor-fail-safe-isolate-safety-core.md).
