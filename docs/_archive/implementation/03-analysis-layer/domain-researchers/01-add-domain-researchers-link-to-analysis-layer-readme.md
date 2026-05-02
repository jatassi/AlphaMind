---
status: not_started
completed_date:
commit_id:
---

# 01 — Add domain-researchers implementation link to analysis-layer README

## Goal

Make the `docs/implementation/03-analysis-layer/domain-researchers/` work tree discoverable from the analysis-layer design README, mirroring the data-layer's collector entry point and the distillation layer's external entry point.

## Reading

- `docs/design/03-analysis-layer/README.md` — current state; the entry point operators land on
- `docs/design/03-analysis-layer/domain-researchers/` — the design subtree the link targets
- `docs/implementation/01-data-layer/collector/01-add-collector-link-to-data-layer-readme.md` — pattern reference (parallel link from a design README to an implementation work tree)

## Depends on

None.

## Scope

In scope:
- A short paragraph or section on `docs/design/03-analysis-layer/README.md` linking to `docs/implementation/03-analysis-layer/domain-researchers/` so an operator reading the design layer can navigate to the implementation work tree.
- The link must use a relative path that resolves from the design README's location.

Out of scope:
- Restructuring the existing component table or sequencing notes.
- Editing any other file (the implementation tree owns its own internal navigation).
- Linking from the top-level `docs/README.md` or design `README.md` — those pick up implementation trees through their own indices, not by direct cross-link from a sibling design README.

## Notes

Place the link as an additional paragraph after the existing component table, not by restructuring the table. The "Implementation" framing should be positive — name the work tree and what it produces, not what was previously missing.

## Acceptance criteria

- [ ] `docs/design/03-analysis-layer/README.md` contains a working relative link to `docs/implementation/03-analysis-layer/domain-researchers/`.
- [ ] No existing content is removed or restructured.
- [ ] All pre-existing relative links in the README still resolve.
