# 03b — Adaptive brief parser

## Goal

Implement `parse_adaptive_brief` — the parser that turns the adaptive researcher's raw text response into a typed `AdaptiveBrief` instance. The first transform in the LLM-output validation stack for this agent class. Strict parse: malformed input raises `ParseError` rather than being silently coerced. Structurally similar to `alphamind.analysis.qualitative_research.parser.parse_qualitative_brief` but with the adaptive-specific complications: a header line carrying two integers (`Threads investigated: N`), a `none`-or-list `Anomalies deferred:` field, and per-thread conditional blocks driven by the `Assessment:` value.

## Reading

* `prompts/analysis/adaptive_researcher.md` § `<output_contract>` — the literal wire format the parser must accept. Sections are `=== INVESTIGATION THREADS ===` only (single section between header and stop). Sequential indexing on `AR-N` starting at 1.
* `docs/design/03-analysis-layer/adaptive-research.md` § Output § Output schema — names the conditional-field discipline per `Assessment` value and the empty-section semantics.
* `src/alphamind/analysis/qualitative_research/parser.py` — sibling-package parser to mirror. Note: fence-strip helpers, `_THREAD_HEADER_RE` pattern, evidence-line bullet parsing, error-raising conventions (`field_path` carries the LLM-readable JSON-pointer-style path, e.g., `threads[2].evidence`).
* `src/alphamind/analysis/domain_researchers/parser.py` — sibling for the section-marker walking pattern; reuse the error-message style (one error per failure, with `field_path` and a short human description).
* `src/alphamind/analysis/adaptive_research/models.py` — produced types (`AdaptiveBrief`, `InvestigationThread`, `Assessment`, `Confidence`).

## Depends on

* <issue id="b01cd510-80fc-44fb-814b-3aad59e877d6">ALP-255</issue> (story 02) — the data model the parser produces.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/parser.py` and `tests/analysis/adaptive_research/test_parser.py`.

### 1\. `ParseError` exception

```python
class ParseError(Exception):
    """Strict-parse failure; carries `field_path` for the harness's corrective-retry message.

    `field_path` is a JSON-pointer-style string (e.g., "threads[2].assessment",
    "header.threads_investigated_count") naming where the parse failed.
    `message` is a one-sentence description of what was malformed.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message
```

### 2\. `parse_adaptive_brief` function

```python
def parse_adaptive_brief(raw_response: str, *, invocation_id: str) -> AdaptiveBrief:
    """Parse the adaptive researcher's raw text response into an AdaptiveBrief.

    `invocation_id` is the value the brief is constructed with — the parser
    cross-checks it against the `Invocation:` header line and raises ParseError on mismatch.
    """
```

### 3\. Parser steps (in order)

**(a) Fence strip.** If the response opens with a markdown fence (```` ``` ```` optionally followed by a language identifier), strip the fence and the matching close. Reuse the qualitative-research `_FENCE_OPEN_RE` / `_FENCE_CLOSE_RE` patterns.

**(b) Header parse.** First lines must match (with whitespace tolerance):

```
ADAPTIVE RESEARCH FINDINGS
Invocation: {invocation_id}
Threads investigated: {N} of {M} anomalies triaged
Anomalies deferred: {refs or "none"}
```

Extract `N` and `M` as non-negative ints; raise `ParseError` if either is non-numeric or negative. Cross-check `invocation_id` against the parser argument; raise on mismatch with `field_path="header.invocation_id"`.

`Anomalies deferred:` value handling:

* The literal string `none` (case-insensitive) parses to `()`.
* Anything else is split on `,` (commas), stripped of whitespace per element, and stored as `tuple[str, ...]`.

**(c) Investigation threads section.** A line `=== INVESTIGATION THREADS ===` is required (even when zero threads). Empty section (no `[AR-N]` blocks following the marker) is valid — produces `threads=()`.

**(d) Per-thread parse.** For each `[AR-N]` block:

* Parse `thread_id = AR-{N}` from the bracketed header.
* Sequential indexing: the first `[AR-N]` must be `AR-1`, the next `AR-2`, etc. Raise `ParseError(field_path="threads[i].thread_id", ...)` on out-of-sequence.
* Each thread's body is a series of `  Field: value` lines (2-space indent), with `Findings:` opening a sub-block of `    - {finding}` lines (4-space indent). Order of fields follows the prompt's contract; the parser tolerates trailing blank lines between fields but raises if a required field is missing.
* Required fields (always): `Trigger`, `Question`, `Tickers`, `Sector`, `Tools used`, `Findings`, `Assessment`, `Confidence`.
* `Tickers:` value is comma-separated; empty value (after the colon, possibly the literal `none`) parses to `()`.
* `Sector:` value parses to a `Sector` enum member; raise on unknown value with `field_path="threads[i].sector"`.
* `Tools used:` value is comma-separated; empty value or `none` parses to `()`.
* `Findings:` is followed by zero or more `    - {text}` bullet lines. Empty `Findings:` (no bullet lines following) parses to `()`.
* `Assessment:` value parses to an `Assessment` enum member; raise on unknown value.
* `Confidence:` value parses to a `Confidence` enum member; raise on unknown value.

**(e) Conditional fields per assessment.** After `Confidence:`:

* If `Assessment: signal`: require `Implication:` (single-line value), `Strengthens:` (comma-separated, `none` → `()`), `Weakens:` (comma-separated, `none` → `()`). Set `dismissal_reason=None`, `missing=None`.
* If `Assessment: noise`: require `Dismissal reason:` (single-line value). Set `implication=None`, `strengthens=None`, `weakens=None`, `missing=None`.
* If `Assessment: inconclusive`: require `Missing:` (single-line value). Set `implication=None`, `strengthens=None`, `weakens=None`, `dismissal_reason=None`.

Missing required conditional field raises `ParseError(field_path="threads[i].{field}", ...)`.

**(f) Construct** `AdaptiveBrief`. Pass parsed fields to the constructor; the model's `_brief_invariants` validator enforces `threads_investigated_count == len(threads)` and `anomalies_triaged_count >= threads_investigated_count`. A model-validator failure inside the constructor surfaces as `ParseError` (wrap the `pydantic.ValidationError` and re-raise with `field_path="header"`).

### 4\. Module exports

```python
__all__ = ["ParseError", "parse_adaptive_brief"]
```

### Out of scope

* Layer-3 referential resolution of `Strengthens` / `Weakens` cross-references — story 03c's validator owns this.
* Layer-2 ticker-universe checks (do these tickers exist?) — story 03c's validator owns this.
* Any LLM call or harness logic — story 05.

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.parser import parse_adaptive_brief, ParseError` resolves cleanly.
- [ ] Parsing a well-formed brief with two threads (one `signal`, one `inconclusive`) returns an `AdaptiveBrief` whose `threads` tuple has length 2 and whose conditional fields match the wire format.
- [ ] Parsing a brief with `Threads investigated: 0 of 0 anomalies triaged`, `Anomalies deferred: none`, and an empty `=== INVESTIGATION THREADS ===` section returns an `AdaptiveBrief` with `threads=()` and `anomalies_deferred=()`.
- [ ] Parsing `Anomalies deferred: SA-TECH-ANOM-1, Distillation: q1.volume_spike NVDA 3.2σ` produces a two-element `anomalies_deferred` tuple with whitespace-stripped entries.
- [ ] An out-of-sequence thread (`[AR-1]` then `[AR-3]`) raises `ParseError` with `field_path` naming `threads[1].thread_id`.
- [ ] A signal thread missing `Strengthens:` raises `ParseError` with `field_path="threads[0].strengthens"`.
- [ ] A noise thread carrying `Implication:` raises `ParseError` (signal-only field present in non-signal thread) — the model's `_assessment_invariant` rejects, parser surfaces as `ParseError`.
- [ ] An invalid `Sector:` value (`Sector: macro`) raises `ParseError` with `field_path="threads[0].sector"`.
- [ ] An invalid `Assessment:` value raises `ParseError`.
- [ ] An invocation_id mismatch between the parser argument and the `Invocation:` header line raises `ParseError(field_path="header.invocation_id")`.
- [ ] A missing `=== INVESTIGATION THREADS ===` marker raises `ParseError(field_path="header.investigation_threads_marker")`.
- [ ] A markdown fence around the response is stripped before parsing (the parser tolerates ```` ```text ```` and bare ```` ``` ```` open/close).
- [ ] `tests/analysis/adaptive_research/test_parser.py` covers each acceptance criterion above.
- [ ] `uv run pytest tests/analysis/adaptive_research/test_parser.py -n auto` passes; lint + mypy clean.

## Verification

Run the parser against the worked example in `prompts/analysis/adaptive_researcher.md` § `<example_output>` and assert it parses cleanly and produces an `AdaptiveBrief` whose threads include one `signal` and one `inconclusive` (matching the example's structure). Construct one round-trip test: parse → `model_dump()` → re-render to wire format → parse again → assert structural equality.