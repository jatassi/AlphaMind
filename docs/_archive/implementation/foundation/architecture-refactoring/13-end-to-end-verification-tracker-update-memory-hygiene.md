# 13 — End-to-end verification + tracker update + memory hygiene

## Goal

Final story of the work tree. Verifies the refactor end-to-end against the audit's measurable criteria (cycle count, L9/L12 counts, lint-imports green), updates `docs/project-tracker.md` to mark the refactor done, and applies the memory hygiene update flagged in audit W7 (pm.md prompt-format compatibility memory entry is stale; the issue was already resolved).

This story is the gate: after it lands, the parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> can move to Done.

## Reading

* `audit-alphamind-2026-05-12.html` — the audit this work tree implements; verification criteria across the load-bearing findings
* Every other story in this work tree (01a–12b)
* `.claude/projects/-Users-jatassi-Developer-AlphaMind/memory/feedback_prompt_output_format_compat.md` — memory entry to update
* `docs/project-tracker.md` — tracker to update

## Depends on

* 12a (<issue id="ab6058d1-db61-4ee1-919b-cc962b789bef">ALP-479</issue>) — vendor Protocols / fakes; final test substrate
* 12b (<issue id="7fce3e74-4dff-4237-b296-29ecbfa470cf">ALP-480</issue>) — narrow excepts; final code-quality polish

(Implicitly depends on the entire work tree via these — they're the leaves of the dependency graph.)

## Scope

In scope: cross-cutting verification per audit criteria; tracker update; memory hygiene. No new code.

### 1\. End-to-end verification

Run each verification gate per the parent issue's "Notes for the orchestrator":

* **Cycle elimination:** `python .claude/skills/python-architecture/scripts/analyze_imports.py src/alphamind | jq '.cycles | length'` returns ≤2 (the two small internal cycles in `config.models.data_sources` ↔ `config.models` and `distillation.calibration_snapshot` ↔ `distillation.orchestrator` should also be resolved by 02a's work; verify); the 10-module and 12-module cycles are gone.
* **Money migration:** `grep -rn ": float" src/alphamind/portfolio_state/snapshot.py src/alphamind/decision/*/models.py src/alphamind/commands/command_models.py` returns zero hits for money fields.
* **Pydantic→dataclass:** antipattern scanner L9 count drops to the warranted-boundary count (~180 was the audit's triage of warranted; actual count after Wave 10 should be in that range).
* **Async hygiene:** L19 count drops to the Protocol-stub + SDK-tool-decorator residue (~70).
* **Import-linter:** `uv run lint-imports` passes with the full set of contracts from story 03.
* **Full test suite:** `uv run pytest -n auto` passes; runtime measurably improved (mock removal in 12a, async-strip in 08a).

Author a one-page audit summary at `docs/audit-summary-2026-05-12.md` capturing the before/after counts for the headline findings.

### 2\. Tracker update

`docs/project-tracker.md` — update the "Architecture refactoring (2026-05-12 audit)" bullet under "Operational tooling" from `_in progress_` to `_done_`. Add a 2-3 sentence summary of what landed.

### 3\. Memory hygiene (folded in from former story 12c)

Update `/Users/jatassi/.claude/projects/-Users-jatassi-Developer-AlphaMind/memory/feedback_prompt_output_format_compat.md`:

* Remove the residual "pm.md still carries it" line (the audit verified it was already resolved at `prompts/decision/pm.md:121`).
* Mark the entry as historical / resolved per the audit's W7 finding.

### 4\. (Optional) Filed follow-ups

Story 07's distillation compute/load propagation to q6, q7, qualitative may have been scoped as follow-up issues (per its Out-of-scope). This story's completion confirms each is filed in Linear under the same parent or as new bullets in project-tracker.

### Out of scope

Additional refactoring beyond what the audit identified. If new findings surface during verification (e.g., a cycle that wasn't in the audit but appears now), file as a new issue under "To-dos"; don't add to this work tree mid-flight.

## Acceptance criteria

- [ ] `analyze_imports.py` reports 0 cycles passing through `portfolio_state.aggregates`, `risk_guardrails.*types`, `distillation.calibration`, `persistence.models`.
- [ ] `analyze_imports.py` reports 0 cycles passing through `decision.portfolio_manager.*` and `execution.{oms,state_persistence}.*`.
- [ ] `grep -rn ": float" src/alphamind/{commands,decision/*/models.py,portfolio_state/snapshot.py,portfolio_state/events,execution/broker_adapter/queries.py}` returns no money-field hits.
- [ ] `uv run lint-imports` passes; the full set of contracts from stories 01a + 03 + later additions are enforced.
- [ ] `uv run pytest -n auto` passes.
- [ ] `docs/audit-summary-2026-05-12.md` exists with before/after counts for headline findings.
- [ ] `docs/project-tracker.md` bullet updated from `_in progress_` to `_done_`.
- [ ] `feedback_prompt_output_format_compat.md` memory entry updated per W7.
- [ ] Any follow-ups filed are linked from the audit summary doc.

## Verification

End-to-end smoke test: a full pipeline invocation runs cleanly with the refactored types; the activity log round-trips a sequence of OPEN/CLOSE/ADJUST events with exact Decimal precision; the continuous monitor restart drill works under TaskGroup-based supervisor; `lint-imports` runs cleanly in CI. The parent issue's verification gates (Money, cycles, Pydantic, async) all pass.