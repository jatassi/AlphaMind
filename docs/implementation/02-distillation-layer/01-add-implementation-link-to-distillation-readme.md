---
status: done
completed_date: 2026-04-28
commit_id: 6fae525a0e12f083d443de2ca3bb613a87a5ba39
---

# 01 — Add implementation link to distillation README

## Goal

Surface the existence of this implementation work tree from the design entry point (`docs/design/02-distillation-layer/README.md`) so future readers can trace from the design doc into the per-story implementation plan without grepping.

## Reading

- `docs/design/02-distillation-layer/README.md` — the file being edited
- `docs/implementation/01-data-layer/collector/01-add-collector-link-to-data-layer-readme.md` — analogous prior story for the collector

## Depends on

None.

## Scope

In scope:
- Add a one-line cross-reference under the "External distillation" section in `docs/design/02-distillation-layer/README.md` pointing at `docs/implementation/02-distillation-layer/` (link rendered as `[Implementation plan](../../implementation/02-distillation-layer/)`).

Out of scope:
- Any change to the design content of the README.
- Changes to the internal-distillation section (separate work tree).
- Creation of a top-level README in `docs/implementation/02-distillation-layer/` (deferred until the internal implementation plan also lands).

## Notes

The cross-reference convention matches the data-layer pattern: design docs link to their implementation directory near the top of the relevant subsection, not in a footer. Place the link on its own line immediately after the existing "See [external.md](external.md)." sentence so the reader sees the design ↔ implementation pair together.

## Acceptance criteria

- [ ] `docs/design/02-distillation-layer/README.md` carries a link to `docs/implementation/02-distillation-layer/` in the External distillation section.
- [ ] The link renders correctly when rendered as Markdown (relative path resolves from the README's location).
- [ ] No other content in the file is changed.
