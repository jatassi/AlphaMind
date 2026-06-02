# Testing playbook

Test-quality rules and their rationale. Referenced from the root `CLAUDE.md`
"Test-quality rules" subsection.

## Rule 1 — Mock only at the four sanctioned boundaries

**Sanctioned boundaries:** LLM / Claude Agent SDK, broker API (Alpaca), the system
clock (`datetime.now` / `time.sleep`), and the database. Every other collaborator is
internal to AlphaMind and must be exercised through its real implementation.

**Rationale.** A mock on an internal collaborator asserts that the unit under test calls
a specific method with specific arguments — it tests the wiring between two objects, not
the behavior visible to an external observer. These tests are maximally fragile (any
refactor that renames or splits the collaborator breaks them) and maximally low-value
(they duplicate the structural knowledge that the integration test already exercises for
free). The four sanctioned boundaries are the only points where a real call would cross a
process boundary, require live credentials, or introduce wall-clock non-determinism;
everywhere else, use the real objects.

**Before (altitude violation):** The `pipeline/` test suite introduced `_CallLog`, a
custom kwargs-capture shim that replaced the real `BriefAssembler` and `AnalysisRunner`
inside `PipelineOrchestrator`. Dozens of test cases asserted `_CallLog.calls[0].kwargs ==
{"ticker": "AAPL", ...}` — verifying call shape rather than pipeline output. Every
internal refactor to the assembler or runner broke these tests without breaking any
real behavior.

**After (correct altitude):** The orchestrator tests drive `PipelineOrchestrator` with
the real assembler and runner (using an in-memory DB boundary mock and a stubbed LLM
boundary mock) and assert on the invocation record written to the DB and the signals
returned — outcomes visible to the next pipeline stage, not internal call shapes.

## Rule 2 — Coverage is a floor, not a target

**Rationale.** A line-coverage percentage is a lower bound on test completeness, not a
quality metric. Chasing it produces tests that re-execute already-covered lines under
slightly different inputs without exercising any new branch, raising the percentage while
adding zero fault-detection value. The result is a test suite that is slower, harder to
read, and creates drag on every refactor — the audit found ~84 % of test LOC had
zero marginal coverage over the nearest sibling test.

**Gate.** Before adding any test, ask: "does this test fail for a reason no other test
currently fails for?" If the answer is no — because an existing integration test already
drives the same branch — the new test is redundant. Delete it or don't write it. If you
cannot answer the question, run `pytest --cov-context=test` and inspect
`CoverageData.contexts_by_lineno` to see which tests cover each line.

**Before:** Several `data_sources/` test files contained a dozen parameterized cases
that all drove the same `parse_bar` happy path with minor input variations; they differed
only in the specific ticker symbol or price value, not in any branch taken.

**After:** One parameterized case per distinct branch (happy path, missing field, bad
type, empty response). Four cases instead of fourteen; same branch coverage; suite runs
faster and failures are immediately locatable.

## Rule 3 — Red-green-refactor's third step includes the tests

**Rationale.** TDD's red-green-refactor loop is commonly applied only to production
code: write a failing test, make it pass, clean up the implementation. The refactor step
must extend to the tests themselves. When you drive out a feature test-first, the
scaffolding tests written to get to green are often finer-grained than the integration
test that now covers the full path. Leaving the scaffolding in place creates redundant
coverage (violating Rule 2) and a maintenance burden.

**Process.** After the implementation is green:

1. Identify every new test you wrote.
2. For each, ask whether a coarser integration test (one that drives a larger slice)
   now subsumes it.
3. Delete tests the coarser test subsumes; keep only tests that cover a branch the
   integration test does not reach.
4. Re-run the suite to confirm nothing regressed.

**Before:** A new `execution/` fill-reconciliation feature was driven out with a
battery of unit tests on `FillReconciler` in isolation (mocked `OrderBook`, mocked
`PositionStore`). The integration test added at the end drove the full fill pipeline
including real `OrderBook` and `PositionStore`. The unit battery remained alongside it,
quadrupling test count for the feature with zero additional fault detection.

**After:** After the integration test was green, the unit battery was deleted. Three
targeted tests for the two edge branches (duplicate fill, over-fill) that the integration
test did not exercise were retained.

## Rule 4 — Architecture invariants belong in `.importlinter` and ruff, not in tests

**Rationale.** Import-layer and code-purity invariants ("no `analysis/` module may
import from `decision/`"; "no bare `except:` clauses") are structural properties of the
codebase. Encoding them as tests — counting AST nodes, grepping source text, comparing
against a hand-maintained numeric baseline — produces the worst of both worlds: the
invariant is not enforced at lint time (so violations accumulate between test runs) and
the baseline drifts as the codebase grows, requiring manual bumps that erode the
invariant entirely. The audit found `test_l4_broad_except_count_below_audit_baseline` had
its ceiling raised 14 times; by the time of the audit it permitted more broad-except
clauses than existed in the codebase at the invariant's introduction.

**Correct homes:**

- *Import-layer invariants* → `.importlinter` contracts (`[importlinter:contract:...]`
  blocks). These run as `uv run lint-imports` in the lint chain and fail immediately on
  any violation.
- *Code-style / purity invariants* → ruff rules (e.g. `E722` for bare `except`).
  These run as `uv run ruff check .` in the lint chain.

**Before:** `tests/test_architecture.py` contained `test_l4_broad_except_count_below_audit_baseline`,
which parsed every `.py` file with `ast`, counted bare-`except` nodes, and asserted the
count was below a numeric ceiling. The ceiling was bumped 14 times across the codebase's
history; the test provided no enforcement between bumps.

**After:** The bare-except invariant is expressed as ruff rule `E722` in `pyproject.toml`
(`select = [..., "E722"]`). It fires on every `ruff check .` invocation, catches
violations at the point of introduction, and needs no maintenance as the codebase grows.
The test was deleted in story 03 of the bloat-remediation work tree.
