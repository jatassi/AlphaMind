Multiple analysis-layer parsers reject model output for literal-template regexes Sonnet drifts from under load. Reproduced on the 2026-05-03 verification run across at least two agents.

**Domain-researcher parser** (`src/alphamind/analysis/domain_researchers/parser.py`): `_CONVICTION_RE` (line 445) requires the literal word `"with"` — Sonnet emits `moderate — technical alignment...`; `_DEGRADED_REASON_RE` (line 54) requires bracketed `[If DEGRADED: reason — ...]` template — Sonnet writes free-form `Signal quality: DEGRADED\n  Sentiment aggregates absent...`.

**Qualitative-researcher parser** (`src/alphamind/analysis/qualitative_research/parser.py`): same `signal_quality is DEGRADED but no reason line found` failure.

**Adaptive-researcher parser** (`src/alphamind/analysis/adaptive_research/parser.py`): `Invocation header 'inv-2026-05-03T00-00Z' does not match caller-supplied invocation_id` — model invents its own ID instead of echoing the one supplied in the input bundle.

**Corrective-retry directive (all analysis harnesses):** model prepends conversational preamble before the corrected brief, tripping the `first non-blank line must be exactly 'QUALITATIVE BRIEF'` (or sector-equivalent) check.

**Three-part fix:**

**(A) Loosen regexes** — accept reasonable separators: `(low|moderate|high)[\s,—\-:]+(.+)`, free-form `Reason:` variant for DEGRADED.

**(B) Add RIGHT/WRONG format examples** in `prompts/analysis/{tech_semis,financials,energy,qualitative}_researcher.md` so the model defaults to canonical syntax.

**(C) Strengthen corrective-retry directive** in each harness: "no preamble, no acknowledgment, output the corrected brief starting with the envelope marker."

*Energy sector parsed cleanly (9 tickers); financials failed both attempts (17 tickers, more drift). The contract is achievable but unforgiving.*