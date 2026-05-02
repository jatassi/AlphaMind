---
status: not_started
completed_date:
commit_id:
---

# 09b — Financials researcher system prompt

## Goal

Draft the system prompt for the financials domain researcher agent at `prompts/analysis/financials_researcher.md`. Implements the role, financials-sector mandate, signal taxonomy discipline, anti-patterns, output contract, and example output the design doc specifies.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/financials.md` — the authoritative spec (sector mandate, key signals — yield curve, credit spreads, loan growth, M&A)
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the shared output contract every sector researcher uses
- `docs/design/01-data-layer/external/qualitative.md` § 6c — sector-specific qualitative input (credit conditions, rate environment, M&A pipeline, regulatory posture, consumer/payment, crypto)
- `docs/design/02-distillation-layer/external.md` § Output format — the distillation slice the agent reads
- `prompts/decision/analyst.md` — pattern reference for prompt structure
- `docs/implementation/03-analysis-layer/domain-researchers/09a-tech-semis-system-prompt.md` — the parallel sector prompt; structural twin

## Depends on

- 04 (parser — the round-trip-test acceptance criterion parses the prompt's example output)
- 05 (validator — the same round-trip test validates the parsed brief)

Same reasoning as story 09a.

## Scope

In scope:
- `prompts/analysis/financials_researcher.md` — single markdown file. Same seven-section structure as story 09a, adapted to the financials sector. The two prompts share the bulk of their text by design; only sector-specific bits differ.

  - **Comment header** — naming the design docs (financials.md, qualitative.md §6c, llm-output-validation.md).
  - **`<role>`** — "You are the financials sector researcher in a systematic trading pipeline. You read the sector's slice of distilled market data, sector-specific qualitative inputs, and the universal volatility regime label, and emit a structured sector brief — key findings, flagged anomalies, and thesis candidates — for downstream cross-domain synthesis. Specialized in rate sensitivity analysis: maps macro data releases (CPI, jobs, FOMC) to expected sector impact across banks, payments, and fintech."
  - **`<operating_context>`** — same operational framing as 09a (fresh context, single input bundle, output consumed by the synthesizer, reference-ID format is load-bearing, no tools).
  - **`<inputs>`** — same three categories, with sector-specific framing on the qualitative slice:
    1. Volatility regime label.
    2. Distillation slice for ~15 financials tickers — banks, payments, fintech. Includes rate-environment-relevant macro/rates outputs (yield curve regime, credit spread changes, dollar-move attribution) per `external.md § 6` distillation outputs.
    3. Sector-specific qualitative input — credit conditions narrative, rate environment commentary, M&A and deal pipeline, regulatory posture, consumer/payment trends, crypto/digital assets narrative.
  - **`<task>`** — produce a single sector brief. Same three-section structure (key findings, flagged anomalies, thesis candidates). Same anti-padding discipline.
  - **`<method>`** — same discipline section, with sector-specific guidance:
    - Rate-environment signals (yield curve regime change, credit spread widening, dollar-move attribution) are first-order for this sector; promote them when signal-strength warrants.
    - M&A rumors on universe names are high-signal at the 4–72h horizon — surface as findings or thesis candidates depending on credibility.
    - Crypto-narrative signals (relevant for COIN, SQ) move largely independently of broader financials dynamics — keep that discipline; do not bundle a crypto signal under a banking-sector finding.
    - Reference-ID prefix is `SA-FIN`. Sequential indexing, no gaps, no duplicates.
  - **`<output_contract>`** — reproduce the `tech-semis.md § Domain researcher output contract` template with `SA-FIN` prefix substitutions on the example IDs.
  - **`<example_output>`** — one canonical valid sector brief with `SA-FIN` reference IDs. Tickers from the financials universe (JPM, BAC, GS, MS, COIN, V, MA, etc.). Cover at least one rate-sensitive finding, one credit-related finding, one M&A or regulatory finding; one anomaly with a non-default severity; one thesis candidate.
  - **`<constraints>`** — same anti-padding, anti-cross-sector-ticker, anti-trade-proposal, anti-hedging, anti-forward-reference, anti-prose constraints as 09a, with the financials sector universe as the membership scope.

Out of scope:
- Same as 09a.

## Notes

The token budget is smaller (300–600) than tech/semis (400–800) per the design docs — reflecting the smaller universe (~15 vs ~35). The system prompt does not set output-length targets (per `feedback_avoid_numeric_anchors`); the harness's `output_token_budget` cap (story 02 sets `600` for financials) is the structural enforcement.

Because the financials researcher reads more macro/rates context (FOMC, CPI, yield curve) than the tech researcher does, the example output should include at least one finding that ties a macro signal to ticker-level positioning. Models the reasoning chain the agent should produce.

Crypto-narrative signals are unique to this sector: COIN and SQ are the primary names, and crypto regulatory developments move them largely independently of broader financials. The example or method section should note this — without crypto coverage, COIN-relevant findings would be misclassified or omitted.

The `<method>` section should explicitly note that yield-curve regime changes (a distillation output) are typically the highest-impact macro lever for this sector — to anchor calibration on what `strong` looks like for a financials brief.

Use the `agent-system-prompts` skill (`Skill("agent-system-prompts")`) when drafting.

## Acceptance criteria

- [ ] `prompts/analysis/financials_researcher.md` exists.
- [ ] The file has the same seven canonical sections as 09a (comment header through constraints).
- [ ] The output-contract section reproduces the shared output contract with `SA-FIN` reference-ID examples.
- [ ] The example-output section contains a complete, parser-valid sector brief using `SA-FIN`-prefixed IDs and tickers from the financials universe.
- [ ] The role section explicitly mentions rate-sensitivity-analysis specialization.
- [ ] The method section calls out crypto-narrative signals as independent of broader financials dynamics.
- [ ] The constraints section names: no invented tickers, no cross-sector tickers (a financials brief mentioning NVDA is structural malformation), no trade proposals, no forward references, no padding, no surrounding prose.
- [ ] No numeric anchors set as targets.
- [ ] A round-trip test parses the example output into a `SectorBrief` and validates it cleanly against a fixture financials sector membership.
