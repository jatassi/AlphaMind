# 03b — Decision-layer fixture migration + initial-greeks persistence verification

## Goal

Migrate ad-hoc `ActiveRiskParameterSet` constructions in `verify_oms_commands.py` and decision-layer test fixtures to use `compose_phase_1_enforcement` from story 02. Add a regression test asserting that for an accepted OPEN command on options, the persisted PositionRecord's `OptionGreeks` matches the `validation_metadata.greeks` from the Acknowledgment — verifies the architecture's "Computed greeks are returned in the validation response and persisted as the position's initial greeks" promise (`docs/design/05-execution-layer/architecture.md` § Greek computation for options validation).

## Reading

* `docs/design/05-execution-layer/architecture.md` § 3. Guardrail enforcement layer — Greek computation for options validation.
* `docs/design/05-execution-layer/oms-commands.md` § OPEN — Validation metadata — the contract that OPEN acknowledgments carry computed greeks.
* `src/alphamind/scripts/verify_oms_commands.py` § ad-hoc `ActiveRiskParameterSet(...)` construction near line 755 — the call site to migrate.
* `src/alphamind/decision/portfolio_manager/runner.py` § `active_risk_parameters` parameter — runner signature unchanged; only callers migrate.
* `src/alphamind/decision/strategist/runner.py` § same parameter shape.
* `src/alphamind/decision/analyst/runner.py` § same parameter shape.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` § `Acknowledgment.validation_metadata.greeks` — source of validation-time greeks.
* `src/alphamind/execution/state_persistence/write_paths/phase2.py` § `persist_envelope_outcome` — persistence path that should land greeks on the PositionRecord.
* `src/alphamind/portfolio_state/records/positions.py` § `OptionGreeks`, `OptionsPositionDetails.greeks` — the persisted shape under verification.
* `src/alphamind/execution/guardrail_enforcement/orchestrator.py` (story 02) — for `compose_phase_1_enforcement`.

## Depends on

* [ALP-395](https://linear.app/alphamind-jatassi/issue/ALP-395/02-compose-phase-1-enforcement-orchestrator) (this work tree, story 02) — for `compose_phase_1_enforcement`.

## Scope

In scope: `src/alphamind/scripts/verify_oms_commands.py`, decision-layer test fixtures that construct `ActiveRiskParameterSet` ad-hoc, and a new test under `tests/execution/guardrail_enforcement/test_initial_greeks_persistence.py`. Runner signatures stay unchanged.

### 1\. `verify_oms_commands.py` migration

Replace the ad-hoc `ActiveRiskParameterSet(...)` construction (currently near line 755) with `compose_phase_1_enforcement(...)` against synthetic `RegimeAdaptationOutput` + `DrawdownState` inputs, with `progressive_tiers` loaded from `config/breach_behavior.yaml`. The Phase-3 PM-envelope-path test continues to operate on the same parameter values — the migration is structural, not behavioral.

### 2\. Decision-layer test fixture migration

Audit `tests/decision/` for ad-hoc `ActiveRiskParameterSet(...)` construction in test fixtures. Where present, migrate to construct via `compose_phase_1_enforcement(...)`. Acceptable to introduce a small fixture helper under `tests/decision/conftest.py` (or a sibling fixtures module) wrapping the orchestrator call for ergonomics; keep the helper thin (a few lines).

If after grep no decision-layer fixtures actually do this — only `verify_oms_commands` and `verify_state_persistence` (the latter handled by 03a) — note the absence and skip this sub-deliverable. The migration's value is wherever ad-hoc constructions exist; not creating new ones.

### 3\. Initial-greeks persistence regression test

`tests/execution/guardrail_enforcement/test_initial_greeks_persistence.py` (new file). Mirror an existing OMS-commands integration test that submits a PMEnvelope containing one OPEN command on an option. Assertions:

1. The returned `Acknowledgment.validation_metadata.greeks` is non-None.
2. After Phase-2 writeback completes, the persisted `PositionRecord.position_details` (cast to `OptionsPositionDetails`) has a `greeks` field equal to the Acknowledgment's `validation_metadata.greeks` (field-by-field comparison of delta / gamma / theta / vega / IV).

If this assertion fails — i.e., the Phase-2 writeback does NOT carry validation-time greeks through to the persisted PositionRecord — the test fails and the agent **pauses and surfaces the gap to the operator** rather than silently fixing by extending `phase2.py`. Per the parent issue's pre-resolved decision (B), persistence-layer changes are an [ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119/state-persistence) follow-up scope question.

### Out of scope

* Modifying the runners' signatures — they continue to accept `active_risk_parameters: ActiveRiskParameterSet` directly.
* Production composition wiring at pipeline-runner level — that is [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring).
* Refactoring `submit_envelope_mcp` out of `oms/`.
* Modifying `phase2.py` to add greek persistence if the test reveals a gap (pause and surface instead).
* Repository `active_risk_parameters_provider` migration (story 03a).

## Acceptance criteria

- [ ] `verify_oms_commands.py` no longer constructs `ActiveRiskParameterSet(...)` inline at the call site near line 755; it builds the value via `compose_phase_1_enforcement(...)` against synthetic `RegimeAdaptationOutput` + `DrawdownState` + `BreachBehaviorConfig.progressive_tiers`.
- [ ] `uv run python scripts/verify_oms_commands.py` runs against a freshly-migrated DB and produces `RESULT: PASS` (regression check).
- [ ] `tests/execution/guardrail_enforcement/test_initial_greeks_persistence.py` exists and contains at least one test that exercises the OPEN-options → Acknowledgment → Phase-2-writeback → PositionRecord path and asserts `validation_metadata.greeks` equals the persisted `OptionGreeks`.
- [ ] If the new test passes: parent issue's decision (B) is verified; record this as a note in the story's PR description so the operator can see the contract holds.
- [ ] If the new test fails: the agent pauses and surfaces the gap to the operator with a one-paragraph note explaining what was expected vs. observed; story does not claim "done" until the operator decides whether to (a) accept the gap as out-of-scope and mark the test xfail with explicit [ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119/state-persistence) reference, or (b) extend scope.
- [ ] Decision-layer test-fixture audit: grep for `ActiveRiskParameterSet(` under `tests/decision/` is documented (in the PR description); any found are migrated to use `compose_phase_1_enforcement` via a thin helper, OR the absence is noted.
- [ ] No runner signature changes (`runner.py` files in `decision/{analyst,strategist,portfolio_manager}/` are unchanged).
- [ ] `uv run pytest -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/guardrail_enforcement/test_initial_greeks_persistence.py -n auto -v` — passes (or fails-and-surfaces per the gap protocol above).
* Run `uv run python scripts/verify_oms_commands.py` — `RESULT: PASS`.
* Run `uv run pytest -n auto` — full suite green.
* Lint clean per CLAUDE.md.