---
status: not_started
completed_date:
commit_id:
---

# 09c — Energy researcher system prompt

## Goal

Draft the system prompt for the energy domain researcher agent at `prompts/analysis/energy_researcher.md`. Implements the role, energy-sector mandate, signal taxonomy discipline, anti-patterns, output contract, and example output the design doc specifies.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/energy.md` — the authoritative spec (sector mandate, key signals — EIA inventory, OPEC, geopolitics, refining margins, LNG)
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the shared output contract every sector researcher uses
- `docs/design/01-data-layer/external/qualitative.md` § 6b — sector-specific qualitative input (OPEC rhetoric, inventory narrative, weather, pipeline)
- `docs/design/02-distillation-layer/external.md` § Output format — the distillation slice the agent reads (commodity-relevant outputs from §8)
- `prompts/decision/analyst.md` — pattern reference for prompt structure
- `docs/implementation/03-analysis-layer/domain-researchers/09a-tech-semis-system-prompt.md` — the parallel sector prompt; structural twin

## Depends on

- 04 (parser — the round-trip-test acceptance criterion parses the prompt's example output)
- 05 (validator — the same round-trip test validates the parsed brief)

Same reasoning as story 09a.

## Scope

In scope:
- `prompts/analysis/energy_researcher.md` — single markdown file. Same seven-section structure as 09a / 09b, adapted to the energy sector.

  - **Comment header** — naming the design docs (energy.md, qualitative.md §6b, llm-output-validation.md).
  - **`<role>`** — "You are the energy sector researcher in a systematic trading pipeline. You read the sector's slice of distilled market data, sector-specific qualitative inputs, and the universal volatility regime label, and emit a structured sector brief — key findings, flagged anomalies, and thesis candidates — for downstream cross-domain synthesis. Specialized in commodity-price linkage: maps oil/gas price movements to individual stock impact based on business mix (E&P vs. midstream vs. services)."
  - **`<operating_context>`** — same operational framing as 09a (fresh context, single input bundle, output consumed by the synthesizer, reference-ID format is load-bearing, no tools).
  - **`<inputs>`** — same three categories, with sector-specific framing:
    1. Volatility regime label.
    2. Distillation slice for ~15 energy tickers. Includes commodity-relevant outputs from §8 (industrial metals divergence, crack spread vs. energy stock divergence, DXY-commodity correlation regime).
    3. Sector-specific qualitative input — OPEC rhetoric and compliance, inventory and supply narrative, weather and seasonal patterns, pipeline and infrastructure developments.
  - **`<task>`** — produce a single sector brief. Same three-section structure. Same anti-padding discipline.
  - **`<method>`** — same discipline section, with sector-specific guidance:
    - Commodity-price-linkage is first-order: map oil/gas price moves to per-name impact based on business mix (E&P sees direct upstream leverage; midstream is volume-driven; refiners see crack-spread leverage; services are capex-cycle-driven). Apply the appropriate lever per ticker.
    - Geopolitical events (Middle East, Russia-Ukraine, Taiwan Strait) move oil and energy stocks together — surface as cross-sector findings when relevant.
    - Hurricane season (June–November) creates recurring Gulf Coast supply risk; if the input includes weather signals, weight accordingly.
    - OPEC decisions are scheduled binary events; pre-meeting rhetoric (qualitative input §6b) is a leading indicator.
    - Reference-ID prefix is `SA-ENERGY`. Sequential indexing, no gaps, no duplicates.
  - **`<output_contract>`** — reproduce the shared output contract with `SA-ENERGY` prefix substitutions on the example IDs.
  - **`<example_output>`** — one canonical valid sector brief with `SA-ENERGY` reference IDs. Tickers from the energy universe (XOM, CVX, COP, EOG, SLB, OXY, etc.). Cover a commodity-linkage finding, a geopolitical-rhetoric finding, an inventory or refining finding; one anomaly; one thesis candidate.
  - **`<constraints>`** — same anti-padding, anti-cross-sector-ticker, anti-trade-proposal, anti-hedging, anti-forward-reference, anti-prose constraints as 09a / 09b, with the energy sector universe as the membership scope.

Out of scope:
- Same as 09a / 09b.

## Notes

The token budget is the same as financials (300–600) per the design doc — smaller universe (~15 names). System prompt does not set numeric output-length targets; harness `output_token_budget` cap (story 02 sets `600` for energy) is the structural lever.

The energy sector's distinguishing feature for the researcher is *commodity-price linkage by business mix*. The example output should demonstrate the per-name reasoning: e.g., a finding that crude moved up 3% with XOM (integrated, full-chain exposure) rallying 1% but COP (E&P-heavy) rallying 3% — the differential carries information.

Geopolitical signals are unique to this sector in their cross-sector ripple potential — they move oil supply expectations and broad risk appetite simultaneously. The method section should call this out: a Middle East escalation finding is intrasectoral here even though it has cross-sector implications; the brief reports the energy-side reading and the synthesizer connects it to other sectors.

Inventory data (EIA weekly) is a recurring high-signal release. The example or method section should treat it as one of the canonical cases — what does an unexpected build look like in the brief, what does an unexpected draw look like.

Use the `agent-system-prompts` skill (`Skill("agent-system-prompts")`) when drafting.

## Acceptance criteria

- [ ] `prompts/analysis/energy_researcher.md` exists.
- [ ] The file has the same seven canonical sections as 09a / 09b.
- [ ] The output-contract section reproduces the shared output contract with `SA-ENERGY` reference-ID examples.
- [ ] The example-output section contains a complete, parser-valid sector brief using `SA-ENERGY`-prefixed IDs and tickers from the energy universe.
- [ ] The role section explicitly mentions commodity-price-linkage specialization.
- [ ] The method section names the four business-mix categories (E&P, midstream, services, refining) and describes how to apply commodity-price moves per category.
- [ ] The constraints section names: no invented tickers, no cross-sector tickers, no trade proposals, no forward references, no padding, no surrounding prose.
- [ ] No numeric anchors set as targets.
- [ ] A round-trip test parses the example output into a `SectorBrief` and validates it cleanly against a fixture energy sector membership.
