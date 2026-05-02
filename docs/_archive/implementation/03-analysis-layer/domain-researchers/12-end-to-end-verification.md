---
status: not_started
completed_date:
commit_id:
---

# 12 — End-to-end verification

## Goal

Produce verification scripts and a runbook proving the domain-researcher layer works end-to-end against a populated database and a live SDK: the parallel orchestrator runs to completion, three sector briefs render, all three validate cleanly, the diagnostic archive is populated, and the wall-clock + token usage stay within the documented budget.

## Reading

- `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup
- `docs/design/cost-and-rate-limit-modeling.md` — the per-invocation token budgets the verification asserts against
- `docs/implementation/01-data-layer/collector/08-end-to-end-verification.md` — the parallel verification doc; mirror the format
- `docs/implementation/02-distillation-layer/external/13-end-to-end-verification.md` — the parallel verification doc on the distillation side
- `docs/architecture/infrastructure.md` § Layer 2 invocation archive — file paths the script verifies

## Depends on

- 11 (the parallel orchestrator must exist before end-to-end verification can run).
- 09a, 09b, 09c (the three sector system prompts must exist before the SDK call in `verify_domain_researchers.py` can produce real briefs; the failure-mode script uses the runner's DI seam and can run without real prompts but is gated alongside the live-SDK script for simplicity).
- A populated database (collector bootstrap + ongoing collection completed) and a populated distillation state (distillation orchestrator has run at least once).

## Scope

In scope:

- `scripts/verify_domain_researchers.py`:
  - Connects to the SQLite database.
  - Runs the distillation orchestrator (or reads its most recent archive) to obtain `DistillationOutputs` for the current `as_of`.
  - Calls `run_domain_researchers` with a real `CLAUDE_CODE_OAUTH_TOKEN`.
  - Asserts:
    - `DomainResearchersOutput` has three populated `DomainResearcherResult` fields.
    - Each sector's `brief` parses cleanly (already enforced by the runner; this is a re-assertion against the returned value).
    - Each sector's `brief.findings` is non-empty.
    - Each brief's reference IDs are sequential per section starting at 1 (re-running the validator against the returned briefs).
    - The diagnostic archive directory `<archive>/<date>/<invocation>/analysis/{tech_semis,financials,energy}_researcher/` contains the documented files (prompt.md, user_message.md, response_initial.md, metadata.json).
    - `total_wall_clock_seconds < 180` (3 minutes — generous bound; the design budget is closer to ~30 seconds per sector with parallelism).
    - `total_tokens_used.input + total_tokens_used.output < 50000` (within the per-invocation budget for the analysis layer per `cost-and-rate-limit-modeling.md`).
  - Reports a summary table: per-sector wall-clock, tokens used, retry count, finding count, anomaly count, thesis-candidate count, signal quality.
  - Exits 0 on all assertions passing; exits 1 with a per-failure report otherwise.

- `scripts/verify_domain_researcher_failure_modes.py`:
  - Runs three injection scenarios end-to-end against a stubbed harness (no real SDK calls — this script is fast and deterministic):
    1. Force-inject a parse-failure response on the first SDK call → assert the corrective retry fires, the second response succeeds, `retry_count=1` on the result.
    2. Force-inject two consecutive parse failures → assert `MalformedOutputFailure` propagates as `OrchestratorFailure` with the sector tag.
    3. Force-inject `stop_reason=max_tokens` paired with a parse failure → assert `ContextOverflowFailure` propagates immediately, no retry.
  - Exits 0 on all three scenarios behaving as specified.

- `docs/implementation/03-analysis-layer/domain-researchers/VERIFICATION.md` — operator runbook with:
  - Pre-conditions (collector bootstrap completed, distillation orchestrator wired, `CLAUDE_CODE_OAUTH_TOKEN` set in `.env`).
  - Sequence of verification steps using the two scripts above.
  - Expected outputs for each.
  - The "mental check" — a worked example showing what a tech/semis brief, a financials brief, and an energy brief look like for a representative invocation. Pull from a real archive run if available; otherwise hand-construct from the prompts' example outputs.
  - Known wall-clock and token-budget expectations (parallelism collapses three ~10–20s sectors to ~20s wall clock; per-sector token usage ~5–10K depending on bundle size).

- Unit tests for the verification scripts themselves:
  - Each script's assertions trigger correctly against fault-injected fixture inputs.

Out of scope:
- The orchestrator implementation itself (story 11).
- Performance benchmarking under load.
- Long-term feedback-loop calibration validation (Phase 4 concern owned by `feedback-loop.md`).
- Running against a live production database — that's an operator concern; the scripts just have to work correctly when invoked.

## Notes

`verify_domain_researchers.py` makes real SDK calls — it consumes from the operator's Max subscription budget. Document this prominently in the runbook so an operator does not re-run it casually. The complementary `verify_domain_researcher_failure_modes.py` script is fast and free; can run on every commit.

The `as_of` parameter for the verification: default to "now" (the script's invocation time). Optionally accept `--as-of YYYY-MM-DDTHH:MM:SSZ` for retrospective runs. The distillation outputs must be available for the chosen `as_of`; if not, surface a clear error and exit.

The 50K-input-plus-output token bound is intentionally loose — the design budget for the entire analysis layer (per `cost-and-rate-limit-modeling.md`) is ~30K per invocation; 50K leaves headroom for verification-time variance. Tighten as the implementation matures and observed usage stabilizes.

The runbook's "mental check" should walk through a single invocation's full archive — show the distillation slice the agent saw, the qualitative input it saw, the system prompt (referenced, not inlined), the brief it produced. That linear narrative is what makes the verification useful for ops who haven't read every story.

For the stubbed-harness failure-mode script, dependency-inject the harness via the runner's seam from story 10 — the script does not need to subclass the SDK or mock at the network layer. The runner's DI is the right test surface.

## Acceptance criteria

- [ ] `scripts/verify_domain_researchers.py` exists and runs the parallel orchestrator end-to-end against a real SDK.
- [ ] All three sector briefs are verified to parse and validate.
- [ ] All three diagnostic archive directories are verified to contain the documented files.
- [ ] Wall-clock and token-usage assertions pass on a representative invocation.
- [ ] Summary table reports per-sector wall-clock, tokens, retry count, finding count, anomaly count, thesis-candidate count, signal quality.
- [ ] `scripts/verify_domain_researcher_failure_modes.py` exists and exercises the three injection scenarios via the runner's DI seam (no real SDK calls).
- [ ] All three failure-mode scenarios behave as specified (corrective retry success, malformed-output failure propagation, context-overflow immediate abort).
- [ ] `VERIFICATION.md` exists with pre-conditions, script invocation procedure, expected outputs, mental-check example, and budget expectations.
- [ ] Unit tests for the verification scripts cover their assertion logic against fault-injected fixtures.
- [ ] After running the scripts on a populated database with valid SDK credentials, both exit 0.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
