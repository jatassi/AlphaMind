---
status: not_started
completed_date:
commit_id:
---

# 09 — System prompt

## Goal

Author the synthesizer's system prompt at `prompts/analysis/synthesizer.md` — the role definition, output expectations, anti-pattern guardrails, and tool-usage discipline the harness (story 08) loads via `ClaudeAgentOptions(system_prompt=...)`. The prompt operationalizes the design contract in [`synthesizer.md`](../../../design/03-analysis-layer/synthesizer.md): find intersections and contradictions across upstream briefs, cite every claim with `[<prefix>-<index>]` references, surface contradictions rather than resolving them, query portfolio tools only when market signals are portfolio-relevant.

Use the `agent-system-prompts` skill (`Skill("agent-system-prompts")`) to drive authoring — it encodes the AlphaMind agent prompt conventions (anti-pattern catalog, role/method/output structure, behavioral-failure-mode language).

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` — full design (purpose, inputs, source-reference mechanism, contradiction handling, output, retrieval-store side-effect, portfolio-state tools)
- `docs/design/03-analysis-layer/synthesizer.md` § Purpose — value-add framing (intersections / contradictions / portfolio cross-references) and the five worked examples
- `docs/design/03-analysis-layer/synthesizer.md` § Contradiction and uncertainty handling — the explicit "do NOT resolve, surface" contract
- `docs/design/03-analysis-layer/synthesizer.md` § Source reference mechanism — the citation contract
- `docs/design/04-decision-layer/analyst.md` and `docs/design/04-decision-layer/strategist.md` — the downstream consumers whose workflows the prompt must serve
- `prompts/decision/analyst.md`, `prompts/decision/strategist.md`, `prompts/decision/pm.md` — the existing AlphaMind agent prompt style (Role / Method / Constraints / Output contract structure, anti-pattern naming, mirror-image discipline language)
- `docs/implementation/03-analysis-layer/synthesizer/04-reference-marker-extraction.md` — the citation format the prompt must instruct on
- `docs/implementation/03-analysis-layer/synthesizer/06b-portfolio-state-mcp-tools.md` — tool names, return shapes, and the "on quiet days, may not be called" framing the prompt must reinforce
- `docs/implementation/03-analysis-layer/synthesizer/07-input-bundle-assembler.md` — the user-message format the LLM will receive

## Depends on

- 04 (`ReferencePrefix` / `parse_reference_id` — the prompt cites the prefix taxonomy)
- 06b (the three tool names the prompt instructs the LLM to call)
- 07 (the user-message bundle shape the prompt frames the LLM's reading order against)

## Scope

In scope:

- Author `prompts/analysis/synthesizer.md` using the AlphaMind agent prompt structure (Role, Inputs, Method, Constraints, Output contract, Worked example). Use the `agent-system-prompts` skill to drive the work.

- The prompt MUST address every dimension below (each backed by a section / subsection of the file), with concrete language operationalizing the design:

  1. **Role.** "Connect signals across the six upstream briefs into a unified picture for the decision layer. You do not propose trades; you do not classify thesis health; you surface where independent vantage points converge, contradict, or expose hidden uncertainty. The decision-layer agents read your output as their primary market context."

  2. **Inputs.** Names the six brief sources (with their reference prefixes), the volatility regime label, and the three portfolio-state tools. Cross-references the user-message structure (story 07) so the LLM knows what to expect at the top of its prompt.

  3. **Method.** Step-by-step reasoning discipline:
     - §1 Read the regime label first as universal context — it changes how every other signal weights.
     - §2 Read each brief in source order; build a working mental model of what each upstream is saying.
     - §3 Look for intersections (different briefs pointing to the same name / signal / theme — convergence increases confidence) and contradictions (different briefs pointing opposite directions — a high-information-density region the decision layer should focus on).
     - §4 For each intersection or contradiction, cite the contributing references. Every claim that traces to an upstream brief MUST carry the corresponding `[<prefix>-<index>]` citation. Multiple citations per claim when convergence is multi-source.
     - §5 When market signals plausibly relate to existing positions, call the portfolio tools. Use them as a cross-reference check, not as a thesis input. On quiet days where no upstream finding is portfolio-relevant, do not call them.
     - §6 Surface contradictions explicitly — state both positions, cite their sources, note the implications of each being correct. Do NOT pick a side; that is the decision layer's job.
     - §7 Surface uncertainty explicitly — when a finding has insufficient corroboration or ambiguous signals, flag it as uncertain rather than omitting it or asserting confidence.
     - §8 Note when a brief is `signal_quality: DEGRADED` (the upstream renders this in its body); the synthesis weights its findings lower and flags the degradation in the output.

  4. **Constraints (anti-patterns + behavioral failure modes).** Each named with a string the feedback loop aggregates on:
     - `forced_resolution` — choosing a side when sources contradict. The contradiction itself is the signal.
     - `uncited_claim` — making a claim that traces to an upstream brief without citing the corresponding reference.
     - `invented_reference` — citing a reference ID that is not present in any upstream brief. Caught downstream by Layer 3, but a self-discipline reminder here reduces drift.
     - `narrative_padding` — adding paragraphs that restate upstream content without surfacing intersections or contradictions. Synthesis is connection, not summary.
     - `thesis_generation` — proposing trade ideas, sizing, or position changes. The decision layer owns those.
     - `thesis_status_classification` — assessing whether a held thesis is on-track / at-risk / invalidated. The strategist owns that.
     - `tool_call_for_no_reason` — calling portfolio tools when no upstream signal is portfolio-relevant. The tools cost tokens and inject portfolio context the synthesizer should reach for only when the synthesis demands it.
     - `confidence_inflation` — presenting a low-corroboration finding as high-confidence. Surface the corroboration density honestly.
     - `summary_at_end` — closing the synthesis with a generic "in summary..." paragraph. The synthesis is the document; a closing summary recapitulates without adding signal.

  5. **Output contract.**
     - Format: prose synthesis. No JSON, no schema, no producer-side reference IDs.
     - Length: as much as the upstream content warrants — quiet days are short (a paragraph or two), volatile days are longer (multi-paragraph with explicit contradiction sections). The output token budget is enforced by the harness; the prompt's role is to set quality expectations, not numeric targets.
     - Citation format: `[SA-TECH-3]`, `[QR-4]`, `[AR-2]`, `[CR-1]`, `[QR-CW-1]`, `[SA-TECH-ANOM-2]`, `[SA-FIN-TC-1]`. Always brackets, always exact prefix-and-index from the upstream.
     - Structure: free-form prose. The LLM organizes by what best serves the synthesis (e.g., contradictions front-and-center on volatile days; cross-asset framing first on regime-transition days; portfolio cross-references when relevant; narrative threads weaving across sources).
     - Worked example included in the prompt itself.

  6. **Worked example.** A short representative synthesis (3–5 paragraphs) demonstrating intersection + contradiction + portfolio cross-reference patterns with realistic citation density. Per the existing AlphaMind agent prompts, the example is the operational anchor that disambiguates the abstract guidance.

- Round-trip discipline: the prompt's worked example MUST contain at least three different reference-prefix families so a future cross-tree validator can confirm the prompt's example is internally consistent. The example does NOT need to round-trip through any parser (the synthesizer has none), but the example IDs must be syntactically well-formed per `ReferencePrefix` (story 03).

- Unit tests under `tests/analysis/synthesizer/`:
  - The prompt file exists at `prompts/analysis/synthesizer.md`.
  - The prompt file is non-empty and contains the documented section headings (Role, Inputs, Method, Constraints, Output contract, Worked example).
  - The prompt names every anti-pattern string above (greppable as quoted-or-backticked literals so the feedback loop can scan for them).
  - Every reference-ID literal in the worked example resolves via `parse_reference_id` to a known prefix (regression check that the example does not drift to malformed citations).
  - The prompt loads cleanly through the harness's system-prompt loader (story 08) — no encoding artifacts, no broken Markdown that would confuse a downstream renderer.
  - The prompt names all three portfolio tools (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`).

Out of scope:
- The harness that loads the prompt (story 08).
- The runner that wires the prompt path (story 10).
- LLM-quality evaluation of the prompt (deferred to the Phase 4 feedback loop, which uses the `forced_resolution` / `uncited_claim` / etc. anti-pattern strings as aggregation labels).
- Few-shot calibration examples beyond the single worked example — additional examples can be added later as the feedback loop surfaces drift in specific failure modes.

## Notes

The `agent-system-prompts` skill exists specifically for AlphaMind agent prompt authoring; per its frontmatter it covers the tag conventions, anti-pattern catalog, and the round-trip-with-parser discipline these prompts require. Use it.

Per [`feedback_avoid_numeric_anchors.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_avoid_numeric_anchors.md), the prompt MUST NOT include numeric anchors like "find at least 3 intersections" or "no more than 5 contradictions" or "synthesis should be 500-1000 tokens." LLMs treat these as targets and produce padding to hit them. The harness's `output_token_budget` (story 02) is the only numeric cap, and it sits at the SDK layer, not in the prompt.

Per [`feedback_no_decision_trails.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_no_decision_trails.md), state contracts positively. "Cite every claim that traces to an upstream brief" is the positive form; "Don't make uncited claims" is the negative. Use both judiciously — the anti-pattern names (`uncited_claim`) need to appear so the feedback loop can aggregate, but the surrounding prose should default to positive framing.

The mirror-image discipline pattern from the strategist and PM prompts ("Every status rationale cites at least one current-cycle signal — the synthesizer flagged a contradiction and you omitted it is the failure mode this discipline catches") translates here as: "Every claim cites the source — leaving citations off is the way `uncited_claim` slips into the synthesis."

The synthesizer's role boundary against the strategist and analyst is critical and easy to violate. The prompt must explicitly decline trade generation (`thesis_generation` anti-pattern) and thesis-status classification (`thesis_status_classification` anti-pattern). A synthesizer that drifts into "this looks like a long opportunity" is reaching past its mandate; the analyst is the one who proposes trades from the synthesis.

The portfolio-tool framing matters. The tools are deliberately scoped narrow per story 06b — no P/L, no thesis components, no system health. The prompt must teach the LLM to call them as a cross-reference check ("multiple briefs flag tech momentum and the portfolio is already 22% tech") rather than as a P/L-aware reasoning aid. The `tool_call_for_no_reason` anti-pattern catches the failure where the LLM reflexively pulls portfolio context that is not relevant to the synthesis.

The "no closing summary" guidance (`summary_at_end` anti-pattern) is a prose-style discipline more than a structural one. Synthesis briefs that close with "In summary, the picture today shows..." read as recapitulation. The synthesis IS the document; a closing summary fragments attention.

Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md), do NOT produce a multi-thousand-word prompt. The existing agent prompts (`prompts/decision/analyst.md` etc.) are concise; this should match. Each anti-pattern gets one line; each method step gets one paragraph; the worked example is short.

Per CLAUDE.md, alert before disabling lint rules. Markdown linting (if any is active) should pass cleanly; the prompt is plain prose with code-fence-bracketed reference-ID examples.

## Acceptance criteria

- [ ] `prompts/analysis/synthesizer.md` exists with the documented section headings (Role, Inputs, Method, Constraints, Output contract, Worked example).
- [ ] Every named anti-pattern string (`forced_resolution`, `uncited_claim`, `invented_reference`, `narrative_padding`, `thesis_generation`, `thesis_status_classification`, `tool_call_for_no_reason`, `confidence_inflation`, `summary_at_end`) appears in the Constraints section as a greppable literal.
- [ ] All three portfolio-tool names (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) appear in the prompt.
- [ ] The Method section operationalizes the design's contradiction-and-uncertainty handling contract (state both positions, do not resolve).
- [ ] The Output contract specifies citation format with bracket-and-prefix-and-index examples.
- [ ] The worked example contains at least three different reference-prefix families and every reference ID resolves via `parse_reference_id`.
- [ ] No numeric anchors of the form "at least N", "no more than N", or token counts in the Method or Output contract sections.
- [ ] No mention of trade proposal, position sizing, thesis-status classification — those are explicitly named as anti-patterns.
- [ ] The prompt loads cleanly through the harness system-prompt loader (file readable, no encoding artifacts).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass (the only Python touched is the test file under `tests/analysis/synthesizer/`).
