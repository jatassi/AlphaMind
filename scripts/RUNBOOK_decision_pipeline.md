# Decision Pipeline End-to-End Live-SDK Verification Runbook

Operator workflow for the ALP-404 verification artifact that proves the
decision-layer pipeline composition (ALP-310 work tree) wires the four
agents (analyst + strategist in parallel, then proposal pre-processor,
then portfolio manager) correctly against the real Claude Agent SDK.
Run after a `git pull` that touches
`src/alphamind/pipeline/decision.py`,
`src/alphamind/portfolio_state/library_snapshot.py`,
any of the four agent runners
(`src/alphamind/decision/{analyst,strategist,proposal_pre_processor,portfolio_manager}/runner.py`),
or the agents.yaml slots for any decision-layer agent.

## When to run

After the decision-pipeline composition wiring lands and any time the
composition runner, the four agent runners, the cross-constraint-impact
derivation helper, the `to_library_snapshot` translator, or any
upstream consumer-view projector changes thereafter. The unit-test
suite (`tests/pipeline/test_decision.py`,
`tests/scripts/test_verify_decision_pipeline.py`) covers the
composition logic and the verify-script's testable helpers in
isolation; this script covers the live-SDK invocation those layers
wrap end-to-end.

## Cost

Four Opus invocations in a single composition run — roughly **$1–$3
per run** at current Anthropic pricing across the four agents
(analyst + strategist parallel, then PM after the pre-processor's
deterministic stage). The pre-processor is pure Python (no SDK); the
remaining three agents share the same `agents.yaml` budgets the
per-agent verify scripts exercise. Re-run gratuitously and you'll burn
through the Opus cap; re-run after every meaningful composition-runner
change.

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it; missing the token surfaces as
   a clean `SDKFailure` block with the documented remediation message
   (no stack trace).
2. **Production `.env` loading** — if `CLAUDE_CODE_OAUTH_TOKEN` lives in
   `.env` (the dev-machine convention), source it inline before the
   command:
   ```bash
   set -a && source .env && set +a && uv run python -m alphamind.scripts.verify_decision_pipeline ...
   ```
   Production servers do not auto-source `.env`.
3. **`uv sync` completed** — the script runs under `uv run`.

## Run the verification

```bash
# Real-SDK end-to-end verification (consumes Opus cap; one composition run
# spans four agents over roughly 3-10 minutes wall-clock).
uv run python -m alphamind.scripts.verify_decision_pipeline \
    --archive-root /tmp/decision-pipeline-archive
```

CLI flags:

- `--archive-root DIR` (required) — root of the verification archive.
  The script writes the serialized `DecisionPipelineResult` to
  `<archive-root>/decision_pipeline/normal/result.json` and threads the
  same root into each per-agent harness's diagnostic archive (under
  `<archive-root>/invocations/<inv-id>/decision/<agent>/`).
- `--scenario {normal}` — only `normal` is supported in this story.
  Halt and emergency scenarios are deferred per parent-issue ALP-310
  decision (G); the per-agent verify scripts already exercise modal
  shape, so the pipeline-composition verify focuses on wiring
  correctness only.

## Expected output

A single composition run with a PASS verdict produces a summary block
like:

```
=== Decision pipeline live-SDK verification: invocation_id=20260510T143000Z-verify-decision-pipeline ===
Invoking SDK (analyst + strategist parallel, then pre-processor, then PM)...
=== Decision pipeline live-SDK verification ===
invocation_id: 20260510T143000Z-verify-decision-pipeline
scenario: normal

--- Stage results ---
analyst.recommendations: 0
strategist.position_assessments: 4
pm.envelopes_submitted: 1
pm.submission_log: 1 entry(s)
pm.verdict_summary: approve=1 approve_with_modification=0 reject=0

archive: /tmp/decision-pipeline-archive/decision_pipeline/normal/result.json

--- Verdict: PASS ---
```

Exit code: `0` on PASS, `1` on FAIL (validation failure on any
acceptance invariant, or a `HarnessFailure` raised by any of the four
agent runners).

## Where artifacts land

- `<archive-root>/decision_pipeline/normal/result.json` — the
  serialized `DecisionPipelineResult` (six fields: `pydantic_snapshot`,
  `library_snapshot`, `analyst_result`, `strategist_result`,
  `pre_processor_bundle`, `pm_result`). Each Pydantic-model field is
  serialized via `model_dump(mode='json')`; the dataclass
  `library_snapshot` and the dataclass `submission_log` entries are
  serialized via the Pydantic equivalent path or `repr` fallback.
- `<archive-root>/invocations/<inv-id>/decision/analyst/` — analyst
  harness diagnostic archive (prompt, user message, response, errors,
  metadata). Same shape `verify_analyst.py` writes.
- `<archive-root>/invocations/<inv-id>/decision/strategist/` — same
  for the strategist.
- `<archive-root>/invocations/<inv-id>/decision/portfolio_manager/` —
  same for the PM, plus `submission_log.json` with the per-envelope
  submission record.

## Verdict rubric

| Verdict | Conditions | Exit code |
|---|---|---|
| PASS | Pipeline returned a `DecisionPipelineResult`; analyst output is structurally valid; strategist `position_assessments` covers held positions; pre-processor `aggregate_observations` carries populated sub-blocks; PM `submission_log` is non-empty; `pm_result.output.envelopes_submitted == len(submission_log)`. | 0 |
| FAIL | Any acceptance invariant violated, OR any `HarnessFailure` (`MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`) raised by any of the four agent runners (analyst-side failures cancel the strategist branch via `asyncio.gather(..., return_exceptions=False)` per the fail-closed policy). | 1 |

## Failure-mode triage

Bullet-list format (Linear's renderer truncates table cells under
nested-list contexts):

- **Invalid `agents.yaml` entry surfaced at script boot.** Likely
  cause — `config/agents.yaml` is missing a decision-layer slot
  (analyst, strategist, or portfolio_manager) or the slot's prompt
  path no longer resolves to a file under repo root. Triage — the
  Pydantic validator's error message names the missing slot or stale
  field; restore the entry per `config/agents.yaml`'s shape.

- **`SDKFailure` rendered before any agent runs.** Likely cause —
  missing or invalid `CLAUDE_CODE_OAUTH_TOKEN`. Triage — the failure
  block names the remediation; re-run `claude setup-token`, confirm
  the token is exported (`echo $CLAUDE_CODE_OAUTH_TOKEN`), and re-run.

- **Per-agent `HarnessFailure` (analyst, strategist, or PM)
  propagates immediately.** Likely cause — that agent's prompt,
  validator, harness, parser, or input-bundle assembler regressed.
  Triage — read the failure block's `agent_name` and `message` fields,
  then consult the per-agent runbook (`scripts/RUNBOOK_analyst.md`,
  `scripts/RUNBOOK_strategist.md`, or `scripts/RUNBOOK_pm.md`) for
  failure-mode triage. The agent's per-invocation diagnostic archive
  under `<archive-root>/invocations/<inv-id>/decision/<agent>/` carries
  the prompt, user message, response, and error trail.

- **Empty PM submission log on PASS attempt** (`PM submitted zero
  envelopes`). Likely cause — the PM declined to submit any envelope
  for the four-position fixture book. The fixture is built with no
  active breaches, no remedy flags, and a hold-only-friendly brief;
  the PM should normally submit at least one envelope (often a
  no-op aggregate verdict). Triage — inspect
  `<archive-root>/invocations/<inv-id>/decision/portfolio_manager/response_*.md`
  to see why the PM emitted no envelopes; the most common cause is
  prompt drift on the PM's "always submit at least one envelope"
  guidance. Surface to the operator if persistent.

- **Schema-validation FAIL on any agent's structured output.** Likely
  cause — the agent emitted output that parsed but violated a
  Layer-2/3 invariant the runner surfaces via the parser's
  `validation_result` (strategist) or harness retry trail (analyst /
  PM). Triage — the per-agent diagnostic archive carries the offending
  payload; consult the per-agent runbook for the specific invariant.

- **Analyst/strategist parallel-branch cancellation propagation.**
  Likely cause — an analyst-side failure raised before the strategist
  finished; per the fail-closed policy
  (`asyncio.gather(..., return_exceptions=False)`), the strategist
  task is cancelled and the analyst's exception propagates. Triage —
  this is correct behavior; the failure block names the failed agent.
  No remediation needed beyond fixing the failed agent.

## Known scope boundaries

1. **Live synthesizer→decision chaining is out of scope.** The verify
   script supplies pre-recorded synthesizer text + a fixture
   `RetrievalStore`; live composition (analysis pipeline →
   `synthesizer_text` → decision pipeline) is the trigger layer's job
   (separately tracked under the trigger-layer feature). Document
   accordingly when interpreting the run: a clean PASS proves the
   decision-layer wiring, not the cross-pipeline chain.
2. **Halt and emergency scenarios are deferred.** Only `normal` mode
   runs. Per parent decision (G), the per-agent verify scripts already
   exercise modal shape; the pipeline-composition verify focuses on
   wiring correctness only.
3. **DB-backed persistence is not exercised.** The PM engine-stub
   writes to in-memory state; SQL writeback verification lives under
   ALP-119's story 09 (state-persistence work tree).

## References

- `src/alphamind/pipeline/decision.py` — `run_decision_pipeline` and
  `DecisionPipelineResult`; the script's primary call.
- `src/alphamind/scripts/verify_decision_pipeline.py` — the verify
  script module; argparse / structured logging / exit-code conventions
  mirror `verify_synthesizer.py`.
- `tests/scripts/test_verify_decision_pipeline.py` — unit tests for
  the verify script's testable helpers (auth check, fixture builders,
  result-validation predicate, result serializer).
- `scripts/RUNBOOK_analyst.md` — per-agent failure triage for the
  analyst slot.
- `scripts/RUNBOOK_strategist.md` — per-agent failure triage for the
  strategist slot.
- `scripts/RUNBOOK_pm.md` — per-agent failure triage for the
  portfolio-manager slot.
- `scripts/RUNBOOK_proposal_pre_processor.md` — pre-processor verify
  runbook (no SDK calls; deterministic stage between strategist and PM).
- `docs/design/04-decision-layer/portfolio-manager.md` — PM design doc;
  authoritative source for the envelope shape and the four-MCP-tool
  contract.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
