# 04 — Reference-marker extraction

## Goal

Implement the deterministic text-scanning primitive that converts a `BriefBundle.text` body into a per-reference-ID map of section text. Given a brief whose body begins like `[SA-TECH-1] {finding}\n  ...\n[SA-TECH-ANOM-1] {finding}\n  ...`, the extractor returns `{"SA-TECH-1": "[SA-TECH-1] {finding}\n  ...", "SA-TECH-ANOM-1": "[SA-TECH-ANOM-1] {finding}\n  ..."}`. The retrieval store (story 05a) consumes this output to build its lookup map.

## Reading

* `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources and their reference prefixes (`SA-TECH`, `SA-FIN`, `SA-ENERGY`, `CR`, `QR`, `AR`, plus sub-typed `SA-{SECTOR}-ANOM`, `SA-{SECTOR}-TC`, `QR-CW`). **There is no** `DC` prefix (a hallucination purged from this work tree during the 2026-05-03 audit).
* `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format rules.
* `docs/design/testing/llm-output-validation.md` § Layer-1 — Envelope parse — strict-parse stance (malformed reference IDs are the producer's contract violation; this primitive assumes the input has passed the producer's parser/validator).
* [ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `BriefBundle` data model + `ReferencePrefix` taxonomy + `parse_reference_id` longest-match helper this primitive uses.
* [ALP-203](https://linear.app/alphamind-jatassi/issue/ALP-203) (story 05a) — the retrieval store that calls this primitive.

## Depends on

[ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `ReferencePrefix` taxonomy and `parse_reference_id`.

## Scope

Module path: `src/alphamind/analysis/synthesizer/reference_extractor.py`.

#### `class ExtractionError(Exception)`

Raised when the text cannot be parsed into well-formed reference sections (a line starts with `[` but doesn't match a valid prefix in `ReferencePrefix`, or a reference ID appears twice). Carries `message: str` and `offending_line: str | None`.

#### `extract_references(brief_text: str) -> dict[str, str]`

The main entry point. Returns a dict mapping each reference ID (e.g., `"SA-TECH-1"`, `"SA-TECH-ANOM-3"`, `"QR-CW-2"`) to its full section text (including the `[<prefix>-<index>]` header and everything until the next reference ID or end-of-string).

Algorithm — must handle multi-segment prefixes including sub-typed variants:

1. Compile the regex `^\[([A-Z]+(?:-[A-Z]+)+-\d+)\]` once at module load. The greedy `+` over `(?:-[A-Z]+)` segments is what makes the regex match `SA-TECH-ANOM-3` and `QR-CW-2` rather than dropping their sub-typed segments.
2. Split the text into lines.
3. Identify reference-header lines using the regex. For each match, extract the captured ID (e.g., `"SA-TECH-ANOM-3"`).
4. Validate the ID by calling `parse_reference_id` from story 03 — `None` return means the prefix is not in the canonical `ReferencePrefix` enum; raise `ExtractionError` with `offending_line`.
5. Start a new section at each header line; accumulate subsequent non-header lines into that section.
6. Return `{ref_id: full_section_text}` for every section.

#### Tests

`tests/analysis/synthesizer/test_reference_extractor.py`:

* `test_basic_extraction` — three `SA-TECH-N` references; verify all three appear in output with correct text.
* `test_subtype_prefixes_preserved` — sample with `SA-TECH-1`, `SA-TECH-ANOM-1`, `SA-TECH-TC-2`, `QR-CW-3`; verify all four headers are recognized as distinct sections (regression guard for the longest-match property).
* `test_mixed_prefixes` — one bundle text with `SA-TECH-1`, `SA-FIN-1`, `CR-1`, `QR-1`, `AR-1`; all five extracted.
* `test_empty_bundle` — empty string returns empty dict (not an error).
* `test_duplicate_ref_id` — same `SA-TECH-1` appears twice → `ExtractionError`.
* `test_unknown_prefix_raises` — line `[ZZ-FOO-1]` (not in `ReferencePrefix`) → `ExtractionError`.
* `test_malformed_header_raises` — line `[not-a-ref-id]` → `ExtractionError`.
* `test_preserves_newlines_and_indentation` — section text retains internal newlines and leading whitespace until the next header.

## Out of scope

* The retrieval store (story 05a).
* Adapter functions that map upstream typed value objects to `BriefBundle` (story 05a).
* Layer-3 referential-integrity checks against the consumer-side validation stack.

## Acceptance criteria

- [ ] `extract_references` returns the correct sections for a single-prefix sample.
- [ ] Sub-typed prefixes (`SA-TECH-ANOM-3`, `QR-CW-2`) are recognized as distinct from their base forms.
- [ ] Mixed-prefix samples extract all references.
- [ ] Duplicate IDs raise `ExtractionError`.
- [ ] Unknown prefixes (not in `ReferencePrefix`) raise `ExtractionError`.
- [ ] Empty bundle returns empty dict without error.
- [ ] No `DC` prefix references anywhere in tests or code.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.