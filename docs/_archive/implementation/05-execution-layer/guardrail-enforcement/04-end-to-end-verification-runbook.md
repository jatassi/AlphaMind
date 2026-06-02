# 04 — End-to-end verification + runbook

## Goal

Ship `scripts/verify_guardrail_enforcement.py` exercising the Phase 1 enforcement orchestrator end-to-end against a freshly-migrated DB with synthetic regime + drawdown inputs — verifies the composition primitive, the orchestrator, the repository provider helper, and snapshot integration all wire together correctly. Add `scripts/RUNBOOK_guardrail_enforcement.md` for the operator and insert the new phase into `scripts/RUNBOOK_end_to_end_verification.md` after regime adaptation so the composed parameter set is verified before downstream agents consume it.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 3. Guardrail enforcement layer.
* `scripts/verify_oms_commands.py` + `src/alphamind/scripts/verify_oms_commands.py` — sibling verify-script pattern (thin shim defers to `alphamind.scripts.verify_*.main`).
* `scripts/RUNBOOK_oms_commands.md` — sibling runbook style (operator prerequisites, invocation, expected output, failure-mode triage table).
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook to insert into; identify the right insertion point (after the regime adaptation phase).
* `src/alphamind/execution/guardrail_enforcement/composition.py` (story 01) — exercised by Phase 1.
* `src/alphamind/execution/guardrail_enforcement/orchestrator.py` (story 02) — exercised by Phase 2.
* `src/alphamind/execution/guardrail_enforcement/repository_provider.py` (story 03a) — exercised by Phase 3.
* `src/alphamind/portfolio_state/assembler.py` § Step 2 `repository.get_active_risk_parameters()` — the consumer Phase 4 verifies.

## Depends on

* [ALP-396](https://linear.app/alphamind-jatassi/issue/ALP-396/03a-repository-active-risk-parameters-provider-helper) (this work tree, story 03a) — for `make_active_risk_parameters_provider`.
* [ALP-397](https://linear.app/alphamind-jatassi/issue/ALP-397/03b-decision-layer-fixture-migration-initial-greeks-persistence) (this work tree, story 03b) — for the migrated `verify_oms_commands.py` pattern (so the new verify script can mirror the same harness shape).

## Scope

In scope: the verify script, the operator runbook, and the central runbook insertion. The verify script's logic should be exercised by a unit test (mirroring `tests/scripts/test_verify_oms_commands.py`) so the script doesn't drift untested.

### 1\. Verify-script entry shim

`scripts/verify_guardrail_enforcement.py` (new file). One-line shim deferring to `alphamind.scripts.verify_guardrail_enforcement.main`. Mirror `scripts/verify_oms_commands.py` exactly — same shape, same docstring template, swap names.

### 2\. Verify-script implementation

`src/alphamind/scripts/verify_guardrail_enforcement.py` (new file). Phased verification:

**Phase 1 — composition primitive.** Exercise `compose_active_risk_parameters` across the documented tier cases: no-tier (drawdown 0%), CONSTRAINED (clears tier-1 trigger), HEAVILY_CONSTRAINED (clears tier-2), FULL_HALT (clears tier-3). Each case asserts the returned `(ActiveRiskParameterSet, DrawdownTier | None)` matches the expected tier and override-applied parameters. All tier triggers loaded from `config/breach_behavior.yaml`.

**Phase 2 — orchestrator.** Build a synthetic `RegimeAdaptationOutput` (using the regime-adaptation orchestrator's existing fixture builders or a minimal hand-constructed value) and a `DrawdownState` for each tier case. Call `compose_phase_1_enforcement(...)` and assert the bundled `Phase1EnforcementResult` matches expectations.

**Phase 3 — repository provider.** Build a `SqlPortfolioStateRepository` against the freshly-migrated DB using `make_active_risk_parameters_provider(result)` for the active provider slot. Call `repository.get_active_risk_parameters()` and assert the returned set equals the result's `active_risk_parameters` field.

**Phase 4 — assembler integration.** Use the same repository to build a portfolio-state snapshot via the existing assembler (the script can mirror `verify_state_persistence.py`'s assembler invocation pattern). Assert `snapshot.active_risk_parameters` equals the composed result (modulo the assembler's `parameter_change_flag` enrichment, which is documented and acceptable).

**Phase 5 — print one** `[PASS]` / `[FAIL]` line per criterion + final `RESULT: PASS` / `RESULT: FAIL` summary.

The script accepts `--db-path` and `--output {text|json}` flags consistent with sibling verify scripts. No SDK invocation; no live broker contact. Sub-minute runtime against a fresh on-disk DB.

### 3\. Operator runbook

`scripts/RUNBOOK_guardrail_enforcement.md` (new file). Sections (mirror `RUNBOOK_oms_commands.md`'s structure):

* Prerequisites — DB migrations applied; `.env` not strictly required (no API keys).
* Invocation — `uv run python scripts/verify_guardrail_enforcement.py [--db-path PATH] [--output {text|json}]`.
* Expected output shape — sample `[PASS]` lines per phase + final summary.
* Failure-mode triage table — common causes (stale schema, missing `progressive_tiers` config entry, drift in `RegimeAdaptationOutput` shape) and recovery steps.

### 4\. Central runbook insertion

Insert the new phase into `scripts/RUNBOOK_end_to_end_verification.md`. Position: after the regime adaptation phase (which produces the `RegimeAdaptationOutput` this work tree's orchestrator consumes), before any downstream phase that consumes `snapshot.active_risk_parameters` (decision-layer agents, OMS envelope path). Include the same shape used by sibling phase entries — phase title, prerequisites, invocation, expected pass criteria, dependency notes.

### 5\. Unit test of the script's phase functions

`tests/scripts/test_verify_guardrail_enforcement.py` (new file). Mirror `tests/scripts/test_verify_oms_commands.py`'s pattern — import each phase function from `alphamind.scripts.verify_guardrail_enforcement`, drive against in-memory or fresh-on-disk fixtures, assert pass-line shape and overall outcome.

### Out of scope

* Live broker contact or LLM agent invocation.
* [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring)'s decision-layer pipeline composition wiring — separate work tree.
* Continuous-monitor scenarios (between-invocation breach detection) — [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor).
* Persistence of the composed parameter set to a new DB table — covered by the existing assembler flow.
* Greek-persistence regression — covered by story 03b's regression test.

## Acceptance criteria

- [ ] `scripts/verify_guardrail_enforcement.py` exists as a thin shim deferring to `alphamind.scripts.verify_guardrail_enforcement.main`.
- [ ] `src/alphamind/scripts/verify_guardrail_enforcement.py` exists with the four phase functions described above plus a `main()` entry point that parses CLI args and prints the phase summary.
- [ ] Running `uv run python scripts/verify_guardrail_enforcement.py` against a freshly-migrated DB produces `RESULT: PASS`.
- [ ] Each phase produces at least one `[PASS]` line per documented criterion.
- [ ] The script supports `--db-path PATH` (defaulting to a temp DB if not provided) and `--output {text|json}` consistent with sibling scripts.
- [ ] `scripts/RUNBOOK_guardrail_enforcement.md` exists with the four documented sections (Prerequisites / Invocation / Expected output / Failure-mode triage).
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is updated to include the guardrail-enforcement phase in the dependency-ordered phase list, positioned after regime adaptation and before decision-layer consumers.
- [ ] `tests/scripts/test_verify_guardrail_enforcement.py` exists and exercises each phase function directly.
- [ ] `uv run pytest -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run python scripts/verify_guardrail_enforcement.py` against a freshly-migrated DB — `RESULT: PASS`.
* Run `uv run pytest tests/scripts/test_verify_guardrail_enforcement.py -n auto -v` — all phase-function tests pass.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check `RUNBOOK_guardrail_enforcement.md` and the central-runbook insertion render as expected on GitHub.
* Lint clean per CLAUDE.md.