# 08 — End-to-end live-SDK verification + runbook + fixture emission

## Goal

Verify the full strategist pipeline against the real Claude Agent SDK using three constructed fixture scenarios — normal, defensive_posture, and emergency-invocation — produced from a recorded synthesizer archive plus hand-crafted `StrategistView` instances per parent Issue decisions (B) and (C). Ship `scripts/verify_strategist.py` (operator-runnable thin shim), `src/alphamind/scripts/verify_strategist.py` (testable logic), `scripts/RUNBOOK_strategist.md` (operator runbook), an update to `scripts/RUNBOOK_end_to_end_verification.md` inserting the strategist into the dependency-ordered phase list, and `tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json` (real strategist outputs for downstream proposal-pre-processor and PM consumption).

## Reading

* `scripts/verify_analyst.py` — sibling thin-shim pattern.
* `src/alphamind/scripts/verify_analyst.py` — sibling testable-logic module (≈900 lines). Mirror its argparse interface, archive-root reading, fixture-emission optional flag, verdict rubric, and per-scenario invocation pattern.
* `scripts/RUNBOOK_analyst.md` — sibling runbook pattern.
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook; story inserts a strategist phase into the ordered phase list.
* `tests/fixtures/decision/analyst/README.md` — sibling fixture provenance documentation pattern.
* `src/alphamind/decision/strategist/runner.py` — `run_strategist`, `StrategistResult` (story 07).
* `src/alphamind/portfolio_state/consumers/strategist.py` — `StrategistView`; the verify-script constructs three fixtures in code per parent decision (C).
* `src/alphamind/portfolio_state/records/{theses,positions,orders,activity_log,capital}.py` — typed records the StrategistView fixtures construct.
* `src/alphamind/risk_guardrails/breach_behavior/__init__.py` — `HaltState` (defensive_posture fixture).
* `src/alphamind/risk_guardrails/regime_adaptation/__init__.py` — `RegimeTransitionBreach` (emergency fixture).
* `src/alphamind/scripts/verify_synthesizer.py` — produces the synthesizer archive the strategist verifier reads (`response.md` + `retrieval_store.json` under `<archive_root>/invocations/<inv-id>/analysis/synthesizer/`).
* Parent Issue <issue id="213b1ce9-8210-4b63-8d1b-7957cc9466f2">ALP-116</issue> § Pre-resolved decisions (B), (C), (G) — three scenarios, in-code fixture construction, sector_resolver from a small ticker→sector map.

## Depends on

* <issue id="31ee1eed-c5f2-4911-a8a8-432fa1d3855b">ALP-308</issue> (Story 07 — Strategist runner). Verify-script invokes `run_strategist`.

## Scope

In scope:

* `scripts/verify_strategist.py` — thin shim.
* `src/alphamind/scripts/verify_strategist.py` — testable logic.
* `scripts/RUNBOOK_strategist.md` — operator runbook.
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook update.
* `tests/fixtures/decision/strategist/README.md` — fixture provenance documentation.
* `tests/fixtures/decision/strategist/normal.json`, `defensive_posture.json`, `emergency.json` — emitted by `--save-fixtures`.
* `tests/scripts/test_verify_strategist.py` — unit tests for the verify-script's testable logic (verdict rubric, fixture construction, scenario dispatch) without touching the real SDK.

### 1\. Thin-shim entry point

`scripts/verify_strategist.py` mirrors `scripts/verify_analyst.py`:

```python
"""Operator entry point — defer to alphamind.scripts.verify_strategist."""
import sys
from alphamind.scripts.verify_strategist import main
if __name__ == "__main__":
    sys.exit(main())
```

### 2\. Testable logic

`src/alphamind/scripts/verify_strategist.py` implements:

* `argparse` interface: `--archive-root DIR --synthesizer-invocation-id INV [--save-fixtures] [--fixtures-dir DIR] [--scenario {normal,defensive_posture,emergency,all}]`. Default `--scenario all`.
* Read the synthesizer archive: `<archive-root>/invocations/<inv-id>/analysis/synthesizer/response.md` + `stage_artifacts/retrieval_store.json`. Reconstitute `RetrievalStore`.
* Construct three `StrategistView` fixtures in code (private helpers, one per scenario):
  * `_build_normal_view()` — 4 positions across 3 sectors (e.g., NVDA-semis-long, JPM-financials-long, XOM-energy-long, AAPL-tech-long) with full thesis records (entry/target/invalidation rationale, key assumptions, prior_status). 1 pending order. Sparse activity log. No drawdown halt. No regime-transition breaches.
  * `_build_defensive_posture_view()` — 6 positions (more diverse sectors), with daily drawdown at the halt threshold. Constructs a `HaltState` matching the threshold. Activity log includes a recent engine-originated CLOSE on a sector-correlated position (so the strategist's `engine_originated_closure_signal` discipline is exercised).
  * `_build_emergency_view()` — 4 positions, normal mode (no halt), with a regime jump that flagged a `RegimeTransitionBreach` on one position (sized 4.5%, new regime limit 3.5%, overage 1.0%). The emergency-invocation flag is in the rendered guardrail header (constructed via the regime_transition_breaches parameter — the rendered header surfaces the EMERGENCY INVOCATION line).
* Build a fixture-backed `sector_resolver` from a ticker→sector map covering the test universe.
* Build a fixture-backed `current_price_lookup` from a ticker→price map.
* For each scenario, invoke `run_strategist(...)` with the appropriate parameters (mode, halt_state, regime_transition_breaches).
* Verdict rubric: PASS iff all three scenarios produced a parsed `StrategistOutput` with `validation_result.overall == "PASS"`. WARN if any scenario emits warnings. FAIL if any scenario raises `HarnessFailure` or returns `validation_result.overall == "FAIL"`.
* When `--save-fixtures`, write each scenario's parsed output to `tests/fixtures/decision/strategist/<scenario>.json` (the canonical location for downstream consumers).
* Emit a per-scenario summary table to stdout: invocation_id, mode, position-assessments-count, pending-orders-count, validation status, tokens used, archive path.

### 3\. Operator runbook

`scripts/RUNBOOK_strategist.md`:

**Prerequisites.** `CLAUDE_CODE_OAUTH_TOKEN` set; a prior `verify_synthesizer.py` run produced the synthesizer archive at `<archive-root>/invocations/<inv-id>/analysis/synthesizer/`.

**Invocation.**

```bash
uv run python scripts/verify_strategist.py \
    --archive-root <archive-root> \
    --synthesizer-invocation-id <synth-inv-id> \
    [--save-fixtures]
```

**Expected output shape.** A per-scenario summary table; PASS verdict on all three scenarios. Total runtime \\~3–5 minutes (three live SDK invocations against Opus).

**Failure-mode triage** (one bullet per common symptom — bullet-list format chosen over a markdown table because Linear's renderer truncates table cells under nested-list contexts):

* `MalformedOutputFailure` on emergency scenario. Likely cause — model emitted `remedy_flag` for a non-flagged breach, or omitted the `regime_transition_summary` block. Triage — re-run once; if persistent, surface to the operator as a prompt-tightening question (the example_output in `prompts/decision/strategist.md` may need a remedy-flagged exemplar).
* `ContextOverflowFailure` on defensive_posture scenario. Likely cause — the 6-position bundle plus `defensive_posture_summary` overran the 4000-token output budget set by story 01. Triage — surface to the operator per parent-issue surfacing condition; further raising `output_token_budget` in `agents.yaml` is the recommended next step.
* **Reference resolution FAIL on any scenario.** Likely cause — LLM invented a `[XX-N]` reference not present in the retrieval store. Triage — re-run once; if persistent, prompt-tightening (the `<output_contract>` reference-ID rule may need stronger phrasing).
* `prior_status` mismatch in output (manual inspection). Likely cause — model not anchoring to `ThesisRecord.prior_status` from the input view. Triage — surface to the operator per parent-issue surfacing condition; the validator was deliberately not given a referential check on this field, but if drift is real the check should be added in a follow-up.

### 4\. Central runbook update

`scripts/RUNBOOK_end_to_end_verification.md`: insert a strategist phase after the analyst phase in the ordered phase list. The phase consumes the synthesizer archive (same as analyst — strategist and analyst run in parallel from the same upstream artifact). Show the canonical command and the dependency on the synthesizer archive.

### 5\. Fixture provenance

`tests/fixtures/decision/strategist/README.md` mirrors `tests/fixtures/decision/analyst/README.md`:

* Names the three scenarios.
* Provenance: produced by `verify_strategist.py --save-fixtures` against a recorded synthesizer archive + in-code-constructed StrategistView fixtures.
* Refresh procedure: re-run when `prompts/decision/strategist.md`, `models.py`, `input_bundle.py`, `validation.py`, or the StrategistView shape changes.
* Intended consumers: `scripts/verify_proposal_pre_processor.py` (future), `scripts/verify_portfolio_manager.py` (future), `tests/decision/{proposal,portfolio_manager}/`.

### 6\. Unit tests

`tests/scripts/test_verify_strategist.py` exercises:

* The verdict rubric (synthetic per-scenario success/fail/warn → expected verdict).
* The three StrategistView fixture builders return non-empty, schema-conformant views.
* The synthesizer-archive reader reads `response.md` + `retrieval_store.json` correctly given a tmp-path archive.
* No SDK calls — use a stub `sdk_query_fn` that returns canned strategist-output JSON validating against the schema.

### Out of scope

* Live synthesizer→strategist composition (lives in the analysis-layer pipeline composition wiring To-do, <issue id="adfd5e93-3b9a-471b-aa2a-fbf85f68960a">ALP-276</issue>).
* PM-side tests against the emitted strategist fixtures (lives in the future PM work tree).

## Acceptance criteria

- [ ] `scripts/verify_strategist.py` (thin shim) exists and runs `python scripts/verify_strategist.py --help` successfully.
- [ ] `src/alphamind/scripts/verify_strategist.py` ships `main()` with the argparse interface above.
- [ ] Three scenarios (normal, defensive_posture, emergency) run end-to-end against the real Claude Agent SDK and produce parsed `StrategistOutput` documents validating against the schema (Layer-2/3 PASS).
- [ ] `--save-fixtures` writes `tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json` (real, recently-generated strategist outputs).
- [ ] `tests/fixtures/decision/strategist/README.md` documents provenance + intended consumers + refresh procedure.
- [ ] `scripts/RUNBOOK_strategist.md` documents prerequisites, invocation, expected output, failure-mode triage (bullet-list format).
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is updated to insert the strategist phase in the dependency-ordered phase list (after the analyst phase, parallel to it from the same synthesizer archive).
- [ ] Unit tests under `tests/scripts/test_verify_strategist.py` cover verdict rubric, fixture builders, archive reader (with stub `sdk_query_fn`).
- [ ] `uv run pytest tests/scripts/test_verify_strategist.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.
- [ ] Manual operator run: `uv run python scripts/verify_strategist.py --archive-root <recent-archive> --synthesizer-invocation-id <recent-inv> --save-fixtures` produces three PASS scenarios and updates the three fixture files.

## Verification

```bash
# Unit tests (no real SDK)
uv run pytest tests/scripts/test_verify_strategist.py -n auto

# Manual operator run (real SDK; requires CLAUDE_CODE_OAUTH_TOKEN)
uv run python scripts/verify_strategist.py \
    --archive-root <archive-root> \
    --synthesizer-invocation-id <synth-inv-id> \
    --save-fixtures

# Confirm fixture files exist and validate
uv run python -c "
import json
from alphamind.decision.strategist import StrategistOutput
for scenario in ['normal', 'defensive_posture', 'emergency']:
    payload = json.load(open(f'tests/fixtures/decision/strategist/{scenario}.json'))
    StrategistOutput.model_validate(payload)
    print(f'{scenario}: OK')
"
```

After successful verification, the strategist work tree is complete; downstream consumers (proposal pre-processor, portfolio manager) can dispatch with strategist-side fixtures available.
