# 02 — Bare-prefix citation detection in consumer-side validators

## Goal

Close the documented bare-prefix citation gap from the 2026-05-04 E2E incident. Add a centralized utility next to `parse_reference_id` in `analysis/synthesizer/models.py` that detects bare-prefix tokens (`[CR]`, `[SA-TECH]`, `[QR]` etc.) — bracketed strings whose body matches a known `ReferencePrefix` value without a trailing `-N` index. Wire the detector into the consumer-side reference resolution in analyst, strategist, and PM validators; bare-prefix tokens become `ValidationError(rule="bare_prefix_citation", ...)` so the corrective-retry path runs.

## Reading

* `docs/design/testing/llm-output-validation.md` § Layer 3 § Failure modes detected here line 154 — the documented incident: synthesizer emitted four bare `[CR]` correlation-pair tokens that the consumer-side regex silently dropped. The validator should treat these as invented references rather than ignoring them as prose.
* `src/alphamind/analysis/synthesizer/models.py` — current home of `ReferencePrefix` enum + `parse_reference_id`. The new bare-prefix detector goes here so the closed-set authority stays in one module. Lines 55-77 (ReferencePrefix enum), 108-114 (`_PREFIXES_BY_LENGTH` longest-match table), 158-180 (`parse_reference_id`).
* `src/alphamind/decision/analyst/validation.py` — `_REF_ID_RE` (line 98) and `_check_narrative_references` (line 332). Three consumer-side fields per recommendation: `thesis_narrative`, `target_rationale`, `position_size_rationale`, `entry_window_rationale`, `counterarguments_acknowledged`, plus `invalidation_rationale[].rationale`.
* `src/alphamind/decision/strategist/validation.py` — `_REF_ID_RE` (line 93). Consumer-side narrative fields: `status_rationale`, `action_rationale`, `cross_position_observations` and the like; verify against the file's `_check_narrative_references` call sites.
* `src/alphamind/decision/portfolio_manager/validation.py` — `_REF_ID_RE` (line 81), `_check_narrative_references` (line 441), `_check_narrative_references_against_store` (line 413). PM scans `rationale_narrative` and `modifications[].rationale`.
* `src/alphamind/analysis/adaptive_research/validation.py` — Adaptive uses typed-field reference resolution (`Strengthens`, `Weakens`) not prose-scanning. Bare-prefix forms like the bare string `"CR"` in a `Strengthens: list[str]` field would already fail `parse_reference_id` (which requires a trailing integer); no change needed here. Read just enough to confirm this stance and document it in your dispatch report.
* [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy) — your prerequisite; ensures `ValidationError` is the canonical type in `commands.validation_results`.
* Memory `feedback_no_inventing_component_names` — the new utility's name must mirror existing primitives (`parse_reference_id`, `ReferencePrefix`).

## Depends on

* [ALP-520](https://linear.app/alphamind-jatassi/issue/ALP-520/01-canonical-validator-result-types-in-commandsvalidation-resultspy) (this work tree, 01) — uses the canonical `ValidationError` from `commands.validation_results` for the new `bare_prefix_citation` rule.

## Scope

In scope:

* `src/alphamind/analysis/synthesizer/models.py` — add the utility next to `parse_reference_id`.
* `src/alphamind/decision/analyst/validation.py`, `src/alphamind/decision/strategist/validation.py`, `src/alphamind/decision/portfolio_manager/validation.py` — extend each `_check_narrative_references` (or equivalent) to also surface bare-prefix tokens.

Tests at:

* `tests/analysis/synthesizer/test_models.py` — extend with detector unit tests.
* `tests/decision/analyst/test_validation.py`, `tests/decision/strategist/test_validation.py`, `tests/decision/portfolio_manager/test_validation.py` — add bare-prefix narrative cases.

### 1\. Detector utility in `analysis/synthesizer/models.py`

Add:

```python
def find_bare_prefix_citations(text: str) -> tuple[str, ...]:
    """Return bracketed tokens in `text` whose body is a known ReferencePrefix value with no index suffix.

    Examples: ``"see [CR] and [SA-TECH-3]"`` returns ``("CR",)`` — the
    well-formed ``SA-TECH-3`` is ignored; ``[CR]`` is a bare prefix.
    ``"[REC]"`` returns ``()`` — ``REC`` is not in the ReferencePrefix
    taxonomy.

    Used by the consumer-side validators (analyst, strategist, PM) to
    surface the 2026-05-04 incident class: a syntactically-bracketed token
    that resembles a citation but cannot resolve in the retrieval store
    (which is always keyed by ``<prefix>-<index>``).
    """
```

Implementation:

* Regex `\[([A-Z][A-Z0-9-]*)\]` matches any bracketed UPPER/digit/hyphen body without a trailing `-N` index. (The existing `_REF_ID_RE` in validators requires the index; this new regex is its complement.)
* For each match, check the captured group against the set of `ReferencePrefix` values (`{p.value for p in ReferencePrefix}`). Return matches in document order; deduplicate stable.
* Add to `__all__`.

### 2\. Wire into analyst validator

In `decision/analyst/validation.py::_check_narrative_references`, after the existing well-formed-reference resolution loop, also call `find_bare_prefix_citations(narrative)`. Each returned prefix yields:

```python
ValidationError(
    field_path=field_path,
    rule="bare_prefix_citation",
    message=(
        f"bare-prefix citation [{prefix}] in {field_path} carries no index; "
        "the retrieval store is keyed by <prefix>-<index> and cannot resolve "
        "bare prefixes. Cite a specific brief section like "
        f"[{prefix}-1]."
    ),
)
```

### 3\. Wire into strategist validator

Same wiring in `decision/strategist/validation.py::_check_narrative_references` (or its equivalent helper — verify the function name). Same `ValidationError` shape.

### 4\. Wire into PM validator

Same wiring in `decision/portfolio_manager/validation.py::_check_narrative_references` (and `_check_narrative_references_against_store` if both scan prose). Same `ValidationError` shape.

### 5\. Adaptive researcher

Document in the dispatch report that adaptive's typed-field resolution path already rejects bare prefixes via `parse_reference_id` (which returns `None` for strings missing the `-N` index, which the validator then surfaces as `unknown_reference`). No code change in `analysis/adaptive_research/validation.py`.

### 6\. Tests

In `tests/analysis/synthesizer/test_models.py`, add:

* `find_bare_prefix_citations` returns the bare prefix when given `"see [CR]"`.
* Returns multiple in document order when given `"foo [CR] bar [SA-TECH] baz"`.
* Deduplicates: `"[CR] and [CR]"` returns `("CR",)`.
* Skips well-formed citations: `"[CR-3]"` returns `()`.
* Skips unknown bracketed tokens: `"[REC] [INV-1] [FOO]"` returns `()` (none are `ReferencePrefix` members).
* Skips longest-match shorter prefix when a longer prefix could also match: `"[SA-TECH]"` returns `("SA-TECH",)` (not the shorter `"SA"` which is not in the taxonomy anyway).

In each of the three consumer-side validator test modules, add cases:

* Narrative containing `"[CR]"` produces a `ValidationError` with `rule == "bare_prefix_citation"`.
* Narrative containing both `"[CR]"` and `"[SA-TECH-3]"` (where SA-TECH-3 is in the retrieval store) produces only the bare-prefix error.
* Narrative containing `"[REC]"` (producer-side prefix, not in `ReferencePrefix` taxonomy) does NOT produce a bare-prefix error — confirms the scope decision (parent issue § C).

### Out of scope

* Producer-side bare-prefix scan on synthesizer output (parent issue § D — synthesizer remains stop-reason-only).
* Adding `REC`, `INV`, `SA`, `SA-ORD`, `ENV-*` to the bare-prefix taxonomy (parent issue § C).
* Refactoring the existing well-formed-reference resolution path — only ADD the bare-prefix check.

## Acceptance criteria

- [ ] `analysis/synthesizer/models.py` exports `find_bare_prefix_citations` with the signature in scope §1.
- [ ] `find_bare_prefix_citations("see [CR] and [SA-TECH-3]") == ("CR",)` and the analogous identities for the other test cases hold.
- [ ] `decision/analyst/validation.py::_check_narrative_references` calls `find_bare_prefix_citations` after the well-formed loop and yields one `ValidationError(rule="bare_prefix_citation", ...)` per detected bare prefix.
- [ ] Same wiring in `decision/strategist/validation.py` and `decision/portfolio_manager/validation.py`.
- [ ] `tests/analysis/synthesizer/test_models.py` exercises every case enumerated in scope §6.
- [ ] `tests/decision/{analyst,strategist,portfolio_manager}/test_validation.py` each cover the three bare-prefix narrative cases enumerated in scope §6.
- [ ] `analysis/adaptive_research/validation.py` is unchanged; the dispatch report explicitly notes why (typed-field path already rejects via `parse_reference_id`).
- [ ] `uv run pytest -n auto` (full suite) is clean.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports` are clean.

## Verification

The orchestrator confirms by:

* Running `uv run pytest tests/analysis/synthesizer/test_models.py tests/decision/analyst/test_validation.py tests/decision/strategist/test_validation.py tests/decision/portfolio_manager/test_validation.py -n auto` and observing 0 failures with the new cases counted in.
* Running the full suite and lint chain clean.
* `git grep find_bare_prefix_citations src/alphamind/` returns 1 definition + 3 use sites (analyst, strategist, PM validators).
* Spot-check the dispatch report's adaptive-researcher rationale.