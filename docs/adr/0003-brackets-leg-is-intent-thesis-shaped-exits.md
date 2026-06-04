# Brackets — a leg is Intent; enforcement is a typed broker-vs-monitor binding; exits are thesis-shaped

Status: Accepted — 2026-06-03

## Context

Bracket legs were stored as `orders` rows with **counterfeit `alp-{order_id}` broker ids**
even when no broker order existed (`write_paths/phase2/_shared.py:305`), conflating Intent
with Broker-Owned Fact. This flattening is the generator of ALP-837 (CANCEL/ADJUST of a
synthetic-id leg sends `alp-…` to Alpaca → 404 → `PermanentRejection`) and ALP-746
(equity native legs need their real Alpaca ids captured at submit). AlphaMind options
stops triggered uniformly on the **underlying** price (`orders-and-brackets.md:73`).

Verified against Alpaca's current API: **no** bracket/OCO/OTO order classes on options
("complex orders not supported for options trading"); **yes** single-leg `stop` /
`stop_limit`, TIF `day` or `gtc`; multi-leg capped at Level 3 = buying spreads.

## Decision

A **protective leg is Intent** — never an order, never given a broker id (`alp-{…}` is
deleted). Enforcement is a separately-typed binding:

- **Broker-enforced** — an equity native bracket/OTO child, or an options
  capital-protection `stop_limit`. Its execution is a Broker-Owned Fact; it survives a
  monitor outage.
- **Monitor-enforced** — armed Intent with no broker order; the monitor watches the
  condition and, on fire, submits a *fresh self-attributing close*. "Cancel" is a local
  Intent state change, not a broker call.

Exits are **thesis-shaped**: a **thesis-invalidation stop** (underlying-triggered for a
*directional* thesis; option-price / net-mark for a *non-directional* vol/spread thesis)
plus an always-present **capital-protection floor** (PnL-denominated, broker-enforced via
single-leg `stop_limit` where possible).

## Why

- The synthetic-id flattening generates ALP-837/746; removing it makes "cancel an order
  that doesn't exist" unrepresentable.
- A broker-enforced capital floor on options is *wedge-survivable* — the exact protection
  options positions lacked during the ALP-841 freeze (equities had native brackets; options
  had nothing).
- An underlying-level trigger is meaningless for a non-directional spread (PnL is nonlinear
  in the underlying); the most PnL-faithful signal (option market price) is also the
  noisiest, so each trigger is matched to the job it fits.

## Considered and rejected

- **Uniform option-price exits** — re-imports false-exit / bad-fill on directional
  single-legs and pollutes per-thesis feedback. Stronger only if the goal is to gut the
  monitor entirely; superseded by [0004](0004-monitor-fail-safe-isolate-safety-core.md).
- **Uniform underlying triggers** (the current design) — wrong for a spreads book.

## Consequences

- ALP-837 becomes unrepresentable; ALP-746's leg-id capture becomes a projection cache,
  not the attribution authority.
- Use `stop_limit` (not `stop_market`) for the broker floor to bound bad fills, accepting
  possible non-fill in a true gap (caught by the monitor + guardrail).
- Account is options Level 3 (confirmed): selling spreads / iron condors / naked shorts
  exceed the approved level — a separate decision-layer constraint, tracked apart.
