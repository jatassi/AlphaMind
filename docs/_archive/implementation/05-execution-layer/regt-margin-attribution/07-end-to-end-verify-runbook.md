# 07 — End-to-end verify + runbook + central RUNBOOK insertion

## Goal

Ship `scripts/verify_regt_margin_attribution.py` (operator-runnable, exercises a representative golden path producing a printed `RegTMarginAttribution` plus a printed `CashLedger.regt_excess_trailing_*` triple), `scripts/RUNBOOK_regt_margin_attribution.md` (operator-facing prerequisites + invocation + failure-mode triage), and an insertion into `scripts/RUNBOOK_end_to_end_verification.md` placing this feature into the dependency-ordered phase list (after state-persistence + corporate-actions, before continuous-monitor).

## Reading

* `scripts/verify_state_persistence.py` — reference for the verify-script narrative: seeded fixtures, invocation-context handling, pass/fail summary printed at the end.
* `scripts/RUNBOOK_state_persistence.md` — reference for runbook structure: prerequisites, invocation, expected output shape, failure-mode triage table.
* `scripts/RUNBOOK_end_to_end_verification.md` — the central runbook this feature inserts into. Walk it to identify the correct phase-ordered position.
* `docs/design/05-execution-layer/regt-margin-attribution.md` § Outputs and § Aggregation — what the verify script exercises and prints.
* `src/alphamind/execution/regt_margin_attribution/__init__.py` (from stories 01a–05) — re-exports the math primitives the script consumes.
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` (from story 06a, with the wedge) — invocation surface the script drives.
* `src/alphamind/portfolio_state/assembler.py` (from story 06b, with Step 11 aggregation) — destination of the trailing-window assertion.
* `ALP-126` parent issue § Surfacing conditions — informs the runbook's failure-mode triage table.

## Depends on

* `ALP-428` (06a — Phase 1 wiring) — the wedge must exist for the verify script to drive end-to-end.
* `ALP-429` (06b — Assembler trailing-window aggregation) — the trailing-window assertion needs the aggregator wired.

## Scope

In scope: `scripts/verify_regt_margin_attribution.py`, `scripts/RUNBOOK_regt_margin_attribution.md`, and an in-place edit to `scripts/RUNBOOK_end_to_end_verification.md`.

### 1\. `scripts/verify_regt_margin_attribution.py`

End-to-end script that:

 1. Loads `RegTMarginAttributionConfig` via `load_regt_margin_attribution_config()`.
 2. Connects to a test SQLite DB at `--db-path` (or a tmp default if absent).
 3. Seeds a representative portfolio: one long-equity position (NVDA), one short-equity position (AMD), one long-call position (SPY), one strategy position (NVDA bull call spread).
 4. Constructs a representative `MarketInputs` with seeded `underlying_prices` and a `FixtureIvProvider` carrying IV entries for every option leg.
 5. Seeds two unprocessed fills (a buy fill on NVDA, a sell fill on AMD).
 6. Invokes `process_unprocessed_fills(handle, market_inputs=..., config=...)` inside an `InvocationContext`.
 7. Reads the resulting fill rows; rehydrates `RegTMarginAttribution` from `regt_attribution_json`; prints the 8 fields for each fill in a human-readable block.
 8. Invokes the public assembler entry point; reads `enriched_cash.regt_excess_trailing_30d_usd / 90d_usd / lifetime_usd`; prints them.
 9. Asserts: all 8 fields are finite per fill; `regt_excess_over_pm == regt_marginal_consumption − pm_marginal_consumption` within `1e-6`; the trailing-30d aggregate equals the sum of the two fills' `regt_excess_over_pm` values within `1e-6`.
10. Prints `PASS` (exit 0) or a structured `FAIL` block (exit 1) naming the failing assertion.

Mirrors the structure of `scripts/verify_state_persistence.py`: argparse with `--db-path`, `--invocation-id`, `--verbose`; logging through `alphamind.config.logging`; no surprises.

### 2\. `scripts/RUNBOOK_regt_margin_attribution.md`

Operator-facing markdown with the standard structure used by other feature runbooks:

* **Purpose** — one paragraph naming what the verify script exercises and what "PASS" tells the operator.
* **Prerequisites** — `.env` loaded (`source .env`), `uv` available, optional `--db-path` for a non-default location, Python `>=3.13`. Explicitly mention the `load_dotenv()` requirement per memory `feedback_handover_env_loading`.
* **Invocation** — exact command line: `uv run python scripts/verify_regt_margin_attribution.py --db-path /tmp/regt_verify.db --invocation-id verify-regt-001`.
* **Expected output** — shape of the printed `RegTMarginAttribution` block per fill + the trailing-window block + the final `PASS`.
* **Failure-mode triage** — table mapping common failure messages to root causes:
  * `IvLookupError` → option leg's `(strike, expiration)` is missing from the seeded `FixtureIvProvider`; extend the fixture.
  * `KeyError: 'SYMBOL'` → underlying-price entry missing from `MarketInputs.underlying_prices`; extend the fixture.
  * `regt_attribution_json IS NULL` on a processed fill → Phase 1 wedge regressed; check `process_unprocessed_fills` body.
  * Trailing-30d aggregate is zero with non-zero per-fill excess → `get_regt_excess_aggregates` regression or assembler Step 11 not wired.
  * `pm_model_version` mismatch → `config/regt_margin_attribution.yaml` was refreshed without updating the version string; rerun.

### 3\. Insert into `scripts/RUNBOOK_end_to_end_verification.md`

Add a new phase entry under the dependency-ordered list. Position: after `verify_corporate_actions` (or wherever corporate-actions landed) and before `verify_continuous_monitor`. Entry shape (markdown):

* Run: `uv run python scripts/verify_regt_margin_attribution.py --db-path "$VERIFY_DB" --invocation-id verify-regt-001`
* Expects: PASS with 8-field `RegTMarginAttribution` printed per fill and three-field trailing-window aggregate printed.
* Stage artifact: the seeded DB at `$VERIFY_DB` carries `fill_records.regt_attribution_json` populated rows that downstream phases (e.g., command-center verify) may read.
* On failure: see `scripts/RUNBOOK_regt_margin_attribution.md`.

### Out of scope

* Production wiring of `MarketInputs` — continuous-monitor's job ([ALP-123](<https://linear.app/alphamind-jatassi/issue/ALP-123>)).
* Refreshing the IBKR shock parameter table — operator-driven via the yaml; documented in the runbook's expected output section but not auto-refreshed by the script.
* Property-based / hypothesis testing — out of scope for the v1 verify script.

## Acceptance criteria

- [ ] `scripts/verify_regt_margin_attribution.py` exists and exits 0 on the golden path.
- [ ] The script prints a `RegTMarginAttribution` block per processed fill with all 8 fields labelled.
- [ ] The script prints the trailing-30d / 90d / lifetime aggregate triple after the per-fill blocks.
- [ ] On any internal assertion failure, the script exits 1 with a structured FAIL block naming the failing assertion.
- [ ] `scripts/RUNBOOK_regt_margin_attribution.md` exists with the five sections in Scope §2.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` contains the new phase entry positioned per Scope §3.
- [ ] The verify script handles `.env` loading (or its docstring + runbook explicitly require `source .env` before invocation, per memory `feedback_handover_env_loading`).
- [ ] `uv run python scripts/verify_regt_margin_attribution.py` (with default args) runs cleanly on a fresh tmp DB.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` clean.

## Verification

Run `uv run python scripts/verify_regt_margin_attribution.py --db-path /tmp/regt_verify_$$.db --invocation-id verify-regt-001 --verbose` and confirm: (a) exit code 0; (b) 8-field `RegTMarginAttribution` block printed for each of the two seeded fills; (c) trailing-window aggregates equal the sum of the per-fill excess values; (d) `PASS` line printed last. Spot-check the runbook contains a failure-mode triage table.