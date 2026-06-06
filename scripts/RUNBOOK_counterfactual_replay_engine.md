# Runbook — Counterfactual replay engine verify (`scripts/verify_counterfactual_replay_engine.py`)

A self-contained **end-to-end correctness check** for the out-of-pipeline
counterfactual replay engine (ALP-129). It seeds a controlled DB of PM-decision
rows + bar history + IV snapshots + (for the strategist paths) position state,
runs the engine driver
([`replay_pending_proposals`](../src/alphamind/execution/counterfactual_replay_engine/engine.py))
directly, and asserts each expected outcome against the returned
`ReplayBatchResult` and the persisted `counterfactual_replays` records, then
re-runs to confirm idempotency.

It is a **dedicated** check — deliberately *not* folded into
[`RUNBOOK_end_to_end_verification.md`](RUNBOOK_end_to_end_verification.md). The
replay engine runs **out-of-pipeline** (CLI only; see
[ALP-565](../src/alphamind/execution/counterfactual_replay_engine/cli.py)), so
the central debug-e2e gate (`scripts/verify_debug_e2e.py`) does **not** exercise
it. The two scripts are independent: the e2e runbook verifies a fully-seeded
pipeline run end-to-end; this verifies the replay engine's algorithm,
persistence path, and upstream contracts against a deterministic seed.

Design reference:
[`docs/design/05-execution-layer/counterfactual-replay-engine.md`](../docs/design/05-execution-layer/counterfactual-replay-engine.md).

---

## When to run

Run this verify:

- **After any code change to the engine** — the
  `execution/counterfactual_replay_engine/` work tree (eligibility, equity /
  option / strategist replay, confidence, record mapping, the driver, the CLI).
- **After a migration that touches `counterfactual_replays`** — a schema or
  CHECK-constraint change to the table, its codec, or its repository helpers.
- **Before merging a PR that modifies any of the engine's modules or its upstream
  contracts** — the analyst / strategist proposal models, the bar / options-
  snapshot / corporate-action repositories, or the as-of position-state
  reconstruction the strategist paths fold against.

It is a fast, no-vendor-API, no-live-account check; run it freely.

---

## How to run

### Self-contained smoke (no DB needed)

Provisions an ephemeral in-memory DB, materialises the full ORM schema, seeds the
eight documented proposals, runs the engine twice, and asserts the whole
checklist:

```bash
uv run python scripts/verify_counterfactual_replay_engine.py
```

Expected tail:

```
PASS | batch counts (first run)
PASS | per-envelope record outcomes
PASS | equity TARGET_HIT realized P/L
PASS | option TARGET_HIT evaluated record
PASS | PM-modified MODIFICATION_ORIGINAL_FORM record
PASS | strategist CLOSE realized P/L
PASS | ineligible strategy unevaluable
PASS | data-missing unevaluable
PASS | corporate-action-in-window unevaluable
PASS | second run is idempotent

verify counterfactual_replay_engine: PASS
```

Exit code: `0` = every assertion holds; `1` = at least one failed (each failing
check prints `FAIL | <assertion>: <detail>` above the result line).

### Against a specific DB

Provisions the schema at the supplied **throwaway** path (no separate migration
step needed), seeds its own controlled fixture rows, and asserts. Point it at a
fresh scratch path; the DB should be otherwise empty of the seeded envelope ids:

```bash
uv run python scripts/verify_counterfactual_replay_engine.py --db-path /path/to/test.db
```

> **On the prod box** (Windows): the DB is WAL-mode SQLite and `.env` is **not**
> auto-sourced. This check touches no vendor API, but it **writes** rows, so do
> not point `--db-path` at the production `alphamind.db`. Use a throwaway test
> DB. See the [production runbook](RUNBOOK_production.md).

---

## What it asserts

The seed is the **ground truth**: `build_test_seed` inserts the eight documented
proposals and returns a `ReplaySeed` carrying, per proposal, the expected
`replay_status` / `unevaluable_reason` / `exit_leg` and (for the two
analytically-derivable trades) the expected realized P/L. The eight proposals:

| # | Proposal | Expected outcome |
|---|---|---|
| 1 | PM-rejected analyst equity | EVALUATED, `exit_leg=target_hit`, positive P/L within tolerance |
| 2 | PM-rejected analyst option (long call) | EVALUATED, `exit_leg=target_hit`, non-null P/L, confidence in {high, medium, low} |
| 3 | PM-modified analyst | EVALUATED record with `replay_kind=modification_original_form` |
| 4 | Strategist CLOSE on an equity position | EVALUATED, `exit_leg=strategist_close_at_proposal`, P/L within tolerance |
| 5 | Strategist ADD on an option position | EVALUATED, `exit_leg=target_hit` |
| 6 | Ineligible strategy instrument | UNEVALUABLE, `unevaluable_reason=unsupported_instrument` |
| 7 | Data-missing (no bars) | UNEVALUABLE, `unevaluable_reason=data_missing` |
| 8 | Corporate-action-in-window | UNEVALUABLE, `unevaluable_reason=corporate_action_in_window` |

Plus a **second-run idempotency** check: re-running the engine writes no new
records and reports `skipped_idempotent` equal to the first run's total processed.

**P/L tolerance.** The engine driver passes `adv_shares=None` /
`realized_volatility=None`, so the paper harness returns no estimate and every
slippage / fee is zero. The equity TARGET_HIT P/L is therefore exactly
`(target - entry) * qty` and the strategist CLOSE P/L is exactly
`(exit_open - basis) * qty`; the verify checks each within a one-cent band that
absorbs only Decimal-rounding noise.

The **DB is the only mocked boundary** — there is no live-account or vendor-API
dependency. The seed drives the *production* engine driver, the real repositories,
and the real state codecs, so the assertions exercise the post-wave behavior, not
re-implementations of it.

---

## What pass / fail means

- **PASS** — the controlled seed produced exactly the expected outcomes: the
  engine algorithm (entry / bracket walk / P/L), the eligibility gate, the
  persistence path, and every upstream contract the seed builds against are
  intact.
- **FAIL** — a regression in one of: the engine algorithm, the
  `counterfactual_replays` persistence path, or an upstream contract the seed
  depends on (a renamed proposal-model field, a changed bar / IV / corporate-
  action repository shape, a drifted as-of position-state reconstruction).

---

## How to debug a failure

Each `FAIL | <assertion>: <detail>` line names the exact rule that broke:

1. **Read the failing assertion's detail.** It names the envelope, the expected
   vs actual status / reason / exit-leg / P/L. The assertion helpers live in
   `src/alphamind/scripts/verify_counterfactual_replay_engine.py` (the `assert_*`
   functions, each returning a failure message or `None`).
2. **Inspect the seed builder for the ground-truth expectations.** Each seeded
   proposal has a `_seed_*` helper and a matching `ExpectedProposal` entry in
   `build_test_seed`. The `ExpectedProposal` is what the proposal *should*
   produce; the `_seed_*` helper is the input that should produce it. A mismatch
   means either the engine changed behavior or the seed drifted from a changed
   upstream contract.
3. **A batch-error (`error_count > 0`) in the first-run counts assertion** prints
   the first per-proposal error (e.g. a Pydantic validation failure on a seeded
   proposal body) — that points at an upstream model contract the seed no longer
   satisfies.
4. **Re-run the unit tests** (`uv run pytest
   tests/scripts/test_verify_counterfactual_replay_engine.py -n auto`) to isolate
   whether the seed builder or an assertion helper regressed independently of the
   engine.

---

## v2 known limitations / confidence-pool caveats

The v2 replay engine makes deliberate modeling simplifications. These are the
operator-facing caveats for interpreting a replay's **confidence** and **P/L** —
none is a bug, and the verify's expectations are written to match them:

- **(a) Stop-limit gap entry is modeled at the limit price, not the gap-open.**
  When a stop-limit entry arms and fills, the fill is recorded at the limit
  price, not at an overnight gap-open that may have blown through it. A real fill
  through a gap could be worse; treat the replay entry as the optimistic
  limit-price bound.
- **(b) Same-bar ambiguity is flagged only for target-vs-stop.** When the target
  and the price stop both trigger within one bar, the engine assumes the stop
  filled first (conservative bias) and demotes confidence. It does **not** flag
  the analogous entry-and-target-in-one-bar or target-at-the-time-stop-bar
  ambiguities — those replays are not demoted on that basis.
- **(c) No-leg-fired exits record as `TIME_STOP_FIRED` at the final bar's open.**
  When no target, stop, or hard time leg fires anywhere in the replay window, the
  exit is recorded as `TIME_STOP_FIRED` at the open of the window's final bar (the
  thesis-duration deadline elapsing). Read a `time_stop_fired` exit as "the thesis
  horizon elapsed without a trigger", which includes this window-end fallback.
- **(d) As-of position-state reduce-fill sides use a terminal-net heuristic.** A
  persisted `PositionFill` carries only a magnitude (no per-fill buy/sell side),
  so the as-of reconstruction attributes the total reduce magnitude to the
  *latest* fills (newest-first), anchored on the position's current net quantity.
  This is correct for the open → add → reduce lifecycle the strategist replay
  measures against; an exotic interleaving of adds and reduces could mis-split the
  cost basis. The strategist CLOSE / ADD / REDUCE P/L inherits this attribution.

These caveats explain why a `low` / `medium` confidence stamp or a particular
exit leg appears, and why aggregated PM-accuracy queries exclude `low`-confidence
replays.

---

## Test coverage

`tests/scripts/test_verify_counterfactual_replay_engine.py` covers the
unit-testable surface: the seed builder (the queue yields exactly the eight
proposals) and every `assert_*` helper (each returns `None` on the real engine
output and a failure message on a tampered record / result). The **full
end-to-end run is exercised by invoking this script** — it is the
verify-of-the-verify, so the whole standalone run is not embedded in pytest.
