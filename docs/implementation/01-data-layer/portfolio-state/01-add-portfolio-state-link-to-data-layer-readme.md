---
status: done
completed_date: 2026-04-27
commit_id:
---

# 01 — Add Portfolio state implementation link to data-layer README

## Goal

Make the `docs/implementation/01-data-layer/portfolio-state/` work tree discoverable from the data-layer design entry point so a reader of `internal/README.md` can navigate to the implementation plan in one hop, mirroring the collector subtree's discoverability.

## Reading

- `docs/design/01-data-layer/internal/README.md` — the file being edited; the index that already routes between raw portfolio state and derived metrics
- `docs/design/01-data-layer/internal/portfolio-state.md` — the design subtree the link points at
- `docs/implementation/02-distillation-layer/01-add-implementation-link-to-distillation-readme.md` — analogous prior story (one-line cross-reference, no restructuring)
- `docs/implementation/03-analysis-layer/synthesizer/01-add-synthesizer-link-to-analysis-layer-readme.md` — sibling pattern under a non-collector design doc

## Depends on

None.

## Scope

In scope:
- Add a working relative link to `docs/implementation/01-data-layer/portfolio-state/` from `docs/design/01-data-layer/internal/README.md`, placed near the document-structure table so a reader who has just oriented themselves on the categories sees the implementation plan beside the design.
- The link renders as `[Implementation plan](../../../implementation/01-data-layer/portfolio-state/)` (relative-path-from-README; verify the path resolves).

Out of scope:
- Any change to the wording of existing content.
- Restructuring the document-structure table or the consumer/cross-reference maps.
- Linking from `docs/design/01-data-layer/README.md` (the top-level data-layer README) — that file already points at `internal/README.md`, which is the right routing hop.
- Linking from `portfolio-state.md` itself — `internal/README.md` is the canonical entry point and avoids duplication.

## Notes

State the link positively. Do not narrate that the implementation plan is "new" or that the README "previously" lacked the link — per `feedback_no_decision_trails.md`, contracts are stated authoritatively. One short line is enough; placement near the document-structure table puts the design ↔ implementation pair in the reader's eye-line on first scan.

## Acceptance criteria

- [ ] `docs/design/01-data-layer/internal/README.md` contains a working relative link to `docs/implementation/01-data-layer/portfolio-state/`.
- [ ] The link's relative path resolves from the README's location.
- [ ] No existing content is removed or restructured.
- [ ] All pre-existing relative links in `internal/README.md` still resolve.
