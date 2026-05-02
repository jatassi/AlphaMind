---
status: done
completed_date: 2026-04-28
commit_id: 33c27cd
---

# 01 — Add state-delivery implementation link to risk-guardrails README

## Goal

Make the `docs/implementation/06-risk-guardrails/state-delivery/` work tree discoverable from the design entry point so a reader of `docs/design/06-risk-guardrails/README.md` can navigate from the existing state-delivery row in the sub-documents table to the implementation plan in one hop, mirroring the convention used by sibling work trees that surface their implementation plans next to the design doc reference.

## Reading

- `docs/design/06-risk-guardrails/README.md` — the file being edited; row 4 of the sub-documents table already names `state-delivery.md`
- `docs/design/06-risk-guardrails/state-delivery.md` — the design doc the link sits next to
- `docs/implementation/02-distillation-layer/replay-harness/01-add-replay-harness-link-to-distillation-readme.md` — sibling pattern for landing an implementation link from a design README without restructuring
- `docs/implementation/01-data-layer/portfolio-state/01-add-portfolio-state-link-to-data-layer-readme.md` — sibling pattern for landing the link inside a sub-documents table row rather than as a paragraph addition

## Depends on

None.

## Scope

In scope:
- Add a working relative link to `docs/implementation/06-risk-guardrails/state-delivery/` in the existing `Guardrail state delivery` row of the sub-documents table in `docs/design/06-risk-guardrails/README.md`.
- The implementation link renders as `[Implementation plan](../../implementation/06-risk-guardrails/state-delivery/)` (relative-path-from-README; verify the path resolves).
- Placement: append a parenthetical or trailing sentence to the existing description cell — do not split the row into a new column, do not restructure the table.

Out of scope:
- Any change to the wording of the existing design content beyond the minimal addition needed to land the link.
- Adding implementation links for the other four risk-guardrail sub-documents (rules-and-limits, regime-adaptation, breach-behavior, guardrail-evaluation) — those are owned by their own work trees and land each one's own README link story.
- Adding a top-level `README.md` inside `docs/implementation/06-risk-guardrails/state-delivery/` — deferred until enough sibling work trees ship to warrant a section index.
- Linking from `state-delivery.md` itself — the README is the canonical entry point and avoids duplication.

## Notes

State the link positively. Do not narrate that the implementation plan is "new" or that the README "previously" lacked the link — per `feedback_no_decision_trails.md`, contracts are stated authoritatively. One short addition is enough.

The risk-guardrails README's sub-documents table is the canonical entry point for this design area; placing the implementation link inside the row's description column keeps the design ↔ implementation pair in the reader's eye-line without disturbing the table shape that the other rows share.

## Acceptance criteria

- [ ] `docs/design/06-risk-guardrails/README.md` contains a working relative link to `docs/implementation/06-risk-guardrails/state-delivery/`.
- [ ] The link's relative path resolves from the README's location.
- [ ] The link is co-located with the existing `state-delivery.md` row in the sub-documents table — not added as a separate paragraph, separate section, or new table column.
- [ ] No content unrelated to the state-delivery implementation link is removed or restructured.
- [ ] All pre-existing relative links in the README still resolve.
