# The continuous monitor is fail-safe by construction; the safety core is isolated

Status: Accepted — 2026-06-03

## Context

The continuous monitor is a single always-on asyncio process bundling ~10 tasks across
five failure concerns — safety (breach detection), precision (options stops, greeks,
entry-window), data (fill stream), accounting (borrow, realized-vol), operator (HTTP) —
in one event loop, with only an **in-process** watchdog. A bare-synchronous `get_orders`
REST call on the loop (a *data*-path recovery call) blocked the loop and everything on it
— including the breach (*safety*) loop and the watchdog itself — silently for ~4.5h
(ALP-841). The documented *"different failure domains"* principle
(`continuous-monitor-runtime.md`) was applied *between* services but not *within* the
monitor.

## Decision

Make the monitor **fail-safe by construction**: a precision/optimization layer above a
**broker-enforced safety floor**, so its death *degrades precision, never removes
protection*. Decompose by failure domain:

- **Safety core** — isolated in its own minimal, non-blocking process: portfolio breach
  detection + price-staleness guard (the lone safety item with **no** broker floor). It
  reads positions from the **broker snapshot** and prices from the **live stream** — never
  the fill-projection — and **writes nothing** to the shared DB. Supervised by an
  **out-of-process** watchdog.
- **Monitor proper** — precision/data, fail-safe under the floor: options thesis-stops,
  greeks, entry-window, fill stream + recovery sweep.
- **Evicted to scheduled/pipeline work** — accounting (borrow, realized-vol) and the
  activity poll (periodic, not streaming).
- **Operator surface** — separate.

## Why

A loop-resident watchdog *cannot* catch a freeze of its own loop — the structural reason
the ALP-825/826/819/832 hardening missed ALP-841. The one item with no broker floor
deserves the hardest isolation. Once the broker floor holds (per
[0003](0003-brackets-leg-is-intent-thesis-shaped-exits.md)), process topology becomes an
*ops/simplicity* question, not a *safety* one — and isolating the safety core makes the
ALP-841 mechanism (a data hang freezing the safety loop) *unrepresentable*, not merely
fixed.

## Considered and rejected

- **One hardened process** (evict crons, kill bare-sync-on-loop, out-of-process watchdog).
  Defensible *because* fail-safe holds, but leaves the lone no-floor item sharing a failure
  domain with blocking I/O. Rejected for the cheap insurance of isolation.

## Consequences

- A monitor freeze under this design = precision degraded, protection intact (contrast
  ALP-841: options unprotected for 4.5h).
- Resolves the monitor liveness/freshness class (ALP-825/826/819/832) structurally rather
  than per-cell.
- The safety core writing nothing to the shared DB is a precondition for
  [0005](0005-single-writer-by-construction.md).

## Update — 2026-06-08 (ALP-940)

The safety core's price *source* moved from its own live market-data websocket to
**REST latest-quote polling**. Alpaca's free IEX plan allows one authenticated
market-data websocket per account, so the safety core's second `StockDataStream`
collided with the monitor's (`connection limit exceeded`) and starved both feeds.
REST polling is not connection-limited; the safety core stays isolated in its own
process with prices independent of the monitor's liveness — only the price source
changed, not the isolation.
