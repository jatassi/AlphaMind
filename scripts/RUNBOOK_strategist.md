# Strategist End-to-End Live-SDK Verification Runbook

Operator workflow for the ALP-309 verification artifact that proves the
strategist work tree (ALP-116) talks to the real Claude Agent SDK
correctly. Run after a `git pull` that touches
`src/alphamind/decision/strategist/`, `prompts/decision/strategist.md`,
or the strategist's `agents.yaml` slot.

## When to run

After the strategist work tree completes (the orchestrator integrates
all sibling stories) and any time the strategist's prompt, runner,
harness, parser, validator, or input-bundle assembler changes
thereafter. The unit-test suite covers the verdict rubric, the
synthesizer-archive reader, and the three in-code `StrategistView`
fixture builders; this script covers the live-SDK invocation those
layers wrap.

The script also emits the canonical strategist-output fixtures
(`tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json`)
consumed by the downstream feature trees (proposal pre-processor and
PM verifiers).

## Cost

Three Opus invocations total — roughly **30K input tokens plus ~12K
output tokens** total across the three scenarios (per
`docs/design/cost-and-rate-limit-modeling.md`). Each scenario calls
`retrieve_brief` and `validate_guardrail` a handful of times against
the strategist's small position fixture and emits a final
structured-output JSON. Re-running gratuitously eats the Opus cap.

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it; missing the token surfaces
   as a clean `SDKFailure` exception with the documented remediation
   message.
2. **A prior `verify_synthesizer.py` archive** — the strategist
   verifier reads the synthesizer's recorded prose from
   `<archive-root>/invocations/<synth-id>/analysis/synthesizer/response.md`
   and the retrieval store from
   `<archive-root>/invocations/<synth-id>/stage_artifacts/retrieval_store.json`.
   Run `scripts/verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>`
   first if no archive exists. The strategist consumes the same
   synthesizer artifact the analyst does — both scripts can run in
   parallel from the same archive.
3. **`uv sync` completed** — the script runs under `uv run`.

## Run the verification

```bash
# Real-SDK end-to-end verification (consumes Opus cap; ~3-5 min).
uv run python scripts/verify_strategist.py \
    --archive-root .archive/verify-pipeline-$(date +%Y%m%d) \
    --synthesizer-invocation-id 20260504T143000Z-verify-pipeline \
    --save-fixtures
```

CLI flags:

- `--archive-root DIR` (required) — root of a prior synthesizer archive.
  The strategist verifier writes its own per-scenario diagnostics to
  `<archive-root>/invocations/<strategist-inv-id>/decision/strategist/`.
- `--synthesizer-invocation-id INV` (required) — the synthesizer's
  invocation_id from the prior verify run; locates the brief text and
  retrieval store.
- `--save-fixtures` — after all three scenarios complete with non-FAIL
  verdicts, write the parsed `StrategistOutput` JSON to
  `tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json`.
  Omit to skip.
- `--fixtures-dir DIR` — override the fixture-output directory; defaults
  to the in-tree `tests/fixtures/decision/strategist/`.
- `--scenario {normal,defensive_posture,emergency,all}` — run only the
  named scenario. Default: `all`.

## Expected output

A per-scenario summary block and a final summary line. With three PASS
verdicts the run looks roughly like:

```
=== Strategist live-SDK verification: scenario=normal, invocation_id=20260504T143000Z-verify-strategist-normal, model=claude-opus-4-7 ===
Invoking SDK...
=== Strategist live-SDK verification ===
scenario: normal
invocation_id: 20260504T143000Z-verify-strategist-normal
model: claude-opus-4-7
tokens: input=8521 output=2103 cache_read=0 cache_write=0
attempts: 1
mode: normal
position_assessments: 4
pending_order_assessments: 1
validation_overall: PASS
validation_failures: 0 ((none))
--- Verdict: PASS ---

[... defensive_posture and emergency scenarios follow ...]

[verify_strategist] fixtures written to tests/fixtures/decision/strategist: normal.json, defensive_posture.json, emergency.json
=== Summary: normal: PASS | defensive_posture: PASS | emergency: PASS ===
```

Exit code: `0` on PASS or WARN (per scenario), `1` on FAIL on any.

## Known scenario status (as of PR #22 land, 2026-05-05)

- **`normal`**: PASS end-to-end against real SDK (4 position assessments, 1 pending order, 17,277 output tokens, validation overall PASS). Fixture `tests/fixtures/decision/strategist/normal.json` is current.
- **`defensive_posture`**: known FAIL on attempt 1 (`remedy_flag_pairing` validator rule — model conflates drawdown halt with regime-transition breach) compounded by attempt-2 regurgitation of the prompt example (harness retry drops the original user_message). Tracked at [ALP-311](https://linear.app/alphamind-jatassi/issue/ALP-311) (harness retry contract) and [ALP-312](https://linear.app/alphamind-jatassi/issue/ALP-312) (prompt clarity on `remedy_flag` scope). Fixture intentionally absent until those land.
- **`emergency`**: not yet exercised end-to-end (the 2026-05-05 verification halted at `defensive_posture` before the fix to remove fail-fast in `--scenario all` mode). Fixture intentionally absent. Recommend running `--scenario emergency` after ALP-311/312 land to confirm.

## Verdict rubric

The rubric is applied per scenario. All scenarios in the chosen run contribute their own verdict; the run exits 1 if any is FAIL. A FAIL on one scenario does not short-circuit the loop — every scenario runs so the operator gets a full picture.

| Verdict | Conditions | Exit code |
|---|---|---|
| PASS | Schema valid; Layer-2/3 invariants hold; no validator warnings. | 0 |
| WARN | Schema valid but Layer-2/3 invariant failure surfacing post-retry, or validator emitted warnings. | 0 |
| FAIL | Any `HarnessFailure` subclass (`MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`). | 1 |

## Where artifacts land

For each scenario the harness writes its diagnostic archive to
`<archive-root>/invocations/<strategist-inv-id>/decision/strategist/`:

- `prompt.md` — system prompt (loaded from `prompts/decision/strategist.md`).
- `user_message.md` — assembled input bundle (header + tool-reminder +
  portfolio state + brief).
- `response_initial.md` — first SDK call's structured output + any
  narration.
- `response_retry.md` — second SDK call's output, if a corrective
  retry occurred.
- `errors.json` — parse / validation error trail.
- `metadata.json` — tokens, tool-call count, attempts, wall clock,
  stop reason, mode.

When `--save-fixtures` is passed, three JSON files are written to
`tests/fixtures/decision/strategist/`:

- `normal.json` — parsed `StrategistOutput` for the normal-mode scenario.
- `defensive_posture.json` — parsed `StrategistOutput` for the
  defensive-posture scenario.
- `emergency.json` — parsed `StrategistOutput` for the emergency-invocation
  scenario.

## Failure-mode triage

Bullet-list format (Linear's renderer truncates table cells under
nested-list contexts):

- **`MalformedOutputFailure` on the emergency scenario.** Likely cause —
  the model emitted `remedy_flag` for a non-flagged breach, or omitted
  the `regime_transition_summary` block. Triage — re-run once; if
  persistent, surface to the operator as a prompt-tightening question
  (the example_output in `prompts/decision/strategist.md` may need a
  remedy-flagged exemplar).

- **`ContextOverflowFailure` on the defensive_posture scenario.** Likely
  cause — the 6-position bundle plus `defensive_posture_summary`
  overran the 4000-token output budget set by story 01. Triage —
  surface to the operator per parent-issue surfacing condition; further
  raising `output_token_budget` in `agents.yaml` is the recommended
  next step.

- **Reference resolution FAIL on any scenario.** Likely cause — the LLM
  invented a `[XX-N]` reference not present in the retrieval store.
  Triage — re-run once; if persistent, prompt-tightening (the
  `<output_contract>` reference-ID rule may need stronger phrasing).

- **`prior_status` mismatch in output (manual inspection).** Likely
  cause — the model not anchoring to `ThesisRecord.prior_status` from
  the input view. Triage — surface to the operator per parent-issue
  surfacing condition; the validator was deliberately not given a
  referential check on this field, but if drift is real the check
  should be added in a follow-up.

- **`SDKFailure` on any scenario.** Likely cause — missing or invalid
  `CLAUDE_CODE_OAUTH_TOKEN`, or an Anthropic-side incident. Triage —
  the exception message names the remediation; re-run
  `claude setup-token` and confirm the token is exported.

- **`TimeoutFailure` on any scenario.** Likely cause — the SDK is slow
  or `latency_budget_seconds` was tightened below realistic. Triage —
  `docs/design/cost-and-rate-limit-modeling.md` § Latency budgets;
  the strategist's budget should accommodate multi-position
  assessment plus tool-call rounds.

## Fixture refresh cadence

Re-run `verify_strategist.py --save-fixtures` whenever any of:

- The strategist prompt at `prompts/decision/strategist.md` changes.
- The `StrategistOutput` schema at
  `docs/design/04-decision-layer/strategist-output-schema.md` (or its
  Pydantic expression at `src/alphamind/decision/strategist/models.py`)
  changes.
- The input-bundle assembler at
  `src/alphamind/decision/strategist/input_bundle.py` changes.
- The Layer-2/3 validator at
  `src/alphamind/decision/strategist/validation.py` changes.
- The `StrategistView` shape at
  `src/alphamind/portfolio_state/consumers/strategist.py` changes.

Stale fixtures cause downstream verify scripts to flag schema drift.
The fixtures are not gitignored — they're part of the test corpus and
changes go through normal review.

## References

- `scripts/verify_synthesizer.py` + `RUNBOOK_synthesizer.md` —
  upstream pattern; produces the brief input the strategist verifier
  consumes.
- `scripts/verify_analyst.py` + `RUNBOOK_analyst.md` — sibling
  pattern; the strategist verifier is structured point-for-point off
  it (fixture builders, archive reader, verdict rubric).
- `docs/design/04-decision-layer/strategist.md` — strategist design
  doc; authoritative source for the assessment shape and
  defensive-posture semantics.
- `docs/design/04-decision-layer/strategist-output-schema.md` — the
  schema the parser/validator/SDK enforce.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
- `tests/fixtures/decision/strategist/README.md` — strategist-fixture
  provenance + downstream consumer contract.
