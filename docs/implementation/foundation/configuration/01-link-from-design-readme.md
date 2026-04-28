---
status: not_started
completed_date:
commit_id:
---

# 01 — Link configuration management from design README

## Goal

Make `docs/design/configuration-management.md` discoverable from `docs/design/README.md` so the doc index reflects the feature. The design doc is a top-level reference (sibling to `command-center.md`, `feedback-loop.md`, `cost-and-rate-limit-modeling.md`), but the README's reference-document table currently does not list it.

## Reading

- `docs/design/README.md` — current state of the design index
- `docs/design/configuration-management.md` — the doc to link to

## Depends on

None.

## Scope

In scope:
- One row added to `docs/design/README.md`'s "Reference documents" table linking `configuration-management.md` with a one-line description matching the tone of the existing rows.

Out of scope:
- Any other edit to `README.md` or to `configuration-management.md`.
- Editing `docs/project-tracker.md` (the orchestrator updates that as stories complete; the tracker entry already exists).

## Notes

The `Reference documents` table currently lists pipeline-overview, asset-universe, design-decisions, command-center, cost-and-rate-limit-modeling, and feedback-loop. Configuration management is the same shape — a top-level reference doc that constrains every layer — and belongs on that table, not under any layer-specific section.

Description should be terse and match the table's voice. A working draft: _"YAML configuration tree, composition model, validation layers, snapshot persistence."_

## Acceptance criteria

- [ ] `docs/design/README.md` "Reference documents" table contains a row with a working relative link to `configuration-management.md`.
- [ ] The description column on that row is one line and reads in the same voice as the surrounding rows.
- [ ] No existing rows are removed or reordered.
- [ ] All pre-existing relative links in `docs/design/README.md` still resolve.
