---
name: agent-system-prompts
description: Use when writing, editing, or reviewing system prompts for Claude agents built on the Claude Agent SDK — the agents in `prompts/analysis/` and `prompts/decision/` (sector analysts, qualitative/adaptive researchers, synthesizer, analyst, strategist, PM). Not for user-facing chatbot personas, one-shot API prompts, or end-user product copy.
---

# Writing system prompts for Claude agents

For the `.md` files passed via `ClaudeAgentOptions(system_prompt=...)`. Does **not** cover tool schemas, MCP wiring, or orchestration code — those live in the surrounding harness.

AlphaMind pipeline facts every prompt author should know: every agent in `docs/architecture/llm-integration.md §Agent inventory` has its own prompt file; prompts are stable across invocations; variable data (distillation output, briefs, portfolio state) goes in the **user turn**, never the system prompt; every agent receives the volatility regime label; source references use the typed format (`SA-TECH-3`, `QR-4`, `AR-2`, `CR-*`).

---

## 1. The job of a system prompt

A Claude Agent loop is `system prompt + tools + user message → model → (tool calls → results) → model → …`. The system prompt is the only prose layer you author; everything else is data the SDK injects around it. Its job is exactly three things:

1. **Who the agent is** and what "done" looks like.
2. **How it decides** — when to call which tool, when to stop, how to handle ambiguity.
3. **What it outputs** — structure, fields, citation style.

Everything else belongs elsewhere. Tool schemas describe tools; user turns carry data; orchestration code sequences agents. If a "rule" in the prompt changes between invocations, it's data — move it to the user turn.

> "Be thoughtful and keep your context informative, yet tight." — Anthropic, *Effective context engineering for AI agents*.

---

## 2. Composition within the context window

| Layer | Source | Varies per invocation? |
|---|---|---|
| System prompt | your `.md` file | No |
| Skill metadata (name + description only) | auto-injected by SDK | No |
| Tool schemas | `@tool` decorators / MCP | No |
| User message (task + payload) | orchestrator | Yes |
| Tool call results | runtime | Yes |
| Skill bodies | loaded only when triggered | Only when relevant |

**SDK system-prompt modes:**

- **Custom string** (AlphaMind default): replaces the built-in preset. Full control; you own tool-use hygiene, refusal shape, and response style — the SDK's defaults are gone.
- **`preset: "claude_code"` + `append`**: layers your instructions on Anthropic's coding defaults. Fits coding agents, not analytical/trading ones.

AlphaMind's agents are task-shaped, not coding-shaped — use custom strings.

---

## 3. Standard structure

Use this section order. Pick XML tags (`<role>`, `<output_contract>`) *or* markdown headers — don't mix within one prompt. Prefer XML when the prompt contains example payloads (the tags disambiguate prose from data).

```
1. Role               — one paragraph: who, what to optimize for
2. Operating context  — non-obvious facts the agent assumes
3. Inputs             — shape of the user turn
4. Task               — deliverable + success criteria
5. Method             — reasoning steps (only if non-obvious)
6. Tool policy        — when / when not / budget / null-result handling
7. Output contract    — exact structure, with one filled example
8. Constraints        — scope limits, escalation paths
9. Examples           — 1–3, only if format or reasoning is hard to describe
```

Target length: 80–150 lines for sector analysts; up to ~200 for the analyst/strategist/PM agents with rich output contracts. Longer than that is almost always a smell.

### Role

One paragraph. Concrete job title, not a persona adjective. State what to optimize for — it resolves every downstream judgment call.

> "You are the tech/semis sector analyst in a systematic trading pipeline. Optimize for thesis falsifiability and explicit uncertainty; do not pad."

Not: "You are a highly experienced financial analyst with deep expertise…"

### Operating context

Non-obvious facts the agent can act on. For AlphaMind agents typically:

- Fresh context window per invocation — no memory of prior runs.
- The user turn contains `{sector_data, anomaly_flags, regime_label}`.
- Output is read by another LLM. Be terse and structured (no greetings, no signposting prose like "In summary…").
- Cite sources as `[SA-TECH-n]`, numbered in order of first use.

Omit anything the agent can't act on ("the synthesizer reads your output" — unless that changes *how* it writes).

### Inputs

Describe the shape, not the content. "You will receive a JSON payload with fields `sector_data`, `anomaly_flags`, `regime_label`." If shape is unclear, the agent hallucinates one.

### Task

Imperative. Include what "good" looks like.

> "Produce one brief covering your coverage universe. A thesis is well-formed when it names a catalyst, direction, time window, and falsification condition."

### Method (optional)

Include only if the reasoning is non-obvious or you've observed it going wrong. Specify *which* steps:

```
For each anomaly:
  1. Classify as {earnings | macro | flow | technical | other}
  2. Check cross-sector correlation against the regime label
  3. If correlation > 0.6, flag as systemic; otherwise idiosyncratic
```

"Think step by step" alone does nothing — modern Claude already does. You're specifying the steps.

### Tool policy

For each tool:

- **When** to call it (triggering condition).
- **When not** (tools have cost — tokens, latency, side effects).
- **Budget** (max calls or max depth).
- **Null-result handling** (otherwise agents retry in a loop).

Example for one tool:

```
news_search:
- Call only for tickers distillation flagged as anomalous that cannot be
  explained from the provided data.
- Max 5 calls per invocation.
- If no hits, record under anomalies_unresolved and move on; do not retry.
```

Reference tools by name. Do not restate their parameters — the schema is authoritative.

### Output contract

The most concrete section. Specify structure, field names/types, length bounds (if any), citation format. Three levers for getting the output to actually conform, strongest first:

1. **Prefill the first token** at the SDK layer (`{` for JSON, `<brief>` for XML) — kills leading prose.
2. **One filled example** inside `<example_output>` tags — demonstrates shape and level of detail.
3. **Prose schema description** — weakest alone, strong with the example.

Pick one format per prompt. Markdown-with-named-H2s for human-debuggable outputs; JSON for programmatically parsed outputs (e.g., PM → OMS commands). Do not wrap JSON in Markdown fences — either the fences leak or the JSON escapes.

### Constraints

State negative rules only when (a) a plausible reading of the positive instructions would violate them, or (b) you've observed the violation. Every "don't" needs a "do this instead" — otherwise the agent invents its own fallback.

> "If no data supports the thesis, return `insufficient_data: true` — do not speculate."

Not: "Do not hallucinate." (Gives the agent nowhere to go.)

Include an explicit escalation path for every halt condition. Autonomous agents should never have a silent failure mode — they should emit a flag the orchestrator can see (e.g., `{status: "insufficient_data", reason: "..."}`).

### Examples

Use when the format has edge cases, the reasoning pattern is hard to elicit from prose, or you want to anchor tone. Don't use when one example would only cover a sliver of the space (risks overfitting) or the contract is already fully specified. Wrap each in `<example>` tags; for reasoning demonstrations separate `<reasoning>` from `<output>` so downstream code can strip reasoning.

---

## 4. Anti-patterns (delete on sight)

- **Biographical role.** "You are an expert with 20 years of experience…" → replace with a concrete job title.
- **Restating tool parameters in prose.** The schema is authoritative.
- **Bare "don't" rules with no "do instead."** Agents invent fallbacks.
- **"Think step by step"** with no specified steps.
- **Hardcoded data.** Dates, tickers, prices in the system prompt break the next invocation. They belong in the user turn.
- **Hedging language.** "Generally," "try to," "when possible." → either it's a rule or it isn't. Rewrite or delete.
- **Conditional on the user turn.** "If the user asks for X…" → agents don't have human users; the orchestrator controls the user turn.
- **Persona adjectives alone.** "Be concise / thorough / rigorous" → show with an example output instead.
- **"Be helpful and harmless."** Pre-tuned into the model; adds tokens and invites chatbot hedging.
- **Unobservable rules.** "Only do X on weekends" when the agent has no clock.
- **Chatbot refusals in pipeline agents.** "I cannot provide financial advice" — the whole pipeline *is* financial advice. Scope refusals to the task: refuse to fabricate when data is missing, not to participate in the domain.
- **Meta-commentary.** "As an AI language model…", "I cannot be certain, but…" → these bleed confidence into prose where a structured `confidence` field belongs.
- **JSON wrapped in Markdown fences** (or vice versa). Pick one format.
- **Rules restated in 3+ places** inside one prompt. One canonical home per rule.

---

## 5. Before fixing the prompt, check the harness

When an agent misbehaves, the prompt is often not the problem:

- **Missing context** → fix the user turn / orchestrator, don't stuff the system prompt.
- **Overlong input** → move rare material behind a retrieval tool (AlphaMind's brief-retrieval pattern in the decision layer).
- **Ambiguous tool calls** → fix the tool schema; schemas are read with the same weight as the prompt.
- **Looping or overrunning** → add a budget and return condition, not more rules.

---

## 6. Iteration

Start minimal — a first draft should be 40–60 lines and missing half the constraints you'll eventually need. Ship, observe, add only what fixes observed failures. Watch three things:

- **Trajectory:** tool calls in the intended order? Extra calls, skipped calls, or retry loops are prompt-fixable.
- **Final output:** conforms to the contract? Schema violations usually mean the example is missing or contradicts the prose.
- **Cross-agent drift:** in the layered pipeline an analyst change can degrade the synthesizer. Re-run the full analysis layer when touching a single prompt.

Keep a small evals harness (~10 recorded input/expected-behavior pairs per agent) and diff prompts like code.

---

## 7. Worked example

**Before** — plausible first draft; vague, redundant, data-leaky:

```
You are a highly experienced financial analyst with deep expertise in the
technology and semiconductor sectors. Your job is to carefully analyze the
data you are given and produce a thorough and insightful report for the
trading team. Be as detailed as possible. Today is April 23, 2026, and
NVDA is at $850. Consider all relevant factors including fundamentals,
technicals, sentiment, and macro conditions. Do not hallucinate. Do not
give financial advice. Think step by step. Use the news_search tool to
look up recent news when appropriate.
```

Defects: biographical role; hardcoded date + price (breaks next invocation); contradictory ("give a report" / "don't give financial advice"); no output contract; no tool budget or stop; bare "do not hallucinate" with no alternative.

**After:**

```
<role>
You are the tech/semis sector analyst in a systematic trading pipeline.
You produce structured briefs consumed by a synthesizer agent, not humans.
Optimize for thesis falsifiability and explicit uncertainty; do not pad.
</role>

<operating_context>
- Each invocation is a fresh context window with no memory.
- The user turn contains: {sector_data, anomaly_flags, regime_label}.
- Output is read by another LLM. Be terse and structured.
- Cite source rows as [SA-TECH-n], numbered in order of first use.
</operating_context>

<task>
Produce one brief covering your coverage universe. A brief is well-formed
when it contains: (a) the current regime read, (b) per-ticker thesis
candidates with catalyst, direction, time window, and falsification
condition, and (c) a deduplicated list of anomalies worth escalating.
</task>

<tool_policy>
- news_search: call at most 5 times, only for tickers where distillation
  flagged an anomaly that cannot be explained from the provided data.
- If news_search returns no hits, record the ticker under
  anomalies_unresolved and move on — do not retry.
- Stop after producing the brief, regardless of remaining budget.
</tool_policy>

<output_contract>
Return Markdown with these H2 sections, in order:
## Regime read
## Thesis candidates
## Anomalies escalated
## Anomalies unresolved

Each thesis candidate:
### TICKER — one-line summary
- Catalyst: ...
- Direction: {long | short | neutral}
- Window: {hours}
- Falsification: ...
- Sources: [SA-TECH-1], [SA-TECH-3]

If a section is empty, write "None." Do not omit the section.
</output_contract>

<example_output>
## Regime read
Elevated-vol regime consistent with regime_label. Semis beta > 1.5 vs.
broad tech [SA-TECH-1].

## Thesis candidates
### NVDA — data-center refresh demand durability
- Catalyst: guidance revisions at mega-cap hyperscalers [SA-TECH-2]
- Direction: long
- Window: 48h
- Falsification: any hyperscaler cuts FY capex guidance
- Sources: [SA-TECH-2], [SA-TECH-4]

## Anomalies escalated
None.

## Anomalies unresolved
- AVGO: 4× average volume at 13:30 with no news [SA-TECH-5]
</example_output>

<constraints>
- If sector_data is empty, return the four sections with "None." under
  each and set a top-level `insufficient_data: true` front-matter key.
- Never fabricate tickers, prices, or catalysts not traceable to the
  input or a news_search result.
- Do not include confidence scores in prose; they belong in structured
  fields.
</constraints>
```

---

## 8. Pre-ship checklist

- [ ] Role is a job title, not an adjective.
- [ ] No variable data (prices, tickers, dates) in the prompt.
- [ ] Every tool has a when-to-call rule, a budget, and null-result handling.
- [ ] Output contract has one filled example.
- [ ] Every "don't" has a paired "do this instead" with a concrete fallback shape.
- [ ] Stop condition is explicit.
- [ ] No tool parameters restated in prose.
- [ ] No hedging language, persona padding, chatbot refusals, or meta-commentary.
- [ ] Run against 3+ recorded inputs; trajectory and output inspected.

---

## 9. AlphaMind cross-references

- Agent inventory, model assignments, tool access: `docs/architecture/llm-integration.md`.
- Analysis layer sequencing and universal context broadcast: `docs/design/03-analysis-layer/README.md`.
- Decision layer information flow and reference-ID taxonomy: `docs/design/04-decision-layer/README.md`.
- OMS command vocabulary (PM output must emit these): `docs/design/05-execution-layer/oms-commands.md`.

If a prompt's output contract depends on one of these, reference it in a comment at the top of the prompt file.

---

## Sources

- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — tight-context principle, section structure, just-in-time retrieval.
- [Building agents with the Claude Agent SDK](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk) — system prompt + tools + harness composition.
- [Building effective agents (research)](https://www.anthropic.com/research/building-effective-agents) — start-simple; workflows vs. agents.
- [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) — tool schemas deserve prompt-engineering rigor.
- [Modifying system prompts (Agent SDK)](https://docs.claude.com/en/api/agent-sdk/modifying-system-prompts) — preset / append / custom modes.
