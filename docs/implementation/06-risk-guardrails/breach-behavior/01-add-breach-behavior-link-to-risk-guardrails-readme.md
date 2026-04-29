---
status: not_started
completed_date:
commit_id:
---

# 01 — Add breach-behavior link to risk-guardrails README

## Goal

Add a single line to the [Risk guardrails design README](../../../design/06-risk-guardrails/README.md) pointing at this implementation directory, mirroring the per-feature link pattern already used for `rules-and-limits`, `guardrail-evaluation`, and `state-delivery`. Pure cross-reference plumbing — no new prose, no change to existing rows.

## Reading

- `docs/design/06-risk-guardrails/README.md` — the design-side index that this story extends. Note the "Sub-documents" table already lists breach-behavior; this story adds an implementation cross-reference next to or below that entry, matching whatever style the sibling features use after their stories landed.
- `docs/design/06-risk-guardrails/breach-behavior.md` — the design doc this work tree implements; no edits required, only referenced from the new line.
- `docs/implementation/06-risk-guardrails/state-delivery/01-add-state-delivery-link-to-risk-guardrails-readme.md` — sibling story precedent. Reuse the same edit shape and link convention.
- `docs/implementation/06-risk-guardrails/rules-and-limits/` and `docs/implementation/06-risk-guardrails/guardrail-evaluation/` — the other two sibling implementation directories whose READMEs are already linked. Match their format.

## Depends on

None.

## Scope

In scope:

- Edit `docs/design/06-risk-guardrails/README.md` to add a link to `docs/implementation/06-risk-guardrails/breach-behavior/` next to or alongside the existing `Breach behavior` row in the Sub-documents table (or in the same location and style the sibling features use). The exact placement matches whatever convention already exists post-rules-and-limits / state-delivery / guardrail-evaluation linking — replicate, do not invent.
- The new link reads "implementation" or "stories" (whichever the sibling rows use) and resolves to the relative path `../../implementation/06-risk-guardrails/breach-behavior/`.

Out of scope:

- Any structural reflow of the design README beyond the single added link.
- New design content (the breach-behavior design is complete; this story is purely cross-reference).
- Any change under `docs/implementation/06-risk-guardrails/breach-behavior/` itself — the directory is created by the orchestrator before dispatch (it must exist for the link to resolve).

## Notes

This is the trivial-edit story analogous to `01-add-state-delivery-link-to-risk-guardrails-readme.md`. The orchestrator may choose to inline the edit rather than dispatch a subagent; the story exists so the work is recorded and tracked alongside the rest of the work tree's progress.

The directory `docs/implementation/06-risk-guardrails/breach-behavior/` is populated by the other 13 stories in this work tree. The link added here resolves to the directory listing once those stories land their files; until then, the link resolves to a directory containing this `ORCHESTRATOR.md` and the 13 story files but no sub-products. That is acceptable — the link's job is to point future readers at where the implementation work is tracked, not at finished code.

## Acceptance criteria

- [ ] `docs/design/06-risk-guardrails/README.md` includes one new link resolving to `../../implementation/06-risk-guardrails/breach-behavior/` and rendered in the same style as the corresponding links for rules-and-limits, guardrail-evaluation, and state-delivery.
- [ ] No other content in `docs/design/06-risk-guardrails/README.md` changes (line-by-line diff against the prior commit shows exactly one added line — or the minimum number of added lines required to add the cross-reference in the established style).
- [ ] The added link resolves at the `mkdocs`/relative-path level: `cd docs/design/06-risk-guardrails && ls ../../implementation/06-risk-guardrails/breach-behavior/` lists at least `ORCHESTRATOR.md` and the per-story `.md` files.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass (no source changes; this is a docs-only edit and the baseline must remain green).
