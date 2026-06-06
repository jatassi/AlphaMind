# 05 — End-to-end verification + runbook (debug-e2e as sole pipeline e2e surface)

## Goal

Make the new `--debug-e2e` mode the sole pipeline e2e verification surface. Delete every per-feature pipeline verify script + its testable body + its tests + its per-feature runbook. Keep four standalone non-pipeline scripts (operator monitoring / data-invariant / offline-utility checks) untouched. Replace `scripts/RUNBOOK_end_to_end_verification.md` entirely with a debug-e2e-focused central runbook that preserves still-applicable content from the legacy version. Ship `scripts/verify_debug_e2e.py` (no shim split — single operator-runnable script with embedded `main` + six check helpers) plus `scripts/RUNBOOK_debug_e2e.md` and `tests/scripts/test_verify_debug_e2e.py`. Rewrite `scripts/build_e2e_report.py` to consume the debug-e2e single-invocation archive instead of per-phase verify archives.

The original draft of this story scoped the new verify script to coexist with the legacy per-feature verify suite (`verify_pm.py`, `verify_synthesizer.py`, etc.). The operator changed direction on 2026-05-17: the legacy suite gets ripped out wholesale, and debug-e2e replaces it as the central e2e flow. The HTML-report tool (`build_e2e_report.py`) likewise gets rewritten around the new single-invocation archive shape.

## Reading

* `docs/design/debug-e2e-mode.md` — the design doc; §§ 3 (package layout), 4 (types), 6 (hardest-to-reverse decisions)
* `scripts/RUNBOOK_end_to_end_verification.md` — the legacy central runbook (62K); read before rewriting to extract still-applicable content (prerequisites, wall-clock + cost expectations, env-var setup, dependency-order reasoning the new flow inherits)
* `scripts/build_e2e_report.py` — the legacy HTML-report generator (984 lines); read before rewriting to understand the rendering machinery (dark-mode CSS, verdict badges, evaluation pills, anti-pattern chips, OMS-command rendering) worth preserving
* `scripts/verify_pipeline_scheduler.py` + `src/alphamind/scripts/verify_pipeline_scheduler.py` — canonical shim/body pattern the operator is deprecating (read once to understand the legacy shape, then delete)
* `ALP-501` (story 04) — provides the `--debug-e2e` flag this script exercises
* Parent issue `ALP-493` Pre-resolved decisions §§ (D), (E) — 13 phases, 9 SDK call pairs (the assertions `check_jsonl_ordering` enforces)

## Depends on

* `ALP-501` (04 — CLI integration + orchestrator branch + import-linter contract)

## Scope

This story has six coherent pieces. See acceptance criteria for the testable end state.

**(A) Delete the per-feature pipeline verifies.** Use `git rm` so the deletion is tracked. Target lists below.

`scripts/verify_*.py` (19 files): `verify_adaptive_researcher.py`, `verify_analyst.py`, `verify_broker_adapter.py`, `verify_continuous_monitor.py`, `verify_decision_pipeline.py`, `verify_distillation.py`, `verify_domain_researcher_failure_modes.py`, `verify_domain_researchers.py`, `verify_guardrail_enforcement.py`, `verify_oms_commands.py`, `verify_pipeline_scheduler.py`, `verify_pm.py`, `verify_proposal_pre_processor.py`, `verify_qualitative_researcher.py`, `verify_regime_transition.py`, `verify_regt_margin_attribution.py`, `verify_state_persistence.py`, `verify_strategist.py`, `verify_synthesizer.py`.

`src/alphamind/scripts/verify_*.py` (20 files): same 19 names as above plus `verify_corporate_actions.py` (which exists only in `src/`).

`tests/scripts/test_verify_*.py`: every paired test file matching the deleted scripts.

`scripts/RUNBOOK_*.md` (17 files): `RUNBOOK_adaptive_researcher.md`, `RUNBOOK_analyst.md`, `RUNBOOK_broker_adapter.md`, `RUNBOOK_continuous_monitor.md`, `RUNBOOK_corporate_actions.md`, `RUNBOOK_decision_pipeline.md`, `RUNBOOK_domain_researchers.md`, `RUNBOOK_guardrail_enforcement.md`, `RUNBOOK_oms_commands.md`, `RUNBOOK_pipeline_scheduler.md`, `RUNBOOK_pm.md`, `RUNBOOK_proposal_pre_processor.md`, `RUNBOOK_qualitative_researcher.md`, `RUNBOOK_regt_margin_attribution.md`, `RUNBOOK_state_persistence.md`, `RUNBOOK_strategist.md`, `RUNBOOK_synthesizer.md`.

**(B) KEEP — do not delete — these serve a different role (data-invariant / monitoring / offline-utility).** `scripts/verify_bootstrap.py` (263 lines, bootstrap DB invariants), `scripts/verify_ongoing_collection.py` (237 lines, collector freshness), `scripts/verify_bootstrap_calibration_mix.py` + `src/alphamind/scripts/verify_bootstrap_calibration_mix.py` + `tests/scripts/test_verify_bootstrap_calibration_mix.py`, `scripts/verify_position_thesis_model.py` (1602 lines, offline pure-function verifier), `scripts/RUNBOOK_position_thesis_model.md`, `tests/scripts/test_verify_position_thesis_model.py`.

**(C) Ship the new debug-e2e verify script.** No shim split — `scripts/verify_debug_e2e.py` is the operator-runnable script directly, with argparse-driven `main()` + six module-level check helpers. Tests import from `scripts.verify_debug_e2e` directly (the test author can use `importlib.util.spec_from_file_location` or add an `__init__.py` if needed, per their judgment).

Argparse surface: `--db-path PATH` (default `data/alphamind-debug-e2e.db`), `--archive-root DIR` (required), `--run-type {pre_open|...|emergency}` (default `market_hours_rolling`), `--reason TEXT` (default `verify_debug_e2e`).

The script subprocesses `uv run python -m alphamind.scheduler run --debug-e2e --once <run_type> --reason <text>` then runs six check helpers, each as a module-level function for testability.

`check_auth` — `CLAUDE_CODE_OAUTH_TOKEN` set; Alpaca creds NOT required.

`check_archive_directory` — `<archive-root>/invocations/<invocation-id>/` exists with `resolved_config.json` and `progress.jsonl`.

`check_jsonl_ordering` — parse `progress.jsonl`; assert 13 `phase_start` events in dependency order per parent § (D), each paired with `phase_done`; 9 `agent_request` / `agent_response` pairs per § (E); monotonic timestamps. The 13 phases are: `seed`, `phase1`, `snapshot_assembly`, `distillation`, `domain_researchers`, `qualitative`, `adaptive`, `synthesizer`, `analyst`, `strategist`, `pre_processor`, `pm`, `phase2`. The `domain_researchers` + `qualitative` pair overlaps (parallel TaskGroup), as does `analyst` + `strategist`. The ordering assertion must tolerate that overlap: each pair's two `phase_start` events may land in either order, but each `phase_done` follows its own `phase_start`.

`check_synthetic_portfolio_visibility` — open the debug DB post-invocation; assert `positions` has 8 rows, `position_theses` has 8 rows, `cash_ledger` has 1 row with `starting_cash_usd == 24_440.0`.

`check_no_alpaca` — grep the invocation log (`pipeline.log` or equivalent under the archive) for `alpaca-py` HTTP indicators; assert none.

`check_invocation_summary` — parse subprocess stdout JSON; assert `staleness_flag == false`, `commands_submitted >= 0`, `trigger_source == "debug_e2e_cli"`.

`main()` orchestrates: subprocess call + each helper + one PASS/FAIL line per check + exits 0 on full pass. `tests/scripts/test_verify_debug_e2e.py` covers each helper via mocks.

**(D) Replace** `scripts/RUNBOOK_end_to_end_verification.md` **and author** `scripts/RUNBOOK_debug_e2e.md`**.** Read the existing 62K legacy central runbook FIRST; extract content worth carrying forward (prerequisites, wall-clock + cost expectations, env-var setup, dependency-order reasoning the new flow inherits, failure-mode triage hints that translate). Replace the central runbook entirely with a debug-e2e-focused version organized around Purpose / Prerequisites / Invocation / Expected output / Failure-mode triage / Wall-clock + cost / Supporting standalone tools (one-line pointers at the 4 surviving standalone scripts). Ship `scripts/RUNBOOK_debug_e2e.md` per the original story spec (prerequisites + invocation + expected output + failure-mode triage table); if the central runbook fully covers the per-feature runbook, this file may be a brief pointer to the central one — implementer's judgment.

**(E) Operator-facing references.** Check `docs/project-tracker.md` for any references to the deleted verify scripts; update only what's load-bearing. `docs/_archive/` references are historical and should stay stale.

**(F) Rewrite** `scripts/build_e2e_report.py` **for the debug-e2e archive shape.** The legacy script (984 lines) reads per-phase verify archives that no longer exist after Part (A) and stitches them into an HTML report covering 11 phases. Rewrite to consume the debug-e2e single-invocation archive: `<archive-root>/invocations/<invocation-id>/progress.jsonl` (the canonical event log), `resolved_config.json`, `pipeline.log`, and any per-agent diagnostics the harness emits under the invocation directory. The new report renders one self-contained HTML page summarizing the single debug-e2e invocation: a header with the 13-phase verdict ribbon, a table of the 9 SDK calls with their `agent_response` field set (`duration_s`, `input_tokens`, `output_tokens`, `tool_calls`, `stop_reason`), the `InvocationSummary` (commands_submitted etc.), and a failure section that surfaces any phase that emitted no `phase_done` (incomplete invocation). Preserve the dark-mode CSS, verdict badges, and `<details>` collapsing patterns from the legacy script — they're operator-tested. Keep argparse minimal: `--archive-root DIR` (required) + `--invocation-id ID` (auto-discovered if exactly one invocation directory exists). Drop the `--phase-summary` JSON-override surface and the `DEFAULT_PHASE_SUMMARY` table — both are debt from the per-phase verify era.

Ship `tests/scripts/test_build_e2e_report.py` covering the new shape via small synthetic fixtures (a fake `progress.jsonl`, a fake `resolved_config.json`, a fake `pipeline.log`). The legacy script has no test file; ship one with the rewrite.

### Out of scope

* Future operator workflows that snapshot/restore the debug DB across runs (out of design § 8 — operator deletes the archive directory + debug DB between runs).
* Adding `--debug-e2e` to the production daemon path.
* Re-authoring functionality from the deleted per-feature verifies that doesn't translate to debug-e2e (e.g., highly specialized assertions that only made sense in isolation). If such an assertion is load-bearing for an operator workflow, surface to the orchestrator rather than improvising scope expansion.

## Acceptance criteria

- [ ] `scripts/verify_debug_e2e.py` exists as the operator-runnable script (no shim split). Exposes `main` + six module-level helpers (`check_auth`, `check_archive_directory`, `check_jsonl_ordering`, `check_synthetic_portfolio_visibility`, `check_no_alpaca`, `check_invocation_summary`).
- [ ] The 19 `scripts/verify_*.py` files, 20 `src/alphamind/scripts/verify_*.py` files, paired `tests/scripts/test_verify_*.py` files, and 17 `scripts/RUNBOOK_*.md` files enumerated under Scope (A) are deleted (verified by `git status` showing them as `D` and `ls` confirming absence).
- [ ] The four standalone scripts under Scope (B) and their paired tests + `RUNBOOK_position_thesis_model.md` are preserved unchanged.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is rewritten as the debug-e2e-focused central runbook (Purpose / Prerequisites / Invocation / Expected output / Failure-mode triage / Wall-clock + cost / Supporting standalone tools).
- [ ] `scripts/RUNBOOK_debug_e2e.md` exists (full per-feature runbook or brief pointer to the central one — implementer's judgment).
- [ ] `tests/scripts/test_verify_debug_e2e.py` covers each of the six check helpers via mocks.
- [ ] `check_jsonl_ordering` tolerates the `domain_researchers`/`qualitative` and `analyst`/`strategist` parallel overlaps correctly.
- [ ] `scripts/build_e2e_report.py` is rewritten to consume the debug-e2e single-invocation archive (`progress.jsonl` + `resolved_config.json` + `pipeline.log`); no references to deleted per-phase verify archives remain. The new report renders the 13-phase verdict ribbon, the 9 SDK call table with `agent_response` fields, and the `InvocationSummary`. Dark-mode CSS / verdict badges / `<details>` patterns are preserved.
- [ ] `tests/scripts/test_build_e2e_report.py` covers the new rendering shape via synthetic fixtures.
- [ ] `uv run pytest -n auto` passes (overall test count drops because of the test-file deletions — expected).
- [ ] Full linter chain clean (`ruff check`, `ruff format --check`, `mypy`, `lint-imports`).

## Verification

`uv run pytest tests/scripts/test_verify_debug_e2e.py tests/scripts/test_build_e2e_report.py -n auto` passes. End-to-end smoke (operator-runnable): `uv run python scripts/verify_debug_e2e.py --archive-root .archive/verify-debug-e2e-smoke` exits 0 with all PASS lines; `uv run python scripts/build_e2e_report.py --archive-root .archive/verify-debug-e2e-smoke` produces a valid HTML page summarizing the invocation.