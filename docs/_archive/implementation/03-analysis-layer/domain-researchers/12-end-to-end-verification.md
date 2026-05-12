## Goal

Produce verification scripts and a runbook proving the domain-researcher layer works end-to-end against a populated database and a live SDK: the parallel orchestrator runs to completion, three sector briefs render, all three validate cleanly, the diagnostic archive is populated, and the wall-clock + token usage stay within the documented budget.

## Reading

* `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup.
* `docs/design/cost-and-rate-limit-modeling.md` — per-invocation token budgets the verification asserts against; load values from there rather than hardcoding.
* `docs/architecture/infrastructure.md` § Layer 2 invocation archive — file paths the script verifies.
* `src/alphamind/distillation/orchestrator.py` — `run_external_distillation()` signature, `DistillationOutputs` shape this verification consumes.
* `src/alphamind/analysis/domain_researchers/orchestrator.py` (story 11) — `run_domain_researchers()` and `DomainResearchersOutput`.
* All prior stories in this work tree — the contracts being verified.
* `docs/design/testing/llm-output-validation.md` — the four-layer validation stack the verification exercises.

## Depends on

* **11** (<issue id="2ef140a0-9cd6-4b87-b4fe-f28d1071cfa2">ALP-190</issue>) — `run_domain_researchers`, `DomainResearchersOutput`.

## Scope — scripts/verify_domain_researchers.py

Connects to the SQLite database, runs `run_external_distillation()` to obtain `DistillationOutputs` for the current `as_of` (or reads its most recent archive snapshot), then calls `run_domain_researchers()` with a real `CLAUDE_CODE_OAUTH_TOKEN`.

Asserts:

* `DomainResearchersOutput` has all three populated `DomainResearcherResult` fields (`tech_semis`, `financials`, `energy`).
* Each sector's `brief` is a valid `SectorBrief` (already enforced by the runner; this is a re-assertion against the returned value).
* Each brief's reference IDs are sequential per section starting at 1 (re-running the validator against the returned briefs).
* The diagnostic archive directory `<archive_root>/invocations/<invocation_id>/analysis/{tech_semis,financials,energy}_researcher/` contains the documented files (`prompt.md`, `user_message.md`, `response_initial.md`, `metadata.json`).
* Total wall-clock seconds stay under the budget loaded from `cost-and-rate-limit-modeling.md` (do not hardcode the threshold; read from config or design-doc loading helper).
* Total tokens used stay under the per-invocation analysis-layer budget loaded from `cost-and-rate-limit-modeling.md`.

Reports a summary table: per-sector wall-clock, tokens used, retry count, finding count, anomaly count, thesis-candidate count, signal quality. Exits 0 on all assertions passing; exits 1 with a per-failure report otherwise.

## Scope — scripts/verify_domain_researcher_failure_modes.py

Runs three injection scenarios end-to-end against a stubbed harness (no real SDK calls — fast and deterministic):

1. Force-inject a parse-failure response on the first SDK call → assert the corrective retry fires, the second response succeeds, `retry_count=1` on the result.
2. Force-inject two consecutive parse failures → assert `MalformedOutputFailure` propagates as a fail-closed exception with the sector tag.
3. Force-inject a validation failure on the first call, clean response on retry → assert `retry_count=1` and the output is the second response.

Uses the harness's diagnostic-archive reader to confirm the correct archive files were written for each scenario. Exits 0 on all scenarios passing.

## Scope — runbook

A short markdown file `scripts/RUNBOOK_domain_researchers.md` that an operator follows to run the verification:

* prerequisites (`CLAUDE_CODE_OAUTH_TOKEN` set, populated database with at least one recent distillation invocation),
* commands to run (`python scripts/verify_domain_researchers.py` then `python scripts/verify_domain_researcher_failure_modes.py`),
* expected outputs (a summary table; both scripts exit 0),
* failure-mode triage (what to check if a script exits non-zero — parse errors point at the prompt; validation errors point at the validator; auth errors point at the env var; budget overruns point at `cost-and-rate-limit-modeling.md`).

## Scope — ground-truth alignment

The original story listed reading paths under `docs/implementation/01-data-layer/collector/...` and `docs/implementation/02-distillation-layer/...`. Those paths are archived under `docs/_archive/...` per commit `ecf6aef`. The verification does not need to read them; the AlphaMind project's collector and distillation layers are landed and their interfaces are read from source (`alphamind.distillation.orchestrator`, etc.) rather than from archived implementation docs.

The path `docs/architecture/llm-agent-failure-handling.md` does not exist; the actual file lives at `docs/design/llm-agent-failure-handling.md`.

## Out of scope

* Performance tuning of the domain-researcher pipeline.
* Multi-invocation regression suite (this script runs a single end-to-end invocation; cumulative pattern monitoring is the feedback-loop layer's territory).
* CI integration — the verification is an operator-driven script, not a CI gate.
* Real-SDK testing of failure modes — the failure-mode script uses stubs.

## Notes

The token-budget assertion reads its threshold from `cost-and-rate-limit-modeling.md` (or from a Pydantic model that loads it) rather than hardcoding `< 50000`. Per `feedback_avoid_numeric_anchors`, hardcoded numeric thresholds in test-side code are a smell — the verification should reference the same authoritative source the runtime uses.

The wall-clock budget similarly reads from the design doc rather than hardcoding `< 180s`. If the design doc does not yet pin a wall-clock budget, document the gap and use a generous bound (e.g., 5 minutes) with a comment naming the missing pin.

The verification script is operator-driven and writes its summary to stdout. It is not a unit test under `tests/`. The failure-mode script may live under `tests/integration/` if the project conventions favor that location; check `tests/` layout before placing.

## Acceptance criteria

- [ ] `scripts/verify_domain_researchers.py` exists and runs end-to-end against a populated database with a real `CLAUDE_CODE_OAUTH_TOKEN`.
- [ ] The script asserts all three sector briefs are returned, each is structurally valid, reference IDs are sequential, and the diagnostic archive is populated under `invocations/<invocation_id>/analysis/<agent_name>/`.
- [ ] Wall-clock and token thresholds are loaded from `cost-and-rate-limit-modeling.md` or a design-doc-derived config helper, not hardcoded.
- [ ] `scripts/verify_domain_researcher_failure_modes.py` exists and runs the three injection scenarios deterministically (no real SDK calls).
- [ ] `scripts/RUNBOOK_domain_researchers.md` exists with prerequisites, commands, expected output, and failure-mode triage.
- [ ] All paths referenced in source resolve (no `docs/implementation/...` references; no `docs/architecture/llm-agent-failure-handling.md` references).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
