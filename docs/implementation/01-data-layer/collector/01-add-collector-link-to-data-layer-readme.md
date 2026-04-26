---
status: done
completed_date: 2026-04-26
commit_id: a528618
---

# 01 — Add collector subtree link to data-layer README

## Goal

Make the `docs/design/01-data-layer/collector/` subtree discoverable from the parent data-layer README.

## Reading

- `docs/design/01-data-layer/README.md` — current state
- `docs/design/01-data-layer/collector/README.md` — what to link to

## Depends on

None.

## Scope

In scope:
- A "Collection" section (or paragraph in the existing notes) on `docs/design/01-data-layer/README.md` linking to `collector/README.md`.

Out of scope:
- Restructuring the existing External / Internal sections.
- Editing any other file.

## Notes

The parent README is currently structured around an External / Internal data-source split. The collector subtree is a different axis — process-categorical, not source-categorical. A new short section (or addition to the existing "Note on..." paragraphs at the bottom) is the natural placement. State the contract positively; do not narrate that the subtree is "new" or that the README "previously" lacked the link.

## Acceptance criteria

- [ ] `docs/design/01-data-layer/README.md` contains a working relative link to `collector/README.md`.
- [ ] No existing content is removed or restructured.
- [ ] All pre-existing relative links in the README still resolve.
