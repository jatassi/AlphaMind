# Runbook split + scripts/ housecleaning + /operate-prod skill — implementation spec

Grilled with the Operator 2026-06-10. Implementation-ready; the Linear issue (project:
Operational tooling) is filed after a workspace consolidation pass — the cap sits at
260/250 (ALP-842 rollup frees 20 slots; stale ALP-924 frees 1). The `CONTEXT.md`
glossary change (Actors → **Operator**) is already committed on this branch.

## 1. Split `scripts/RUNBOOK_production.md` into `docs/runbooks/`

Content moves verbatim — no editorializing beyond the one precondition addition below.
Pre-fix history blocks (§8.9 etc.) stay with their gotchas. Each new doc opens with a
one-line scope sentence. `scripts/RUNBOOK_production.md` and
`scripts/RUNBOOK_command_center.md` are deleted; no tombstones — `CLAUDE.md` nav and
the README map are the discovery path.

| New file (`docs/runbooks/`) | Source |
|---|---|
| `README.md` | New: one-line-per-doc map + the boundary rule: this directory holds procedures executed against the live prod system; verification companion docs live beside their scripts under `scripts/verify/`. |
| `update-loop.md` | §1 |
| `bootstrap.md` | §2 |
| `invocations.md` | §3 + §4 |
| `monitoring.md` | §5 |
| `services.md` | Preamble service table + §7 + §10 |
| `troubleshooting.md` | §8 — the living gotchas doc; agents append per the skill's accumulation duty |
| `feedback-loop.md` | §9 — cross-points `docs/agents/feedback-loop-skills.md` |
| `command-center.md` | `scripts/RUNBOOK_command_center.md` with §6 folded in |
| `genesis-cutover.md` | Unchanged |

**The one substantive addition.** `update-loop.md` and `services.md` gain the
service-stop precondition: if the market is open AND open positions exist, obtain
explicit Operator confirmation before stopping services; otherwise proceed.

## 2. `scripts/` reorganization

| Destination | Files |
|---|---|
| `scripts/services/` | All 9 `install_*` / `uninstall_*` `.ps1` |
| `scripts/linear/` | `archive_linear_issues.py`, `check_linear_cap.py`, `export_linear_issues.py`, `linear_consolidation_candidates.py`, `linear_parity_diff.py`, `linear_stale_sweep.py` |
| `scripts/verify/` | All 9 `verify_*.py`, `build_e2e_report.py`, `snapshot_prod_for_debug_e2e.py`, `_e2e_report_assets/`, and the 4 companion docs: `RUNBOOK_end_to_end_verification.md`, `RUNBOOK_genesis_verify.md`, `RUNBOOK_position_thesis_model.md`, `RUNBOOK_counterfactual_replay_engine.md` |
| `scripts/ops/` | `reconcile_alpaca_state.py`, `run_revoked_key_drill.py`, `watch_invocation.sh`, `report_emergency_invocations.py`, `report_flag_rates.py`, `validate_universe.py` |
| `scripts/` root | `gen_package_nav.py`; `mutation/` unchanged |
| Deleted | `spike_alp288_structured_output.py` (referenced nowhere) |

Shim scripts (4-line wrappers over `src/alphamind/scripts/`) move freely — their tests
import the package. The standalone monoliths are loaded by tests via
`importlib.util.spec_from_file_location` with hardcoded `parents[2] / "scripts" /
"<name>.py"` paths: update `_SCRIPT_PATH` in `tests/scripts/test_build_e2e_report.py`,
`tests/scripts/test_verify_debug_e2e.py`,
`tests/scripts/test_verify_position_thesis_model.py` (and sweep `tests/scripts/` for any
other path-based loader). `_e2e_report_assets/` moves together with
`build_e2e_report.py`; confirm its asset resolution is `__file__`-relative.

## 3. The `/operate-prod` skill

One skill at `.claude/skills/operate-prod/SKILL.md`, task passed as the argument. No
`references/` directory — canonical procedures live in `docs/runbooks/` and the skill
never restates them (per `docs/agents/` skill-prose discipline).

**Routing table.** `update` → `update-loop.md` · `invoke` → `invocations.md` (+
`monitoring.md` to watch completion) · `watch` → `monitoring.md` · `restart` →
`services.md` (restarts honor the update-loop ordering rule) · `triage` →
`troubleshooting.md`.

**Pointer-only entries** (named, not routed): feedback-loop cadences → the
`feedback-review` / `feedback-validate` / `feedback-retrospective` skills +
`docs/runbooks/feedback-loop.md`; bootstrap → `bootstrap.md` (Operator-supervised);
genesis cutover → `genesis-cutover.md` (Operator-executed — the skill does not run it).

**Standing directives.** Prod is Windows / NSSM / elevated PowerShell. `.env` never
auto-sources — load it explicitly. Single-writer discipline: no DB writes outside
sanctioned CLIs. Confirm the Alpaca `account_number` before treating broker output as
prod evidence. Establish local time + timezone before reporting any time.

**Autonomy gate.** Before any service-stopping route, evaluate the runbook
precondition; inside the risk window, halt and obtain Operator confirmation.
Never-autonomously: halt-mode changes, force-close, cancel-order, unsanctioned DB
writes, genesis cutover.

**Self-correction duty.** After every executed procedure, compare observed behavior
against the runbook followed and against this skill. Where they diverge, propose the
correction: new experiential gotchas append to `docs/runbooks/troubleshooting.md`;
runbook/skill inaccuracies become edits. Deliver corrections as a docs-only PR from a
fresh branch (CI skips docs paths; cheap merge), then restore the checkout to clean
`main` — the update loop requires it. If pushing or opening a PR is unavailable from
prod, present the exact diff to the Operator in-session instead.

## 4. Reference updates

- `src/alphamind/command_center/__main__.py` + `config.py`: message/docstring text
  `RUNBOOK_command_center.md` → `docs/runbooks/command-center.md`; keep § anchors
  aligned with the new doc's headings.
- `tests/command_center/test_main.py` (≈line 135): assertion text matches the new
  message.
- `.claude/skills/draft-user-stories/SKILL.md`, `.claude/skills/refine-issue/SKILL.md`:
  `scripts/RUNBOOK_production.md` pointers → `docs/runbooks/`.
- Root `CLAUDE.md`: navigation mention of runbook location.
- `docs/project-tracker.md`: live pointers.
- `.ps1` installers and `verify_*` / `src/alphamind/scripts/*` docstrings that name
  `scripts/RUNBOOK_*` paths: grep-sweep and update.
- `docs/design/**` stays untouched — point-in-time, not code-truth.
- Final gate: no `scripts/RUNBOOK_` references remain outside `docs/design/`,
  `docs/_archive/`, `.archive/`.

## 5. Dead-pointer guard extension

`tests/docs/test_claude_md_pointers.py`: extend the scan set to `docs/runbooks/**/*.md`
and `.claude/skills/operate-prod/SKILL.md`; narrow the `.claude/` skip to
`.claude/worktrees/`. Fenced-code-block skip semantics unchanged. The runbooks carry
`%USERPROFILE%`-style Windows paths — the existing absolute-machine-path exemption must
keep them unflagged.

## 6. Delivery + verification

Single PR (docs + scripts + skill + reference updates + guard extension together — the
`src/`/`tests/` edits make it CI-gated, so the extended guard validates the new pointer
web before merge). Verification: the standard lint chain, then
`uv run pytest tests/docs/ tests/scripts/ tests/command_center/ -n auto`, then the §4
final-gate grep.

## Out of scope

- Two-tier migration of the four standalone monoliths into `src/alphamind/scripts/`
  (separate follow-up issue if ever).
- Runbook content rewrites beyond the move and the §1 precondition addition.
- Any change to the three feedback-loop skills.
