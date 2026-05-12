# 07 — End-to-end verification

## Goal

Produce verification scripts and a runbook proving the qualitative-researcher work tree works end-to-end against a populated database and a live Claude Agent SDK: the runner runs to completion, a `QualitativeBrief` parses and validates cleanly, the diagnostic archive is populated (system prompt, user message, response, errors, metadata), the news digest renders with sector buckets and reference IDs, the agent makes at least one tool call against `news_search` or `prediction_markets` or `earnings_commentary`, and the wall-clock + token usage stay within the documented budget. The story is verification, not new feature code — the runner already exists from story 06.

## Reading

* `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN`-based authentication; the verification script reads the token from the environment.
* `docs/architecture/infrastructure.md` § Invocation archive — the diagnostic archive layout the verification asserts on.
* `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate — the runtime token budget the verification asserts against.
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Token budget — the brief-output budget (300–600 tokens nominal).
* `src/alphamind/analysis/qualitative_research/runner.py` — the entry point the verification script calls.
* `tests/analysis/domain_researchers/test_e2e.py` (or whatever the ALP-186 sibling shipped) — the canonical end-to-end test for the domain researchers; mirror its structure for this work tree.

## Depends on

* ALP-252 — the runner this verification exercises.
* ALP-250 — the prompt round-trip; the verification reuses the same prompt.

## Scope

In scope under `scripts/verify_qualitative_researcher.py` (manual one-shot script) and `tests/analysis/qualitative_research/test_e2e.py` (CI-friendly automated test gated on a `RUN_LIVE_LLM_TESTS=1` environment flag).

### 1\. Live verification script

`scripts/verify_qualitative_researcher.py` — a runnable script the operator invokes manually:

```
$ CLAUDE_CODE_OAUTH_TOKEN=... uv run python scripts/verify_qualitative_researcher.py \
    --as-of 2026-05-02T14:30:00Z \
    --last-invocation 2026-05-02T13:30:00Z \
    --archive-root .archive/verify
```

Procedure:

1. Open a SQLAlchemy session against the production database (or a populated test DB; the script accepts `--db-url`).
2. Load `agents_config` via `load_config()`, the `universe` via `load_sector_roster()`, and the `universal_regime_label` from the most recent `DistillationOutputs` (or a synthetic stub if no recent run exists; surface the choice in the script's stdout).
3. Call `await run_qualitative_researcher(...)`.
4. Print the resulting `QualitativeBrief` as text via the prompt's example-output format.
5. Print summary stats: wall-clock seconds, token counts, tool calls used, retry count, signal quality.
6. Confirm the diagnostic archive at `<archive-root>/invocations/<invocation_id>/analysis/qualitative_researcher/` carries `prompt.md`, `user_message.md`, `response_initial.md`, `errors.json`, `metadata.json`. Print missing files if any.
7. Exit `0` on success, `1` on any failure.

### 2\. Automated test

`tests/analysis/qualitative_research/test_e2e.py` — a pytest test that runs only when `RUN_LIVE_LLM_TESTS=1` is set. Otherwise the test `pytest.skip`s.

The test asserts:

* The runner returns a `QualitativeResearcherResult` with no exceptions.
* `result.brief.threads` has at least one entry.
* `result.brief.sentiment_snapshot` has populated `extremes`, `divergences`, `regime` fields.
* `result.tokens_used.input_tokens > 0` and `result.tokens_used.output_tokens > 0`.
* `result.wall_clock_seconds < agent_config.latency_budget_seconds`.
* `result.tool_calls_used <= agent_config.cumulative_tool_call_limit`.
* `result.news_digest.entries` is non-empty (assuming the test DB has news in the lookback window).
* The diagnostic archive contains all expected files.

### 3\. Runbook

A short prose runbook at the bottom of the story body documenting how to run the verification (CLI invocation), what success looks like, what to do on failure (re-run with verbose logging; check the diagnostic archive for clues; surface to operator if budget exceeded).

The runbook can live in the script's `--help` output rather than as a separate doc — the canonical sibling pattern.

### Out of scope

* Pipeline-level orchestration combining qualitative + domain researchers + adaptive — out of scope for this work tree.
* Performance regression testing or wall-clock benchmarking across multiple runs — manual one-shot verification is sufficient for the work-tree completion gate.
* Mocking the SDK in this story — the whole point is live verification. Per-component unit tests (stories 03–06) cover the no-SDK-required paths.

## Acceptance criteria

- [ ] `scripts/verify_qualitative_researcher.py` exists and runs end-to-end against a populated database when `CLAUDE_CODE_OAUTH_TOKEN` is set.
- [ ] The script prints a non-empty `QualitativeBrief` text representation.
- [ ] The script populates the diagnostic archive at the named path with `prompt.md`, `user_message.md`, `response_initial.md`, `errors.json`, `metadata.json` (and `response_retry.md` if a retry happened).
- [ ] `tests/analysis/qualitative_research/test_e2e.py` exists, skips when `RUN_LIVE_LLM_TESTS` is unset, and passes when set against a populated test database.
- [ ] When the test runs live, `result.tokens_used.output_tokens` falls within the `output_token_budget` configured in `agents.yaml` for `qualitative_researcher`.
- [ ] When the test runs live, `result.wall_clock_seconds < agent_config.latency_budget_seconds`.
- [ ] All non-live tests in the work tree continue to pass under `uv run pytest tests/analysis/qualitative_research/ -n auto`.
- [ ] The script's `--help` output documents the CLI flags and what success looks like.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/ -n auto` to confirm the broader work tree's tests still pass. Run `RUN_LIVE_LLM_TESTS=1 uv run pytest tests/analysis/qualitative_research/test_e2e.py -n auto` (skip on CI; run by operator on the dev machine when verifying) to exercise the live path. Run `uv run python scripts/verify_qualitative_researcher.py --as-of <recent_ts> --last-invocation <recent_ts - 1h> --archive-root .archive/verify-qual` and inspect the printed brief + archive contents. If the brief or archive is missing fields, surface to the operator before marking the story `Done`.
