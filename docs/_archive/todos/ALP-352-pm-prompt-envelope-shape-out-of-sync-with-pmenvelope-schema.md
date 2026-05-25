## Problem

The `<output_contract>` envelope description in `prompts/decision/pm.md` does not match the `PMEnvelope` Pydantic schema in `src/alphamind/decision/portfolio_manager/models.py:221-274`. PM agent following the prompt verbatim emits envelopes that fail Layer-1 (Pydantic) validation.

## Mismatch

**Prompt says** (`prompts/decision/pm.md` `<output_contract>`, \~lines 132-145, and example at \~lines 173-200):

```json
{
  "evaluation": {
    "verdict": "approve",
    "criteria": { "falsifiability": "pass", ... },
    "concerns": ["string", ...],
    "rationale_narrative": "..."
  }
}
```

**Schema requires** (`models.py` `PMAnalystEnvelope` / `PMStrategistEnvelope`):

```json
{
  "verdict": "approve",
  "evaluation": { "falsifiability": {"status": "pass", "note": null}, ... },
  "concerns": [{"source": "...", "summary": "..."}],
  "rationale_narrative": "..."
}
```

`verdict`, `concerns`, `rationale_narrative` live at top level. `evaluation` directly contains `CriterionAssessment {status, note}` objects keyed by criterion name (no `criteria` nesting). `concerns` is a tuple of `ConcernRecord {source, summary}`, not strings.

## Evidence

`.archive/verify-pipeline-20260504/invocations/20260505T172913Z-verify-pm-normal/decision/portfolio_manager/response_initial.md` shows the agent attempting 4 envelopes and getting persistently rejected with `schema_invariant: Unable to extract tag using discriminator 'source_provenance'`. `submission_log.json` is empty (`[]`) despite `tool_calls_used: 10` — see also [ALP-353](https://linear.app/alphamind-jatassi/issue/ALP-353/submit-envelope-silently-drops-layer-1-rejections-from-submission-log) (silent Layer-1 drop) for why those attempts aren't even logged.

## Root cause

* [ALP-322](https://linear.app/alphamind-jatassi/issue/ALP-322/02-verify-promptsdecisionpmmd-drop-prefill-sentinel-output) (`bb272eb`, 2026-05-05 09:51) rewrote `pm.md` for sentinel-only output. The diff swapped only the wrapper (top-level `envelopes` array → `envelopes_submitted` + `verdict_summary`); the inline envelope description was preserved from the 2026-04-26 initial commit.
* [ALP-323](https://linear.app/alphamind-jatassi/issue/ALP-323/03-pmenvelope-sentinel-minimal-oms-command-models) (`fada008`, 2026-05-05 09:58) authored `PMEnvelope` Pydantic models directly from `docs/design/04-decision-layer/pm-envelope-schema.md`.

The two work trees ran from different sources of truth seven minutes apart and never reconciled. The `e5a069c` PR #24 review fix touched envelope_id format strings only, not the structural mismatch.

## Authoritative source

`docs/design/04-decision-layer/pm-envelope-schema.md` — `required` block lists `verdict`, `evaluation`, `modifications`, `concerns`, `rationale_narrative`, `commands` at top level. Pydantic schema matches this; prompt does not.

## Fix

Rewrite the `<output_contract>` envelope shape and the example `submit_envelope` call in `prompts/decision/pm.md` to mirror `pm-envelope-schema.md` directly:

* Move `verdict`, `concerns`, `rationale_narrative` to top level.
* Replace `criteria: {key: "pass"|"fail"}` with `evaluation: {key: {status, note}}` per `CriterionAssessment`.
* Replace `concerns: [string]` with `concerns: [{source, summary}]` per `ConcernRecord`.
* Verify the rewritten example round-trips through `PMEnvelope.model_validate()` in a test.