# 09 — E2E live-SDK verify + runbook

## Goal

Author the live-SDK end-to-end verification artifact for the PM work tree: a `scripts/verify_pm.py` shim deferring to `src/alphamind/scripts/verify_pm.py` (testable module), `scripts/RUNBOOK_pm.md` (operator runbook), and an insert into `scripts/RUNBOOK_end_to_end_verification.md` (central runbook). The verification runs the PM against four scenarios (normal, halt, emergency, synchronous_rejection per parent decision (H)), saves the parsed `PMCompletionRecord` + submission log to `tests/fixtures/decision/pm/<scenario>.json` for downstream consumers, and prints a per-scenario PASS/WARN/FAIL summary the operator can read. Mirrors strategist story 08 ([ALP-309](<https://linear.app/alphamind-jatassi/issue/ALP-309>)) extended with the synchronous-rejection scenario and the engine-stub state-construction wiring.

## Reading

* `src/alphamind/scripts/verify_strategist.py` — sibling pattern (1431 lines); the closest analogue. The structure this story mirrors most closely.
* `src/alphamind/scripts/verify_proposal_pre_processor.py` — sibling pattern; PM verifies four scenarios like the pre-processor.
* `src/alphamind/scripts/verify_analyst.py` — sibling pattern; provides the synthesizer-archive reader pattern this story re-uses.
* `scripts/RUNBOOK_strategist.md` — sibling runbook to mirror for shape (when-to-run, cost, prerequisites, run-the-verification, expected-output, known-scenario-status, verdict-rubric, where-artifacts-land, failure-mode-triage, fixture-refresh-cadence, references).
* `scripts/RUNBOOK_proposal_pre_processor.md` — sibling runbook for the four-scenario operator pattern.
* `scripts/RUNBOOK_end_to_end_verification.md` — the central runbook; this story adds a PM section in dependency-ordered phase list.
* `tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json` — the input fixtures the PM verify script reads.
* `src/alphamind/decision/portfolio_manager/runner.py` (story 08 / [ALP-330](<https://linear.app/alphamind-jatassi/issue/ALP-330>)) — `run_portfolio_manager`, `PMResult`, `PM_TOOL_NAMES`.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` (story 06c / [ALP-328](<https://linear.app/alphamind-jatassi/issue/ALP-328>)) — submission-log accessor for fixture export.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decision (H) names the four scenarios + fixture-export pattern.

## Depends on

* [ALP-330](<https://linear.app/alphamind-jatassi/issue/ALP-330>) (this work tree, story 08) — provides `run_portfolio_manager`.

## Scope

Code at:

* `scripts/verify_pm.py` — thin shim deferring to the testable module.
* `src/alphamind/scripts/verify_pm.py` — testable module (verdict rubric, archive reader, in-code fixture builders, four scenario runners, CLI argparse, fixture-export logic).
* `scripts/RUNBOOK_pm.md` — operator runbook.
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook insertion.

Tests at `tests/scripts/test_verify_pm.py`.

Fixture writes:

* `tests/fixtures/decision/pm/normal.json`, `halt.json`, `emergency.json`, `synchronous_rejection.json` — produced by the script with `--save-fixtures`.

### 1\. CLI shape

The shim defers to `alphamind.scripts.verify_pm.main`. The CLI accepts:

* `--archive-root DIR` (required) — root of a prior synthesizer + analyst + strategist + pre-processor archive (the pre-processor's verify script doesn't itself use an archive, but the synthesizer + analyst + strategist do; this script reads them).
* `--synthesizer-invocation-id INV` (required) — locates the synthesizer's brief + retrieval-store artifacts.
* `--save-fixtures` — write `tests/fixtures/decision/pm/<scenario>.json` after PASS-or-WARN scenarios.
* `--fixtures-dir DIR` — override fixture-output dir; defaults to `tests/fixtures/decision/pm/`.
* `--scenario {normal,halt,emergency,synchronous_rejection,all}` — defaults to `all`.
* `--invocation-id-prefix INV` — synthetic prefix for the PM's per-scenario invocation IDs (defaults to a timestamp-based ID).

### 2\. In-code scenario builders

Author four builders, each returning the inputs `run_portfolio_manager` needs:

* `_build_normal_scenario(synthesizer_text, retrieval_store)` — reads `tests/fixtures/decision/proposal_pre_processor/normal.json`, constructs a matching `PortfolioManagerView` (small portfolio: 4 positions across tech/financials sectors), `RiskBudgetConsumption`, `ActiveRiskParameterSet`, `LibraryConfig`, `MarketInputs` with the same fixtures the strategist verify script uses.
* `_build_halt_scenario(...)` — reads `halt.json`, constructs a `PortfolioManagerView` + `HaltState` (daily drawdown −5.5% of −5.0% limit), pending orders fixture, `current_price_lookup` from a fixed price map.
* `_build_emergency_scenario(...)` — reads `emergency.json`, constructs a `PortfolioManagerView` with one position regime-transition-breached (e.g., POS-NVDA-001 at 4.2% exceeds elevated regime limit 3.5%), `RegimeTransitionBreach` records.
* `_build_synchronous_rejection_scenario(...)` — reads `normal_with_breach.json` (the pre-processor's normal-mode-with-breach fixture), constructs a `PortfolioManagerView` whose initial validation state will reject at least one PM-emitted command. The realistic path: configure `LibraryConfig.regime_overrides` so the per-rule limit is tight enough that any reasonable analyst proposal would push past it; the PM, attempting to submit, gets rejected and exercises the post-rejection modification path. **The scenario must trigger at least one** `submit_envelope` rejection in steady-state operation (per the parent's surfacing condition for story 09).

Each builder returns a dict (or named tuple) with all parameters `run_portfolio_manager` requires.

### 3\. Per-scenario runner

`async def _run_scenario(scenario_name, builder_fn, args, archive_root, ...) -> ScenarioResult`:

1. Build inputs via `builder_fn`.
2. Call `await run_portfolio_manager(mode=..., **inputs, archive_root=archive_root, sdk_query_fn=None)` — `sdk_query_fn=None` means use the real SDK.
3. On `HarnessFailure`: catch, classify as FAIL, return `ScenarioResult(scenario_name, "FAIL", reason=...)`.
4. On success: apply the verdict rubric:
   * Mirror strategist's three-tier rubric: PASS (no validator warnings), WARN (warnings present), FAIL (already covered by exception path).
   * Additionally for `synchronous_rejection`: assert the submission log has at least one entry with `status: rejected`. If zero rejections occurred, record as WARN (the scenario's purpose was to exercise rejection; achieving an all-accepted outcome means the scenario fixture needs adjustment, not that the PM is broken).
5. Print per-scenario summary block (matches strategist's format: invocation_id, model, tokens, attempts, mode, envelopes_submitted, verdict_summary, validation_overall, validation_failures, --- Verdict: ... ---).
6. If `--save-fixtures`: write `tests/fixtures/decision/pm/<scenario>.json` with structured payload:

   ```json
   {
     "completion_record": {...PMCompletionRecord...},
     "submission_log": [{...SubmissionLogEntry per call...}, ...],
     "scenario_metadata": {"name": "...", "verdict": "...", "tokens_used": {...}, "wall_clock_seconds": ...}
   }
   ```

   Pretty-printed JSON (`indent=2`). Mirror strategist's fixture-write pattern.

### 4\. Verdict rubric + main entry point

Mirror strategist's `main()` exactly: argparse → archive reader (synthesizer brief + retrieval store + analyst+strategist outputs) → loop over scenarios → final summary line → exit 0 on PASS/WARN, exit 1 on FAIL.

The final summary line: `=== Summary: normal: PASS | halt: PASS | emergency: PASS | synchronous_rejection: PASS ===`.

### 5\. `scripts/verify_pm.py` shim

Mirror strategist's shim exactly:

```python
"""Thin shim — defer to :mod:`alphamind.scripts.verify_pm`.

Operator entry point so ``uv run python scripts/verify_pm.py`` works
without installing console-script entry points. The testable logic — the
verdict rubric, the synthesizer-archive reader, and the four in-code
scenario builders — lives in the ``alphamind.scripts.verify_pm`` module so
unit tests under ``tests/scripts/`` can exercise it without touching the
real Claude Agent SDK.

Usage:
  uv run python scripts/verify_pm.py \\
      --archive-root DIR --synthesizer-invocation-id INV \\
      [--save-fixtures] [--fixtures-dir DIR] \\
      [--scenario {normal,halt,emergency,synchronous_rejection,all}]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment.
  - A prior ``verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>``
    run that produced ``analysis/synthesizer/response.md`` and
    ``stage_artifacts/retrieval_store.json`` under that archive root.
  - ``tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json``
    must exist (run ``scripts/verify_proposal_pre_processor.py --save-fixtures``
    if missing).

See ``scripts/RUNBOOK_pm.md`` for the operator runbook.
"""

from __future__ import annotations
import sys
from alphamind.scripts.verify_pm import main

if __name__ == "__main__":
    sys.exit(main())
```

### 6\. `scripts/RUNBOOK_pm.md`

Mirror `scripts/RUNBOOK_strategist.md`'s structure:

* **Title + intro paragraph.**
* **When to run** — after PM work tree completes; whenever the PM's prompt, runner, harness, parser, validator, input-bundle assembler, or one of the four MCP wrappers changes.
* **Cost** — four Opus invocations (approx 40K input + approx 30K output tokens; budget set by story 01's `output_token_budget: 16000` per invocation).
* **Prerequisites** — `CLAUDE_CODE_OAUTH_TOKEN` set; prior synthesizer archive; pre-processor fixture set.
* **Run the verification** — invocation example with `--archive-root .archive/verify-pipeline-$(date +%Y%m%d) --synthesizer-invocation-id ... --save-fixtures`.
* **CLI flags** — full list.
* **Expected output** — per-scenario summary block + final summary line (sample).
* **Known scenario status** — initially "all four scenarios untested; first run establishes baseline."
* **Verdict rubric** — PASS / WARN / FAIL table (mirror strategist's).
* **Where artifacts land** — diagnostic archive at `<archive-root>/invocations/<pm-inv-id>/decision/portfolio_manager/`; fixture files at `tests/fixtures/decision/pm/`.
* **Failure-mode triage** — bullet list addressing common failure patterns (MalformedOutputFailure, ContextOverflowFailure, reference-resolution failures, zero-rejection-in-synchronous-rejection scenario, SDKFailure, TimeoutFailure).
* **Fixture refresh cadence** — re-run when prompt, models, parser, validator, input bundle, validation, harness, or pre-processor fixtures change.
* **References** — sibling verify scripts + runbooks.

### 7\. Insert into `scripts/RUNBOOK_end_to_end_verification.md`

Add a new section in the dependency-ordered phase list for "Phase ?: Decision-layer PM verification". The phase ordering (after strategist + pre-processor): Synthesizer → Analyst → Strategist → Proposal pre-processor → **Portfolio Manager** → (eventually decision-layer pipeline composition).

The section names: prerequisites (pre-processor fixtures must exist), invocation, expected outputs, fixture handoff to downstream consumers (when decision-layer pipeline composition wiring lands at [ALP-310](<https://linear.app/alphamind-jatassi/issue/ALP-310>)).

### 8\. Tests

Tests at `tests/scripts/test_verify_pm.py`:

* `test_verdict_rubric_passes_on_clean_run` — given a synthetic `PMResult` with no validator warnings, verdict is PASS.
* `test_verdict_rubric_warns_on_validator_warnings` — synthetic result with non-empty warnings → WARN.
* `test_verdict_rubric_fails_on_zero_rejections_in_synchronous_rejection_scenario` — synthetic result with empty rejections in the synchronous_rejection scenario → WARN with reason naming "no rejection triggered".
* `test_archive_reader_locates_synthesizer_artifacts` — given a fixture archive directory, the reader returns brief text + retrieval store.
* `test_each_scenario_builder_returns_runnable_inputs` — for each of the four builders, the returned inputs satisfy `run_portfolio_manager`'s signature (don't actually call run_portfolio_manager — assert kwargs cover the required-set).
* `test_fixture_export_writes_completion_record_plus_submission_log` — given a stub `PMResult`, the fixture-write produces a JSON with the expected three keys.
* `test_main_returns_exit_code_0_on_pass` — using a stubbed harness, run main with `--scenario normal`; exit code 0.

### Out of scope

* Production pipeline composition — that lives in [ALP-310](<https://linear.app/alphamind-jatassi/issue/ALP-310>). This story's verification is fixture-based.
* Verifying the engine-stub `submit_envelope` MCP wrapper's correctness — that lives in story 06c's tests. This story exercises the wrapper in real-SDK end-to-end mode.
* Verifying live persistence — there is none (engine-stub is in-memory only). When [ALP-119](<https://linear.app/alphamind-jatassi/issue/ALP-119>) ships, the verification chain extends.

## Acceptance criteria

- [ ] `scripts/verify_pm.py` exists as a thin shim.
- [ ] `src/alphamind/scripts/verify_pm.py` exists with CLI argparse, the four scenario builders, the per-scenario runner, the verdict rubric, the fixture-export logic.
- [ ] `scripts/RUNBOOK_pm.md` exists with the structural sections named in scope §6.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` carries a new PM section in dependency-ordered position.
- [ ] All seven tests in `tests/scripts/test_verify_pm.py` pass.
- [ ] `uv run pytest tests/scripts/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.
- [ ] After live-SDK invocation with `--save-fixtures`, `tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json` are created with the documented schema (completion_record + submission_log + scenario_metadata).

## Verification

Run `uv run pytest tests/scripts/test_verify_pm.py -n auto` — all tests pass. Then run `uv run python scripts/verify_pm.py --archive-root .archive/verify-pipeline-$(date +%Y%m%d) --synthesizer-invocation-id <prior-INV> --save-fixtures` against the live SDK; confirm all four scenarios produce per-scenario PASS/WARN summary blocks, the final summary line shows the four-scenario verdicts, and the fixture files are written. Inspect one fixture file to confirm schema integrity.
