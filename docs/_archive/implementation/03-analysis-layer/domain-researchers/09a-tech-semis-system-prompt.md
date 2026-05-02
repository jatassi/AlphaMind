---
status: not_started
completed_date:
commit_id:
---

# 09a — Tech & semis researcher system prompt

## Goal

Draft the system prompt for the tech & semiconductors domain researcher agent at `prompts/analysis/tech_semis_researcher.md`. Implements the role definition, sector mandate, signal taxonomy discipline, anti-patterns, output contract, and example output the design doc specifies.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` — the authoritative spec (sector mandate, key signals, output contract)
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the prose schema the prompt's output-contract section codifies
- `docs/design/01-data-layer/external/qualitative.md` § 6a — sector-specific qualitative input the agent reads
- `docs/design/02-distillation-layer/external.md` § Output format — the distillation slice the agent reads
- `prompts/decision/analyst.md` — pattern reference for prompt structure (role, operating context, inputs, task, method, output contract, example, constraints)
- The user's auto-memory `feedback_avoid_numeric_anchors.md` — preference for structural over numeric constraints
- The user's auto-memory `feedback_llm_agents_uniformly_critical.md` — fail-closed posture; no peer recovery

## Depends on

- 04 (parser — the round-trip-test acceptance criterion parses the prompt's example output)
- 05 (validator — the same round-trip test validates the parsed brief)

The prompt file itself can be drafted from the design docs alone; the dependencies exist because the acceptance-criterion round-trip test needs 04 and 05 landed. Drafting the prompt without those primitives in place defers the safety check that catches drift between the prompt's example output and the parser/validator.

## Scope

In scope:
- `prompts/analysis/tech_semis_researcher.md` — single markdown file. Sections (modeled after `prompts/decision/analyst.md` but adapted to the domain researcher's role):

  - **Comment header** — naming the design docs the prompt implements (tech-semis.md, qualitative.md §6a, llm-output-validation.md).
  - **`<role>`** — "You are the tech & semiconductors sector researcher in a systematic trading pipeline. You read the sector's slice of distilled market data, sector-specific qualitative inputs, and the universal volatility regime label, and emit a structured sector brief — key findings, flagged anomalies, and thesis candidates — for downstream cross-domain synthesis. Your job is intra-sector pattern recognition: surface what is happening in tech and semis right now, with the discipline a sector specialist brings."
  - **`<operating_context>`** — fresh context window per invocation; the user turn contains a single composed input bundle (regime, distillation slice, qualitative slice); your output is consumed by the synthesizer (an LLM) which assigns reference IDs and indexes by them — so reference-ID format and sequential indexing are load-bearing; you have no tools and produce a single sector brief.
  - **`<inputs>`** — three input categories:
    1. The volatility regime label (universal context broadcast — frame how strongly to weight pattern-recognition signals; e.g., flow signals are more meaningful in vol-expansion regimes than low-vol-compression).
    2. The distillation slice for tech/semis tickers (per-ticker indicators, anomaly flags, divergence detections).
    3. Sector-specific qualitative input — sector-tagged headlines plus scheduled events. Treat as raw material for narrative detection (AI infrastructure spending, supply chain, product cycle, competitive dynamics, export controls).
  - **`<task>`** — produce a single sector brief conforming to the output contract. Three sections:
    - Key findings: 3–5 in normal conditions; fewer on a quiet day, more on a volatile day. The output contract enforces zero-or-more; quality over quantity. Do not pad.
    - Flagged anomalies: 0–3. Only flag genuinely unusual observations. Tech/semis routine market movements (NVDA at 2× ATR is not unusual on an earnings-week day) are not anomalies.
    - Thesis candidates: 0–3 preliminary sketches. Lean records, not full trade theses. The analyst develops further; you surface candidates.
    Your job is *not* to predict prices, propose trades, or assess portfolio fit. Pattern recognition; pass forward.
  - **`<method>`** — the discipline section:
    1. Read the regime label first; let it tilt how aggressively you interpret signals.
    2. For each finding, name the affected tickers, classify by signal type taxonomy (`price_action | flow | options | fundamental | sentiment | technical | cross_asset`), assign a strength (`strong | moderate | weak`), and elaborate in 2–3 sentences with specific data points from the input bundle.
    3. Anomalies are flagged with severity (`investigate_now | investigate_if_persists | note_for_context`) — `investigate_now` is reserved for items the adaptive researcher should pick up this invocation; the rest are calibration signals.
    4. Thesis candidates are direction + setup type + catalyst + time horizon + conviction sketch + key risk. Time horizon is a string (e.g., `"4–24h"`, `"24–72h"`) — the design doc deliberately accepts imprecision; do not force false precision.
    5. Set `Signal quality: DEGRADED` when input data is materially incomplete (API failure, stale data evident from data-freshness timestamp). Otherwise `HIGH | MODERATE | LOW` as you read the signal density.
    6. **Reference-ID discipline**: every `[SA-TECH-N]`, `[SA-TECH-ANOM-N]`, `[SA-TECH-TC-N]` you emit must be sequential per section starting at 1. Gaps and duplicates are rejection-grounds. Never use a non-tech prefix; the synthesizer routes by prefix.
  - **`<output_contract>`** — the structured-text format. Replicate the canonical block from `tech-semis.md § Domain researcher output contract` verbatim (the prompt is the LLM's source of truth). Include the section headers (`=== KEY FINDINGS ===`, etc.) literally — the parser keys on them. Direct the agent to emit the brief as plain text with no surrounding prose, no markdown code fences, no preface.
  - **`<example_output>`** — one canonical valid sector brief. ~12–18 lines covering 3 findings (one of each strength), 1 anomaly, 1 thesis candidate. Tickers from the actual tech/semis universe (NVDA, AMD, TSM, ASML, etc.). Reference IDs `SA-TECH-1` through `SA-TECH-3`, `SA-TECH-ANOM-1`, `SA-TECH-TC-1`. Use plausible signal types and findings that exercise the taxonomy.
  - **`<constraints>`** —
    - Do not invent tickers. Every ticker mentioned must be in the tech/semis sector universe (the synthesizer rejects cross-sector tickers in a tech brief).
    - Do not propose trades. Thesis candidates are sketches; sizing, brackets, and validation are the analyst's job downstream.
    - Do not hedge with "could," "might," "possibly" beyond what the strength field already conveys. A `weak` finding is honest about its strength; a `strong` finding hedged with weasel words conflicts with itself.
    - Do not duplicate findings across the three sections — a ticker movement is a finding, an anomaly, or a thesis candidate, not all three.
    - Do not reference IDs that do not yet exist (forward references, e.g., a finding citing a thesis candidate that comes later). The brief is read top-to-bottom; references are backward.
    - Stop after emitting the brief. No prose before, after, or within the section markers.

Out of scope:
- Tool use directives (tech/semis researcher has no tools).
- Anything portfolio-state-aware — the agent does not see held positions; that is the strategist's territory downstream.
- Reasoning about the *other* sectors (financials, energy) — even if a finding has cross-sector implications, the brief is intra-sector.
- The watchlist / emergency / halt-mode behavioral shifts — those apply to decision-layer agents (analyst, strategist) per their respective specs. Domain researchers run uniformly across modes; the regime label is the only behavioral lever.

## Notes

The design doc's `Mandate:` opening line ("surface intra-sector conditions, anomalies, and thesis candidates for ~35 tech and semiconductor names...") is the prompt's anchor. Quote or paraphrase it in the role section so the prompt and design doc remain visibly aligned.

The "do not pad" discipline is the same one the analyst's prompt enforces — reuse the framing. A quiet day with two findings is honest; padding to five is the failure mode.

The sector-specific qualitative input renderings come in as headlines + scheduled events (per story 06's `SectorQualitativeInput`). The prompt should describe how to interpret them rather than re-listing what's in the bundle. Trust the LLM's reading of the structured slice.

Per the user memory `feedback_avoid_numeric_anchors`: the prompt should *not* set numeric targets like "produce exactly 4 findings" or "use 6.5 as a strength threshold." The advisory ranges in the design doc (3–5 findings, 0–3 anomalies, 0–3 thesis candidates) are operating ranges, not targets — frame them as such.

The example output is the most prompt-engineering-sensitive part: it sets the visual template the LLM imitates. Make it crisp; cover the diversity the parser expects (different signal types across findings, a moderate-severity anomaly so the LLM does not default to `investigate_now`, a thesis candidate with a `moderate` conviction sketch and a sentence-length justification).

The `<role>` section should also explicitly note the agent runs in parallel with the financials and energy researchers — they do not see each other's output, and the prompt must not reference them as if it did.

Use the `agent-system-prompts` skill (`Skill("agent-system-prompts")`) when drafting; it is the project skill specifically for AlphaMind agent system prompt authoring per the skill's frontmatter.

## Acceptance criteria

- [ ] `prompts/analysis/tech_semis_researcher.md` exists.
- [ ] The file has the seven canonical sections: comment header, `<role>`, `<operating_context>`, `<inputs>`, `<task>`, `<method>`, `<output_contract>`, `<example_output>`, `<constraints>` (matching the structural pattern of `prompts/decision/analyst.md`).
- [ ] The output-contract section reproduces the `tech-semis.md § Domain researcher output contract` template verbatim, including the literal `=== KEY FINDINGS ===`, `=== FLAGGED ANOMALIES ===`, `=== THESIS CANDIDATES ===` markers.
- [ ] The example-output section contains a complete, parser-valid sector brief (parses without `ParseError` against the story 04 parser; passes story 05 validation against a fixture sector membership).
- [ ] The constraints section names: no invented tickers, no trade proposals, no cross-sector tickers, no forward references, no padding, no surrounding prose.
- [ ] The reference-prefix used throughout is `SA-TECH` (not `SA-FIN` or `SA-ENERGY`).
- [ ] No numeric anchors are set as targets (per `feedback_avoid_numeric_anchors`); advisory ranges from the design doc are framed as ranges, not targets.
- [ ] A round-trip test parses the example output (extracted from the prompt file) into a `SectorBrief` and validates it cleanly.
