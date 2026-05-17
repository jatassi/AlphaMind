# 09 — End-to-end live-SDK verification + runbook + fixture emission

## Goal

Verify the full analyst pipeline against the real Claude Agent SDK using two fixture scenarios — one normal-mode invocation and one halt-mode invocation — produced from a recorded synthesizer output (per parent issue's pre-resolved decisions C and D). Ship `scripts/verify_analyst.py` (operator-runnable thin shim), `src/alphamind/scripts/verify_analyst.py` (testable logic), `scripts/RUNBOOK_analyst.md`, and an insertion into `scripts/RUNBOOK_end_to_end_verification.md` placing the analyst in the dependency-ordered phase list. The verify run produces real analyst-output JSON files saved to `tests/fixtures/decision/analyst/{normal,halt}.json` for downstream feature trees (strategist, proposal pre-processor, PM) to consume as their input fixtures.

## Reading

* `scripts/verify_synthesizer.py` + `src/alphamind/scripts/verify_synthesizer.py` — sibling pattern; the analyst verifier mirrors its shim-+-module structure.
* `scripts/RUNBOOK_synthesizer.md` — sibling-pattern runbook.
* `scripts/RUNBOOK_end_to_end_verification.md` — the central runbook the new analyst phase inserts into.
* `scripts/verify_qualitative_researcher.py` + `RUNBOOK_qualitative_researcher.md` — second sibling.
* `src/alphamind/decision/analyst/runner.py` (story 08) — `run_analyst` is the function the verifier exercises.
* `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup.
* `docs/design/04-decision-layer/analyst.md` § Output (especially § Conviction scale) — the rubric the verifier's verdict logic applies.

## Depends on

* `08 — Runner` (ALP-299, this work tree).

## Scope

In scope:

* `scripts/verify_analyst.py` (thin shim).
* `src/alphamind/scripts/verify_analyst.py` (testable logic).
* `scripts/RUNBOOK_analyst.md`.
* An edit to `scripts/RUNBOOK_end_to_end_verification.md` inserting the analyst phase between synthesizer and the next decision agent (or appended after synthesizer if no further decision phases exist yet).
* A new fixtures directory at `tests/fixtures/decision/analyst/` with a `README.md` documenting provenance.

Tests at `tests/scripts/test_verify_analyst.py`.

### 1\. Verify-script structure

The `scripts/verify_analyst.py` shim mirrors `scripts/verify_synthesizer.py`:

```python
"""Thin shim — defer to :mod:`alphamind.scripts.verify_analyst`.

Operator entry point so ``uv run python scripts/verify_analyst.py`` works
without installing console-script entry points.

Usage:
  uv run python scripts/verify_analyst.py --archive-root DIR \
      [--invocation-id INV-NORMAL] [--invocation-id-halt INV-HALT] \
      [--synthesizer-fixture path] [--save-fixtures]

Prerequisites:
  - CLAUDE_CODE_OAUTH_TOKEN set in the environment.
  - A prior verify_synthesizer.py archive to read the synthesizer text from.

See scripts/RUNBOOK_analyst.md for the operator runbook.
"""

from __future__ import annotations
import sys
from alphamind.scripts.verify_analyst import main

if __name__ == "__main__":
    sys.exit(main())
```

The module at `src/alphamind/scripts/verify_analyst.py` carries:

* CLI parsing (argparse).
* A helper that reads the recorded synthesizer text from the archive and reconstructs the retrieval store from the upstream brief fixtures (or, simpler: re-runs the retrieval-store assembly from the same brief bundles the synthesizer's verify run used).
* Fixture builders for `AnalystView`, `RiskBudgetConsumption`, `ActiveRiskParameterSet`, `PortfolioStateSnapshot`, `LibraryConfig`, `MarketInputs`, `sector_resolver` — minimal but realistic (a small ticker→sector map covering the test universe; the active profile from `config/profiles/medium.yaml` or equivalent).
* Two scenarios:
  * **Normal-mode scenario**: full guardrail header rendered, mode="normal", expected outcome 1+ recommendations OR an explicit empty array with reasoning visible in the response.
  * **Halt-mode scenario**: halt-mode header rendered, mode="watchlist", expected outcome 1+ watchlist entries.
* Two `await run_analyst(...)` calls (one per scenario).
* A verdict rubric per scenario:
  * Schema validation passes (parser returns clean AnalystOutput).
  * Layer-2 cross-field invariants hold (validator returns `is_valid=True`).
  * Layer-3 references resolve (no `unknown_reference` errors).
  * Tool-call usage non-zero on normal mode (`validate_guardrail` was invoked at least once for any non-empty recommendations); zero on halt mode (watchlist mode skips validation).
  * At least one recommendation in normal mode OR an explicit zero-result with the model's prose reasoning visible in archive `response_initial.md`.
  * All watchlist entries in halt mode have `estimated_conviction` ≥ 1 and non-empty `thesis_summary`.

### 2\. Fixture saving

When `--save-fixtures` is passed, after a successful verify run, write the parsed `AnalystOutput` JSON for each scenario to `tests/fixtures/decision/analyst/normal.json` and `.../halt.json`. Add a small README at `tests/fixtures/decision/analyst/README.md` documenting:

* The provenance of each fixture (which `verify_analyst.py` run, what synthesizer fixture, what date).
* The intended consumers (strategist verifier, proposal pre-processor verifier, PM verifier).
* The refresh procedure (re-run `verify_analyst.py --save-fixtures` against a fresh synthesizer fixture).

### 3\. Runbook — `scripts/RUNBOOK_analyst.md`

Mirror `RUNBOOK_synthesizer.md` shape:

* Prerequisites (auth, prior synthesizer archive, optional `--save-fixtures` flag).
* Invocation: `uv run python scripts/verify_analyst.py --archive-root /tmp/alphamind-verify --save-fixtures`.
* Expected output shape (the verdict-rubric items as a checklist).
* Where artifacts land (archive directory paths for each scenario, `decision/analyst/{prompt,user_message,response_initial,response_retry,errors,metadata}` files).
* Failure-mode triage table — parse error → recheck schema; unknown_reference → check synthesizer text drift; halt-mode header rendered without halt context → confirm `--mode halt` flag handling; cumulative-impact note missing → check validate_guardrail wrapper state-cell wiring; etc.
* A note on fixture refresh cadence: re-run after every change to the analyst prompt or schema.

### 4\. Central runbook update

`scripts/RUNBOOK_end_to_end_verification.md`: insert the analyst phase between the synthesizer phase and the next decision-layer phase (when those exist; for now, append after synthesizer). Include the dependency note that the analyst's verify script depends on a synthesizer fixture being present, and document the fixture handoff to downstream features (the saved `tests/fixtures/decision/analyst/{normal,halt}.json` files).

### Out of scope

* Strategist + PM verifiers — those will consume the analyst's saved fixtures but live in their own work trees.
* Live composition (analyst chained from a fresh synthesizer call) — fixture-based per parent issue's pre-resolved decision C.
* Proposal pre-processor verification — same; downstream feature.

## Acceptance criteria

- [ ] `scripts/verify_analyst.py` exists as a thin shim deferring to the module.
- [ ] `src/alphamind/scripts/verify_analyst.py` exists with CLI parsing and the two-scenario verify logic.
- [ ] `scripts/RUNBOOK_analyst.md` exists with the shape described in step 3.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is updated to include the analyst phase with a fixture-handoff note.
- [ ] `tests/fixtures/decision/analyst/README.md` exists documenting fixture provenance and intended consumers.
- [ ] `tests/scripts/test_verify_analyst.py` exists exercising the verdict-rubric logic without touching the real SDK (stub the runner).
- [ ] Running `uv run python scripts/verify_analyst.py --archive-root /tmp/test --save-fixtures` against a fresh synthesizer archive completes both scenarios and emits both fixture files (operator-runnable; this is the operator's manual verification step).
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

The operator's manual run of `uv run python scripts/verify_analyst.py --archive-root /tmp/test-analyst --save-fixtures` against a fresh synthesizer archive completes successfully and emits both fixture files; the runbook's expected-output checklist is satisfied; the central `RUNBOOK_end_to_end_verification.md` correctly orders the analyst phase. Verify token/cost is within budget (≤ \~$1 per run at 4000-output-tokens × Opus pricing across two scenarios).