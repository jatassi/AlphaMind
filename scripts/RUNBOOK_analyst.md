# Analyst End-to-End Live-SDK Verification Runbook

Operator workflow for the ALP-300 verification artifact that proves the
analyst work tree (ALP-115) talks to the real Claude Agent SDK
correctly. Run after a `git pull` that touches
`src/alphamind/decision/analyst/`, `prompts/decision/analyst.md`, or the
analyst's `agents.yaml` slot.

## When to run

After the analyst work tree completes (the orchestrator integrates all
sibling stories) and any time the analyst's prompt, runner, harness,
parser, validator, or input-bundle assembler changes thereafter. The
unit-test suite covers the verdict rubric and the synthesizer-archive
reader; this script covers the live-SDK invocation those layers wrap.

The script also emits the canonical analyst-output fixtures
(`tests/fixtures/decision/analyst/{normal,halt}.json`) consumed by the
downstream feature trees (strategist, proposal pre-processor, PM
verifiers).

## Cost

Two invocations consume a measurable slice of the weekly Opus cap —
roughly **15K input tokens plus ~4K output tokens** total across both
scenarios (per `docs/design/cost-and-rate-limit-modeling.md`). The
analyst calls `validate_guardrail` and `retrieve_brief` a handful of
times in the normal-mode scenario and emits a final structured-output
JSON; the halt-mode scenario skips `validate_guardrail` entirely.
Re-running gratuitously eats the cap.

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it; missing the token surfaces
   as a clean `SDKFailure` exception with the documented remediation
   message.
2. **A prior `verify_synthesizer.py` archive** — the analyst verifier
   reads the synthesizer's recorded prose from
   `<archive-root>/invocations/<synth-id>/analysis/synthesizer/response.md`
   and the retrieval store from
   `<archive-root>/invocations/<synth-id>/stage_artifacts/retrieval_store.json`.
   Run `scripts/verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>`
   first if no archive exists.
3. **`uv sync` completed** — the script runs under `uv run`.

## Run the verification

```bash
# Real-SDK end-to-end verification (consumes Opus cap; ~30-90s).
uv run python scripts/verify_analyst.py \
    --archive-root .archive/verify-pipeline-$(date +%Y%m%d) \
    --synthesizer-invocation-id 20260504T143000Z-verify-pipeline \
    --save-fixtures
```

CLI flags:

- `--archive-root DIR` (required) — root of a prior synthesizer archive.
  The analyst verifier writes its own per-scenario diagnostics to
  `<archive-root>/invocations/<analyst-inv-id>/decision/analyst/`.
- `--synthesizer-invocation-id INV` (required) — the synthesizer's
  invocation_id from the prior verify run; locates the brief text and
  retrieval store.
- `--save-fixtures` — after both scenarios complete with non-FAIL
  verdicts, write the parsed `AnalystOutput` JSON to
  `tests/fixtures/decision/analyst/{normal,halt}.json`. Omit to skip.
- `--fixtures-dir DIR` — override the fixture-output directory; defaults
  to the in-tree `tests/fixtures/decision/analyst/`.

## Expected output

```
=== Analyst live-SDK verification: scenario=normal, invocation_id=20260504T143000Z-verify-analyst-normal, model=claude-opus-4-7 ===
Invoking SDK...
=== Analyst live-SDK verification ===
scenario: normal
invocation_id: 20260504T143000Z-verify-analyst-normal
model: claude-opus-4-7
wall_clock: 28.41s
tokens: input=8521 output=2103 cache_read=0 cache_write=0
tool_calls: 3
retry_count: 0
stop_reason: end_turn
mode: normal
recommendations: 2
watchlist: 0
validator_errors: 0 ((none))
--- Verdict: PASS ---

=== Analyst live-SDK verification: scenario=halt, invocation_id=20260504T143000Z-verify-analyst-halt, model=claude-opus-4-7 ===
Invoking SDK...
=== Analyst live-SDK verification ===
scenario: halt
invocation_id: 20260504T143000Z-verify-analyst-halt
model: claude-opus-4-7
wall_clock: 12.80s
tokens: input=6210 output=1442 cache_read=0 cache_write=0
tool_calls: 0
retry_count: 0
stop_reason: end_turn
mode: watchlist
recommendations: 0
watchlist: 3
validator_errors: 0 ((none))
--- Verdict: PASS ---

[verify_analyst] fixtures written to tests/fixtures/decision/analyst: normal.json + halt.json
=== Summary: normal: PASS | halt: PASS ===
```

Exit code: `0` on PASS or WARN (per scenario), `1` on FAIL on either.

## Verdict rubric

The rubric is applied per scenario. Both scenarios contribute their own
verdict; the run exits 1 if either is FAIL.

### Normal mode

| Verdict | Conditions | Exit code |
|---|---|---|
| PASS | Schema valid; Layer-2/3 invariants hold; if recommendations are non-empty, ≥1 `validate_guardrail` tool call recorded; explicit empty `recommendations: []` is allowed. | 0 |
| WARN | Schema valid but Layer-2/3 invariant failure (e.g. `unknown_reference`), OR non-empty recommendations with zero tool calls. | 0 |
| FAIL | Any `HarnessFailure` subclass (`MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`). | 1 |

### Halt (watchlist) mode

| Verdict | Conditions | Exit code |
|---|---|---|
| PASS | Schema valid; Layer-2/3 invariants hold; zero tool calls (halt mode skips `validate_guardrail`). Watchlist-entry shape (conviction ≥ 1, non-empty `thesis_summary`) is enforced by the Pydantic model. | 0 |
| WARN | Schema valid but any tool call recorded, OR Layer-2/3 invariant failure. | 0 |
| FAIL | Any `HarnessFailure` subclass. | 1 |

## Where artifacts land

For each scenario the harness writes its diagnostic archive to
`<archive-root>/invocations/<analyst-inv-id>/decision/analyst/`:

- `prompt.md` — system prompt (loaded from `prompts/decision/analyst.md`).
- `user_message.md` — assembled input bundle (header + tool-reminder + brief).
- `response_initial.md` — first SDK call's structured output + any narration.
- `response_retry.md` — second SDK call's output, if a corrective retry occurred.
- `errors.json` — parse / validation error trail.
- `metadata.json` — tokens, tool-call count, retry count, wall clock, stop reason.

When `--save-fixtures` is passed, two JSON files are written to
`tests/fixtures/decision/analyst/`:

- `normal.json` — the parsed `AnalystOutput` for the normal-mode scenario.
- `halt.json` — the parsed `AnalystOutput` for the halt-mode scenario.

## Failure-mode triage

If the script exits non-zero or reports a failure block:

| Failure type | Root cause likely lives in | First place to look |
|---|---|---|
| `SDKFailure` | Missing or invalid `CLAUDE_CODE_OAUTH_TOKEN`, or an Anthropic-side incident. | The exception message names the remediation; re-run `claude setup-token` and confirm the token is exported. |
| `TimeoutFailure` | Either the SDK is slow or `latency_budget_seconds` was tightened below realistic. | `docs/design/cost-and-rate-limit-modeling.md` § Latency budgets — analyst's budget should accommodate tool-call rounds. |
| `ContextOverflowFailure` | Empty response paired with `stop_reason=max_tokens`. The synthesizer brief or guardrail header overflowed. | Inspect the diagnostic archive's `user_message.md`; compare prompt size against the model's window. |
| `MalformedOutputFailure` | Parse or Layer-2/3 validation failed on both attempts. | `errors.json` lists every failed rule; fix the prompt's contract guidance for the named field. |
| `WARN` (`unknown_reference`) | The analyst cited a reference ID not present in the retrieval store — synthesizer text drift or LLM hallucination. | Check the synthesizer prose for the cited ID; if absent, the prompt's reference-mechanism section needs tightening. |
| `WARN` (zero tool calls + non-empty recs in normal mode) | The analyst skipped `validate_guardrail`. | Diff the prompt's tool-use directive; the agent should call the tool once per drafted recommendation. |
| `WARN` (tool calls present in halt mode) | The halt-mode header instructs no `validate_guardrail` use; any call violates the contract. | Check the rendered halt-mode header in `user_message.md` for the watchlist directive. |
| `cumulative_impact_note` missing in archive | The validate_guardrail wrapper's state cell wasn't threaded through the runner. | `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — confirm `build_initial_validation_state` is built fresh per invocation. |
| Halt-mode header rendered without halt context | `--mode halt` flag handling in the runner's mode dispatch. | `src/alphamind/decision/analyst/runner.py` § `_assemble_user_message` — confirm `halt_state` reaches `assemble_input_bundle_halt`. |

## Fixture refresh cadence

Re-run `verify_analyst.py --save-fixtures` whenever any of:

- The analyst prompt at `prompts/decision/analyst.md` changes.
- The `AnalystOutput` schema at
  `docs/design/04-decision-layer/analyst-output-schema.md` (or its Pydantic
  expression at `src/alphamind/decision/analyst/models.py`) changes.
- The input-bundle assembler at
  `src/alphamind/decision/analyst/input_bundle.py` changes.

Stale fixtures cause downstream verify scripts to flag schema drift.
The fixtures are gitignored neither — they're part of the test corpus
and changes go through normal review.

## References

- `scripts/verify_synthesizer.py` + `RUNBOOK_synthesizer.md` — sibling
  pattern; produces the brief input the analyst verifier consumes.
- `docs/design/04-decision-layer/analyst.md` — analyst design doc;
  authoritative source for the conviction-scale rubric.
- `docs/design/04-decision-layer/analyst-output-schema.md` — the
  schema the parser/validator/SDK enforce.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
