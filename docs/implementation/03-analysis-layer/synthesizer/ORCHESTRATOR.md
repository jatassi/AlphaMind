You are orchestrating completion of the AlphaMind synthesizer agent. Thirteen user stories live at `docs/implementation/03-analysis-layer/synthesizer/` (files `01-...md` through `11-...md`, with letter suffixes `05a`/`05b` and `06a`/`06b` for parallel-eligible groups). Each story is self-contained — read it before you act on it.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, the single-line README edit in story 01, file-existence checks.

**Model selection per CLAUDE.md.** Mechanical changes use Sonnet; everything else uses Opus. The split for this work tree:
- **Sonnet:** 01 (one-line README link).
- **Opus:** every other story. The synthesizer involves contract decomposition (per-source brief adapters, retrieval-store assembly, reference-marker extraction), Agent SDK harness wiring with Layer-4 stop-reason classification, system-prompt authoring with anti-pattern instrumentation, and runner composition with cross-tree dependencies (domain-researchers `SectorBrief`, distillation `CorrelationRegimeBrief`) — Sonnet's mechanical-change strength is the wrong fit for the bulk of this work.

Always include the model name in the `Agent` tool's `description` field per CLAUDE.md: `[Opus] Implement story 04 — reference-marker extraction`. Per the user's memory, this makes model selection visible at a glance.

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. The filename convention (letter suffixes — `05a`/`05b`, `06a`/`06b`) marks parallel-eligible groups. When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, validator catches the boundary case, prompt's example text resolves through `parse_reference_id`). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/03-analysis-layer/synthesizer/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive code work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, prompt structure) are verified by post-implementation inspection.

For story 09 (system prompt), use the `agent-system-prompts` skill (`Skill("agent-system-prompts")`) — it is the project skill specifically for AlphaMind agent system prompt authoring per its frontmatter. The skill encodes the tag conventions, anti-pattern catalog, and the prose discipline these prompts require.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only:

  uv run ruff check .
  uv run ruff format .
  uv run mypy

Per CLAUDE.md, alert the orchestrator before disabling the linter or any rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`). Do not silently suppress.

When done:
1. Run `uv run pytest` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z").

If you hit a blocker — schema gap, ambiguous spec, test that won't pass without scope creep, design-doc cross-reference that doesn't resolve, cross-tree dependency not yet landed — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/03-analysis-layer/synthesizer/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Always tag the model name in the `description` per CLAUDE.md.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state. For story 09 (system prompt), the spot-check includes scanning the worked-example reference IDs through `parse_reference_id` to confirm syntactic well-formedness; for story 11 (verification script), the spot-check confirms importability and the operator-report format matches the documented shape.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 13 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
Level 0 (independent):
    01            02
                  ↓
Level 1 (deps on 02):
                  ↓
                  03
                  ↓
Level 2 (deps on 03):
                  ↓
                  04         05b ← 02
                  ↓           ↓
Level 3 (deps on 04 / 05b):
            ┌─────┴─────┐
           05a         06b ← 05b
            ↓           ↓
Level 4:    06a ← 05a   |    07 ← 04
            |           |    ↓
Level 5:    |           +→  08 ← 02, 06b, 07     09 ← 04, 06b, 07
            +→ 06a       ↓
Level 6:                10 ← 02, 05a, 06a, 06b, 07, 08, 09
                         ↓
Level 7:                11 ← 09, 10
```

01 and 02 dispatch immediately in parallel — independent. 03 (BriefBundle data model) unblocks once 02 lands. 04 (extractor) and 05b (portfolio-state read protocol) unblock once 03 / 02 respectively land — they can run in parallel with each other. 05a (retrieval store + adapters) needs 04. 06a (`retrieve_brief` MCP tool) needs 05a; 06b (portfolio-state MCP tools) needs 05b. 07 (input bundle assembler) needs 04. 08 (harness) needs 02 + 06b + 07; 09 (system prompt) needs 04 + 06b + 07. 08 and 09 can run in parallel. 10 (runner) needs everything (02, 05a, 06a, 06b, 07, 08, 09). 11 (verification) needs 09 + 10.

The longest path is 02 → 03 → 04 → 05a → 06a → 10 → 11 (7 stories). Parallelism collapses it; expect total wall-clock to land around 5–7 sequential agent runs if the parallel groups dispatch cleanly.

## Cross-work-tree dependencies

Story 05a (retrieval store + adapters) and story 10 (runner) reference types from sibling work trees:

- **`SectorBrief`** from `alphamind.analysis.domain_researchers.models` — defined in the domain-researchers tree's [story 03](../domain-researchers/03-sector-brief-data-model.md). Adapters in story 05a accept `SectorBriefProtocol` (a `typing.Protocol` matching the documented shape) so this work tree's tests do not depend on the domain-researchers tree being complete on `main`. Production wiring (story 10's `UpstreamBriefs.tech_semis: SectorBriefInput`) uses the real type.

- **`CorrelationRegimeBrief`** from `alphamind.distillation.correlation_brief` — defined in the distillation tree's [story 11b](../../02-distillation-layer/11b-correlation-regime-brief-assembly.md). Same protocol pattern (`CorrelationRegimeBriefProtocol`).

- **`RegimeLabel`** from `alphamind.analysis.domain_researchers.input_bundle` — defined in the domain-researchers tree's [story 08](../domain-researchers/08-input-bundle-assembler.md). Used directly (no protocol — the type is small and the cross-tree dependency is acceptable; a duplicate definition would diverge from `feedback_no_inventing_component_names.md`).

If any of these cross-tree types is not yet on `main` when stories 05a / 07 / 10 dispatch, **surface this dependency at dispatch time**. The protocols (`SectorBriefProtocol`, `CorrelationRegimeBriefProtocol`) let the work tree complete in isolation; production wiring completes when the cross-tree types land. For `RegimeLabel`, if domain-researchers story 08 has not landed, story 07 here is blocked — coordinate the order.

The qualitative-research and adaptive-research implementation work trees do NOT exist yet. Stories 05a (`raw_brief_to_bundle`) and 10 (`UpstreamBriefs.qualitative` / `.adaptive` as `RawBriefInput(text, freshness)`) handle this via a generic raw-text adapter. When those work trees land, they may introduce typed adapters analogous to the sector / CR ones, at which point a small refactor swaps `RawBriefInput` for the typed shapes — not blocking now.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- Cross-work-tree types not yet published (above).
- Schema gaps discovered when a story needs a field the upstream contract does not carry. Story 05a is the most likely surface for this — `SectorBriefProtocol` and `CorrelationRegimeBriefProtocol` need to match the actual cross-tree types' shapes when those land.
- Design-doc ambiguities (e.g., the bundle-ordering convention in story 07's Notes — `CR → SA-ENERGY → SA-FIN → SA-TECH → QR → AR` — should be confirmed against the analysis-layer execution flow doc when the pipeline orchestrator work tree lands). Surface the resolution choice; do not silently pick.
- Test failures the subagent could not resolve.
- API key or auth issues that block the verification script in story 11 (`CLAUDE_CODE_OAUTH_TOKEN` setup is operator territory).
- Cross-story coordination needs (e.g., the `metadata.json` shape that story 08's harness writes and story 10's runner amends with `unknown_markers_by_source` — both stories must agree on the field's name and type. Story 08 is the canonical writer; if the runner needs more than story 08 documents, surface the gap).
- System-prompt round-trip: story 09's worked-example reference IDs MUST resolve via `parse_reference_id` from story 03/04. If they do not, the prompt is malformed in a way the system itself catches at dispatch verification — re-dispatch with the specific IDs that failed.

## When you handle work directly

Skip delegation only when overhead exceeds the work:
- Story 01 (one-line README link).
- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving trivial conflicts when a subagent's commit fails to apply.

For everything else, delegate.

## Boundaries

- Do not push to remote — the user owns push timing.
- Do not amend commits — create new commits instead.
- Do not skip hooks (`--no-verify`, `--no-gpg-sign`).
- Do not dispatch a subagent without `isolation: "worktree"` — parallel work on the main checkout corrupts state.
- Do not declare a story `done` without `uv run pytest` green, `uv run ruff check .` clean, `uv run mypy` clean, and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done`.
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without alerting the user first per CLAUDE.md. If the subagent reports having done so, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let a subagent invent component names beyond those documented in the design and story files (per the user's memory `feedback_no_inventing_component_names.md`). The story files are the authoritative contract; the subagent's creativity is bounded by them. The component names introduced by this work tree (`BriefBundle`, `BriefSource`, `ReferencePrefix`, `SOURCE_PREFIXES`, `parse_reference_id`, `RetrievalStore`, `RetrievalAssemblyError`, `PortfolioStateReader`, `PositionSummary`, `ThesisSummary`, `ExposureSnapshot`, `SynthesizerInputBundle`, `HarnessSuccess`, `HarnessFailure` and subclasses, `SynthesisOutput`, `UpstreamBriefs`) are the implementation lexicon — subagents reuse them rather than coining new ones.
- Do not let a subagent introduce numeric anchors in the system prompt (story 09) — `feedback_avoid_numeric_anchors.md` explicitly rules out "at least N intersections", "no more than N contradictions", or per-section token caps. The harness's `output_token_budget` is the only numeric cap, and it sits at the SDK layer.
- Do not let a subagent introduce a corrective-retry path in the synthesizer's harness (story 08). The synthesizer's only validation surface is the Layer-4 stop-reason check per `llm-output-validation.md § Per-agent surface mapping`. A subagent that adds Layer 1-3 validation or a corrective retry has over-tightened the contract; treat as failed verification and re-dispatch with the contract restated.
