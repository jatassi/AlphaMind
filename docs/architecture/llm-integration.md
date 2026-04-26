# LLM integration

Claude Agent SDK, authenticated via Claude Max OAuth token.

---

## Decision

All LLM agent invocations use the **Claude Agent SDK for Python**, authenticated with a **Claude Max 5x subscription OAuth token** (`CLAUDE_CODE_OAUTH_TOKEN`). This provides programmatic agent execution within the pipeline process — async, with per-agent model selection, budget caps, custom MCP tools, and system prompt control — billed against the flat-rate Max subscription.

The subscription is owned and used by a single operator on operator-owned hardware; this is the personal-use posture that Anthropic's [Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance) accommodates with the line *"Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."* The third-party-developer prohibitions on the same page (rerouting subscription credentials on behalf of other users) do not apply.

API-key billing on Anthropic's Commercial Terms is the escape hatch if Max becomes untenable (cap exhaustion, technical-enforcement rejection, or an Anthropic policy change). The SDK switches credentials by environment variable; no code change is required. See [design/cost-and-rate-limit-modeling.md § API-key escape hatch](../design/cost-and-rate-limit-modeling.md#api-key-escape-hatch) for the full posture.

---

## Rationale

### Why the Agent SDK over the raw Anthropic API

The Agent SDK provides the orchestration primitives AlphaMind needs without building them from scratch:

- **Built-in tool-use loops.** The adaptive research agent and decision layer agents use agentic tool-use cycles (generate question → call tool → reason about result → repeat). The SDK manages this loop natively. With the raw API, you'd write your own tool dispatch + conversation management (~200-300 lines of boilerplate per agentic pattern).
- **MCP tool integration.** Custom tools are defined as Python functions with decorators and registered as MCP servers. The retrieval tool (fetch analysis briefs by reference ID), research tools (news API, prediction market queries), and data tools (on-demand quantitative pulls) are all just Python functions wired into the SDK.
- **Per-agent configuration.** Each agent gets its own `ClaudeAgentOptions` with model, system prompt, budget cap, and allowed tools — the exact per-agent control surface AlphaMind needs.
- **Async execution.** The SDK is fully async, enabling parallel sector analyst execution via `asyncio.gather`.

### Why Claude Max over pay-as-you-go API billing

**Cost structure is fundamentally different.** AlphaMind runs nine LLM agents across ~32 pipeline invocations per week. The Max 5x subscription at $100/month covers this workload with comfortable headroom in normal weeks, narrowing toward the weekly Opus cap in stress weeks (see [design/cost-and-rate-limit-modeling.md](../design/cost-and-rate-limit-modeling.md) for the full headroom analysis and the operating posture under cap pressure). Pay-as-you-go API billing for the same workload, with prompt caching enabled on stable system prompts and the synthesizer brief, lands in the $150–450/month range — comparable to the subscription, but with no cap to manage.

The Max subscription also removes cost-per-token pressure from model selection. Using Opus for every agent is "free" relative to Haiku — the choice becomes purely about quality and speed, not cost. This is significant for a system where the quality of reasoning directly determines P/L.

**Authentication:** Generate an OAuth token via `claude setup-token`, set `CLAUDE_CODE_OAUTH_TOKEN` in the environment. No API key needed.

### What the Agent SDK doesn't provide

- **No temperature/sampling control.** The SDK does not expose temperature, top_p, or top_k. Claude's default sampling behavior is used for all agents. In practice this is acceptable — the default behavior produces good analytical output — but it means you can't tune for more creative (adaptive research) vs. more deterministic (PM commands) agents.
- **No raw token counting per call.** Token usage is available in response metadata but not as fine-grained as the raw API's `usage` object. Cost tracking per agent is approximate rather than exact (less important with flat-rate billing).
- **Abstraction over conversation internals.** The SDK manages the tool-use loop internally. Detailed logging of every intermediate tool call requires working within the SDK's callback/streaming structure.

---

## Agent inventory

The pipeline invokes these LLM agents per invocation, each with independent configuration:

| Agent | Parallel group | Model (initial) | Tool access | Notes |
|-------|---------------|-----------------|-------------|-------|
| Tech/semis analyst | Analysis (parallel) | Sonnet | None (pure analysis) | Reads distilled sector data, produces structured brief |
| Financials analyst | Analysis (parallel) | Sonnet | None | Same pattern, financials sector |
| Energy analyst | Analysis (parallel) | Sonnet | None | Same pattern, energy sector |
| Portfolio state analyst | Analysis (parallel) | Sonnet | None | Reads internal distillation output |
| Qualitative researcher | Analysis (parallel) | Sonnet | None (baseline is pre-fetched) | Reads pre-collected qualitative data |
| Adaptive researcher | Analysis (sequential) | Sonnet | Research tools (news, APIs, data pulls) | Agentic loop: triage anomalies → generate questions → investigate |
| Synthesizer | Analysis (sequential) | Sonnet | None | Reads all briefs, produces unified snapshot |
| Trader agent | Decision (sequential) | Opus | Brief retrieval tool | Reads synthesis, drills into source briefs on demand |
| PM agent | Decision (sequential) | Opus | Brief retrieval tool, OMS command tool | Evaluates trader proposals, issues execution commands |

**Model rationale:** Sector analysts, qualitative researcher, and synthesizer do structured analytical work where Sonnet's capability is sufficient. The trader and PM agents make high-stakes judgment calls (trade selection, risk evaluation, position sizing) where Opus's stronger reasoning justifies the slower speed. With flat-rate billing, the cost difference is zero — the choice is purely about quality and latency.

**Model flexibility:** These assignments are starting points. If Sonnet proves insufficient for synthesis quality or Opus is too slow for the trader agent, models can be swapped per-agent without code changes — it's a configuration parameter in `ClaudeAgentOptions`.

---

## Orchestration pattern

### Pipeline execution flow

```python
async def run_analysis_layer(distillation_output, portfolio_state):
    # Parallel group: sector analysts + portfolio + qualitative
    sector_briefs, portfolio_brief, qual_brief = await asyncio.gather(
        run_agent("tech_semis_analyst", distillation_output.tech_sector),
        run_agent("financials_analyst", distillation_output.financials_sector),
        run_agent("energy_analyst", distillation_output.energy_sector),
        run_agent("portfolio_analyst", portfolio_state),
        run_agent("qualitative_researcher", distillation_output.qualitative),
    )

    # Sequential: adaptive research (reads anomaly flags from analysts)
    adaptive_briefs = await run_agent("adaptive_researcher",
        anomaly_flags=collect_anomalies(sector_briefs, distillation_output))

    # Sequential: synthesizer (reads all briefs)
    synthesis = await run_agent("synthesizer",
        all_briefs=[*sector_briefs, portfolio_brief, qual_brief, *adaptive_briefs])

    return synthesis

async def run_decision_layer(synthesis):
    # Sequential: trader then PM
    trade_recommendations = await run_agent("trader", synthesis)
    pm_commands = await run_agent("pm", synthesis, trade_recommendations)
    return pm_commands
```

### Tool registration

Custom tools are Python functions registered as MCP servers:

```python
# Brief retrieval tool — used by trader and PM agents
@tool("retrieve_brief", "Retrieve analysis brief by reference ID",
      {"ref_id": str})
async def retrieve_brief(args):
    content = db.query_brief(current_invocation_id, args["ref_id"])
    return {"content": [{"type": "text", "text": content}]}

# Research tools — used by adaptive research agent
@tool("news_search", "Search recent news for a ticker or topic",
      {"query": str, "ticker": str})
async def news_search(args):
    results = await news_api.search(args["query"], args["ticker"])
    return {"content": [{"type": "text", "text": format_results(results)}]}
```

### Fresh context windows

Each `query()` call in the Agent SDK creates a new session by default — fresh context window with no carryover from previous agents. This is exactly the design spec's requirement: each agent sees only what's explicitly passed to it, preventing cognitive anchoring between stages.

---

## Prompt management

System prompts are loaded from files, one per agent role:

```
prompts/
  analysis/
    tech_semis_analyst.md
    financials_analyst.md
    energy_analyst.md
    portfolio_analyst.md
    qualitative_researcher.md
    adaptive_researcher.md
    synthesizer.md
  decision/
    trader.md
    pm.md
```

Each prompt file contains the agent's role definition, output format specification, and any few-shot examples. The pipeline loads the prompt file and passes it via `ClaudeAgentOptions(system_prompt=...)`.

Data payloads (distillation output, briefs, portfolio state) are injected into the user message, not the system prompt. This keeps the system prompt stable across invocations while the data varies.
