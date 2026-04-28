---
status: done
completed_date: 2026-04-28
commit_id:
---

# 01 — Add replay-harness implementation link to distillation README

## Goal

Make the `docs/implementation/02-distillation-layer/replay-harness/` work tree discoverable from the design entry point so a reader of `docs/design/02-distillation-layer/README.md` can navigate from the existing replay-harness mention to the implementation plan in one hop, mirroring how the external-distillation implementation plan is already linked.

## Reading

- `docs/design/02-distillation-layer/README.md` — the file being edited; already names the replay harness in the threshold-values paragraph
- `docs/design/02-distillation-layer/replay-harness.md` — the design subtree the link points at
- `docs/implementation/02-distillation-layer/01-add-implementation-link-to-distillation-readme.md` — analogous prior story for external distillation (one-line cross-reference, no restructuring)
- `docs/implementation/01-data-layer/portfolio-state/01-add-portfolio-state-link-to-data-layer-readme.md` — sibling pattern under a non-collector design doc

## Depends on

None.

## Scope

In scope:
- Add a working relative link to `docs/implementation/02-distillation-layer/replay-harness/` in the existing replay-harness sentence in `docs/design/02-distillation-layer/README.md`. The current sentence reads: *"Regime-sensitive sanity checking of threshold edits runs through the [replay harness](replay-harness.md), which re-runs the deterministic layer..."* Extend it (or add an adjacent line) so the implementation plan is discoverable next to the design doc reference.
- The implementation link renders as `[Implementation plan](../../implementation/02-distillation-layer/replay-harness/)` (relative-path-from-README; verify the path resolves).

Out of scope:
- Any change to the wording of the existing design content beyond the minimal addition needed to land the link.
- Restructuring the README's table or sections.
- Linking from `replay-harness.md` itself — the README is the canonical entry point and avoids duplication.
- Adding a top-level `README.md` inside `docs/implementation/02-distillation-layer/replay-harness/` — deferred until other implementation subtrees in `02-distillation-layer/` follow the same convention.

## Notes

State the link positively. Do not narrate that the implementation plan is "new" or that the README "previously" lacked the link — per `feedback_no_decision_trails.md`, contracts are stated authoritatively. One short addition is enough; placement next to the existing design link puts the design ↔ implementation pair in the reader's eye-line.

The companion implementation link for external distillation already lives in the *External distillation* section. The replay harness sits in the threshold-values paragraph above; place this story's link in that paragraph, not in *External distillation*, so the design ↔ implementation pairing is preserved.

## Acceptance criteria

- [ ] `docs/design/02-distillation-layer/README.md` contains a working relative link to `docs/implementation/02-distillation-layer/replay-harness/`.
- [ ] The link's relative path resolves from the README's location.
- [ ] The link is co-located with the existing replay-harness mention in the threshold-values paragraph, not under External distillation.
- [ ] No content unrelated to the replay-harness link is removed or restructured.
- [ ] All pre-existing relative links in the README still resolve.
