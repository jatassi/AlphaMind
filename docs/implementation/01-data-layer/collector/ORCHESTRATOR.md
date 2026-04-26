# Orchestrator prompt — data-layer collector build

A self-contained brief for the agent driving completion of the 19-story collector build. Hand this to a fresh Claude Code session; it does not need prior conversation context.

---

You are orchestrating completion of the AlphaMind data-layer collector. Nineteen user stories live at `docs/implementation/01-data-layer/collector/` (files `01-...md` through `08-...md`). Each story is self-contained — read it before you act on it.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `model: sonnet` unless the story note flags otherwise) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, single-line README edits, file-existence checks.

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. The filename convention (letter suffixes — `03a`/`03b`, `05a`–`05j`, `06a`/`06b`) marks parallel-eligible groups. When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (file exists, schema validates, function exhibits the documented behavior). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/01-data-layer/collector/<file>.md`.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, NSSM service config) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes, then lint and address all findings from your changes only.

When done:
1. Run `uv run pytest` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z").

If you hit a blocker — missing API key, ambiguous spec, test that won't pass without scope creep — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/01-data-layer/collector/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message.
3. **Verify.** When agents return, for each:
   - Run the relevant `uv run pytest` scope to confirm green.
   - Spot-check non-test acceptance criteria.
   - On pass: update frontmatter (`status: done`, `completed_date`, `commit_id`), commit (`chore: mark story <ID> done`).
   - On fail: re-dispatch with the specific gap noted.
4. **Repeat** until all 19 are `done`.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- Missing API keys (any vendor adapter's connectivity acceptance criterion will fail).
- Schema-spec ambiguities discovered mid-implementation.
- Test failures the subagent could not resolve.

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
- Do not declare a story `done` without `uv run pytest` green and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done`.
