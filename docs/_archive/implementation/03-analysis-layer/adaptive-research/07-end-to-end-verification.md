# 07 — End-to-end verification

## Goal

Produce verification scripts and a runbook proving the adaptive-researcher work tree works end-to-end against a populated database and a live Claude Agent SDK: the runner runs to completion, an `AdaptiveBrief` parses and validates cleanly (including Layer-3 referential resolution against fixture upstream briefs), the diagnostic archive is populated (system prompt, user message, response, errors, metadata), the input bundle renders all four sections with live regime + anomaly data, and the seven registered tools are wired correctly through the SDK MCP server. Per parent issue resolution (H): this story fakes the upstream pipeline composition by loading a recorded `(tuple[SectorBrief, ...], QualitativeBrief, CorrelationRegimeBrief, DistillationOutputs)` fixture rather than running domain researchers + qualitative + distillation live. Live composition belongs in the separate execution-layer wiring story tracked in `docs/project-tracker.md`.

## Reading

* Parent issue § Pre-resolved configuration decisions (H) — names the fixture-fake approach this story implements.
* `src/alphamind/analysis/qualitative_research/` — sibling work tree with a completed E2E story (<issue id="b465a39c-e055-4a31-acfb-3ffb3bdeca43">ALP-253</issue>). Mirror the verification-script shape and the runbook structure.
* `src/alphamind/analysis/adaptive_research/runner.py` § `run_adaptive_researcher` — the entry point this story exercises end-to-end.
* `docs/design/03-analysis-layer/adaptive-research.md` § Output § Output schema — the contract this story verifies live output conforms to.
* `prompts/analysis/adaptive_researcher.md` — the system prompt the live SDK call loads.
* `config/agents.yaml` § `adaptive_researcher` — the live config the runner reads (post-story-01 edits).
* `tests/analysis/qualitative_research/test_e2e.py` (or whatever the qualitative E2E test file is named) — fixture/runbook pattern to mirror.

## Depends on

* <issue id="b53a47b2-7c95-42b2-987d-a88d14b86c2c">ALP-264</issue> (story 06) — adaptive-researcher runner.

## Scope

In scope under `tests/analysis/adaptive_research/test_e2e.py`, `tests/analysis/adaptive_research/fixtures/`, and `scripts/verify_adaptive_researcher.py` (or equivalent path matching the qualitative-research convention).

### 1\. Fixture data

`tests/analysis/adaptive_research/fixtures/`:

* `sector_briefs.json` — recorded `tuple[SectorBrief, ...]` payload from a representative pipeline invocation. Three briefs (one per sector); each carries findings, anomalies (with `SA-{SECTOR}-ANOM-N` IDs), and thesis candidates.
* `qualitative_brief.json` — recorded `QualitativeBrief` payload. Carries narrative threads (`QR-N`), catalyst watches (`QR-CW-N`), and a sentiment snapshot.
* `correlation_regime_brief.json` — recorded `CorrelationRegimeBrief` payload. Includes a `reference_index` keyed by `CR-N` IDs.
* `distillation_outputs.json` — recorded `DistillationOutputs` payload. Multi-block, mixed audience, mixed anomaly counts; representative volume-spike, correlation-breakdown, and macro-surprise flags.
* `universal_regime_label.json` — the `dict[str, Any]` regime payload (regime name, transition flag, confidence, freshness ts).

A fixture-loader helper (`tests/analysis/adaptive_research/fixtures/__init__.py`) exposes `load_e2e_fixtures()` returning the tuple `(sector_briefs, qualitative_brief, correlation_regime_brief, distillation_outputs, universal_regime_label)` — typed objects ready for `run_adaptive_researcher`.

Generation strategy: capture from a one-time run of the upstream pipeline against the production database (development machine has access via the network volume). Commit the resulting fixtures. Re-capture only when an upstream contract changes.

### 2\. End-to-end test

`tests/analysis/adaptive_research/test_e2e.py` — pytest test marked `@pytest.mark.e2e` and skipped when `CLAUDE_CODE_OAUTH_TOKEN` (or whatever the live-SDK env-var gate is named in the qualitative-research E2E story) is not set:

```python
@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), reason="needs live SDK")
async def test_adaptive_researcher_e2e_against_live_sdk(populated_db_session, agents_config, tmp_path):
    """Runs the adaptive researcher against fixture upstream briefs + live SDK + populated DB.

    Asserts:
      - runner returns a non-error AdaptiveResearcherResult
      - brief.threads is a tuple (possibly empty for low-anomaly fixtures)
      - all Strengthens/Weakens references resolve against the fixture upstream IDs
      - diagnostic archive at <tmp_path>/invocations/<inv_id>/analysis/adaptive_researcher/
        contains prompt.md, user_message.md, response_initial.md, errors.json, metadata.json
      - input bundle text contains all four section markers
      - tools_used in each thread (when threads exist) names registered tool IDs only
    """
```

### 3\. Runbook

`scripts/verify_adaptive_researcher.py` — a CLI script the operator runs ad-hoc to verify the runner against the production database. Mirrors the qualitative-research equivalent (which I expect lives at `scripts/verify_qualitative_researcher.py` per the established naming convention; check and match):

```
$ uv run python scripts/verify_adaptive_researcher.py [--invocation-id INV] [--archive-root PATH]
```

Loads the fixtures, invokes `run_adaptive_researcher` with a real session against the production DB and a real Claude SDK, prints the brief, the diagnostic archive path, the token + tool-call accounting, and exits zero on success. Uses the same fail-fast discipline as the qualitative-research equivalent (no try-except wrapping the runner — failures surface with full tracebacks).

### 4\. Runbook documentation

A short markdown runbook at `tests/analysis/adaptive_research/E2E_RUNBOOK.md` (or matching the qualitative-research convention) covering:

* Prerequisites: `CLAUDE_CODE_OAUTH_TOKEN` set, populated dev database accessible, fixtures present.
* How to run the e2e test: `uv run pytest -m e2e tests/analysis/adaptive_research/test_e2e.py -n auto`.
* How to run the ad-hoc verification script.
* How to refresh fixtures when upstream contracts change.
* Known limitations: this verification fakes the upstream pipeline; full live composition lands in the separate execution-layer story.

### Out of scope

* Live pipeline composition with upstream `run_external_distillation`, `run_qualitative_researcher`, and the three sector researchers — separate execution-layer story tracked in `docs/project-tracker.md` § Substantial.
* Replacing the fixture upstream with live calls into upstream runners — same separate story.
* Adding adaptive-researcher to a pipeline orchestrator that wires runner ordering — same.
* Any prompt-quality measurement (does the agent triage well?) — out of scope for verification; that's the feedback-loop machinery's job.
* CI integration — runs on demand, not in CI by default (the live SDK call costs tokens).

## Acceptance criteria

- [ ] `tests/analysis/adaptive_research/fixtures/` contains `sector_briefs.json`, `qualitative_brief.json`, `correlation_regime_brief.json`, `distillation_outputs.json`, `universal_regime_label.json`, and a `__init__.py` exposing `load_e2e_fixtures()` returning the typed tuple.
- [ ] `tests/analysis/adaptive_research/test_e2e.py` defines a `@pytest.mark.e2e`-marked async test that skips cleanly when the live-SDK env-var is absent.
- [ ] When the live-SDK env-var is set, the e2e test runs `run_adaptive_researcher` against the fixtures + populated DB + live SDK and asserts the returned `AdaptiveResearcherResult.brief` is a typed `AdaptiveBrief` (no exceptions, no parse/validation failures from the harness).
- [ ] The e2e test asserts every `Strengthens` / `Weakens` reference in every SIGNAL thread resolves against the fixture upstream IDs (no Layer-3 violations on the live output).
- [ ] The e2e test asserts the diagnostic archive populates `prompt.md`, `user_message.md`, `response_initial.md`, `errors.json`, `metadata.json` under `<archive_root>/invocations/<invocation_id>/analysis/adaptive_researcher/`.
- [ ] The e2e test asserts the rendered `input_bundle.bundle_text` contains all four section markers (`=== ADAPTIVE RESEARCH INPUT`, `=== VOLATILITY REGIME ===`, `=== DISTILLATION ANOMALY FLAGS`, `=== SECTOR-RESEARCHER ANOMALIES`).
- [ ] The e2e test asserts every `tools_used` entry in every thread (when threads exist) is in the seven registered tool IDs (`news_search`, `prediction_markets`, `earnings_commentary`, `sec_lending`, `short_interest`, `earnings_calendar`, `macro_data`, `ticker_deep_pull`).
- [ ] `scripts/verify_adaptive_researcher.py` exists, takes optional `--invocation-id` and `--archive-root` arguments, runs the runner against the production DB + live SDK, and exits zero on success.
- [ ] `tests/analysis/adaptive_research/E2E_RUNBOOK.md` (or matching path) documents prerequisites, how to run the e2e test, how to run the verification script, and how to refresh fixtures.
- [ ] The non-e2e portions of the test file pass under `uv run pytest tests/analysis/adaptive_research/test_e2e.py -n auto` (which skips the live-SDK test when the env-var is absent).
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

With `CLAUDE_CODE_OAUTH_TOKEN` set and the production DB accessible, run `uv run pytest -m e2e tests/analysis/adaptive_research/test_e2e.py -n auto` and confirm the test passes. Run `uv run python scripts/verify_adaptive_researcher.py --archive-root /tmp/adaptive_e2e` and confirm the script prints a brief and exits zero. Inspect the diagnostic archive at `/tmp/adaptive_e2e/invocations/<inv_id>/analysis/adaptive_researcher/` and confirm all five files are populated.

Without the live-SDK env-var, run `uv run pytest tests/analysis/adaptive_research/test_e2e.py -n auto` and confirm the e2e test is skipped with a clear reason and the file otherwise passes lint + mypy.