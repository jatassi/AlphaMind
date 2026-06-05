# Runbook — Genesis verify (`scripts/verify_genesis.py`)

A self-contained **wiring smoke test** that asserts the
[genesis-cutover runbook](../docs/runbooks/genesis-cutover.md) **§8 first-run
genesis checklist** in code against a synthetic canary, so the cutover prep (story
08, operator-executed) is backed by a green check rather than a human reading a
list. This is broker-boundary-redesign **invariant 6** ("genesis is clean") made
executable — see
[the build spec §4 / §5 W5a / §9](../docs/design/05-execution-layer/broker-boundary-redesign.md)
and [ADR-0001](../docs/adr/0001-broker-facts-are-a-projection-not-a-mirror.md)
(genesis is clean by construction — nothing to reconcile).

It is a **dedicated** check — deliberately *not* folded into
`RUNBOOK_end_to_end_verification.md`. The e2e runbook verifies a fully-seeded
pipeline run end-to-end; this verifies the redesign's broker-boundary machinery
is wired correctly at the clean-genesis moment.

---

## What it asserts (genesis-cutover runbook §8)

The check drives a synthetic **canary** options position through the *production*
broker-boundary primitives (the real fresh-start bootstrap, the real
self-attributing fill consumer, the real state codecs + broker-carried-link
derivation) and then asserts, against the real tables:

| # | Assertion | Behavior it exercises |
|---|---|---|
| 1 | **Zero reconciliation alerts** | the deleted reconcile-adjudication path (W2a / ADR-0001) — a flat genesis has nothing to reconcile |
| 2 | **Canary self-attributes** | the broker-carried link in `client_order_id` (W0b / 02a): the entry fill attributes to its thesis / invocation / position with **no** local `orders` row and **no** `unattributed_fills` strand |
| 3 | **Projection + Intent both reflect it** | the projection `positions` quantity *and* the Intent `thesis_pnl_ledger` cost basis + `capital_reservations` (03c / 04b) |
| 4 | **Greeks in the side table** | greeks land in `position_greeks` (single-writer side table, ADR-0005 / 04b), never on the `positions` row |
| 5 | **Options floor resting** | the durable `STOP_LIMIT` capital-protection floor `OrderRow` (04c / ADR-0003) rests at the broker |
| 6 | **No `alp-…` synthetic id** | invariant 5 — no synthetic broker-id placeholder on any order (a fired monitor stop submits a fresh self-attributing close instead) |

The **DB is the only mocked boundary** — there is *no* live-account dependency.
The canary's broker side is a synthetic fresh/flat snapshot + a canary fill.

---

## How to run

### Self-contained smoke (no DB needed)

Provisions an ephemeral genesis DB, seeds the flat bootstrap + the canary, and
asserts the whole checklist. Use this to confirm the redesign's machinery is
wired *before* the cutover window:

```bash
uv run python scripts/verify_genesis.py
```

Expected tail:

```
  zero reconciliation alerts                    OK
  canary self-attributes via the link           OK
  projection + Intent reflect the canary        OK
  greeks in the side table                      OK
  options capital floor resting                 OK
  no alp- synthetic broker id                   OK
RESULT: PASS
```

Exit code: `0` = every assertion holds; `1` = at least one failed (the failing
check's code + message is printed above the result line).

### Against a specific DB

Runs the **same synthetic-canary-keyed assertions** against the supplied DB *with
no seeding*. Every assertion keys off the hardcoded `GENESIS_CANARY` identity
(fixed AAPL / command-id / `pos-genesis-canary`), so this only validates a DB that
was itself seeded with that exact synthetic canary:

```bash
uv run python scripts/verify_genesis.py --db-path /path/to/seeded-canary.db
```

> ⚠️ **Not a real-DB cutover gate.** A live post-genesis `alphamind.db` carries
> *real* ids (a different invocation/thesis/position), so the synthetic-canary
> assertions will **FAIL on a perfectly correct genesis** — the FAIL means "the
> canary this script keys off isn't in this DB", not "genesis is broken". The
> script prints a one-line notice to that effect in `--db-path` mode. The
> **authoritative** check is the ephemeral self-contained smoke above (no
> `--db-path`); validating an arbitrary real genesis is out of scope (the cutover,
> story 08, is operator-executed).

> **On the prod box** (Windows): the DB is WAL-mode SQLite and `.env` is **not**
> auto-sourced. Run scripts that hit vendor APIs only after `source .env` (this
> check itself touches no vendor API — the broker boundary is mocked). See the
> [production runbook](RUNBOOK_production.md).

---

## When it fails

Each failing assertion prints a stable kebab-case `code` and a message naming the
violated invariant. Map the code back to the §8 item and the owning workstream:

- `genesis-reconciliation-alerts-present` → W2a deleted the adjudication path; a
  `RECONCILIATION_ALERT`/`CORRECTION` row means it regressed.
- `genesis-canary-not-attributed` / `-stranded` → the broker-carried link (02a)
  is not resolving the fill; the entry stranded into `unattributed_fills`.
- `genesis-projection-*` / `genesis-intent-*` → projection (W2a) or Intent (03c /
  04b) does not reflect the canary.
- `genesis-greeks-missing` → greeks did not land in the `position_greeks` side
  table (04b / ADR-0005).
- `genesis-floor-*` → the options capital floor (04c / ADR-0003) is not a resting
  `STOP_LIMIT`.
- `genesis-synthetic-broker-id-present` → an `alp-…` placeholder exists (invariant
  5 regression — the floor / a protective leg should carry NULL, not `alp-`).

---

## Test coverage

`tests/scripts/test_verify_genesis.py` drives each assertion against an in-memory
genesis fixture (the precondition for the cutover gate). The companion
`fresh_start` precondition (the bootstrap refuses a non-flat account — open
orders *and* positions empty) is covered by `tests/scheduler/test_fresh_start.py`.
