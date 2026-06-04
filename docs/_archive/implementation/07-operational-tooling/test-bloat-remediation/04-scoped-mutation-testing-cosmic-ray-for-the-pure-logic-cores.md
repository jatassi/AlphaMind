# 04 — Scoped mutation testing (cosmic-ray) for the pure-logic cores

## Goal

Stand up in-place, per-module mutation testing for the six deterministic pure-logic modules the audit names, producing one mutation report per module. The reports are the evidence that the pure-compute tests actually constrain the math — the gate that de-risks the q7 (06b) and corporate-actions (06a) consolidations, where the plan is "drop the redundant DB-layer/per-postcondition tests because the pure tests pin the arithmetic."

## Reading

* Parent `ALP-783` — the audit's §6 deferred-mutation note (mutmut is not viable here; its whole-tree copy dies on a macOS/SMB artifact `/.VolumeIcon.icns`).
* The six target modules' source + tests (below).
* cosmic-ray docs (in-place mutator; no whole-tree copy).

## Depends on

* none.

## Scope

* Add `scripts/mutation/`: a per-module cosmic-ray config template + a runner `run_mutation.sh <module>` that runs cosmic-ray **in-place** (NOT mutmut), **single-threaded**, scoped to one source module and that module's tests, writing `scripts/mutation/reports/<module>.txt`.
* Target modules: `src/alphamind/portfolio_state/computations`, `execution/regt_margin_attribution`, `execution/corporate_actions`, `risk_guardrails/guardrail_evaluation`, `distillation/q7`, `risk_guardrails/breach_behavior`.
* `scripts/mutation/README.md` — how to run + how to read (surviving mutants = weak/redundant tests; killed = the test constrains the line).
* Produce baseline reports for at least the four gating modules: `portfolio_state/computations`, `execution/regt_margin_attribution`, `execution/corporate_actions`, `distillation/q7`.
* Invoke cosmic-ray ephemerally (`uv run --with cosmic-ray ...`) or as a dev-dep; do not break the existing dev env or dirty the lockfile unnecessarily.

### Out of scope

* Acting on the mutation results — that is 06a / 06b and any future tightening.

## Acceptance criteria

- [ ] `scripts/mutation/run_mutation.sh <module>` runs cosmic-ray in-place, single-threaded, scoped to the module + its tests, and writes a report.
- [ ] Baseline reports exist under `scripts/mutation/reports/` for the four gating modules.
- [ ] `scripts/mutation/README.md` documents run + interpretation.
- [ ] The dev environment and lockfile are not broken by the tooling.
- [ ] `uv run ruff check . && uv run mypy` clean for any added scripts.

## Verification

Run the harness on `portfolio_state/computations`; confirm a report with a mutation score is produced.

**Surfacing condition (do not burn more than one debugging pass):** if cosmic-ray *also* hits a filesystem/tooling wall on this repo (as mutmut did), STOP and surface to the operator. Mutation evidence is an enabling nicety; if it proves intractable, 06a/06b can proceed on assertion-reading judgment + the coverage regression guard, and this story is marked blocked rather than chased.