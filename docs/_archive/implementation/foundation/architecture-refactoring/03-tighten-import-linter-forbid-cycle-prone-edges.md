# 03 — Tighten import-linter (forbid cycle-prone edges)

## Goal

Now that stories 02a and 02b have eliminated the two system-spanning cycles, add the import-linter `forbidden` contracts that prevent the cycles from regrowing: `decision.* ↛ execution.*`, `portfolio_state.* ↛ risk_guardrails.*`, `distillation.* ↛ persistence.*` (the inversion that anchored the 10-module cycle). Also tighten the `_kernel/*` rule: nothing inside `_kernel/` may import from any other `alphamind.*` module. Remove every `ignore_imports` entry from story 01a that the cycle fixes have eliminated.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L1 — names the two forbidden contracts
* Story 01a (<issue id="93254003-faaf-4c77-a6b7-920879ed87e9">ALP-455</issue>) — the lenient scaffolding this story tightens
* Story 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) + 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — must complete before this story; they remove the violations the new contracts will catch
* `.claude/skills/python-architecture/references/foundations.md` § A2 — import-graph thesis
* `.claude/skills/python-architecture/scripts/analyze_imports.py` — also useful as a cross-check that contracts match graph state

## Depends on

* 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — must land first; otherwise the 10-module cycle violates the new contract
* 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — must land first; otherwise the 12-module cycle violates the new contract

## Scope

In scope: `pyproject.toml` (or `.importlinter`) — extend with forbidden contracts; remove obsolete `ignore_imports`. No code changes outside the linter config; if any consumer still violates a contract, surface to operator (likely indicates 02a/02b missed something).

### 1\. Add `forbidden` contracts

* `decision-not-execution`: `forbidden_modules = ["alphamind.execution"]`, `source_modules = ["alphamind.decision"]`
* `portfolio_state-not-risk_guardrails`: `forbidden_modules = ["alphamind.risk_guardrails"]`, `source_modules = ["alphamind.portfolio_state"]`
* `distillation-not-persistence-models`: scope of the contract is tighter — `distillation.*` may import from `persistence` engine but not from `persistence.models` directly (force going through a Protocol). For now, this is the same `forbidden` shape; if it produces too many false positives, downgrade to a `Layered` contract that places `persistence.models` strictly below.
* `kernel-leaf`: `source_modules = ["alphamind._kernel"]`, `forbidden_modules = ["alphamind"]` with the special-case exception that `_kernel` may import from itself (the linter has a syntax for this; use it).
* `commands-leaf` (paired with `_kernel-leaf`): `source_modules = ["alphamind.commands"]`, `forbidden_modules = ["alphamind"]` with the exception that `commands` may import from `_kernel` and from itself.

### 2\. Remove obsolete `ignore_imports`

Story 01a's lenient scaffolding included `ignore_imports` entries documenting known violations. After 02a+02b eliminate the cycles, those entries are obsolete. Remove every `ignore_imports` whose justifying comment named a punch-list item now closed. Any `ignore_imports` that remains must have a current justifying comment (typically pointing at a story still pending — e.g., the `distillation.* → sqlalchemy` violation that finding L6 / story 07 will address).

### 3\. Verify in CI

`uv run lint-imports` continues to exit 0; CI build green. The new contracts are enforced on every PR.

### Out of scope

Adding new contracts not derived from the audit's two cycles. Story 07 (distillation P1) and others may add their own contracts as they land; this story handles the two specific cycle-prevention contracts plus the kernel-leaf rules.

## Acceptance criteria

- [ ] `forbidden` contracts for `decision → execution` and `portfolio_state → risk_guardrails` exist in the import-linter config.
- [ ] `_kernel-leaf` and `commands-leaf` contracts exist forbidding either kernel package from importing any other `alphamind.*` module (except `_kernel` itself; `commands` may also import `_kernel`).
- [ ] `ignore_imports` entries justified solely by the two cycles (now broken) are deleted.
- [ ] `uv run lint-imports` exits 0 against `main` after 02a+02b are merged.
- [ ] Synthetically injecting `from alphamind.execution.oms import OpenCommand` into `decision/analyst/runner.py` produces a non-zero exit from `lint-imports` with a message naming the `decision-not-execution` contract.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

The new contracts fail clearly on the synthetic-violation injection above. CI build is green on `main`. The orchestrator can now treat `lint-imports` as a per-PR gate that catches new cycles immediately.