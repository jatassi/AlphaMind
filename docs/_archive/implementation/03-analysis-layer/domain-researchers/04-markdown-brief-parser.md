---
status: not_started
completed_date:
commit_id:
---

# 04 — Markdown brief parser

## Goal

Implement the parser that turns a domain researcher's raw text response into a typed `SectorBrief` instance — the first transform in the LLM-output validation stack for this agent class. The parser is strict in the same sense as the formal-schema parsers in [llm-output-validation.md § Layer 1](../../../design/testing/llm-output-validation.md#layer-1--envelope-parse): malformed input fails parse rather than getting silently coerced.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the text format the parser consumes
- `docs/design/testing/llm-output-validation.md` § Layer 1 — Envelope parse — strict-parse stance the parser implements (analogue for text-format agents)
- `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy § Producer-side format rules — the `SA-{SECTOR}-N` patterns the parser extracts

## Depends on

- 03 (`SectorBrief` data model — the parser's output type)

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `parser.py` defining:
  - `class ParseError(Exception)` — raised on any structural malformation. The exception carries `field_path: str` and `message: str` attributes mirroring the JSON Schema validator's error shape (used by the validator story 05's corrective-retry message construction).
  - `parse_brief(text: str, sector: Sector) -> SectorBrief` — the main entry point. Strips a single leading and trailing markdown code fence (` ```json` / ` ```text` / ` ``` ` / no fence) before parsing; nested fences are a `ParseError` (matches the Layer 1 stance for JSON-format agents). Tolerates leading/trailing whitespace; does not tolerate prose-wrapped content.
  - The `sector` parameter is required because briefs do not self-identify their sector in the text — the prefix `SA-TECH` / `SA-FIN` / `SA-ENERGY` is implicit from which agent emitted the brief. The harness (story 07) passes the agent's configured sector.
  - The parser asserts every reference-ID prefix in the text matches the expected sector prefix; mismatch is a `ParseError` (a tech/semis researcher emitting `SA-FIN-1` is structural malformation, not validator territory).
- Section-by-section parser logic against the prose contract:
  - **Header block**: extract `SECTOR BRIEF: {sector_label}`, `Invocation: {invocation_id}`, `Signal quality: {HIGH | MODERATE | LOW | DEGRADED}` and the optional `[If DEGRADED: reason — ...]` line. The bracketed reason is collapsed into `signal_quality_reason`; absence raises a `ParseError` only when `signal_quality == DEGRADED` (match the Pydantic model's invariant).
  - **`=== KEY FINDINGS ===` section**: zero-to-many `Finding` records. Each record opens with `[SA-{SECTOR}-N] {one-sentence headline}` and continues with indented `Tickers:`, `Signal type:`, `Strength:`, `Detail:` lines (in any order; the parser uses field labels, not positional order). The 3–5 advisory range from the design doc is not enforced — that is the system prompt's job to encourage; the parser accepts any non-negative count.
  - **`=== FLAGGED ANOMALIES ===` section**: zero-to-many `Anomaly` records. Same indentation pattern; required fields `Anomaly type:`, `Tickers:`, `Severity:`, `Suggested question:`. Section header is required even when the section has zero records (the LLM emits an empty section rather than skipping the header).
  - **`=== THESIS CANDIDATES ===` section**: zero-to-many `ThesisCandidate` records. Required indented fields `Ticker:`, `Direction:`, `Setup type:`, `Catalyst/driver:`, `Time horizon:`, `Conviction sketch:`, `Key risk:`. The conviction-sketch line is `{low | moderate | high} with {1-sentence justification}` — split on the literal `" with "` separator into `conviction_sketch` and `conviction_justification`.
- The `Tickers:` field accepts a comma-separated list (e.g., `"NVDA, AMD"`) and is normalized to `tuple[str, ...]` with whitespace stripped per element. Single-ticker entries (no comma) yield a one-element tuple.
- Tolerance rules:
  - Whitespace inside section bodies is tolerated; section-header lines must match `^=== {SECTION_NAME} ===$` exactly (no trailing or leading whitespace inside the markers).
  - Trailing whitespace on field-label lines (`Tickers: NVDA, AMD  `) is stripped.
  - Empty `Tickers:` value or `Tickers:` line missing entirely → `ParseError`.
  - Enum-valued fields (`Signal type:`, `Strength:`, `Anomaly type:`, `Severity:`, `Direction:`, `Setup type:`, `Conviction sketch:`) accept exact case-sensitive matches against the design doc's spellings (e.g., `price_action`, `investigate_now`, `long`, `mean_reversion`). Mismatches → `ParseError`. The `StrEnum` lookup happens after the parser confirms the literal string; case-folding is not a recovery path.
- Unit tests:
  - Round-trip: a hand-constructed canonical valid brief text parses to the expected `SectorBrief` instance for each of three sectors.
  - Code-fence stripping: same brief wrapped in ` ```text ... ``` `, ` ``` ... ``` `, or unfenced parses identically.
  - Nested code fences raise `ParseError`.
  - Prose preceding or following the brief raises `ParseError`.
  - Each section-header missing → `ParseError` naming the section.
  - Section out of order → `ParseError`.
  - Wrong sector prefix in any reference ID → `ParseError`.
  - Each enum-valued field with an out-of-set value → `ParseError`.
  - `signal_quality: DEGRADED` without the bracketed reason line → `ParseError`.
  - Empty findings / anomalies / thesis-candidates section parses to an empty tuple (zero is valid for anomalies and thesis candidates per the design doc; required-min-1 for findings is the validator's job, not the parser's).
  - Tickers list with single, multiple, and trailing-comma cases all parse correctly.

Out of scope:
- Sequential indexing checks (validator story 05): the parser preserves the IDs as it finds them (`SA-TECH-1`, `SA-TECH-3` with no `SA-TECH-2` is parser-acceptable; the validator catches the gap).
- Cross-record consistency (e.g., a finding's `signal_type` matching the synthesizer's expectation): not the parser's concern.
- Reverse-direction serialization (`SectorBrief → text`). The LLM produces text; tests compare parsed model to expected model.
- The Layer 4 stop-reason check that re-classifies a parse failure paired with `max_tokens` as `context_overflow` — that lives in the harness story 07.

## Notes

Why text format rather than JSON: the design doc deliberately specifies a markdown-style structured text — reading-friendly for the synthesizer (which is itself an LLM) and for human inspection of the invocation archive. Imposing JSON would change the design.

The parser's strict stance mirrors the JSON-output parsers' Layer 1 stance: lenient parsing converges on accepting outputs the validator can no longer assess. A failed parse triggers the corrective-retry path in the harness story 07 — same single-attempt behavior as Layer 2 / 3 failures.

The case-sensitive enum match (no case-folding) is intentional: the system prompt instructs the agent to use exact lowercase spellings. Drift between prompt and parser is detectable through the parse-failure rate in the feedback loop. If drift becomes routine, fix the prompt, not the parser.

The `field_path` shape on `ParseError`: use a JSONPath-like syntax for consistency with the Layer 2 validators in `llm-output-validation.md` (`/findings/2/signal_type`, `/header/signal_quality_reason`). The corrective-retry message construction (story 07) uses `field_path` to point the agent at the specific malformation.

The single-pass parser implementation: walk lines, accumulate by section, build records. Aim for ~150–200 lines of straightforward parser code, not a regex zoo. Pure-function, no I/O, deterministic.

## Acceptance criteria

- [ ] `parse_brief(text, sector)` returns a `SectorBrief` for every canonical valid brief fixture (one per sector).
- [ ] `parse_brief` strips a single leading/trailing markdown code fence (`json`-labeled, unlabeled, or none).
- [ ] Nested or multiple top-level code fences raise `ParseError`.
- [ ] Prose-wrapped briefs (text preceding or following the structured content) raise `ParseError`.
- [ ] Each missing required section header (`=== KEY FINDINGS ===`, `=== FLAGGED ANOMALIES ===`, `=== THESIS CANDIDATES ===`) raises `ParseError` with the section name in the message.
- [ ] Sections out of declared order raise `ParseError`.
- [ ] Reference-ID prefix mismatched with the `sector` parameter raises `ParseError`.
- [ ] Each enum-valued field with an out-of-set value raises `ParseError`.
- [ ] `signal_quality: DEGRADED` without a bracketed-reason line raises `ParseError`.
- [ ] `signal_quality_reason` line present without `signal_quality: DEGRADED` raises `ParseError`.
- [ ] `Tickers:` lines with single, comma-separated, and whitespace-padded values all parse to canonical `tuple[str, ...]`.
- [ ] Empty `=== FLAGGED ANOMALIES ===` / `=== THESIS CANDIDATES ===` sections parse to empty tuples.
- [ ] `ParseError` carries `field_path` and `message` attributes consistent with the Layer 2 validator's error shape.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
