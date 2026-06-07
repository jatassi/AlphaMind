# Broker-owned facts are a Projection, not a reconciled Mirror

Status: Accepted — 2026-06-03

## Context

AlphaMind kept a local SQLite copy of broker-owned execution facts — position
quantity, fills, order status, cash — written by **two processes** (pipeline + continuous
monitor) from **two authorities** (AlphaMind's own intent at submission; observed broker
state from the fill stream), then reconciled toward Alpaca every fill collection under the
doctrine *"Alpaca's positions and account endpoints are the source of truth. On
disagreement, Alpaca wins"* (`broker-adapter.md:186`). This dual-source-of-truth is the
generator of a recurring bug class — husks, stranded fills, mis-booked PnL
(ALP-836 / 834 / 761 / 760 / 619). No design doc ever proposed to make divergence
*impossible*; the documented philosophy is "divergence is normal, reconcile toward
Alpaca."

## Decision

Treat every **Broker-Owned Fact** as a **Projection**: a durable, locally-queryable
read-model derived from the System of Record (Alpaca), never written as an independent
authority and always rebuildable. Separate it from **Intent** — theses, capital
reservations, the order→thesis link, per-thesis cost basis and realized PnL — which
AlphaMind authors and the broker never owns. The broker snapshot
(`get_positions` / `get_account`) survives as a **checkpoint that rebuilds** the
projection, not a peer to **adjudicate** against. There is no "reconcile," only "rebuild."

## Why

Two authorities for one fact is the root generator of the reconciliation class.
Hardening the mirror (locking, retry, atomic order persistence) makes divergence *rarer*,
never *impossible*. A projection has a single authority-flow (broker→local), so
"divergence" as an adjudicable concept disappears.

## Considered and rejected

- **Hardened mirror** — keep a co-equal local authority, add `BEGIN IMMEDIATE` / retry /
  atomic-persist. Rejected: it manages the symptom; the husk class stays *expressible*.

## Consequences

- The `reconcile()` adjudication path is deleted — including the `RECONCILIATION_ALERT`
  insert that hit `SQLITE_BUSY_SNAPSHOT` in ALP-824.
- "Alpaca wins" (`broker-adapter.md:186`) is replaced by projection rebuild from the
  checkpoint; a broker position with no Intent overlay becomes a first-class projection
  state (the DVN/manual-trade case), not a reconcile alert.
- Per-thesis realized PnL is **Intent**, never overwritten by a broker snapshot — the
  husk's silent PnL corruption (`share_count→0`, PnL unbooked) cannot occur.
- Enabling decisions: [0002](0002-attribution-rides-a-broker-carried-link.md) (how facts
  attribute), [0005](0005-single-writer-by-construction.md) (who writes them).
