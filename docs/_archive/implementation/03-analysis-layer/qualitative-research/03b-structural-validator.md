# 03b — Structural validator

## Goal

Implement the hand-written structural validator for the qualitative researcher's output — the Layer-2 + Layer-3 producer-side check from `docs/design/testing/llm-output-validation.md` for this agent class. Runs on a `QualitativeBrief` (already parsed by story 03a) and reports the structural and referential failures that schema-style validation would catch on JSON-output agents. Returns the *full* error list so the diagnostic record receives the complete inventory, even though story 04b's harness surfaces only the first error in the corrective-retry message.

## Reading

* `docs/design/testing/llm-output-validation.md` § Layer-2 — type-and-shape, § Layer-3 — referential integrity, § Reference-ID format rules — names the rule families this validator implements.
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Output schema — the contract; the validator enforces what story 02's model invariants do not.
* `src/alphamind/analysis/domain_researchers/validation.py` — the canonical analog. Mirror the `ValidationError` shape (`field_path`, `rule`, `message`), the `ValidationResult` aggregate type, the `_CHECKS` list discipline, the per-check generator pattern.
* `src/alphamind/analysis/qualitative_research/models.py` — the records the validator inspects (story 02).
* `src/alphamind/distillation/sector_assembly.py` § `load_sector_roster` — the source of universe-ticker membership the catalyst-watch validator can use to confirm a `CatalystWatch.ticker` is in the universe (or the asset_universe table directly).

## Depends on

* ALP-242 — story 02 lands `QualitativeBrief`; the validator inspects it.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/validation.py` and `tests/analysis/qualitative_research/test_validation.py`.

### 1\. Public types

`ValidationError(BaseModel, frozen=True)` and `ValidationResult(BaseModel, frozen=True)` mirror the domain-researcher analog field-for-field. `validate_qualitative_brief(brief: QualitativeBrief) -> ValidationResult`.

### 2\. Checks

* **invocation_id non-empty.** `field_path="invocation_id"`, `rule="invocation_id_not_empty"`. The model already constrains `str` non-empty for many fields; this check restates the brief-level invariant.
* **Threads sequential indexing.** Extract trailing integer from each `thread_id` (`QR-1`, `QR-2`, …). Must form `1, 2, …, N` with no gaps and no duplicates. `field_path="threads[*].thread_id"`, `rule="threads_sequential_indexing"`.
* **Catalyst watches sequential indexing.** Same rule applied to `catalyst_id` (`QR-CW-1`, `QR-CW-2`, …). `field_path="catalyst_watches[*].catalyst_id"`, `rule="catalyst_watches_sequential_indexing"`.
* **Thread reference-prefix uniformity.** Every `thread_id` must have prefix `QR` and not `QR-CW`. `field_path="threads[i].thread_id"`, `rule="reference_prefix_consistency"`.
* **Catalyst-watch reference-prefix uniformity.** Every `catalyst_id` must have prefix `QR-CW` and not `QR`. Same rule name, different field path.
* **Catalyst-watch ticker universe membership.** Each `CatalystWatch.ticker` must appear in `asset_universe`. The validator accepts an injectable `universe: frozenset[str]` parameter (mirrors how the harness can pre-load the universe once and pass it through). When `universe` is `None`, skip the check (so the validator remains a pure function for unit tests). `field_path="catalyst_watches[i].ticker"`, `rule="ticker_in_universe"`.
* **Thread evidence per-line uniformity.** Within a thread, every `EvidenceLine.citation` must be non-empty (the model enforces this). The validator additionally checks that no two evidence lines within the same thread are byte-identical (catches an LLM duplicating an evidence line to satisfy the two-source minimum). `field_path="threads[i].evidence[j].citation"`, `rule="evidence_lines_distinct_within_thread"`.

### 3\. Public entry point

`validate_qualitative_brief(brief, *, universe=None) -> ValidationResult`. Iterates `_CHECKS` and aggregates errors. `is_valid = not errors`.

### Out of scope

* Cross-stream referential integrity (a thread cites `[ND-T3]` from the digest but the digest has no such ID) — out of scope for this work tree, consistent with the domain-researcher validator's scope choice.
* Token-count budgets — the harness's `output_token_budget` enforces this at the SDK level; revalidating in the structural pass is duplicative.
* Verifying that `Direction` and `TimeHorizon` are valid enum values — the model's typed fields already gate this; if the validator runs, those values are already legal.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.validation import validate_qualitative_brief, ValidationError, ValidationResult` resolves.
- [ ] A brief with `threads = (QR-1, QR-2, QR-4)` raises `threads_sequential_indexing` with `field_path="threads[*].thread_id"`.
- [ ] A brief with two threads carrying the same `thread_id` `QR-1` raises both a duplicate-index error and the sequential-indexing error.
- [ ] A brief with `catalyst_watches = (QR-CW-1, QR-CW-3)` raises `catalyst_watches_sequential_indexing`.
- [ ] A brief with a thread whose `thread_id` is `QR-CW-1` (wrong prefix) raises `reference_prefix_consistency`.
- [ ] A brief with a catalyst watch whose `catalyst_id` is `QR-1` (missing the `-CW-` infix) raises `reference_prefix_consistency`.
- [ ] A brief with a catalyst-watch ticker not in the supplied `universe` raises `ticker_in_universe`. Same brief with `universe=None` does not raise.
- [ ] A brief whose `threads[0].evidence` has two byte-identical `EvidenceLine` records raises `evidence_lines_distinct_within_thread`.
- [ ] A well-formed brief produces `ValidationResult(is_valid=True, errors=())`.
- [ ] `tests/analysis/qualitative_research/test_validation.py` exercises every rule. Tests pass under `uv run pytest tests/analysis/qualitative_research/test_validation.py -n auto`.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_validation.py -n auto`. Run `uv run mypy src/alphamind/analysis/qualitative_research/validation.py` clean. Confirm via inspection that the validator drives test assertions through direct `QualitativeBrief(...)` construction (per the parent issue's "type purity at the seam" invariant), not via `parse_qualitative_brief` round-trip.
