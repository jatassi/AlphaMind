You are orchestrating completion of AlphaMind's Portfolio state work tree. Twenty user stories live at `docs/implementation/01-data-layer/portfolio-state/` (files `01-...md` through `09-...md`, with letter suffixes marking parallel-eligible groups). Each story is self-contained — read it before you act on it.

This work tree implements the data-layer's consumer-facing read surface: typed records for raw state categories 1–6, a master `PortfolioStateSnapshot`, a `PortfolioStateRepository` Protocol with a stub, snapshot-time computation modules, a snapshot assembler, per-consumer view projections, and a freshness contract. The work tree owns no production OMS reader — it owns the consumer-facing contract. The state-persistence and broker-adapter work trees, when they land, implement the production-side wiring against these contracts.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `model: sonnet` unless the story note flags otherwise, `isolation: "worktree"` always) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, single-line README edits, file-existence checks. Per the project's CLAUDE.md, include the model name in each agent's `description` field (e.g., `[Sonnet] Implement story 03a`).

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. Letter suffixes mark stories that *could* run in parallel; an explicit dependency on a same-letter sibling overrides the suffix. The dependency map for this work tree is non-trivial — see "Dispatch graph" below.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (the type exists, the validator rejects what it should, the function returns the documented value). Do not trust the subagent's self-report alone.

## Dispatch graph

The dependency graph for this work tree:

```
01 (README link, no deps)
  ↓
02 (skeleton + config)
  ↓
03a (positions)         03d (capital state)         03e (activity log)         03g (price provider)
  ↓                       ↓
03c (orders/brackets)    03f (thesis quality)
  ↓                       ↑
03b (theses) ────────────┘  (03b depends on 03c for BracketLegType)
  │
  └──────┬───────────┬───────────┬──────────────┐
         ↓           ↓           ↓              ↓
        04a (snapshot)  04b (repository)    (continues)

04a ──────┐
04b ──────┤
03g ──────┤
          ├→ 05a (position computations)
          │   ↓
          │  05b (P/L rollup) — also depends on 03a, 03d, 04a
          │   05c (exposure) — depends on 03a, 04a
          │   05d (capital/order/parameter computations) — depends on 03c, 03d, 04b
          │   05e (activity log filtering) — depends on 03e
          ↓
         06 (snapshot assembler) — depends on 03a–g, 04a, 04b, 05a–e
          ↓
         07 (per-consumer views/readers) — depends on 03a–f, 04a, 05e
         08 (freshness) — depends on 03a, 04a, 06
          ↓
         09 (end-to-end verification) — depends on 02–08
```

Practical dispatch order:

| Round | Stories | Notes |
|-------|---------|-------|
| 1 | `01` | One-line README edit; handle yourself. |
| 2 | `02` | Sequential; load-bearing for everything. |
| 3 | `03a`, `03d`, `03e`, `03g` (4 parallel) | Pure-records / Protocol stories with no inter-dependency on siblings. |
| 4 | `03c`, `03f` (2 parallel) | `03c` depends on `03a` (InstrumentType); `03f` depends on `03d` (RegimeLabel). |
| 5 | `03b` | Depends on `03c` (BracketLegType). |
| 6 | `04a`, `04b` (2 parallel) | Both depend on all of `03a–f`; mutually independent. |
| 7 | `05a`, `05b`, `05c`, `05d`, `05e` (5 parallel) | Computation modules; dependencies on `03*`/`04*` already satisfied. |
| 8 | `06`, `07` (2 parallel) | Both depend on `04a`+ records. `06` also depends on `05a–e`. |
| 9 | `08` | Refines `06`'s return type from `PortfolioStateSnapshot` to `AssembledSnapshot`; depends on `06`. |
| 10 | `09` | End-to-end verification; final gate. |

When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/01-data-layer/portfolio-state/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes, then lint and address all findings from your changes only.

When done:
1. Run `uv run pytest` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z").

If you hit a blocker — ambiguous spec, sibling-story drift, test that won't pass without scope creep — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/01-data-layer/portfolio-state/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`.
2. **Dispatch.** Group eligible stories per the dispatch graph. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Each agent's `description` includes the model name (e.g., `[Sonnet] story 03a — Position records`).
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Spot-check non-test acceptance criteria against the worktree state.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 20 are `done`.

## Cross-story convergence to flag

Several stories declare types that other stories import; verify the import is actually used as planned during verification:

- `BracketLegType` declared in `03c`; imported by `03b`.
- `InstrumentType`, `OptionContractType` declared in `03a`; imported by `03c`.
- `RegimeLabel` declared in `03d`; imported by `03f`.
- `SectorExposureEntry`, `DirectionalExposure` declared in `04a` (`snapshot.py`); imported by `05c`.
- `PortfolioPnL` declared in `04a` (`snapshot.py`); imported by `05b`.
- `AssembledSnapshot` declared in `08`; consumed by `06`'s refined return type.

If a subagent redeclares a type that should have been imported (or vice versa), flag it during verification and re-dispatch the story with the specific correction.

## Cross-tree convergence (out of this work tree's scope)

The synthesizer work tree (`docs/implementation/03-analysis-layer/synthesizer/`) declares its own slim `PortfolioStateReader` Protocol and value objects in `05b-portfolio-state-read-protocol.md`. Story 07 in this work tree declares the canonical analogue (`SynthesizerPortfolioStateReader`, `SynthesizerPositionSummary`, etc.). Convergence — the synthesizer migrating to import from this work tree — is a follow-up story, not this work tree's responsibility. Do not attempt it during this work tree's completion.

The state-persistence work tree (when it lands) implements the production-side `PortfolioStateRepository` against the OMS database. The Protocol declared here in `04b` is the contract that production wiring satisfies. This work tree's stub repository is the only concrete implementation at completion time.

The risk-guardrails work tree owns `RiskBudgetConsumption` and `ActiveRiskParameterSet` *computation* — the values populated on those records arrive via the production repository, which the enforcement layer wires. This work tree's stubs supply fixture-built values for testing.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- Cross-story type-import drift (e.g., a subagent redeclares `BracketLegType` after the convergence note in 03b).
- Schema-spec ambiguities discovered mid-implementation.
- Test failures the subagent could not resolve.
- Acceptance criteria a subagent attests to but you cannot verify.

## When you handle work directly

Skip delegation only when overhead exceeds the work:
- Story 01 (one-line README edit).
- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving trivial conflicts when a subagent's commit fails to apply.

For everything else, delegate.

## Story-specific dispatch notes

- **03e (activity log records)** — the largest story by line count; 35 per-event-type detail-payload classes plus the `EVENT_TYPE_TO_DETAIL_CLASS` and `EVENT_TYPE_TO_GROUP` exhaustive mappings. Sonnet handles this fine; the size is justified by scope. Verify the `mypy --strict` pass on the dict declarations catches missing-key cases.

- **04a (master snapshot)** — declares `PortfolioPnL`, `SectorExposureEntry`, `DirectionalExposure` *inline* in `snapshot.py` rather than in `records/capital.py`. The verification pass should confirm these did not migrate to records files.

- **05d (capital/order/parameter computations)** — preserves the `risk_budget.py` filename from story 02's package layout despite the narrower-than-original scope. The story's docstring explains; do not let a subagent rename the module.

- **06 (snapshot assembler)** — has a known limitation around option-contract pricing (uses `premium_paid_per_contract` as a stand-in). The story documents this; do not let a subagent expand scope to introduce an `OptionPriceProvider` Protocol — that is deferred to a future story.

- **08 (freshness)** — refines `06`'s return type from `PortfolioStateSnapshot` to `AssembledSnapshot`. The implementer must update `assembler.py` and the assembler's tests as part of the same commit. Verify both the freshness types and the refined assembler signature land together.

- **09 (end-to-end verification)** — the only story that produces no production code. Verifies the integration of all prior stories. If story 09 fails, the failing area points back to whichever earlier story has a contract drift; re-dispatch *that* story with the specific failure noted.

## Boundaries

- Do not push to remote — the user owns push timing.
- Do not amend commits — create new commits instead.
- Do not skip hooks (`--no-verify`, `--no-gpg-sign`).
- Do not dispatch a subagent without `isolation: "worktree"` — parallel work on the main checkout corrupts state.
- Do not declare a story `done` without `uv run pytest` green and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done`.
- Do not let a subagent expand scope across story boundaries — if a piece of code touches another story's territory, reject it and report to the user.
- Do not introduce new package directories beyond the layout fixed in `02-package-skeleton-and-config.md` — file paths in stories are authoritative.
