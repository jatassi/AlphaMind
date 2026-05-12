# 03a — Markdown brief parser

## Goal

Implement `parse_qualitative_brief` — the parser that turns the qualitative researcher's raw text response into a typed `QualitativeBrief` instance. The first transform in the LLM-output validation stack for this agent class. Strict parse: malformed input raises `ParseError` rather than being silently coerced. The parser is structurally similar to `alphamind.analysis.domain_researchers.parser.parse_brief` but recognizes a different section vocabulary (`=== NARRATIVE THREADS ===`, `=== CATALYST WATCH ===`, `=== SENTIMENT SNAPSHOT ===`) and produces a different brief shape.

## Reading

* `prompts/analysis/qualitative_researcher.md` § `<output_contract>` — the literal section markers, indentation, and field labels the parser must recognize. Quote this section verbatim into the parser module's docstring so future readers can audit the contract without context-switching.
* `prompts/analysis/qualitative_researcher.md` § `<example_output>` — a worked example. The parser must accept this example verbatim and produce a `QualitativeBrief` whose `threads`, `catalyst_watches`, and `sentiment_snapshot` round-trip back to the example text via story 03b's validator.
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Output schema — the section structure and field semantics; § Schema design rationale for the per-field intent.
* `docs/design/testing/llm-output-validation.md` § Layer-1 — envelope-parse — the strict-parse stance: a single leading/trailing markdown code fence is stripped, nested fences are a `ParseError`, prose-wrapped content is a `ParseError`.
* `src/alphamind/analysis/domain_researchers/parser.py` — the canonical analog. Mirror the `ParseError` shape (`field_path`, `message`), the strict envelope-parse behavior, the per-section line-by-line walking pattern, and the `_VALID_*` constants discipline.
* `src/alphamind/analysis/qualitative_research/models.py` — the records the parser produces (story 02).

## Depends on

* ALP-242 — story 02 lands `QualitativeBrief` and its constituent records; the parser produces them.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/parser.py` and `tests/analysis/qualitative_research/test_parser.py`.

### 1\. `ParseError`

A module-private exception class with `field_path: str` and `message: str` attributes — same shape as `domain_researchers.parser.ParseError` so story 04b's harness can build corrective-retry messages from a uniform shape.

### 2\. `parse_qualitative_brief(text: str, *, invocation_id: str) -> QualitativeBrief`

Public entry point. The brief does not self-identify its `invocation_id` (the design doc has `Invocation: {invocation_id}` in the header but the parser need not trust the agent's claim — the harness supplies the canonical value). Behavior:

**Envelope strip.** Strip leading/trailing whitespace. If the body is wrapped in a single `` ``` ``/`` ```text ``/`` ```markdown `` fence, strip exactly one fence pair. Multiple fences, partial fences, or any prose preceding the `QUALITATIVE BRIEF` header is a `ParseError` with `field_path="envelope"`.

**Header parse.** First non-blank line must be exactly `QUALITATIVE BRIEF`. Next line: `Invocation: <id>` — parsed but discarded (the supplied `invocation_id` wins). Next line: `Signal quality: <HIGH|MODERATE|LOW|DEGRADED>`. If `DEGRADED`, the next line must match `r"^\s*\[If DEGRADED:\s*reason\s*[—\-]\s*(.+?)\]\s*$"` capturing the reason; otherwise no reason line is allowed.

**Section detection.** Three section markers are required and must appear in order: `=== NARRATIVE THREADS ===`, `=== CATALYST WATCH ===`, `=== SENTIMENT SNAPSHOT ===`. Missing or out-of-order section markers are a `ParseError`.

**Narrative threads.** Each thread starts with a line `[QR-N] {summary}` where `N` is a positive integer. Subsequent indented lines name fields:

* `Relevance: {free text}`
* `Direction: {bullish|bearish|mixed|uncertain} for {subject text}`
* `Time horizon: {immediate (<24h)|near-term (24-72h)|developing (>72h)}`
* `Evidence:` followed by `- {source_type}: {observation} [from {citation}]` lines.
* `Implication: {free text}`

Per-evidence-line parse: split on the first colon for `source_type`, the trailing bracketed `[from ...]` block for `citation`, and the middle for `observation`. Trim whitespace.

**Catalyst watch.** Empty section is valid (no entries). Each entry: `[QR-CW-N] {ticker}: {catalyst_name} in {hours}h` followed by an indented `Thesis impact: {text}` line. The `hours` token must be a non-negative integer.

**Sentiment snapshot.** Three lines, in order: `Extremes: {text}`, `Divergences: {text}`, `Regime: {text}`. All three are required.

**End of input.** Any non-whitespace content after the `Regime:` line is a `ParseError`.

### 3\. Strictness

* Unknown enum values (an unrecognized `Direction` token, an unrecognized `Time horizon` token, an unrecognized `Signal quality`) raise `ParseError` rather than coercing.
* A wrong reference-ID prefix (`[SA-TECH-1]` in the threads section) raises `ParseError` with `field_path="threads[i].thread_id"`.
* A `[QR-N]` heading with no body or fewer than 2 evidence lines raises `ParseError` (the two-source minimum is enforced at the model level too — both raise; the parser's check gives a more specific error message).
* Entirely-empty narrative-threads section raises `ParseError` with the at-least-one-thread message (story 02's model also enforces it).

### Out of scope

* Sequential-index validation (`QR-1, QR-2, QR-3`) and prefix-uniqueness checks — story 03b's structural validator owns these.
* Ticker-universe-membership validation — story 03b.
* Cross-stream reference integrity (e.g., a thread cites `[ND-T3]` but the digest does not contain `ND-T3`) — out of scope for this work tree per the parent issue's notes.
* Tolerating prose/markdown variations the contract does not name — strict parse means the agent must produce exactly the contracted shape.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.parser import parse_qualitative_brief, ParseError` resolves.
- [ ] `parse_qualitative_brief(prompt_example_output, invocation_id="inv-...")` returns a `QualitativeBrief` whose `len(threads) >= 1`, `sentiment_snapshot.extremes` / `divergences` / `regime` are populated, and (if the example contains catalyst watches) `len(catalyst_watches) >= 1`.
- [ ] Stripping a single ` ```text ` fence around an otherwise-valid brief succeeds; nested fences raise `ParseError`.
- [ ] An `=== NARRATIVE THREADS ===` section with one thread that has only one evidence line raises `ParseError` whose `field_path` names the offending thread.
- [ ] `Direction: long for X` raises `ParseError` (`long` is not a valid `ThreadDirection`).
- [ ] `Time horizon: medium-term` raises `ParseError`.
- [ ] `Signal quality: DEGRADED` without the `[If DEGRADED: reason — ...]` line raises `ParseError`.
- [ ] `Signal quality: HIGH` followed by a `[If DEGRADED: reason — ...]` line raises `ParseError` (reason forbidden when not degraded).
- [ ] Empty `=== CATALYST WATCH ===` section produces `catalyst_watches=()`.
- [ ] Out-of-order section markers raise `ParseError` whose `field_path` names the misplaced marker.
- [ ] Trailing prose after the `Regime:` line raises `ParseError`.
- [ ] `tests/analysis/qualitative_research/test_parser.py` covers each rule above. Tests pass under `uv run pytest tests/analysis/qualitative_research/test_parser.py -n auto`.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_parser.py -n auto`. Spot-check that the parser accepts the verbatim `<example_output>` from `prompts/analysis/qualitative_researcher.md` (a one-test fixture). Confirm `uv run mypy src/alphamind/analysis/qualitative_research/parser.py` is clean.
