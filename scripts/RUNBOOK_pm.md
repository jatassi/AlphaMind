# Portfolio Manager End-to-End Live-SDK Verification Runbook

Operator workflow for the ALP-331 verification artifact that proves the
portfolio-manager work tree (ALP-117) talks to the real Claude Agent
SDK correctly. Run after a `git pull` that touches
`src/alphamind/decision/portfolio_manager/`, `prompts/decision/pm.md`,
the engine-stub `submit_envelope` MCP wrapper at
`src/alphamind/execution/oms/submit_envelope_mcp.py`, the per-envelope
Layer-2/3 validator, the input-bundle assembler, the harness, the
parser, or the PM's `agents.yaml` slot.

## When to run

After the PM work tree completes (the orchestrator integrates all
sibling stories) and any time the PM's prompt, runner, harness, parser,
validator, input-bundle assembler, engine-stub, or one of the four MCP
wrappers (`validate_guardrail`, `retrieve_brief`, `get_thesis_components`,
`submit_envelope`) changes thereafter. The unit-test suite covers the
verdict rubric, the synthesizer-archive reader, and the four in-code
scenario builders; this script covers the live-SDK invocation those
layers wrap.

The script also emits the canonical PM-output fixtures
(`tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json`)
consumed by future decision-layer pipeline-composition wiring (ALP-310).

## Cost

Four Opus invocations total — roughly **40K input tokens plus ~30K
output tokens** total across the four scenarios (per the PM's
`output_token_budget: 16000` per invocation in `agents.yaml`). Each
scenario calls `validate_guardrail`, `retrieve_brief`,
`get_thesis_components`, and `submit_envelope` a handful of times per
envelope and emits a final structured-output completion sentinel.
Re-running gratuitously eats the Opus cap.

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it; missing the token surfaces as
   a clean `SDKFailure` exception with the documented remediation
   message.
2. **A prior `verify_synthesizer.py` archive** — the PM verifier reads
   the synthesizer's recorded prose from
   `<archive-root>/invocations/<synth-id>/analysis/synthesizer/response.md`
   and the retrieval store from
   `<archive-root>/invocations/<synth-id>/stage_artifacts/retrieval_store.json`.
   Run `scripts/verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>`
   first if no archive exists.
3. **Pre-processor fixtures** — the PM verifier reads
   `tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json`.
   Run `scripts/verify_proposal_pre_processor.py --save-fixtures` first
   if any are missing. The `synchronous_rejection` scenario consumes the
   pre-processor's `normal_with_breach` fixture (three REC-N
   recommendations); the other three scenarios consume the
   correspondingly-named fixture.
4. **`uv sync` completed** — the script runs under `uv run`.

## Run the verification

```bash
# Real-SDK end-to-end verification (consumes Opus cap; ~20–30 min worst case
# at the 600s/scenario latency budget).
uv run python scripts/verify_pm.py \
    --archive-root .archive/verify-pipeline-$(date +%Y%m%d) \
    --synthesizer-invocation-id 20260504T143000Z-verify-pipeline \
    --save-fixtures
```

CLI flags:

- `--archive-root DIR` (required) — root of a prior synthesizer archive.
  The PM verifier writes its own per-scenario diagnostic archive to
  `<archive-root>/invocations/<pm-inv-id>/decision/portfolio_manager/`.
- `--synthesizer-invocation-id INV` (required) — the synthesizer's
  invocation_id from the prior verify run; locates the brief text and
  retrieval store.
- `--save-fixtures` — after each scenario completes with a non-FAIL
  verdict, write the parsed `PMCompletionRecord` plus engine-stub
  submission log to
  `tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json`.
  Omit to skip.
- `--fixtures-dir DIR` — override the fixture-output directory; defaults
  to the in-tree `tests/fixtures/decision/pm/`.
- `--scenario {normal,halt,emergency,synchronous_rejection,all}` — run
  only the named scenario. Default: `all`.

## Expected output

A per-scenario summary block and a final summary line. With four PASS
verdicts the run looks roughly like:

```
=== Portfolio Manager live-SDK verification: scenario=normal, invocation_id=20260504T143000Z-verify-pm-normal, model=claude-opus-4-7 ===
Invoking SDK...
=== Portfolio Manager live-SDK verification ===
scenario: normal
invocation_id: 20260504T143000Z-verify-pm-normal
model: claude-opus-4-7
tokens: input=8521 output=2103 cache_read=0 cache_write=0
retry_count: 0
tool_calls_used: 6
envelopes_submitted: 4
verdict_summary: approve=4 approve_with_modification=0 reject=0
submission_log_rejections: 0
--- Verdict: PASS ---

[... halt / emergency / synchronous_rejection scenarios follow ...]

[verify_pm] fixtures written to tests/fixtures/decision/pm: normal.json, halt.json, emergency.json, synchronous_rejection.json
=== Summary: normal: PASS | halt: PASS | emergency: PASS | synchronous_rejection: PASS ===
```

Exit code: `0` on PASS or WARN (per scenario), `1` on FAIL on any.

## Known scenario status

- All four scenarios untested end-to-end; first run establishes baseline.
  Re-run `--scenario synchronous_rejection` after any change to the
  engine-stub's per-command validation path or
  `position_max_size_pct: 0.5` tightening — the scenario is the only
  one structurally designed to trip a synchronous rejection.

## Verdict rubric

The rubric is applied per scenario. A FAIL on one scenario does not
short-circuit the loop — every scenario runs so the operator gets a
full picture.

| Verdict | Conditions | Exit code |
|---|---|---|
| PASS | Schema valid; PM completion sentinel parses; submission log free of rejections (or for `synchronous_rejection`: at least one rejection present). | 0 |
| WARN | Submission log contains at least one rejected command on a non-`synchronous_rejection` scenario, OR `synchronous_rejection` produced zero rejections (scenario fixture too lenient). | 0 |
| FAIL | Any `HarnessFailure` subclass (`MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`). | 1 |

## Where artifacts land

For each scenario the harness writes its diagnostic archive to
`<archive-root>/invocations/<pm-inv-id>/decision/portfolio_manager/`:

- `prompt.md` — system prompt (loaded from `prompts/decision/pm.md`).
- `user_message.md` — assembled input bundle (header + tool-reminder +
  pre-processor bundle + brief + portfolio state).
- `response_initial.md` — first SDK call's structured output + any
  narration.
- `response_retry.md` — second SDK call's output, if a corrective
  retry occurred.
- `errors.json` — parse / validation error trail.
- `metadata.json` — tokens, tool-call count, attempts, wall clock, stop
  reason, mode.
- `submission_log.json` — per-envelope record of every `submit_envelope`
  tool call, with the per-command results (accepted / rejected) the
  engine-stub returned. PM-only diagnostic.

When `--save-fixtures` is passed, four JSON files are written to
`tests/fixtures/decision/pm/`:

- `normal.json`, `halt.json`, `emergency.json`,
  `synchronous_rejection.json` — each carrying `completion_record`
  (parsed sentinel), `submission_log` (per-envelope submission record),
  and `scenario_metadata` (verdict, tokens, wall clock, tool call count).

## Failure-mode triage

Bullet-list format (Linear's renderer truncates table cells under
nested-list contexts):

- **`MalformedOutputFailure` on the synchronous_rejection scenario.**
  Likely cause — the model emitted a post-rejection modification with
  the wrong adjustment_category (something other than
  `guardrail_rejection_response`), or paired a `pre_submission` phase
  with `guardrail_rejection_response`. Triage — re-run once; if
  persistent, surface to the operator (the PM prompt's post-rejection
  guidance section likely needs tightening).

- **`ContextOverflowFailure` on the emergency scenario.** Likely cause —
  the regime-transition-breach context plus the pre-processor bundle
  overran the `output_token_budget` (currently 16000 in `agents.yaml`).
  Triage — surface to the operator per parent-issue surfacing condition;
  further raising `output_token_budget` in `agents.yaml` is the
  recommended next step.

- **Reference-resolution FAIL on any scenario.** Likely cause — the
  model invented a `[XX-N]` reference not present in the retrieval
  store. Triage — re-run once; if persistent, prompt-tightening (the PM
  prompt's reference-resolution guidance likely needs stronger phrasing).

- **`synchronous_rejection` scenario produces zero rejections.** Likely
  cause — the tightened `position_max_size_pct: 0.5` limit is no longer
  tight enough to trip the engine-stub's $1,000 token sizing, OR the PM
  emitted no constructive (OPEN/ADD) commands. Triage — surface to the
  operator; adjust the scenario's library-config tightening or the
  pre-processor `normal_with_breach` analyst recommendation set.

- **`SDKFailure` on any scenario.** Likely cause — missing or invalid
  `CLAUDE_CODE_OAUTH_TOKEN`, or an Anthropic-side incident. Triage —
  the exception message names the remediation; re-run
  `claude setup-token` and confirm the token is exported.

- **`TimeoutFailure` on any scenario.** Likely cause — the model's
  pre-emit extended-thinking phase exceeded
  `latency_budget_seconds: 600`. Triage — re-run once; if persistent,
  raise `latency_budget_seconds` in `agents.yaml` and cross-reference
  `docs/design/cost-and-rate-limit-modeling.md` § Latency budgets.

## Fixture refresh cadence

Re-run `verify_pm.py --save-fixtures` whenever any of:

- The PM prompt at `prompts/decision/pm.md` changes.
- The `PMCompletionRecord` schema or `PMEnvelope` discriminated union
  at `src/alphamind/decision/portfolio_manager/models.py` changes.
- The input-bundle assembler at
  `src/alphamind/decision/portfolio_manager/input_bundle.py` changes.
- The Layer-2/3 envelope validator at
  `src/alphamind/decision/portfolio_manager/validation.py` changes.
- The engine-stub at `src/alphamind/execution/oms/submit_envelope_mcp.py`
  changes.
- The harness at `src/alphamind/decision/portfolio_manager/harness.py`
  changes.
- The pre-processor's bundle output schema changes (the four input
  fixtures the PM consumes are produced by the pre-processor's verify
  script).
- The `PortfolioManagerView` shape at
  `src/alphamind/portfolio_state/consumers/portfolio_manager.py` changes.

Stale fixtures cause downstream pipeline-composition wiring (ALP-310)
to flag schema drift. The fixtures are not gitignored — they're part
of the test corpus and changes go through normal review.

## References

- `scripts/verify_synthesizer.py` + `RUNBOOK_synthesizer.md` —
  upstream pattern; produces the brief input the PM verifier consumes.
- `scripts/verify_strategist.py` + `RUNBOOK_strategist.md` — sibling
  pattern; the PM verifier is structured point-for-point off it
  (verdict rubric, archive reader, fixture builders) extended with the
  synchronous-rejection scenario.
- `scripts/verify_proposal_pre_processor.py` +
  `RUNBOOK_proposal_pre_processor.md` — sibling pattern; produces the
  four pre-processor fixtures the PM verifier consumes as inputs.
- `docs/design/04-decision-layer/portfolio-manager.md` — PM design
  doc; authoritative source for the envelope shape and the four-MCP-tool
  contract.
- `docs/design/04-decision-layer/pm-envelope-schema.md` — the PM
  envelope schema the parser/validator/SDK enforce.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
- `tests/fixtures/decision/pm/README.md` — PM-fixture provenance +
  downstream consumer contract (created with the first fixture write).
