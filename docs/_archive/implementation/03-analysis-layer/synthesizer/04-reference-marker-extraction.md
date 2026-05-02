---
status: not_started
completed_date:
commit_id:
---

# 04 — Reference-marker extraction

## Goal

Implement the deterministic text-scanning primitive that converts a `BriefBundle.text` body into a per-reference-ID map of section text. Given a brief that begins like `[SA-TECH-1] {finding}\n  ...\n[SA-TECH-2] {finding}\n  ...`, the extractor returns `{"SA-TECH-1": "[SA-TECH-1] {finding}\n  ...", "SA-TECH-2": "[SA-TECH-2] {finding}\n  ..."}`. The retrieval store (story 05a) consumes this output to back the `retrieve_brief` MCP tool the decision-layer agents call.

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` § Source reference mechanism — `[<prefix>-<index>]` format, the citation contract decision-layer agents follow
- `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — what the per-section map is for
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the canonical sector-brief layout with `[SA-{SECTOR}-N]` markers, indented continuation lines, and `=== SECTION ===` separators
- `docs/design/03-analysis-layer/qualitative-research.md` § Output schema — the qualitative brief layout with `[QR-N]` and `[QR-CW-N]` markers
- `docs/design/03-analysis-layer/adaptive-research.md` § Output schema — the adaptive brief layout with `[AR-N]` markers
- `docs/implementation/02-distillation-layer/11b-correlation-regime-brief-assembly.md` — the CR brief layout with `[CR-N]` markers across multiple sections
- `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format invariants the extractor relies on (sequential indexing, bracket-delimited markers, prefix correctness)

## Depends on

- 03 (`BriefBundle`, `ReferencePrefix`, `SOURCE_PREFIXES`, `parse_reference_id`)

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `extraction.py` defining:

  - `class ExtractionResult(BaseModel, frozen=True)` carrying:
    - `sections: dict[str, str]` — per-reference-ID section text. Key is the full reference ID (e.g., `"SA-TECH-3"`); value is the substring of the brief from the opening `[SA-TECH-3]` marker up to (but not including) the next `[<known-prefix>-N]` marker that belongs to one of the same brief's `prefixes`, with trailing whitespace stripped.
    - `unknown_markers: tuple[str, ...]` — bracketed tokens of the form `[X-Y]` that look like reference markers but resolve to no known prefix. Empty in the steady state; populated when an upstream produces a malformed reference (caught downstream as a Layer 3 referential failure when consumers cite it). Used by story 05a to decide whether to log a warning.

  - `extract_sections(bundle: BriefBundle) -> ExtractionResult` — pure function. Implementation outline:
    1. Scan `bundle.text` for occurrences of `[<TOKEN>]` where `<TOKEN>` matches the regex `[A-Z][A-Z0-9-]*-\d+` (broadest plausible reference shape — captures known and unknown markers in one pass).
    2. For each match, call `parse_reference_id(token)` from story 03:
       - If it resolves to a `(prefix, index)` pair AND `prefix in bundle.prefixes`: this is a real section opener. Record the start offset.
       - If it resolves but `prefix not in bundle.prefixes`: structural anomaly (an SA-TECH brief carrying a `[QR-1]` marker). Record the marker in `unknown_markers`; do NOT treat it as a section opener. The extractor does not raise — silent recording lets the retrieval-store assembly (story 05a) decide policy.
       - If it does not resolve (`parse_reference_id` returns None): record in `unknown_markers`; do NOT treat as a section opener.
    3. The list of recorded section openers, in source order, defines the segmentation. For each opener at offset `i`, the section spans from `i` up to the next opener's offset (or end of text). Trailing whitespace on each section is stripped; leading whitespace is preserved (the marker `[SA-TECH-3]` may be preceded by section-header whitespace from the brief's own framing).
    4. Build `sections: dict[str, str]` with the full reference ID as key. Duplicate openers in source order overwrite (last write wins) and the duplicate is also recorded in `unknown_markers` — duplicates indicate the upstream produced a malformed brief; the extractor surfaces it without aborting (Layer 3 catches the consumer-side citation later).

  - The function never raises on the structure of `bundle.text`. Empty text returns `ExtractionResult(sections={}, unknown_markers=())`. Text with no matching markers returns the same. The single failure mode is a `TypeError` if `bundle` is not a `BriefBundle` — Pydantic's type system catches that at the call site.

- Section-header tolerance: the extractor must NOT consume `=== KEY FINDINGS ===` or similar section-divider lines as part of a preceding section. Concretely: a section's content runs from its opener up to *either* the next reference-marker opener *or* the next `=== ... ===` divider line — whichever comes first. This is the rule that lets a sector brief like

  ```
  === KEY FINDINGS ===
  [SA-TECH-1] foo
    Tickers: ...
  [SA-TECH-2] bar
    Tickers: ...
  === FLAGGED ANOMALIES ===
  [SA-TECH-ANOM-1] baz
    Anomaly type: ...
  ```

  produce three sections (`SA-TECH-1`, `SA-TECH-2`, `SA-TECH-ANOM-1`) where `SA-TECH-2`'s text ends before the `=== FLAGGED ANOMALIES ===` divider.

- Unit tests:
  - **Empty text** → `ExtractionResult(sections={}, unknown_markers=())`.
  - **Single sector brief**: a tech-semis brief with three findings, two anomalies, one thesis candidate (six sections total) is decomposed into six `sections` entries with the correct keys and contiguous-substring values. Verify by reconstructing the brief text from the sections plus the dividers and asserting equality up to whitespace at section boundaries.
  - **CR brief with multiple sections**: a CR brief covering regime + intra-sector correlation + lead-lag (e.g., 5 `[CR-N]` openers across three section dividers) is decomposed into 5 sections with correct keys; the section-divider lines do not leak into any section's text.
  - **QR brief with `QR-N` and `QR-CW-N`**: longest-match correctness — `[QR-1]` and `[QR-CW-1]` are *distinct* reference IDs and produce two distinct sections.
  - **Unknown marker mid-brief**: a brief containing `[FOO-1]` (unknown prefix) records the marker in `unknown_markers` and treats neighboring known markers normally. The unknown marker's text is included in whatever known section precedes it (or, if no preceding known section, contributes to no section).
  - **Foreign-prefix marker**: an `SA-TECH` brief containing a `[QR-1]` marker (a known prefix not in `SOURCE_PREFIXES[BriefSource.SA_TECH]`) records `[QR-1]` in `unknown_markers` and treats it as not-a-section-opener.
  - **Duplicate marker**: a brief with two `[SA-TECH-3]` openers — last write wins on `sections["SA-TECH-3"]`; `[SA-TECH-3]` appears in `unknown_markers`. Document this is the synthesizer's "unknown_markers also catches malformed-as-duplicate" semantics.
  - **Markers inside indented detail lines**: if a section's body contains `[SA-TECH-3]` literally inside a `Suggested question:` paragraph, the extractor treats that as a new opener. This may produce false segmentation; the design accepts it because the upstream contracts (sector brief, qualitative brief) instruct the LLM to use markers ONLY at section openings — anything else is producer-side malformedness the extractor should not silently mask. Test asserts the behavior so it does not regress unintentionally.
  - **Determinism**: identical `BriefBundle` input produces byte-identical `ExtractionResult` (key order is insertion order; insertion order is source order). Run twice; assert equality.
  - **Unicode safety**: a brief containing non-ASCII characters in section bodies (e.g., currency symbols, ellipses) extracts cleanly with no character-encoding artifacts.

Out of scope:
- The retrieval store assembly that aggregates per-bundle `ExtractionResult`s into a single per-invocation lookup (story 05a).
- Adapters that produce `BriefBundle` from upstream value objects like `SectorBrief` or `CorrelationRegimeBrief` (story 05a covers them as part of the retrieval-store assembly entry point).
- Validation of the references the synthesizer's output cites (downstream Layer 3 work in the analyst, strategist, PM trees).
- The `retrieve_brief` MCP tool wrapping the retrieval store (story 06a).

## Notes

The extractor is intentionally permissive on malformed input. The synthesizer's contract is "stop-reason check only" per [`llm-output-validation.md § Per-agent surface mapping`](../../../design/testing/llm-output-validation.md#per-agent-surface-mapping); upstream-brief malformedness produces consumer-side Layer 3 failures when the analyst / strategist / PM cites the malformed reference. The extractor's job is to give the retrieval store the best decomposition it can compute and surface the anomalies via `unknown_markers` for diagnostic logging — not to abort.

The `=== ... ===` divider rule is load-bearing for sector and CR briefs. Without it, the last finding before a divider would absorb the divider plus the entire next section's content into its text — making the retrieval-store entry for `SA-TECH-2` include `=== FLAGGED ANOMALIES === [SA-TECH-ANOM-1] ...`. Decision-layer agents calling `retrieve_brief("SA-TECH-2")` would then see anomaly text mixed into a finding section.

The longest-match guarantee for `parse_reference_id` (story 03) flows through the extractor. The extractor calls `parse_reference_id` with the captured `<TOKEN>` and trusts its result — no second prefix-resolution logic here.

The `unknown_markers` field is informational and used by the retrieval-store assembly (story 05a) to decide whether to log a warning. It is NOT surfaced to the synthesizer's LLM (which would just be confusing noise) and is NOT raised as an error (which would over-tighten the synthesizer's fail-closed semantics — the design says only stop-reason failures abort).

The "trailing whitespace stripped, leading whitespace preserved" choice serves two purposes: stripping trailing whitespace makes section keys diffable across invocations (avoiding spurious differences from arbitrary blank lines); preserving leading whitespace means a section's text starts at column 0 with the `[REF-N]` opener, which is what the synthesizer's LLM and any human reader expect.

Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md): keep `extract_sections` a single pass over the text. Do not introduce a separate prefix-trie or pre-tokenizer; the regex pass + per-match `parse_reference_id` lookup is fast enough at the scale of one invocation's worth of brief text (~tens of KB total).

## Acceptance criteria

- [ ] `extract_sections(bundle)` returns `ExtractionResult(sections={}, unknown_markers=())` for empty `bundle.text`.
- [ ] A canonical six-section sector-brief fixture decomposes into six `sections` entries with the correct keys.
- [ ] A CR-brief fixture spanning three section dividers and five `[CR-N]` openers decomposes into five sections with no divider text leaking into any section.
- [ ] A QR-brief with both `[QR-1]` and `[QR-CW-1]` produces two distinct sections (longest-match correctness).
- [ ] An unknown-prefix marker like `[FOO-1]` is recorded in `unknown_markers` and is not treated as a section opener.
- [ ] A foreign-prefix marker `[QR-1]` inside an `SA-TECH` bundle is recorded in `unknown_markers` and is not treated as a section opener.
- [ ] A duplicate-opener brief overwrites the section value and records the duplicate ID in `unknown_markers`.
- [ ] A `[SA-TECH-3]` marker inside an indented detail line is treated as a new section opener (the extractor does not silently absorb it; producer-side malformedness surfaces).
- [ ] Identical `BriefBundle` input produces byte-identical `ExtractionResult` across repeated calls.
- [ ] Non-ASCII content in section bodies extracts cleanly.
- [ ] The function never raises on the structure of `bundle.text`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
