# 02 — Bake the testing policy into the dispatch skills

## Goal

Encode story 01's four test-quality rules as constraints the AlphaMind dispatch skills carry, so every future subagent dispatch enforces them at the point of work. In-repo skills (`implement-issue`, `orchestrate`) change via the repo PR; the global `/tdd` skill changes via a local-file edit. Skills point at the canonical wording rather than duplicating it.

## Reading

* `.claude/skills/implement-issue/SKILL.md` — dispatch + verification guidance; insertion point.
* `.claude/skills/orchestrate/SKILL.md` — per-wave dispatch + verification guidance.
* `~/.claude/skills/tdd/SKILL.md` — the global TDD skill (outside the repo).
* `CLAUDE.md` "## Testing" + `docs/agents/testing.md` — the canonical rules landed by story 01 (link to these).
* Parent `ALP-783`.

## Depends on

* `01` ([ALP-784](https://linear.app/alphamind-jatassi/issue/ALP-784/01-testing-policy-in-claudemd-docsagentstestingmd)) — the canonical rule wording must land first so the skills can point at it.

## Scope

### 1\. In-repo skills (part of the PR)

In `implement-issue/SKILL.md` and `orchestrate/SKILL.md`, add a short **Test-quality constraints** note to the dispatch/verification sections: the four-boundary mock rule, coverage-as-a-floor, the refactor-the-tests third step, and "no ceiling/source-grep invariant tests." Reference `CLAUDE.md` "## Testing" / `docs/agents/testing.md` — do not restate the full rules.

### 2\. Global `/tdd` skill (local edit, NOT in the PR)

Apply the same short insertion to `~/.claude/skills/tdd/SKILL.md`. This file lives outside the repo and is not under repo VCS — edit it in place via its absolute path and note in the PR description that the `/tdd` change was applied locally and is not part of the committed diff.

### Out of scope

* Writing the canonical rules — story 01 owns `CLAUDE.md` / `docs/agents/testing.md`.

## Acceptance criteria

- [ ] `implement-issue/SKILL.md` and `orchestrate/SKILL.md` reference the four test-quality rules in their dispatch/verification guidance and link to the canonical home.
- [ ] `~/.claude/skills/tdd/SKILL.md` carries the same reference (local edit).
- [ ] The PR description notes the `/tdd` edit was applied locally (outside repo VCS).
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` clean.

## Verification

Doc review. The repo PR touches only `.claude/skills/**` (+ nothing else), which CI `paths-ignore` (`.claude/**`) skips — merges on lint green. Confirm the `/tdd` local file shows the insertion.