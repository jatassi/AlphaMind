# AlphaMind — Canonical Vocabulary

Canonical vocabulary for AlphaMind. This is a glossary, not a spec — it fixes the words
so both the architecture conversation and the code stay precise about *who owns which
fact* and *who holds which authority*.

## Actors

**Operator**:
The human with decision authority over the trading system — the escalation target for
any fork an agent cannot resolve from evidence, and the executor of supervised one-time
procedures. Agents execute operator workflows on the Operator's behalf; they do not
hold the Operator's authority.
_Avoid_: operator (for an agent running prod procedures — the agent operates prod; it
is not the Operator)

## Invocation lifecycle

**Fill collection**:
The invocation's opening write — AlphaMind brings its Projection level with the broker's
settled facts before deliberating, draining the unprocessed fills (and corporate-action
activities) the continuous monitor buffered since the last run and integrating each into
positions / orders / brackets / theses / cash / drawdown. Deliberation then runs on
settled state with no in-flight orders.
_Avoid_: phase 1, phase1, collect phase (ordinal labels naming *when*, not *what*)

**Command execution**:
The invocation's closing write — the portfolio manager's issued commands are validated
against guardrails and enacted at the broker, each command's mutations made durable on
their own so a mid-batch failure cannot unwind the commands already executed.
_Avoid_: phase 2, phase2, execute phase

## Broker boundary

**System of Record**:
The external broker (Alpaca) — the sole authority for execution facts (position
quantity, fills, order status, cash / buying power).
_Avoid_: source of truth (looser synonym; prefer System of Record when naming the broker
as the authoritative actor)

**Broker-Owned Fact**:
A datum the System of Record owns and AlphaMind cannot author — a position quantity, a
fill, an order's status, a cash / buying-power balance.
_Avoid_: account state, position state (those name the *Projection* of these facts, not
the facts themselves)

**Projection**:
AlphaMind's durable, locally-queryable read-model of Broker-Owned Facts, derived from the
System of Record (the fill stream plus snapshot checkpoints) and never written as an
independent authority. Stale is allowed; *wrong* is not — it makes no claim the broker
hasn't.
_Avoid_: mirror, local state, cache

**Mirror** (rejected):
A local copy of Broker-Owned Facts held as a *co-equal* authority and patched toward the
broker after the two diverge. The legacy design, and the generator of the reconciliation
bug class. Use **Projection** instead.

**Intent**:
A fact AlphaMind authors that the broker never owns — the thesis, the order→thesis link,
a capital reservation, per-thesis cost basis, and per-thesis realized PnL with
provenance. Sole authority: AlphaMind; never overwritten by the System of Record.
_Avoid_: metadata, annotation

## Attribution

**Broker-carried link**:
The order→Intent foreign key — reaching the originating thesis (*why*) and invocation
(*when*), optionally the position — embedded in Alpaca's `client_order_id` and echoed
back on every fill, so a fill attributes to its Intent without any local order row.
_Avoid_: client_order_id (that names the transport; the *link* is what it carries)

**Self-attributing fill**:
A fill that names its own Intent via the Broker-carried link — resolvable to
thesis / position with no join to a local order table.

**Broker-event log**:
The complete stream of broker→local events that change a Broker-Owned Fact —
**fills ∪ account-activities ∪ corporate-actions**. Realized PnL is derived from this
log, so it must be gap-free; fills alone are insufficient (an option expiry or
assignment changes a position with no fill).

## Brackets & exits

**Protective leg**:
A thesis-linked exit condition on a position (price-stop, take-profit, time-stop, or
event) — a unit of Intent, never an order and never assigned a broker id. Enforced by one
of the two bindings below.
_Avoid_: synthetic leg, synthetic order (the rejected pattern of giving a leg a
counterfeit `alp-…` broker id so it fits the `orders` schema)

**Broker-enforced leg**:
A Protective leg backed by a real broker order (an equity native bracket/OTO child; an
options capital-protection `stop_limit`). Its execution is a Broker-Owned Fact; it
survives a monitor outage.

**Monitor-enforced leg**:
A Protective leg with no broker order — armed Intent the continuous monitor watches, and
acts on by submitting a fresh close when it fires. "Cancel" is a local Intent state
change, not a broker call.

**Thesis-invalidation stop**:
The exit answering "is the thesis wrong?" — *thesis-shaped*: underlying-price-triggered
for a directional thesis, option-price / net-mark for a non-directional (vol / spread)
thesis. The primary exit.

**Capital-protection floor**:
The exit answering "have I lost too much, regardless of thesis?" — always present,
denominated in option price / PnL, Broker-enforced where possible so it survives a
monitor wedge.

**Directional / non-directional thesis**:
Directional — invalidated by an underlying price level (a long call on an up-move).
Non-directional — a vol / time / spread thesis with no single invalidating underlying
level, PnL nonlinear in the underlying. Determines which signal the Thesis-invalidation
stop triggers on.
