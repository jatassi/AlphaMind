# 03 — Cross-harness contract consistency tests

## Goal

Add a single parametrized test module that asserts all 7 LLM-output harnesses follow the same cross-cutting contract: the retry-message shape, the diagnostic-record schema, the stop-reason classification semantics, and the canonical `ValidationResult` ownership. Today these invariants are individually tested in 7 separate `test_harness.py` modules; nothing fails if one harness drifts. The new file is the consistency oracle.

## Reading

* `src/alphamind/analysis/_harness_core.py` — the shared core. Pay attention to: `DiagState.write` (the canonical diagnostic record schema), `_build_retry_message` (the canonical 4-part retry message), the exception hierarchy (`MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`), and `invoke_sdk`'s stop-reason classification logic (lines 696-710 and the Layer-4 mapping).
* `docs/design/testing/llm-output-validation.md` § Corrective-retry message construction (lines 286-313) — the contract: framing + first-error + contract-reference + directive; no full error list, no analytical guidance, no raw input data.
* `src/alphamind/decision/{analyst,strategist,portfolio_manager}/harness.py` and `src/alphamind/analysis/{domain_researchers,qualitative_research,adaptive_research,synthesizer}/harness.py` — the 7 harness files. Each defines its own retry-message construction wrapper around `_harness_core._build_retry_message`.
* `tests/analysis/test_harness_core.py` — existing unit tests for `_harness_core.py` as a module. The new contract tests are a sibling, not a replacement.
* `tests/analysis/{domain_researchers,qualitative_research,adaptive_research,synthesizer}/test_harness.py` and `tests/decision/{analyst,strategist,portfolio_manager}/test_harness.py` — read enough to understand the existing per-harness test fixtures (especially the SDK-stub patterns) so the new file can reuse them or follow the same conventions.
* [ALP-466](https://linear.app/alphamind-jatassi/issue/ALP-466/06d-extract-analysis-harness-corepy-from-4-duplicated-llm-harnesses) — for context on the shared-core consolidation.
* [ALP-493](https://linear.app/alphamind-jatassi/issue/ALP-493/debug-e2e-mode) — for context on the recent decision-harness migration through `_harness_core.invoke_sdk`.
* [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy) — your prerequisite for canonical `ValidationResult`.
* [ALP-521](https://linear.app/alphamind-jatassi/issue/ALP-521/02-bare-prefix-citation-detection-in-consumer-side-validators) — your prerequisite for the bare-prefix discipline being part of the asserted contract.

## Depends on

* [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy) (this work tree, 01) — canonical `ValidationResult` shape is asserted as the return type for every validator-bearing harness.
* [ALP-521](https://linear.app/alphamind-jatassi/issue/ALP-521/02-bare-prefix-citation-detection-in-consumer-side-validators) (this work tree, 02) — bare-prefix-detection participation is part of the asserted retry-reachable failure modes.

## Scope

In scope:

* `tests/test_llm_output_validation_contract.py` (new file at the test root, not under any sub-package) — single file, parametrized over the 7 agents.

Out of scope:

* Production source under `src/alphamind/` — this story is test-only. If a contract assertion fails for a real divergence in production code, surface it and fold the fix into the next reasonable story (or open a follow-up issue) — do not silently mutate production behavior to make the test pass.

### 1\. Parametrization shape

A single `pytest.fixture` (or `pytest.mark.parametrize` table) describing the 7 agents and their per-agent characteristics:

| Agent | Has validator? | Has retry? | Has structured output? | `archive_layer` |
| -- | -- | -- | -- | -- |
| `analyst` | Yes | Yes | Yes | `"decision"` |
| `strategist` | Yes | Yes | Yes | `"decision"` |
| `pm` (`portfolio_manager`) | Yes | Yes | Yes | `"decision"` |
| `domain_researchers` | Yes | Yes | Yes | `"analysis"` |
| `qualitative_research` | Yes | Yes | Yes | `"analysis"` |
| `adaptive_research` | Yes | Yes | Yes | `"analysis"` |
| `synthesizer` | No | No | No | `"analysis"` |

The synthesizer has degenerate retry and validator contracts — assertions that require a retry path or a `ValidationResult` must `pytest.skip(...)` or branch around the synthesizer entry rather than fail.

### 2\. Asserted invariants

For each parametrized agent, the test file asserts:

**Retry-message shape (skip synthesizer).** Given a synthetic `ValidationResult` (or `ParseError`) with one error, the harness's retry-message wrapper produces a string that:

* Contains the framing line (`"The prior response did not meet ..."`-style).
* Contains the `field_path` and `rule` and `message` of the first error verbatim.
* Contains a reference to the contract source (schema file name, design doc section).
* Does NOT contain the raw input data (synthesize a marker string in the user message and assert absent in the retry message).
* Does NOT contain the second-or-later error from a multi-error `ValidationResult` (synthesize two errors; assert only the first's `message` is present).
* Ends with the canonical "emit single JSON object" directive (or equivalent for the harness's contract).

**Diagnostic-record schema (all 7 agents).** Construct a synthetic `DiagState` instance (or invoke the harness with a stubbed SDK that returns a clean success) and assert that `DiagState.write` produces exactly: `prompt.md`, `user_message.md`, `response_initial.md` (or `response.md` for synthesizer), optional `response_retry.md`, `errors.json`, `metadata.json`. Validate `metadata.json` has at minimum: `model`, `tokens_used`, `wall_clock_seconds`, `stop_reason`, `success`. The diag write must happen even on failure paths (verify by inducing a failure-path code path).

**Stop-reason classification semantics (skip synthesizer for the** `max_tokens` **arm).** For each harness that goes through `invoke_sdk`:

* Verify the harness propagates `outcome.stop_reason` into the diagnostic metadata unchanged.
* Verify the harness translates `_CLIResultError` correctly: analysis harnesses → `ContextOverflowFailure` (per `on_cli_result_error="context_overflow"`); decision harnesses → `SDKFailure` (per `on_cli_result_error="sdk_failure"`). The classification table is the contract — drift here breaks Layer 4.
* Verify the harness raises `MalformedOutputFailure` on parse + retry exhaustion (skip for synthesizer — no retry path; verify `EmptyResponseFailure` instead for the empty-with-end_turn case).

**Canonical** `ValidationResult` **ownership (skip synthesizer).** For each validator-bearing harness, assert the validator function returns an instance of `alphamind.commands.validation_results.ValidationResult` (not a per-agent dataclass). This locks in [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy)'s consolidation.

**Bare-prefix discipline (analyst, strategist, pm only).** For each of the three consumer-side validators, assert that `find_bare_prefix_citations` is invoked (mock-and-verify it's called, or assert a narrative with `[CR]` produces a `bare_prefix_citation` error). This locks in [ALP-521](https://linear.app/alphamind-jatassi/issue/ALP-521/02-bare-prefix-citation-detection-in-consumer-side-validators)'s wiring.

### 3\. Existing fixture reuse

Where the per-harness `test_harness.py` modules already define synthetic SDK-stub fixtures, conftest-level helpers, or canonical malformed-fixture builders, reuse them by import. Avoid re-rolling SDK stubs in the new file unless the existing fixture's scope is too narrow.

### 4\. Imports and discovery

The new test file lives at the test root (`tests/test_llm_output_validation_contract.py`) rather than nested under `tests/analysis/` or `tests/decision/` because it cross-cuts both. The path should be discoverable by `uv run pytest -n auto` without conftest tweaks.

### Out of scope

* Refactoring `_build_retry_message` itself (or any production helper) to ease testing — write the test against the as-built helper.
* Adding a contract-asserting decorator or framework — a single parametrized file is the right granularity.
* Per-agent unit-test coverage that duplicates the per-harness `test_harness.py` files.

## Acceptance criteria

- [ ] `tests/test_llm_output_validation_contract.py` exists and is discovered by pytest.
- [ ] Parametrization covers all 7 agents per the table in scope §1; synthesizer-skip / synthesizer-branch logic is explicit (no silent ignore).
- [ ] Retry-message shape invariants (scope §2) assert for the 6 retry-bearing agents.
- [ ] Diagnostic-record schema invariants assert for all 7 agents.
- [ ] Stop-reason classification invariants assert the analysis-vs-decision branch table per scope §2 (`on_cli_result_error="context_overflow"` for the 4 analysis harnesses + synthesizer-classified-as-empty; `on_cli_result_error="sdk_failure"` for the 3 decision harnesses) — and the per-call argument is verified rather than just the surface behavior.
- [ ] Canonical `ValidationResult` ownership invariant asserts for the 6 validator-bearing agents.
- [ ] Bare-prefix discipline invariant asserts for analyst, strategist, pm.
- [ ] No test imports a deprecated `ValidationFailure` symbol or accesses `.overall` / `.failures` (verifies the migration is clean).
- [ ] `uv run pytest tests/test_llm_output_validation_contract.py -n auto` passes.
- [ ] `uv run pytest -n auto` (full suite) is clean.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports` are clean.

## Verification

The orchestrator confirms by:

* Running the new test file in isolation and observing all parametrized cases produce passes (or skips for synthesizer-specific cases).
* Running the full suite and lint chain clean.
* `wc -l tests/test_llm_output_validation_contract.py` is in the 200-500 line range — much smaller means the asserted contract is underspecified; much larger means scope creep.