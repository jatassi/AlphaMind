# Handoff — scope investigation for SHORT-equity enablement

**Prepared by**: prior session, 2026-05-27
**Audience**: investigation agent in a fresh session
**Output expected**: a scope report (markdown) — NO code changes, NO Linear filings — the operator reads the report and decides between (a) single tactical issue, (b) feature work-tree with sub-stories, or (c) further investigation.

---

## What you're being asked to do

Scope the engineering work needed to **implement the SHORT-equity write path end-to-end** in AlphaMind. The system currently rejects SHORT-equity OPEN at the OMS boundary (see `_validate_equity_direction` at `src/alphamind/commands/command_models.py:488-506`, landed by ALP-644 / PR #165). That guard was always meant to be temporary — its own comment says: *"Retire this guard when the option-A SHORT-equity write path (borrow modeling + SHORT entry/exit math) lands."* That trigger is now firing: the operator wants SHORT equity to actually work, not to add a second mirroring guard upstream.

Your job is to read the relevant files, understand the actual surface of changes, and report a clear scope. **Do not write code. Do not file Linear issues.** Produce a scope report that lets the operator pick between issue-shaped and feature-shaped breakdowns.

---

## Background — why this matters now

Production first-time bootstrap was attempted 4 times in this session (2026-05-27). Attempts 0–3 each surfaced a different wiring gap; the fixes landed as ALP-711, PR #222 (latency budgets), and ALP-712. **Attempt 4 completed cleanly** (exit 0, ~36 min) — pipeline reached the PM, the PM proposed exactly one envelope (a CRWD short conviction-3 at 2.45%), evaluated all five envelope criteria as PASS, then `submit_envelope` failed 3 retries inside the PM harness:

1. Pydantic schema invariant: `leg_id` extra field on `invalidation_legs` (PM stripped on retry).
2. Pydantic schema invariant: `order_parameters` extra field on event-type invalidation legs (PM stripped on retry).
3. **Business-rule rejection** at `_validate_equity_direction` — `"OpenCommand does not support SHORT equity entry: the Phase 1 write path has no SHORT entry-fill helper and EquityPositionDetails carries no borrow-cost field. Use a single-leg short option or a net-credit options strategy to express a bearish view on the underlying."`

The PM gave up after 3 retries and emitted a clean `verdict: REJECT`. Result: `commands_submitted: 0`. The PM's own `rationale_narrative` named the underlying defect: *"the analyst's pre-validation tool (`validate_guardrail`) is returning PASS on a SHORT equity instrument that the OMS write path categorically refuses — this validator/executor inconsistency is the underlying defect that allowed an unexecutable proposal to surface here."*

The first fix instinct (added by the prior session as ALP-714, then **canceled**) was: add a mirror of ALP-644's guard to `validate_guardrail` so the analyst sees FAIL upfront. The operator redirected: *"We should instead enable SHORT EQUITY so that it actually works."* That's the work you're scoping.

**Operational stakes**: the production scheduler / monitor / command-center services are held in `Stopped` startup state pending this fix. Until SHORT equity executes correctly, every pre-open cycle in which the analyst surfaces a SHORT-equity thesis (which it now does routinely — see the CRWD + SCHW examples in attempt 4's analyst output) wastes ~35 min of compute and produces no positions.

---

## Concrete files to read

You'll want to start here and let the imports lead you outward:

### Core write-path surface

* **`src/alphamind/commands/command_models.py`** — focus on:
  * `_validate_equity_direction` at lines 488-506 — the OMS-boundary guard added by ALP-644 / PR #165. Comment names the retire-trigger.
  * `OpenCommand` model and its sibling validators around lines 466-485 — adjacent rejector (`_validate_strategy_target_type`) for pattern reference.
  * `EquityInstrument` type and the `direction` field shape.
* **`src/alphamind/execution/write_paths/phase1.py`** — focus on:
  * `_apply_fill_to_equity_position` at lines 602-617 — currently raises `NotImplementedError` on the `not is_buy_side and PENDING` branch. This is the missing SHORT entry helper.
  * `_apply_entry_fill` — the LONG entry case. The SHORT-entry helper will need to mirror this with sign flips.
  * `_apply_add_fill` — LONG add case. SHORT-add case is missing.
  * `_apply_exit_fill` — the sell-side exit handler. Need to verify: does it correctly handle cover-to-close (BUY-side exit of a SHORT position) with correct P/L sign convention? Or does it assume LONG?
  * `_apply_fill_to_position` at line 593 — the dispatcher that routes to `_apply_fill_to_equity_position` vs `_apply_fill_to_options_position`.
  * `_apply_fill_to_options_position` at line 620+ — the **already-working** SHORT-options reference. The docstring at 629-631 explicitly says SHORT options are first-class; the asymmetry with equity is intentional pending this fix.
  * `_integrate_one_fill` at line 445 — the caller, threading `direction_is_buy = direction_to_side(order.direction) == "buy"`.

### Position model + persistence

* **`src/alphamind/portfolio_state/records/positions.py`** — focus on:
  * `EquityPositionDetails` — currently has no borrow-cost field. Compare with `OptionsPositionDetails` for shape reference. Consider what borrow-cost fields are needed: `daily_borrow_cost_usd` (the rate), `accrued_borrow_cost_usd` (cumulative), `last_borrow_cost_resolved_at` (provenance), or some subset.
  * `Direction` enum and how it's stamped on positions vs orders.
  * `PositionStatus` enum (`PENDING` → `OPEN` → `CLOSED`) — verify SHORT positions follow the same lifecycle.
* **Alembic migrations** under `migrations/versions/` — find the most recent migration to see the convention (`b8c9d2e4f7a1` is currently head). Any borrow-cost field on `EquityPositionDetails` needs a new migration; check the `position_details_json` column storage pattern (it's JSON-on-row in some places, separate table in others).

### Guardrail + pre-processor wiring (already done, for context)

* **`src/alphamind/risk_guardrails/state_delivery/validation_tool.py`** — focus on:
  * `_resolve_borrow_cost` and the `MISSING_BORROW_COST` short-circuit path (around lines 690-710).
  * `_short_circuit_result` at lines 648+ — the existing precedent for a structured-FAIL with explanatory message.
  * Profile-flag gate at lines 643-644 (`is_short and not flags.short_selling_enabled`) — the coarse short-selling toggle. Decide whether SHORT-equity-enabled is one flag or two (equity + options separately).
* **`src/alphamind/decision/proposal_pre_processor/`** — ALP-712 landed `borrow_cost_resolver` threading into `compute_combined_set_impact` and `translate_recommendation_to_proposed_delta`. Read `translator.py` and `runner.py` to see the pattern. Files: `translator.py`, `runner.py`, `observations.py` (per the PR #223 diff).

### Reg T margin attribution (already supports shorts, for verification)

* **`src/alphamind/execution/regt_margin_attribution/`** — ALP-126 / ALP-424. Per-leg formulas: long equity = 50% MV, short equity = 150% MV. Verify the short-equity formula and `regt_attribution` field stamping is wired through the fill-orchestrator hook for SHORT equity entries too.

### Data sources (already populated, for context)

* `iborrowdesk.borrow_cost` collector — feeds `borrow_cost_daily` table. ALP-586 wired the resolver but only into the guardrail-state layer (`validation_tool.py`). ALP-712 extended into the pre-processor. The execution write-path is the last unwired hop.

### Tests as design hints

* `tests/execution/write_paths/test_phase1.py` (or similar — find via Glob) — see how LONG equity entry-fill is tested.
* `tests/execution/oms/test_submit_envelope_mcp.py` — submit-envelope integration.
* `tests/risk_guardrails/state_delivery/test_validation_tool.py` — validator behavior on SHORT (currently profile-flag-gated).
* `tests/commands/test_command_models.py` (if exists) — direct tests of `_validate_equity_direction`.

### Related landed issues (read for design context)

* **ALP-644** (Done, PR #165) — OMS-side guard. Read the full body for approach (A) vs (B) discussion — approach (A) is what you're scoping.
* **ALP-712** (Done, PR #223) — pre-processor `borrow_cost_resolver` wiring. Read the diff (`git show 2f32fb6d`) for an example of the in-progress shape of borrow-cost threading.
* **ALP-586** (Done) — initial `borrow_cost_resolver` wiring (guardrail state only).
* **ALP-581** (Done) — prior CSCO short proposal dropped pre-ALP-586. Same shape as the CRWD example in attempt 4.
* **ALP-647** (Done) — CLOSE/ADJUST on disabled options/shorts unblocked. Relevant if SHORT-equity profile toggle splitting is in scope.
* **ALP-126 / ALP-424** (Done) — Reg T per-leg margin formulas (short equity = 150% MV).

---

## Questions the scope report needs to answer

### Surface

1. **Schema additions to `EquityPositionDetails`**: which borrow-cost fields are needed, and what's the storage pattern (JSON-on-row vs separate table)? Does the schema also need a `borrow_cost_state_id` foreign key, or is daily cost stamped directly?
2. **`_apply_fill_to_equity_position` SHORT entry**: can it mirror `_apply_entry_fill` with sign flips, or does the borrow-cost field require non-trivial extra plumbing? Cite specific lines from the existing LONG path that need their LONG vs SHORT assumption examined.
3. **`_apply_exit_fill` cover-to-close**: does the existing handler correctly invert P/L sign for SHORT positions, or does it assume LONG? Read the code and confirm.
4. **`_apply_add_fill` SHORT add**: same question — does it generalize, or does adding to a SHORT position require its own helper?
5. **Borrow-cost lifecycle**: where does daily accrual live? Options:
   * (a) Continuous monitor cron writing a `BORROW_COST_ACCRUED` activity-log event per day per held SHORT position.
   * (b) Per-fill stamp at entry + nightly mark via a new write-path entrypoint.
   * (c) Stub at zero for v1; file a follow-up for the accrual lifecycle under ALP-123 (continuous monitor).
   * The right answer depends on whether you can get from "fill recorded with daily_borrow_cost_usd stamped" to "P/L correctly reflects accrued cost" without a separate accrual loop. Read `_compute_position_pnl` (or equivalent — find it) to see if the P/L computation already accumulates daily borrow cost from `position_details.daily_borrow_cost_usd × days_held`, or if it expects a pre-accumulated field. **This is the highest-uncertainty design question.**
6. **Pre-processor wiring already done**: ALP-712 already threads `borrow_cost_resolver` into `compute_combined_set_impact`. Verify what's actually needed downstream — does the pre-processor pass the resolved cost into the envelope, or does the execution side re-resolve at fill time? If the latter, the resolver needs to be available inside `_apply_fill_to_equity_position` (probably as a constructor argument on the Phase 1 orchestrator).
7. **Reg T short-equity formula coverage**: confirm `_compute_regt_attribution` (or equivalent — find it in `regt_margin_attribution/`) already supports SHORT equity entries and the fill-orchestrator wedge stamps `FillRecord.regt_attribution` correctly for SHORT-side fills. ALP-424 says it does; verify.
8. **Profile flags**: is `short_selling_enabled` granular enough, or does it need splitting into `short_equity_enabled` / `short_options_enabled`? Currently it's a single coarse flag (`validation_tool.py:643-644`). Operator preference is probably one flag (`short_selling_enabled` covers both, with this work the equity path no longer 404s), but call it out either way.
9. **Tests**: what's the minimum test surface? Almost certainly:
   * Unit: `_apply_fill_to_equity_position` SHORT entry + SHORT add + cover-to-close
   * Unit: `EquityPositionDetails` borrow-cost field default + serialization round-trip
   * Unit: `_compute_position_pnl` (or equivalent) correctness on SHORT with borrow cost
   * Unit: `_validate_equity_direction` removed; SHORT-equity `OpenCommand` constructs without raising
   * Integration: end-to-end OPEN → fill → OPEN-status SHORT equity position with stamped borrow_cost
   * Integration: cover-to-close → CLOSED status with realized P/L sign correct
   * Regression: existing LONG equity tests still pass
10. **Removals**:
    * `_validate_equity_direction` from `command_models.py:488-506` (the ALP-644 guard) deletes entirely.
    * Any analyst-side downstream tests asserting SHORT EQUITY is rejected need updating.
    * The `validate_guardrail` profile-flag gate (`validation_tool.py:643-644`) may stay or move depending on Q8 above.

### Risk

1. **Highest-uncertainty risks**:
   * Borrow-cost accrual lifecycle (Q5) — easy to get wrong silently, hard to verify without backtesting.
   * P/L sign conventions on cover-to-close — easy to land with a sign-flip bug that produces inverse P/L on every SHORT position.
   * Reg T attribution on SHORT entries — if not wired correctly, `regt_excess_over_pm` will be wrong for any portfolio holding shorts.
2. **Lower-risk items**:
   * Schema migration (additive only).
   * Removing the OMS guard (single deletion).
   * SHORT entry-fill helper (pattern-matches LONG entry).

### Sizing recommendation

After answering the surface questions, recommend one of:

* **(A) Single tactical issue** — like ALP-712, all-in-one PR. Suitable if the borrow accrual lifecycle (Q5) can be stubbed or already exists in a usable shape, AND the cover-to-close sign handling (Q3) is already correct.
* **(B) Single tactical issue with one explicit follow-up** — main PR handles the entry/add/exit + Reg T + schema + OMS guard removal; follow-up issue tracks the daily accrual lifecycle under ALP-123 continuous monitor.
* **(C) Feature work-tree with sub-stories** — only if the surface is bigger than expected (e.g., the P/L computation needs a non-trivial rewrite, the schema needs a separate table, multiple existing call sites need migration). 4-6 sub-stories per the `/draft-user-stories` skill.

Cite specific code reads (file + line) for each recommendation so the operator can verify.

---

## Constraints

* **No code changes.** This is investigation. Do not edit `src/`, do not write tests, do not run migrations.
* **No Linear filings.** The operator decides what to file after reading your report. ALP-714 has been canceled and will not be reused.
* **Do not run `pytest -n auto` against the full suite** (per `CLAUDE.md`). If you need to verify a specific behavior, run a scoped pytest (`uv run pytest tests/<file>::<test> -n auto`).
* **Do not start the production services** (`alphamind-scheduler`, `alphamind-monitor`, `AlphaMindCommandCenter` are intentionally Stopped — see "Operational stakes" above).
* **Do not modify the prod DB** at `data/alphamind.db`. Read-only inspection is fine.
* **Treat the existing archive** at `archive/2026-05-27/inv-20260527T162007Z-9d754a59/` as read-only evidence — useful for examples but not for replay.

---

## Deliverable

A single markdown file at `docs/_handoffs/short-equity-enablement-scope-report.md` containing:

1. **Surface** — concrete file:line-grounded answers to questions 1–10 above.
2. **Risk** — top 3 risks ranked by severity × likelihood, each with mitigation.
3. **Recommended scope** — one of (A), (B), (C) above, with rationale.
4. **Open questions for operator** — anything you couldn't resolve from code-reading alone.
5. **Next-step proposal** — a draft Linear issue body (or feature parent-issue body) for the operator to review.

Aim for ~500–1500 lines of markdown depending on what you uncover. Be specific. Cite code.

---

## Memory pointer

This session updated `~/.claude/projects/C--Users-jacks-AlphaMind/memory/project-prod-blocked-on-alp-711.md` with the attempt-4 outcome and resume sequence (which now needs the SHORT-equity work to land before services start). Your scope report doesn't need to update memory — the operator will revise the project memory once the implementation issue is filed.
