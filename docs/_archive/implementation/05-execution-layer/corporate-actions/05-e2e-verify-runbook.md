# 05 — E2E verify + runbook

## Goal

Ship `src/alphamind/scripts/verify_corporate_actions.py` exercising the corporate-actions pipeline end-to-end against a freshly-migrated DB with synthetic positions + synthetic `CorporateActionActivity` records covering every `CorporateActionType` member. The script prints a pass/fail summary an operator can read. Also ship `scripts/RUNBOOK_corporate_actions.md` with operator prerequisites, the verify-script invocation, expected output shape, and a failure-mode triage table. Insert the corporate-actions phase into `scripts/RUNBOOK_end_to_end_verification.md` in dependency order (after state-persistence + broker-adapter, before continuous-monitor when [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) lands).

## Reading

* `src/alphamind/scripts/verify_state_persistence.py` — sibling pattern: phases, seeded fixtures, pass/fail summary table
* `src/alphamind/scripts/verify_broker_adapter.py` — sibling pattern from [ALP-393](https://linear.app/alphamind-jatassi/issue/ALP-393/05-e2e-verify-runbook); live-Alpaca-vs-synthetic decision
* `scripts/RUNBOOK_state_persistence.md` — sibling runbook section structure (Prerequisites / Invocation / Expected output / Failure-mode triage)
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook's dependency-ordered phase list (where to insert corporate-actions)
* `docs/design/05-execution-layer/corporate-actions.md` (all sections) — the verify script exercises every documented behavior in the per-action-type matrix

## Depends on

* `ALP-415` (story 04) — Phase 1 entry-point reworked + reconciliation step in place

## Scope

In scope: `src/alphamind/scripts/verify_corporate_actions.py`, `scripts/RUNBOOK_corporate_actions.md`, edit to `scripts/RUNBOOK_end_to_end_verification.md`.

### 1\. `verify_corporate_actions.py`

Phases:

* **Phase A — Seed.** Migrate a fresh DB; insert one equity-long position, one equity-short position, one options-long position (call), one strategy position (vertical spread). Seed `cash_ledger` + `drawdown_state` singletons. Use the sibling `_fk_substrate` pattern from `tests/execution/state_persistence/`.
* **Phase B — Construct CA activities.** Build synthetic `CorporateActionActivity` records covering all 9 `CorporateActionType` members. For each, construct the corresponding `PositionSnapshot` records the handlers will look up via `AlpacaPositionLookup`. Build the `TradeAccountSnapshot` reflecting the expected post-CA cash balance.
* **Phase C — Run Phase 1.** Invoke `process_unprocessed_fills(handle, ca_activities, alpaca_positions, alpaca_account, config=...)` inside an `InvocationContext`. Commit.
* **Phase D — Assert.** For each action type, assert the post-state matches the design's per-action-type matrix: quantity, cost basis, ticker, status, cash impact, and `corporate_action_adjustment_needed`. Assert one `corporate_action_integration_ledger` row per `alpaca_activity_id`. Assert `RECONCILIATION_ALERT` activity-log entries are emitted only when synthetic Alpaca snapshots deliberately diverge from expected post-state (one variant test path).
* **Phase E — Print.** Pass/fail table with one row per `CorporateActionType` member + a reconciliation summary line (deltas detected / alerts emitted). Exit code 0 on all-pass, 1 on any-fail.

### 2\. `RUNBOOK_corporate_actions.md`

Four sections:

* **Prerequisites.** Fresh migrated DB; `config/main.yaml` configured; no live Alpaca calls in verify mode (verify uses synthetic snapshots, not the live API).
* **Invocation.** `uv run python -m alphamind.scripts.verify_corporate_actions [--db-path PATH]`.
* **Expected output shape.** Sample pass output: 9-row table with one row per action type, all PASS; reconciliation summary line showing 0 alerts emitted.
* **Failure-mode triage table.** Mapping of common failure messages to root causes (e.g., "missing ledger row for activity X" → "handler didn't call `mark_ca_activity_processed`"; "unexpected `RECONCILIATION_ALERT`" → "handler's post-state diverged from the synthetic Alpaca snapshot expected by the verify script"; "spin-off invariant validator raised" → "child `PositionRecord` constructed with mismatched (origin, parent_position_id, corporate_action_adjustment_needed) triplet").

### 3\. `RUNBOOK_end_to_end_verification.md` update

Insert the corporate-actions phase into the central runbook's dependency-ordered phase list at the position immediately after state-persistence + broker-adapter and before continuous-monitor (when [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) lands). Single-paragraph entry naming the script invocation, expected output shape, and the cross-tree handoff (the chronological-merge step depends on State persistence + Broker adapter being verified clean upstream).

### Out of scope

Live Alpaca integration (the verify script uses synthetic `PositionSnapshot` and `TradeAccountSnapshot` records, not the live API). Live integration verification belongs with [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) (continuous monitor) when it lands and wires the fetcher to production.

## Acceptance criteria

- [ ] `src/alphamind/scripts/verify_corporate_actions.py` exists and runs end-to-end against a freshly-migrated test DB.
- [ ] The script seeds equity-long, equity-short, options, and strategy positions.
- [ ] All 9 `CorporateActionType` members are exercised by the verify script.
- [ ] Each per-action assertion matches the design doc's per-action-type matrix (quantity, cost basis, ticker, status, cash impact, flag).
- [ ] One variant path deliberately diverges the synthetic Alpaca snapshot from expected post-state to trigger a `RECONCILIATION_ALERT`; the script asserts the alert is emitted.
- [ ] The script prints a 9-row pass/fail table plus a reconciliation summary line; exit code 0 on all-pass, 1 on any-fail.
- [ ] `scripts/RUNBOOK_corporate_actions.md` exists with Prerequisites / Invocation / Expected output / Failure-mode triage sections.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is updated to include corporate-actions in the dependency-ordered phase list at the documented position.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run python -m alphamind.scripts.verify_corporate_actions` against a freshly-migrated test DB. Inspect the printed pass/fail table; all 9 rows should report PASS, and the reconciliation-summary line should show one alert emitted (from the deliberate-divergence variant).