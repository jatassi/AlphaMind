# Attribution rides a broker-carried link; the broker-event log is fills ∪ activities ∪ corporate-actions

Status: Accepted — 2026-06-03

## Context

A fill was attributed to a thesis only through a local `orders` row
(`client_order_id` → position FK → thesis). Losing that row — e.g. the ALP-836 lost
Phase-2 commit — stranded the fill permanently in `unattributed_fills`, because the
recovery machinery can only resolve a fill to an *existing* order row, never reconstruct
a dropped one. Separately, option lifecycle events (expiry / assignment / exercise)
produce **no fills**; `get_account_activities` is implemented but has zero production
call sites, so those events were uncaptured and fell through to the husk-making reconciler
(an `OPEN/0` position with no PnL booked) — the husk class *by design*, on every option
that expires or is assigned.

## Decision

Embed the durable **Intent** foreign key — reaching the thesis (*why*) and invocation
(*when*) — into Alpaca's `client_order_id` (verified 128-char budget), which the broker
echoes on every order object and `trade_updates` fill. A fill is then **self-attributing**
with no local order row required; the order row demotes from required hop to optional
projection cache. Define the **broker-event log** as **fills ∪ account-activities ∪
corporate-actions**, captured gap-free, as the sole substrate for realized PnL. Wire up
`get_account_activities` (`OPEXP` / `OPEXC` / `OPASN` / `OPTRD`) as a first-class event
source. Position lifecycle events, which carry no `client_order_id`, attribute via the
**position→thesis Intent edge**.

## Why

Moving the FK into the System of Record makes the strand class *unrepresentable* — there
is no "fill AlphaMind submitted that it cannot attribute." Realized PnL cannot be derived
from fills alone, because an option expiry/assignment changes a position with no fill
(verified: these surface only as REST account activities, never on `trade_updates`).

## Considered and rejected

- **Durable-local-order-row-before-submit** (ALP-836's shipped fix) — keeps the local row
  load-bearing for attribution. The broker-carried link demotes it to a cache, so a lost
  commit becomes a rebuildable cache miss, not a permanent strand.

## Consequences

- Dissolves ALP-836 (husk), the `unattributed_fills` strand class, and the
  option-expiry/assignment husk-by-design.
- The `OPTRD` paired activity fully describes the assignment/exercise equity leg at the
  strike, so the resulting equity position opens with correct cost basis and an Intent
  stub linked to the originating thesis.
- Residuals: native equity bracket legs carry **Alpaca-generated** `client_order_id`s
  (resolve via captured broker UUID — a projection cache, per ALP-746); out-of-band orders
  (no thesis) correctly become broker-facts with no Intent overlay.
- Depends on [0001](0001-broker-facts-are-a-projection-not-a-mirror.md).
