# LLM integration

Claude Agent SDK, authenticated via Claude Max OAuth token.

---

## Decision

All LLM agent invocations use the **Claude Agent SDK for Python**, authenticated with a **Claude Max 5x subscription OAuth token** (`CLAUDE_CODE_OAUTH_TOKEN`) — async, programmatic execution with per-agent model selection, budget caps, custom MCP tools, and system prompt control, billed against the flat-rate Max subscription.

A single operator on operator-owned hardware — the personal-use posture Anthropic's [Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance) accommodates: *"Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."*

API-key billing on Anthropic's Commercial Terms is the escape hatch if Max becomes untenable (cap exhaustion, technical-enforcement rejection, policy change); the SDK switches credentials by environment variable. See [design/cost-and-rate-limit-modeling.md § API-key escape hatch](../design/cost-and-rate-limit-modeling.md#api-key-escape-hatch).

---

## Rationale

### Why the Agent SDK over the raw Anthropic API

- **Built-in tool-use loops.** The adaptive researcher and decision layer agents use agentic cycles (generate question → call tool → reason about result → repeat). The raw API would require ~200-300 lines of tool dispatch + conversation management per pattern.
- **MCP tool integration.** Custom tools are Python functions with decorators registered as MCP servers — retrieval tool, research tools (news API, prediction market queries), and data tools (on-demand quantitative pulls).
- **Per-agent configuration.** Each agent gets its own `ClaudeAgentOptions` with model, system prompt, budget cap, and allowed tools.
- **Async execution.** Parallel sector analyst execution via `asyncio.gather`.

### Why Claude Max over pay-as-you-go API billing

Nine LLM agents across ~32 invocations per week. Max 5x at $100/month covers this with comfortable headroom in normal weeks, narrowing toward the weekly Opus cap in stress weeks (see [design/cost-and-rate-limit-modeling.md](../design/cost-and-rate-limit-modeling.md)). API billing with prompt caching lands in $150-450/month — comparable, but with no cap to manage.

Max also removes cost-per-token pressure from model selection. Opus is "free" relative to Haiku — model choice becomes purely quality and speed, significant where reasoning quality directly determines P/L.

**Authentication:** Generate an OAuth token via `claude setup-token`, set `CLAUDE_CODE_OAUTH_TOKEN` in the environment.

### What the Agent SDK doesn't provide

- **No temperature/sampling control.** Temperature, top_p, top_k aren't exposed; Claude's default sampling applies to all agents. Rules out tuning for creative (adaptive research) vs. deterministic (PM commands) agents.
- **No raw token counting per call.** Token usage is in response metadata but less fine-grained than the raw API's `usage` object. Per-agent cost tracking is approximate (less important under flat-rate billing).
- **Abstraction over conversation internals.** Detailed logging of intermediate tool calls requires working within the SDK's callback/streaming structure.

---

## Agent inventory

Each invocation runs these agents with independent configuration:

| Agent | Parallel group | Model (initial) | Tool access | Notes |
|-------|---------------|-----------------|-------------|-------|
| Tech/semis analyst | Analysis (parallel) | Sonnet | None | Reads distilled sector data, produces structured brief |
| Financials analyst | Analysis (parallel) | Sonnet | None | Financials sector |
| Energy analyst | Analysis (parallel) | Sonnet | None | Energy sector |
| Qualitative researcher | Analysis (parallel) | Sonnet | None | Reads pre-collected qualitative data |
| Adaptive researcher | Analysis (sequential) | Sonnet | Research tools (news, APIs, data pulls) | Agentic loop: triage anomalies → generate questions → investigate |
| Synthesizer | Analysis (sequential) | Sonnet | None | Reads all briefs, produces unified snapshot |
| Trader agent | Decision (sequential) | Opus | Brief retrieval | Reads synthesis, drills into source briefs on demand |
| PM agent | Decision (sequential) | Opus | Brief retrieval, OMS command | Evaluates trader proposals, issues execution commands |

**Model rationale:** Sector analysts, qualitative researcher, and synthesizer do structured analytical work where Sonnet suffices. Trader and PM make high-stakes judgment calls (trade selection, risk evaluation, position sizing) where Opus's stronger reasoning justifies the slower speed. Models swap per-agent via `ClaudeAgentOptions` without code changes.

---

## Orchestration pattern

### Pipeline execution flow

```python
async def run_analysis_layer(distillation_output):
    # Parallel group: sector analysts + qualitative
    sector_briefs, qual_brief = await asyncio.gather(
        run_agent("tech_semis_analyst", distillation_output.tech_sector),
        run_agent("financials_analyst", distillation_output.financials_sector),
        run_agent("energy_analyst", distillation_output.energy_sector),
        run_agent("qualitative_researcher", distillation_output.qualitative),
    )

    # Sequential: adaptive research (reads anomaly flags from analysts)
    adaptive_briefs = await run_agent("adaptive_researcher",
        anomaly_flags=collect_anomalies(sector_briefs, distillation_output))

    # Sequential: synthesizer (reads all briefs)
    synthesis = await run_agent("synthesizer",
        all_briefs=[*sector_briefs, qual_brief, *adaptive_briefs])

    return synthesis

async def run_decision_layer(synthesis):
    # Sequential: trader then PM
    trade_recommendations = await run_agent("trader", synthesis)
    pm_commands = await run_agent("pm", synthesis, trade_recommendations)
    return pm_commands
```

### Tool registration

Tools are Python functions registered as MCP servers:

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

Each `query()` call creates a new session by default — fresh context window with no carryover. Each agent sees only what's explicitly passed, preventing cognitive anchoring between stages.

---

## Prompt management

System prompts load from files, one per agent role:

```
prompts/
  analysis/
    tech_semis_analyst.md
    financials_analyst.md
    energy_analyst.md
    qualitative_researcher.md
    adaptive_researcher.md
    synthesizer.md
  decision/
    trader.md
    pm.md
```

Each file contains the agent's role definition, output format, and any few-shot examples, passed via `ClaudeAgentOptions(system_prompt=...)`. Data payloads (distillation output, briefs, portfolio state) go into the user message — system prompt stable across invocations while data varies.
