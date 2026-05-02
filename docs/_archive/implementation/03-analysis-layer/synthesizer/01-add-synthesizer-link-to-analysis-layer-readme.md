---
status: not_started
completed_date:
commit_id:
---

# 01 — Add synthesizer implementation link to analysis-layer README

## Goal

Make the `docs/implementation/03-analysis-layer/synthesizer/` work tree discoverable from the analysis-layer design README, mirroring the data-layer's collector entry point, the distillation layer's external entry point, and the analysis-layer domain-researchers entry point.

## Reading

- `docs/design/03-analysis-layer/README.md` — current state; the entry point operators land on
- `docs/design/03-analysis-layer/synthesizer.md` — the design subtree the link targets
- `docs/implementation/03-analysis-layer/domain-researchers/01-add-domain-researchers-link-to-analysis-layer-readme.md` — pattern reference (parallel link from a sibling design README to an implementation work tree)

## Depends on

None.

## Scope

In scope:
- A short paragraph or section on `docs/design/03-analysis-layer/README.md` linking to `docs/implementation/03-analysis-layer/synthesizer/` so an operator reading the design layer can navigate to the implementation work tree.
- The link must use a relative path that resolves from the design README's location.
- If the domain-researchers link landed first (and added an "Implementation" section), append the synthesizer entry to that section rather than duplicating the framing.

Out of scope:
- Restructuring the existing component table or sequencing notes.
- Editing any other file (the implementation tree owns its own internal navigation).
- Linking from the top-level `docs/README.md` or design `README.md` — those pick up implementation trees through their own indices, not by direct cross-link from a sibling design README.

## Notes

Place the link as additional copy after the existing component table, not by restructuring the table. The "Implementation" framing should be positive — name the work tree and what it produces, not what was previously missing.

## Acceptance criteria

- [ ] `docs/design/03-analysis-layer/README.md` contains a working relative link to `docs/implementation/03-analysis-layer/synthesizer/`.
- [ ] No existing content is removed or restructured.
- [ ] All pre-existing relative links in the README still resolve.
