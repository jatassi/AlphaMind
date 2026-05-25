## Background

The PM work tree's e2e verification (`scripts/verify_pm.py`, [ALP-331](<https://linear.app/alphamind-jatassi/issue/ALP-331>)) defines four scenarios per parent decision (H) on [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>). The `synchronous_rejection` scenario's design intent is to exercise the engine-stub `submit_envelope` MCP wrapper's rejection path + the PM's post-rejection modification logic in steady-state operation.

First production run on 2026-05-05 (commit `4ff56f6`) produced **WARN — "no rejection triggered"**: the PM rejected 6 of 7 proposals at the verdict level (correct risk-aware behavior given the breach in `combined_set_impact`) but never had a constructive (OPEN/ADD) command rejected by per-command guardrail validation, so the post-rejection modification path was not exercised.

The PM implementation itself is correct — story 06c ([ALP-328](<https://linear.app/alphamind-jatassi/issue/ALP-328>)) carries unit-test coverage of the engine-stub's rejection path. This is a scenario-fixture design problem, not a code defect.

## Goal

Adjust the `_build_synchronous_rejection_scenario(...)` builder in `src/alphamind/scripts/verify_pm.py` so that, when run end-to-end against the live SDK, at least one `submit_envelope` invocation results in a per-command rejection that the PM then resolves via post-rejection modification (`adjustment_category: guardrail_rejection_response`, `phase: post_rejection`, `triggering_rule` populated).

## Reading

* `src/alphamind/scripts/verify_pm.py` — current `_build_synchronous_rejection_scenario` (look for `LibraryConfig.effective_limits["position_max_size_pct"]: 0.5`).
* `tests/fixtures/decision/proposal_pre_processor/normal_with_breach.json` — the pre-processor fixture this scenario reads.
* `tests/fixtures/decision/pm/synchronous_rejection.json` — most recent run's submission log; inspect to understand what *did* happen.
* `scripts/RUNBOOK_pm.md` § Failure-mode triage — the documented WARN path.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) § Surfacing conditions: *"Story 09's* `synchronous_rejection` scenario fails to trigger a `submit_envelope` rejection (every command accepts). Pause — the scenario fixture or the initial validation state needs adjustment. Do not paper over by lowering a limit; the scenario must be a realistic combined-set breach."

## Approach (sketch — not prescriptive)

The current scenario tightens `position_max_size_pct` from 5.0 to 0.5 to make per-position rule trip on token-sized OPEN commands. That's the "papered-over" path the parent issue warns against.

The realistic path: construct the input fixture so the *combined set* (analyst recommendations + strategist position assessments) genuinely exceeds a guardrail when applied cumulatively, and the PM's natural risk-aware sequencing forces it to attempt at least one constructive command that gets rejected. Options:

* Pre-load the `PortfolioManagerView` with positions that are already 80–90% of sector concentration; any incremental ADD trips the rule.
* Construct `combined_set_impact.per_rule[]` such that two analyst recommendations + one strategist ADD together breach `aggregate_gross_exposure_pct`, forcing the PM to either reject all three (current behavior, WARN) or attempt and modify.
* Tighten `LibraryConfig.regime_overrides` for one *specific* rule that a realistic proposal would trip, while keeping per-position limits at production levels.

The scenario must remain a believable combined-set breach — a real PM operating against a real risk parameter set encountering a real guardrail-tightening event.

## Acceptance criteria

- [ ] After re-running `uv run python scripts/verify_pm.py --scenario synchronous_rejection --save-fixtures`, the resulting `tests/fixtures/decision/pm/synchronous_rejection.json` has `submission_log[].submission_results[].status == "rejected"` for at least one entry.
- [ ] At least one envelope in the fixture has a `modifications[]` entry with `phase: "post_rejection"` and `adjustment_category: "guardrail_rejection_response"`.
- [ ] The verdict from `verify_pm.py` is `PASS` (not WARN).
- [ ] The scenario remains a realistic combined-set breach — no per-rule limit lowered below the production default solely to force a trip.
- [ ] `uv run pytest tests/scripts/test_verify_pm.py -n auto` still passes (the fixture builder's structural tests don't regress).

## Verification

Run the verify script end-to-end against the live SDK:

```
uv run python scripts/verify_pm.py --archive-root .archive/verify-pipeline-pm-tuning --synthesizer-invocation-id <prior-INV> --scenario synchronous_rejection --save-fixtures
```

Confirm: `--- Verdict: PASS ---`, the per-scenario summary's `submission_log_rejections >= 1`, and the rendered envelope's modifications carry the `guardrail_rejection_response` adjustment category.

## Cross-references

* Origin run: PR #24 / commit `4ff56f6` / 2026-05-05 verification.
* Parent: [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>).
* Implementation source: [ALP-331](<https://linear.app/alphamind-jatassi/issue/ALP-331>).