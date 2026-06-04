# 01 — Testing policy in CLAUDE.md + docs/agents/testing.md

## Goal

Install the four test-quality rules from the bloat audit as enforceable, subagent-visible policy so future feature work stops re-accumulating bloat. Canonical home is the `CLAUDE.md` "## Testing" section (what every subagent reads); a fuller `docs/agents/testing.md` playbook carries rationale + examples. Doc-only change.

## Reading

* `CLAUDE.md` (repo root) — the "## Testing" and "## Linting" sections: insertion point + house style.
* `docs/agents/ci.md`, `docs/agents/linear.md` — the playbook format to mirror for the new `testing.md`.
* Parent `ALP-783` — pre-resolved decisions + the audit's process-fix rationale.

## Depends on

* none.

## Scope

Doc-only. No `src/` or `tests/` changes.

### 1\. `CLAUDE.md` "## Testing" — append a "Test-quality rules" subsection

State, concisely:

* **Mock only at the four sanctioned boundaries:** the LLM / Claude Agent SDK, the broker API (Alpaca), the system clock, and the database. Patching an internal collaborator (a runner, assembler, composition-root factory) is a smell — it tests wiring, not behavior.
* **Coverage is a floor, not a target.** A new test must assert a behavior or branch no existing test asserts — the gate is "does this test fail for a reason no other test fails for?". Do not add tests that only execute an already-covered line.
* **Red-green-refactor's third step includes the tests.** After driving out an implementation test-first, consolidate the new tests against each other and against the integration tests covering the same path; delete the scaffolding the coarser test subsumes.
* **Architecture / purity invariants belong in** `.importlinter` **contracts or ruff rules** — never in drifting-count "audit-baseline ceiling" tests or source-text AST greps (both retired in story 03).
* A one-line pointer to `docs/agents/testing.md`.

### 2\. New `docs/agents/testing.md`

A playbook expanding each of the four rules with a one-paragraph rationale and a concrete before/after example from the audit (e.g. the `pipeline/` `_CallLog` kwargs-capture battery as the altitude-rule violation; the `test_l4_broad_except_count_below_audit_baseline` ceiling — raised 14 times — as the ban example).

### Out of scope

* Editing the skills (`/tdd`, `implement-issue`, `orchestrate`) — story 02.
* Deleting or rewriting any test — story 03 and the Wave 2–4 stories.

## Acceptance criteria

- [ ] `CLAUDE.md` "## Testing" names the four mock boundaries and states internal-collaborator mocking is disallowed.
- [ ] `CLAUDE.md` states coverage-as-a-floor with the "fails for a reason no other test fails for" gate.
- [ ] `CLAUDE.md` states the refactor-the-tests third step.
- [ ] `CLAUDE.md` states architecture invariants go in `.importlinter` / ruff, not ceiling/grep tests.
- [ ] `docs/agents/testing.md` exists and expands all four rules with examples.
- [ ] `CLAUDE.md` links to `docs/agents/testing.md`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` all clean (doc-only — nothing should change).

## Verification

Doc review against the four rules; lint chain green. Pure-docs PR → CI `paths-ignore` skips the test run; merges on lint green.