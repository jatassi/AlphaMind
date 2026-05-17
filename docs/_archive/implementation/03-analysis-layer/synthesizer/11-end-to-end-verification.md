# 11 — End-to-end live-SDK verification

## Goal

Verify the full synthesizer pipeline against the real Claude Agent SDK using a canonical fixture set spanning all six upstream brief sources. Not a unit test — an executable verification script the operator runs when the work tree completes.

## Reading

* `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup.
* `docs/design/cost-and-rate-limit-modeling.md` — the script consumes a measurable slice of the weekly Sonnet cap (\~10K input plus \~1.5K output).
* `prompts/analysis/synthesizer.md` — the prompt under verification.
* `src/alphamind/analysis/qualitative_research/` — sibling agent's runner/harness as a pattern reference for how the script wires fixtures into the runner.
* [ALP-209](https://linear.app/alphamind-jatassi/issue/ALP-209) (story 09) — system prompt verified.
* [ALP-210](https://linear.app/alphamind-jatassi/issue/ALP-210) (story 10) — runner the script's entry point.

## Depends on

[ALP-210](https://linear.app/alphamind-jatassi/issue/ALP-210) (story 10) — runner. Implicitly all preceding stories via the runner's dependency chain.

## Scope

Script path is `scripts/verify_synthesizer.py`. The script is standalone (not invoked by CI) and targets the real SDK.

#### Script behavior

1. Builds a canonical fixture upstream-brief set — three sector briefs, correlation/regime brief, qualitative brief, adaptive brief — using realistic-shaped data sufficient to exercise contradictions and intersections.
2. Builds an inline stub `SynthesizerPortfolioStateReader` returning fixture positions/theses/exposure.
3. Loads the synthesizer's `BaseAgentConfig` from `config/agents.yaml`.
4. Calls `await run_synthesizer(...)` against the real SDK (no `sdk_query_fn` injection).
5. Reports per the operator-facing format below.
6. Exits with code 0 on PASS or WARN, non-zero on FAIL.

#### Operator report format

Printed to stdout. The `Verdict` line carries the script's exit-code intent.

```
=== Synthesizer live-SDK verification ===
invocation_id: <id>
model: claude-sonnet-4-6
wall_clock: 12.3s
tokens: input=10241 output=1893 cache_read=0 cache_write=0
tool_calls: get_exposure_snapshot=1, get_positions_summary=0, get_active_theses_summary=1
stop_reason: end_turn

--- Synthesis text ---
<full text>

--- Reference-coverage analysis ---
Cited references: 7 (SA-TECH-1, SA-TECH-3, CR-1, QR-2, QR-CW-1, AR-1, AR-2)
Resolved against retrieval store: 7
Invented references: 0

--- Verdict: PASS ---
```

#### Verdict rubric

* **PASS** — response non-empty, zero invented references, `stop_reason` in `{end_turn, max_tokens}`.
* **WARN** — response non-empty, ≥1 invented reference, `stop_reason` in `{end_turn, max_tokens}`. (Surfaces a prompt-tightening signal without aborting the verification.)
* **FAIL** — any `HarnessFailure` subclass.

#### Documentation

A `scripts/README.md` (or section therein) documents when to run the script (work-tree completion), the cost (\~10K input plus \~1.5K output Sonnet tokens), the auth prerequisite (`CLAUDE_CODE_OAUTH_TOKEN`), and the expected outputs.

#### Tests

Test module path is `tests/scripts/test_verify_synthesizer.py`. Test cases listed below.

* `test_reference_coverage_classifier_correct` — given a synthesis text plus retrieval store, the classifier correctly partitions cited references into resolved vs invented.
* `test_verdict_pass_when_zero_invented`.
* `test_verdict_warn_when_invented_present_but_response_nonempty`.
* `test_verdict_fail_when_harness_failure`.
* `test_missing_auth_raises_sdk_failure` — the script surfaces the missing-token case as a clean `SDKFailure` rather than a stack trace.

## Out of scope

* Running the script in CI.
* Comparing the synthesis text against a "golden" expected output (the LLM is non-deterministic by design here).
* Multi-model verification.
* A budget-tracking dashboard.

## Acceptance criteria

- [ ] `scripts/verify_synthesizer.py` exists and is importable.
- [ ] Running the script with a valid `CLAUDE_CODE_OAUTH_TOKEN` produces the documented operator report.
- [ ] Reference-coverage analysis correctly classifies valid vs invented references.
- [ ] Verdict logic returns PASS / FAIL / WARN per the rubric.
- [ ] Script exits 0 on PASS or WARN, non-zero on FAIL.
- [ ] README documents when, cost, expected outputs, auth prerequisite.
- [ ] Unit tests cover the classifier and verdict logic.
- [ ] Missing `CLAUDE_CODE_OAUTH_TOKEN` surfaces as clean `SDKFailure`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.