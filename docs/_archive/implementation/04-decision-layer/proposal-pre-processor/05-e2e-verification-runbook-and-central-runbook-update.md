# 05 — E2E verification, runbook, and central-runbook update

## Goal

Ship `scripts/verify_proposal_pre_processor.py`, `scripts/RUNBOOK_proposal_pre_processor.md`, and update `scripts/RUNBOOK_end_to_end_verification.md` to insert the proposal pre-processor into the central dependency-ordered phase list. The verify script reads the analyst's and strategist's recorded fixture JSON files, constructs the matching portfolio snapshot + library config + market inputs in code, runs `run_proposal_pre_processor`, validates the result against `BUNDLE_OUTPUT_SCHEMA`, and writes the produced bundles to `tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json` for downstream PM consumption. No SDK invocation; sub-second runtime per scenario.

## Reading

* `scripts/verify_strategist.py` — the canonical pattern this script mirrors; understand the operator-facing pass/fail rubric, the per-scenario archive structure, and the verdict reporting format.
* `scripts/verify_analyst.py` — sibling pattern (predates the strategist's; lighter on operator output).
* `scripts/RUNBOOK_strategist.md` — the runbook structure to mirror.
* `scripts/RUNBOOK_end_to_end_verification.md` — the central phase list this story extends; understand the analyst → strategist → PM ordering and where pre-processor fits.
* `tests/fixtures/decision/analyst/{normal,halt}.json` and `tests/fixtures/decision/analyst/README.md` — input fixture format and schema reference.
* `tests/fixtures/decision/strategist/README.md` — input fixture format. The actual JSON files materialize when `verify_strategist.py` runs (recently landed at commit `e70cdcc` per <issue id="8ad73d80-fff7-4755-b6e5-dd03ee0bd22d">ALP-309</issue>); this story may need to run that script first as part of the e2e flow.
* `docs/design/04-decision-layer/proposal-pre-processor.md` § Edge cases — the four-scenario coverage targets each scenario named in parent decision F.
* `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` § Notes on cross-field invariants — invariants the verify script asserts on each produced bundle.
* <issue id="55c28a3e-dd5c-4247-947a-7e3dde6f9f9e">ALP-318</issue> (this work tree, story 04) — `run_proposal_pre_processor` is the entry point; understand its inputs and pure-function semantics.
* `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py` — `FixtureIvProvider` for the verify script's market inputs.

## Depends on

* <issue id="55c28a3e-dd5c-4247-947a-7e3dde6f9f9e">ALP-318</issue> (this work tree, story 04) — the runner that this script invokes.

## Scope

In scope:

* `scripts/verify_proposal_pre_processor.py` — the verify script.
* `scripts/RUNBOOK_proposal_pre_processor.md` — the operator runbook.
* `scripts/RUNBOOK_end_to_end_verification.md` — extended with a new phase entry.
* `tests/fixtures/decision/proposal_pre_processor/README.md` — the fixture-directory convention readme.

Tests at `tests/scripts/test_verify_proposal_pre_processor.py` (the script itself can be tested end-to-end against an in-memory fixture pipeline).

### 1\. The verify script

`scripts/verify_proposal_pre_processor.py` is a CLI tool with `--scenario` flag accepting `normal`, `halt`, `emergency`, `normal_with_breach`, or `all`. Default `all`.

Per scenario:

* **normal**: Read `tests/fixtures/decision/analyst/normal.json` and `tests/fixtures/decision/strategist/normal.json`. Construct a portfolio snapshot consistent with the strategist's `StrategistView` fixture (the strategist's verify script uses an in-code constructor; mirror it here). Run pre-processor. Assert no breaches (`breaches == ()`).
* **halt**: Read `tests/fixtures/decision/analyst/halt.json` (analyst.mode="watchlist") + `tests/fixtures/decision/strategist/defensive_posture.json` (strategist.mode="defensive_posture"). Construct the matching halt-mode snapshot. Run pre-processor. Assert mode-conditional shape: §3 carries `watchlist[]`; §1.A's `analyst_proposal_ids` is `()`.
* **emergency**: Read `tests/fixtures/decision/analyst/normal.json` + `tests/fixtures/decision/strategist/emergency.json`. Construct snapshot with the regime-transition breach state. Run pre-processor. Assert that strategist position assessments with `remedy_flag` populated pass through unchanged into §2's wrapped records.
* **normal_with_breach**: Read `tests/fixtures/decision/analyst/normal.json` + `tests/fixtures/decision/strategist/normal.json`. Construct a snapshot whose existing exposure is close to a rule limit (e.g., `net_long_pct=49.5`) such that the analyst's recommendations push the combined exposure past the limit. Run pre-processor. Assert `breaches[]` non-empty for `net_long_exposure`; assert each breach's `contributors[]` accounts for at least one analyst recommendation with positive contribution; assert the schema validation passes.

After each scenario:

* `jsonschema.validate(bundle.model_dump(by_alias=True, mode="json"), BUNDLE_OUTPUT_SCHEMA)` — the produced bundle must validate against the canonical schema. Failure = scenario FAIL.
* Walk the cross-field invariants from the design doc (mirror-symmetry, basis-id consistency, total identities) — assert each holds. Failure = scenario FAIL.
* Write the bundle to `tests/fixtures/decision/proposal_pre_processor/<scenario>.json` (pretty-printed, sorted keys for diff stability).

Print a per-scenario verdict line (`✓ normal: PASS (no breaches, schema valid, invariants hold)` / `✗ halt: FAIL (mode-conditional check: analyst_section.mode mismatch)`) and an end-of-run summary table. Exit code 0 if all scenarios PASS, 1 otherwise.

### 2\. The per-feature runbook

`scripts/RUNBOOK_proposal_pre_processor.md` covers:

* **When to run** — after `verify_analyst.py` and `verify_strategist.py` have produced their fixtures; whenever pre-processor code changes.
* **Cost** — zero. No SDK calls. Sub-second total runtime across all four scenarios.
* **Prerequisites** — analyst and strategist fixtures must exist; if missing, instruct operator to run those verify scripts first.
* **Invocation** — `uv run python scripts/verify_proposal_pre_processor.py [--scenario {normal|halt|emergency|normal_with_breach|all}]`.
* **Expected output** — the verdict table format; what each PASS / FAIL means.
* **Failure-mode triage table** — for each likely failure mode, the most-likely root cause and the next operator step. Cover at least: schema-validation FAIL, mirror-symmetry FAIL, missing input fixture, snapshot-construction error.
* **Output artifacts** — where the produced bundle JSONs land; their downstream consumers (PM verify script when that work tree drafts).

### 3\. Central runbook update

Edit `scripts/RUNBOOK_end_to_end_verification.md`. Insert a new phase entry between the strategist phase and any subsequent phase (likely the PM phase, when it lands). The entry follows the existing phase-list convention:

```markdown
### Phase NN — Proposal pre-processor

**Purpose.** Verify the deterministic pre-processor that bundles analyst + strategist
outputs for the PM, including combined-set impact projection, conviction histogram,
book-health summary, and same-underlying conflict detection.

**Prerequisites.** Phases for analyst and strategist must have completed (their
fixtures at `tests/fixtures/decision/{analyst,strategist}/*.json` are inputs to this
phase).

**Run.**

```bash
uv run python scripts/verify_proposal_pre_processor.py
```

**Outputs.** Four bundle JSON files at `tests/fixtures/decision/proposal_pre_processor/`
consumed by the PM phase.

**Cost.** Zero. No SDK calls.

**See:** [`RUNBOOK_proposal_pre_processor.md`](<RUNBOOK_proposal_pre_processor.md>)

```

(Use the existing runbook's exact heading style and section structure — read it first to confirm.)

### 4. Fixture-directory README

Create `tests/fixtures/decision/proposal_pre_processor/README.md`. Mirror the analyst/strategist fixture READMEs: explain that the JSON files are produced by `verify_proposal_pre_processor.py`, list the four scenarios, name the downstream consumer (PM verify script), and clarify that the files are checked into version control to provide a stable contract surface for downstream consumers.

### 5. Snapshot-construction helpers

The verify script needs four `PortfolioStateSnapshot` (library shape) fixtures, one per scenario. Implement these as in-code constructors inside the script (or in a sibling private helper module `scripts/_proposal_pre_processor_fixtures.py`). Each constructor returns `(snapshot, library_config, market, snapshot_timestamp, expected_invocation_id)`. The strategist's verify script's snapshot constructors (read its module to find them) are the reference; this story's constructors mirror their shape with adjustments for the four scenarios.

### Out of scope

* Live SDK integration (this work tree is fixture-based).
* Production wiring (lives in ALP-310 — Decision-layer pipeline composition).
* PM consumption of the produced fixtures (lives in PM work tree, ALP-117).

## Acceptance criteria

* [ ] `scripts/verify_proposal_pre_processor.py` exists and is executable with `uv run python scripts/verify_proposal_pre_processor.py`.
* [ ] Running with `--scenario normal` produces a bundle JSON at `tests/fixtures/decision/proposal_pre_processor/normal.json` and exits 0 if the bundle validates against `BUNDLE_OUTPUT_SCHEMA` and all cross-field invariants hold.
* [ ] Running with `--scenario halt` produces `halt.json` and asserts `analyst_section.mode == "watchlist"`, `strategist_section.mode == "defensive_posture"`, `combined_set_impact.basis.analyst_proposal_ids == []`.
* [ ] Running with `--scenario emergency` produces `emergency.json` and verifies that at least one position assessment with `remedy_flag` populated passes through unchanged into the wrapped record.
* [ ] Running with `--scenario normal_with_breach` produces `normal_with_breach.json` whose `combined_set_impact.breaches` is non-empty; for each breach, `contributors[]` is non-empty and contains at least one positive-contribution `REC-N` id.
* [ ] Default `--scenario all` runs all four and prints a summary table.
* [ ] The script does NOT import `claude_agent_sdk` (verifiable via grep on the file).
* [ ] The script's runtime is under 5 seconds total for all four scenarios on a development machine (no SDK budget).
* [ ] `scripts/RUNBOOK_proposal_pre_processor.md` exists with the sections named above.
* [ ] `scripts/RUNBOOK_end_to_end_verification.md` has a new phase entry for proposal pre-processor between the strategist and PM phases (or, if PM phase doesn't yet exist, after strategist).
* [ ] `tests/fixtures/decision/proposal_pre_processor/README.md` exists.
* [ ] `tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json` are checked into version control after the first run.
* [ ] If `tests/fixtures/decision/analyst/normal.json` or `tests/fixtures/decision/strategist/normal.json` is missing when the script runs, the script prints a clear error pointing to the upstream verify script and exits 1 (does not silently succeed).
* [ ] `tests/scripts/test_verify_proposal_pre_processor.py` exists and asserts that running each scenario produces a JSON file that validates against `BUNDLE_OUTPUT_SCHEMA` and matches its expected mode.
* [ ] `uv run pytest tests/scripts/test_verify_proposal_pre_processor.py -n auto` passes.
* [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run python scripts/verify_proposal_pre_processor.py` from the repository root. Inspect the printed verdict table — all four scenarios should report PASS. Open each produced JSON in `tests/fixtures/decision/proposal_pre_processor/` and confirm the structure matches the schema (spot-check `aggregate_observations`, `strategist_section`, `analyst_section`). Confirm `tests/fixtures/decision/proposal_pre_processor/README.md` exists. Confirm `scripts/RUNBOOK_end_to_end_verification.md` has the new phase entry by `git diff scripts/RUNBOOK_end_to_end_verification.md`.
