You are orchestrating completion of the AlphaMind risk-guardrails Regime Adaptation feature. Eleven user stories live at `docs/implementation/06-risk-guardrails/regime-adaptation/` (`01-...md` through `10-...md`, with letter suffixes for parallel-eligible groups). Each story is self-contained — read it before you act on it.

## What this work tree builds

The runtime resolver that bridges the distillation layer's volatility regime classification to the configuration resolver's runtime dimensions and produces the `ActiveRiskParameterSet` (raw state §4d) the rest of the system consumes. Inputs: the distillation regime block (label + VIX level + skip-emergency flag), the held-positions snapshot, the prior persisted regime adaptation state, the event calendar, the distillation composite alert state, and the loaded config. Outputs: the runtime dimensions for `compose_config(...)`, the per-rule effective limits (post-interpolation, post-overlay), the typed `ActiveRiskParameterSet`, the `RegimeTransitionBreach` records the strategist consumes, the regime-skip emergency passthrough, the new state to persist, and a tuple of audit log entries.

The library terminates at `resolve_regime_adaptation(...)` (story 09). The downstream caller (the pipeline runtime) calls this once per invocation, persists the new state, hands the runtime dimensions to `compose_config(...)`, and feeds the breach records to the state-delivery renderers. That orchestration lives in *future* work trees, not here.

The corresponding source-of-truth design is `docs/design/06-risk-guardrails/regime-adaptation.md` plus the upstream context in `rules-and-limits.md`, `state-delivery.md`, `breach-behavior.md`, and `guardrail-evaluation.md`.

## Cross-feature gates (read first)

Regime adaptation sits adjacent to three sibling work trees. The cross-cutting interfaces are defined; the implementations may or may not have shipped at dispatch time:

- **`rules-and-limits` work tree** ships `RuleRegistry`, the runtime accessor over `GuardrailsConfig.rules`. Story 09 (orchestrator) consumes a `Mapping[str, RuleMetadata]` constructed from the registry; until the rules-and-limits stories that produce `RuleRegistry` are `done`, dispatch story 09 with a hand-built `rule_metadata` fixture (the orchestrator's contract is the typed mapping, not the registry).
- **`guardrail-evaluation` work tree** ships the deterministic projection primitives. Regime adaptation does not consume these directly — the breach detector (story 07) compares positions to limits inline rather than calling out to the projection engine — so the cross-feature gate is informational rather than blocking.
- **`state-delivery` work tree** consumes `RegimeTransitionBreach` records (this work tree produces them) and the `ActiveRiskParameterSet` typed record (already shipped in `portfolio_state.records.capital`). The strategist guardrail state header renders the `Regime-transition breaches` block from the orchestrator's output. State-delivery's stories are independent of this work tree's stories; the integration is verified at story 10.

If any sibling work tree's required output is absent at dispatch time, dispatch this work tree's stories against fixtures and stubs as documented in each story's `Notes` section. Surface the gap to the operator before dispatching the dependent story; the operator decides whether to proceed against fixtures or wait.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when overhead exceeds the work — frontmatter updates, the single-line README edit in story 01, file-existence checks while planning a batch.

**Model selection.** Per CLAUDE.md, mechanical changes go to Sonnet and everything else to Opus. The split here:

- **Sonnet:** 01 (one-line README link).
- **Opus:** 02 through 10. Story 02 (package skeleton + typed records) is borderline-mechanical, but the typed-record invariants are non-trivial; default to Opus to avoid coordination cost. The pure-function stories (03, 04b, 04c, 06a, 06b, 07, 08), the persistence story (04a, with Alembic + ORM + repository semantics), the YAML loader (05), the orchestrator (09), and the integration tests (10) all carry algorithmic, contract, or composition nuance that warrants Opus.

Tag the model name in every `Agent` tool `description` per CLAUDE.md (`feedback_subagent_title_model_name`).

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. The filename convention (`04a`, `04b`, `04c`; `06a`, `06b`) marks parallel-eligible groups. When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest -n auto` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, fixture file content matches the design's worked example). Do not trust the subagent's self-report alone (`feedback_subagent_must_commit`).

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/06-risk-guardrails/regime-adaptation/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, fixture file presence, doc structure) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only.

When done:
1. Run `uv run pytest -n auto` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by fixture-string comparison Z").

If you hit a blocker — schema gap, ambiguous spec, sibling-work-tree primitive missing or shaped differently than the story expected, test that won't pass without scope creep — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/06-risk-guardrails/regime-adaptation/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`. For story 09, also confirm the `rules-and-limits` cross-feature gate (the `RuleRegistry` accessor) — if absent, dispatch with a fixture-built `rule_metadata` map and re-dispatch when the registry lands.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Tag the model name in the `description`.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest -n auto` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — story 01 edits the design README (confirm the link resolves and renders); story 02 lands `types.py` plus the empty submodule files; story 04a lands the SQLAlchemy table + Alembic migration (confirm the migration upgrades cleanly and the CHECK constraints exist); story 05 lands `config/event_calendar.yaml` (confirm the file exists and parses); story 10 commits the integration test files.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 11 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
01 (independent — README link)
02 (independent — package skeleton + typed records)
              ↓
        ┌─────┼─────┬─────┬─────┬─────┬─────┐
       03   04a   04b   04c    05   06b    07     (parallel — 7-way after 02 lands)
        │     │     │     │     │     │     │
        │     │     │     │     ↓     │     │
        │     │     │     │    06a    │     │     (after 05; runs alongside 08)
        │     │     │     │     │     │     │
        └─────┼─────┴─────┴─────┴─────┴─────┘
              │     ↓
              │     08 (after 03, 04b, 04c)
              │     │
              └─────┴────────────┐
                                 ↓
                                 09 (after 02, 03, 04a, 04b, 04c, 05, 06a, 06b, 07, 08)
                                 ↓
                                 10 (after 09 — and every prior story)
```

Wave 1 dispatches stories 01 and 02 in parallel.
Wave 2 dispatches stories 03, 04a, 04b, 04c, 05, 06b, 07 in parallel — seven concurrent subagents, each in its own worktree. (Story 06a needs 05 to land first; story 08 needs 03+04b+04c.)
Wave 3 dispatches stories 06a (after 05) and 08 (after 03+04b+04c) in parallel.
Wave 4 dispatches story 09 alone (after every prior story).
Wave 5 dispatches story 10 alone.

Critical path: 02 → 03 → 08 → 09 → 10 = 5 sequential subagent runs (or equivalently 02 → 05 → 06a → 09 → 10). Stories 04a/04b/04c/06b/07 ride along in wave 2 without extending the critical path.

The longest critical-path chain is 5 stories. The widest parallel batch is 7 concurrent subagents in wave 2.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:

- **`RuleRegistry` cross-feature gate (story 09).** Story 09 depends on a `Mapping[str, RuleMetadata]` derived from the rules-and-limits work tree's `RuleRegistry`. If `RuleRegistry` has not landed at story 09's dispatch time, surface and pause; the operator decides whether to proceed with a fixture-built map or wait. The orchestrator's contract is the typed mapping, so a fixture-build is straightforward; the *integration* with the registry happens in the pipeline-wiring story (future work tree).
- **`croniter` dependency for story 06a.** The pre-event activator uses `croniter` to enumerate scheduled firings. If `croniter` is not in `pyproject.toml`, surface — the scheduler implementation already depends on it, so the dependency is almost certainly already present.
- **Alembic head drift for story 04a.** The migration's `revises` value depends on the current Alembic head. If two parallel work trees land migrations at the same time, expect FF-merge to fail on the migration revision chain; the resolution is to set this story's revision to chain off the *other* story's revision, and the subagent re-runs the migration to verify the chain.
- **Schema drift between this work tree's expected typed inputs and the upstream produced shapes.** If the subagent finds that `Position.sector` is shaped differently than the breach detector expects (story 07), or `RiskBudgetEntry.unit` is missing the per-sector suffix story 08 expects, surface — coordinate a follow-up edit before re-dispatching.
- **Test failures the subagent could not resolve.** Standard surface; the orchestrator decides whether to re-dispatch with a clarification or escalate to the operator.
- **Subagent reports of disabled linter rules** (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) that aren't trivially-fixable. Per `feedback_lint_suppression_triage`, assess each suppression: trivial-fix unwarranted ones yourself; dispatch for nontrivial fixes; accept warranted ones with a note in the verification report. Do not pause to ask the user except for genuinely ambiguous cases.
- **Cross-doc edits.** None of the stories should edit `docs/design/06-risk-guardrails/regime-adaptation.md` or any sibling design doc. Story 01's README edit is the only design-doc touch. If a subagent proposes a design-doc edit to reconcile a found inconsistency, surface — design-doc edits are operator decisions, not subagent improvisation.

## When you handle work directly

Skip delegation only when overhead exceeds the work:

- Story 01 dispatch with Sonnet is borderline; if the link edit is trivial, you may inline the edit yourself rather than spawn a subagent. Confirm against CLAUDE.md's "do not delegate trivial work" guidance.
- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving trivial conflicts when a subagent's commit fails to apply — most frequently the `__init__.py` re-export-list union conflict when stories 03/04a/04b/04c/05/06b/07 finish out of order.
- Updating the project tracker entry in `docs/project-tracker.md` when all eleven stories are `done` (replace `_requirements pending_` with `_done_` and add the `[stories]` link).

For everything else, delegate.

## Boundaries

- Do not push to remote — the user owns push timing.
- Do not amend commits — create new commits instead.
- Do not skip hooks (`--no-verify`, `--no-gpg-sign`).
- Do not dispatch a subagent without `isolation: "worktree"` — parallel work on the main checkout corrupts state.
- Do not declare a story `done` without `uv run pytest -n auto` green, `uv run ruff check .` clean, `uv run mypy` clean, and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done` — and for story 09, confirm the `rules-and-limits` cross-feature gate (or proceed with a fixture-built `rule_metadata` per the gate-handling guidance above).
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without triaging per `feedback_lint_suppression_triage`. If the subagent reports having suppressed without warrant, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let any pure function in this work tree perform I/O. Stories 03, 04b, 04c, 06a, 06b, 07, 08 are pure-function stories; the only exception is story 04a (persistence) and story 09 (orchestrator's `select_most_recent_state` read). If a subagent's implementation introduces a `Session` argument to a pure-function module, the story fails verification and re-dispatches.
- Do not let a subagent invent a typed value object that duplicates an existing `portfolio_state.records.*` or `config.models.*` type. Per `feedback_no_inventing_component_names`, every typed input mirrors an existing upstream record; new types only land when they encode a regime-adaptation-specific concept absent upstream. The full canonical surface lives in `regime_adaptation/types.py` (story 02): `RegimeAdaptationState`, `RegimeAdaptationOutput`, `RegimeTransitionBreach`, `EventCalendarEntry`, `EventCalendar`, `OverlayActivationDecision`, `RegimeAdaptationAuditEntry`, `NextTransitionDecision`, `VixBoundaryThresholds`, `RuleMetadata`, `CompositeAlertState`, plus `StaleCalendarReport` (lands in story 05's event-calendar loader). Subsequent stories (03, 04b, 06b, 07) import from `types.py`; they must not redeclare.
- Do not coordinate cross-doc edits silently. If a subagent on any story needs to update `docs/design/06-risk-guardrails/regime-adaptation.md`, `state-delivery.md`, `breach-behavior.md`, `rules-and-limits.md`, or any other design doc to reconcile a found inconsistency, surface the conflict before the subagent writes the edit. Cross-cutting design-doc edits are operator decisions.
