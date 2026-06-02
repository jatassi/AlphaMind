# 04 — Rewrite llm-output-validation.md to reflect as-built

## Goal

Rewrite `docs/design/testing/llm-output-validation.md` to document the as-built per-agent validation pattern instead of the centralized "validator module" the original design implied. Preserve the conceptual 4-layer mental model and the reference-ID taxonomy section (both still authoritative). Replace claims about a "single seam" centralized validator with the actual shape: SDK-enforced Layers 1+2 via `output_format={"type":"json_schema",...}`, per-agent `validation.py` modules for Layer 2 cross-field + Layer 3 referential, `_harness_core.py` for Layer 4 + corrective retry + diagnostic persistence, canonical `ValidationResult` in `commands/validation_results.py`, and `find_bare_prefix_citations` next to `parse_reference_id` for bare-prefix detection.

## Reading

* `docs/design/testing/llm-output-validation.md` — the file you are rewriting. Read in full so you preserve voice and the genuinely-correct passages.
* `src/alphamind/analysis/_harness_core.py` — module docstring + `DiagState` + `_build_retry_message` + `invoke_sdk` + exception hierarchy. These are the authoritative shape of Layer 4 + retry + diagnostic.
* `src/alphamind/commands/validation_results.py` — canonical types (post-[ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy)). Reference these in the rewritten "Per-agent surface mapping" section.
* `src/alphamind/decision/{analyst,strategist,portfolio_manager}/validation.py` and `src/alphamind/analysis/{domain_researchers,qualitative_research,adaptive_research}/validation.py` — the 6 per-agent validators. Note each one's module docstring and the headline checks it implements; the rewritten doc references their paths.
* `src/alphamind/analysis/synthesizer/models.py` — `ReferencePrefix` enum + `parse_reference_id` + the new `find_bare_prefix_citations` from [ALP-521](https://linear.app/alphamind-jatassi/issue/ALP-521/02-bare-prefix-citation-detection-in-consumer-side-validators). Reference these as authoritative.
* `src/alphamind/analysis/synthesizer/harness.py` — synthesizer's explicit Layer-4 check + `EmptyResponseFailure` for the empty-with-end_turn case.
* `src/alphamind/decision/{analyst,strategist,portfolio_manager}/harness.py` — the 3 decision harnesses; observe their `output_format` payload + parser + validator + retry wrapping. These are the worked examples to cite for "per-agent validator pattern".
* `src/alphamind/analysis/{domain_researchers,qualitative_research,adaptive_research}/harness.py` — the 3 analysis harnesses with structured output.
* `docs/design/llm-agent-failure-handling.md` — the runtime-policy partner doc. Cross-references must remain accurate.
* `docs/design/04-decision-layer/analyst-output-schema.md`, `docs/design/04-decision-layer/strategist-output-schema.md`, `docs/design/04-decision-layer/pm-envelope-schema.md`, `docs/design/05-execution-layer/oms-command-schema.md`, `docs/design/05-execution-layer/engine-envelope-schema.md` — confirm these still exist at the cited paths; verify in passing.
* [ALP-454](https://linear.app/alphamind-jatassi/issue/ALP-454/architecture-refactoring-2026-05-12-audit) (architecture refactor), [ALP-466](https://linear.app/alphamind-jatassi/issue/ALP-466/06d-extract-analysis-harness-corepy-from-4-duplicated-llm-harnesses) (harness consolidation), [ALP-493](https://linear.app/alphamind-jatassi/issue/ALP-493/debug-e2e-mode) (debug-e2e + final decision-harness migration), [ALP-458](https://linear.app/alphamind-jatassi/issue/ALP-458/02b-extract-commands-kernel-invert-submit-envelope-mcp-break) (validation-results hoist) — for the "as-built history" framing in the rewritten doc.
* [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy), [ALP-521](https://linear.app/alphamind-jatassi/issue/ALP-521/02-bare-prefix-citation-detection-in-consumer-side-validators), [ALP-522](https://linear.app/alphamind-jatassi/issue/ALP-522/03-cross-harness-contract-consistency-tests) — your prerequisites; their deliverables are what the rewritten doc documents as the current state.

## Depends on

* [ALP-522](https://linear.app/alphamind-jatassi/issue/ALP-522/03-cross-harness-contract-consistency-tests) (this work tree, 03) — cross-harness contract tests must be in place before the doc documents the contract as load-bearing.
* Transitively [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy) and [ALP-521](https://linear.app/alphamind-jatassi/issue/ALP-521/02-bare-prefix-citation-detection-in-consumer-side-validators) — the doc references their deliverables (canonical types, bare-prefix utility) as the as-built.

## Scope

In scope:

* `docs/design/testing/llm-output-validation.md` — rewrite the file in place. Preserve the file path (many existing docstrings link to it).

Out of scope:

* `docs/design/llm-agent-failure-handling.md` — the runtime-policy partner doc. Don't restructure; only fix cross-references if any are broken.
* Other design docs that link to this one — they link to the file path, not internal anchors. If you rename a section header, update the back-references; otherwise leave them alone.
* The `docs/project-tracker.md` flip from `_requirements pending_` to `_done_` — the skill's Phase 7 closeout step handles this after all stories land.

### 1\. Sections to preserve (with edits)

* **Top-of-file intro** (lines 1-6). Preserve the framing: "structural validation decides whether an LLM agent's response is usable; runtime policy lives in the partner doc". Update the second paragraph: the validator "sits at a single seam" is wrong — replace with "the validator stack is distributed across per-agent harnesses + a shared core module".
* **Scope § In scope** (lines 9-23). Preserve the bullet structure; each bullet's content needs light rephrasing to match the as-built (e.g., "Validation stack" still applies as a mental model but the layer-implementation locations have changed).
* **Scope § Out of scope (deferred elsewhere)** (lines 25-35). Preserve.
* **Design principles** (lines 39-67). Preserve the five principles; they still hold. Update the "Validation lives in the pipeline process" principle to reflect the per-agent module layout rather than implying a single module.
* **Validation stack** (lines 71-170). Preserve the 4-layer mental model and per-layer mandates. Replace each layer's implementation description with the as-built:
  * **Layer 1**: SDK consumes `output_format={"type":"json_schema",...}`; structured_output reaches per-agent `parser.py` as a dict.
  * **Layer 2**: schema is API-enforced; cross-field invariants Pydantic cannot see across siblings (leg-id pairing, halt-mode constraints, etc.) live in per-agent `validation.py` modules listed by path.
  * **Layer 3**: referential resolution via `RetrievalStore` + the new `find_bare_prefix_citations` for bare-prefix tokens. Cross-reference `analysis/synthesizer/retrieval.py` and `analysis/synthesizer/models.py`.
  * **Layer 4**: implemented in `_harness_core.py::invoke_sdk` (`on_cli_result_error="context_overflow"` for analysis; `"sdk_failure"` for decision); synthesizer harness has an explicit Layer-4 check for the empty-response case.
* **Per-agent surface mapping** (lines 174-193). Preserve the table; update each row's Validation mechanism column to cite the per-agent `validation.py` path. Update the synthesizer row to note that its Layer-4 check is in `analysis/synthesizer/harness.py`.
* **Reference-ID taxonomy** (lines 196-254). Preserve as authoritative. Add a paragraph at the end noting that `analysis/synthesizer/models.py::ReferencePrefix` is the authoritative enum, and `find_bare_prefix_citations` is the bare-prefix detector that all consumer-side validators (analyst, strategist, PM) call.
* **Output parsing and envelope extraction** (lines 258-280). Preserve; lightly edit to reference `_harness_core.py::_collect_response` for the SDK driver that produces `structured_output` and per-agent `parser.py` for the consumer.
* **Corrective-retry message construction** (lines 284-313). Preserve the contract (what the message contains, what it omits, why). Replace implementation details with a pointer to `_harness_core.py::_build_retry_message` as the canonical builder; note that per-harness wrappers (`_build_retry_message_for_parse_error`, `_build_retry_message_for_validation_failure`) provide agent-specific framing on top.
* **Cross-references** (lines 456-475). Verify every linked path exists post-[ALP-454](https://linear.app/alphamind-jatassi/issue/ALP-454/architecture-refactoring-2026-05-12-audit) relocations. Add new cross-references to `src/alphamind/commands/validation_results.py`, `src/alphamind/analysis/_harness_core.py`, and the `tests/test_llm_output_validation_contract.py` contract test from [ALP-522](https://linear.app/alphamind-jatassi/issue/ALP-522/03-cross-harness-contract-consistency-tests).

### 2\. Sections to rewrite substantially

* **Resolved design questions** (lines 438-453). The "Validator placement" entry is wrong (placement is per-agent, not "module inside the pipeline process at the LLM invocation seam — single module"). Rewrite to reflect: placement is per-agent `validation.py` modules + shared `_harness_core.py` for layer 4 + retry + diagnostic; no single module owns the validator. Other entries (PM envelope formality, referential vs semantic, retry granularity, fresh vs same context, strict vs lenient parsing, stop-reason ordering) remain correct — preserve.
* **Unit test plan** (lines 316-417). The per-layer test-concern lists and the preliminary unit catalog table remain useful — preserve the structure. Update the test file paths: each agent's tests live at `tests/<layer>/<agent>/test_validation.py` (and `test_harness.py` for harness-level concerns) rather than a single hypothetical validator test module. Add a paragraph noting that `tests/test_llm_output_validation_contract.py` (from [ALP-522](https://linear.app/alphamind-jatassi/issue/ALP-522/03-cross-harness-contract-consistency-tests)) is the cross-harness consistency oracle.

### 3\. Sections to add

* A new short section near the top (after Scope, before Design principles) titled **As-built implementation map** with a small table:

  ```
  Layer 1 (envelope parse)         | SDK ResultMessage.structured_output | _harness_core._collect_response + per-agent parser.py
  Layer 2 (schema validation)      | API-enforced via output_format      | Pydantic models in each agent's models.py
  Layer 2 (cross-field invariants) | Per-agent code                      | <agent>/validation.py
  Layer 3 (referential integrity)  | Per-agent code + shared utilities   | <agent>/validation.py + analysis/synthesizer/{retrieval,models}.py
  Layer 4 (stop-reason)            | Shared infrastructure               | analysis/_harness_core.py::invoke_sdk
  Retry orchestration              | Per-harness, shared message builder | <agent>/harness.py + _harness_core._build_retry_message
  Diagnostic persistence           | Shared infrastructure               | analysis/_harness_core.py::DiagState
  Canonical result types           | Single module                       | commands/validation_results.py
  ```

  This is the "TL;DR for the reader who needs to find the code" — present near the top.

### 4\. Sections / claims to remove

* Any phrasing implying a single "validator service", "validator module", or "validator process" — replace with the per-agent + shared-core decomposition.
* Any claim that the validator is "a module" the pipeline imports — replace with the per-agent pattern + shared core.
* If the doc names schema files that no longer exist or have moved post-[ALP-454](https://linear.app/alphamind-jatassi/issue/ALP-454/architecture-refactoring-2026-05-12-audit), fix the paths. (My audit confirmed all referenced design doc paths exist as of 2026-05-17, but do a final grep when you start.)

### Out of scope

* Renaming the doc file or moving it under a different directory — preserve `docs/design/testing/llm-output-validation.md`.
* Updating cross-referenced sibling docs (`llm-agent-failure-handling.md` etc.) unless a link is genuinely broken.
* Updating per-validator module docstrings to match the new design-doc text — those docstrings already reference `docs/design/testing/llm-output-validation.md` by path, which is preserved.

## Acceptance criteria

- [ ] `docs/design/testing/llm-output-validation.md` no longer contains language implying a centralized validator module; instead documents the per-agent + shared-core pattern.
- [ ] The As-built implementation map section exists near the top with the table in scope §3.
- [ ] The Validation stack section's per-layer implementation descriptions point at the as-built file paths (`_harness_core.py`, per-agent `validation.py`, `commands/validation_results.py`, `analysis/synthesizer/models.py`).
- [ ] The Per-agent surface mapping table's Validation-mechanism column cites per-agent `validation.py` paths.
- [ ] The Reference-ID taxonomy section references `find_bare_prefix_citations` and `ReferencePrefix` as authoritative.
- [ ] The Corrective-retry message construction section points at `_harness_core._build_retry_message` as canonical and notes per-harness wrappers.
- [ ] The Resolved design questions section's "Validator placement" entry reflects the as-built (per-agent + shared core).
- [ ] The Cross-references section lists `src/alphamind/commands/validation_results.py`, `src/alphamind/analysis/_harness_core.py`, and `tests/test_llm_output_validation_contract.py`.
- [ ] Every file path the doc references exists in the work tree (verify via `for f in $(grep -oE '\(\.\./\.\./[^)]+\)' docs/design/testing/llm-output-validation.md | tr -d '()'); do [ -f docs/design/$f ] || echo missing: $f; done` or equivalent).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports` are clean (no code changes; this just confirms the doc edit didn't accidentally include a `.py` typo).
- [ ] No `*.py` file modified (this is a docs-only story; a touched .py file is a scope creep).

## Verification

The orchestrator confirms by:

* `git diff --stat HEAD~1 HEAD` shows exactly one file modified: `docs/design/testing/llm-output-validation.md`.
* Visually reviewing the rewritten doc for tone consistency, section-header progression, and the new As-built implementation map.
* Spot-checking 3 randomly-selected internal links in the rewritten doc and confirming the target paths exist.