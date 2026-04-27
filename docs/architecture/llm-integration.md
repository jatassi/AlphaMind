# LLM integration

Claude Agent SDK, authenticated via Claude Max OAuth token.

---

## Decision

All LLM agent invocations use the **Claude Agent SDK for Python**, authenticated with a **Claude Max 5x subscription OAuth token** (`CLAUDE_CODE_OAUTH_TOKEN`). This provides async, programmatic agent execution within the pipeline process with per-agent model selection, budget caps, custom MCP tools, and system prompt control — billed against the flat-rate Max subscription.

The subscription is owned and used by a single operator on operator-owned hardware — the personal-use posture that Anthropic's [Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance) accommodates with *"Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."* The third-party-developer prohibitions on the same page (rerouting subscription credentials on behalf of other users) do not apply.

API-key billing on Anthropic's Commercial Terms is the escape hatch if Max becomes untenable (cap exhaustion, technical-enforcement rejection, Anthropic policy change). The SDK switches credentials by environment variable; no code change required. See [design/cost-and-rate-limit-modeling.md § API-key escape hatch](../design/cost-and-rate-limit-modeling.md#api-key-escape-hatch).

---

## Rationale

### Why the Agent SDK over the raw Anthropic API

The Agent SDK provides the orchestration primitives AlphaMind needs without building them from scratch:

- **Built-in tool-use loops.** The adaptive research agent and decision layer agents use agentic tool-use cycles (generate question → call tool → reason about result → repeat). The SDK manages this loop natively; the raw API would require ~200-300 lines of tool dispatch + conversation management per agentic pattern.
- **MCP tool integration.** Custom tools are Python functions with decorators registered as MCP servers. The retrieval tool, research tools (news API, prediction market queries), and data tools (on-demand quantitative pulls) are wired into the SDK directly.
- **Per-agent configuration.** Each agent gets its own `ClaudeAgentOptions` with model, system prompt, budget cap, and allowed tools.
- **Async execution.** Fully async, enabling parallel sector analyst execution via `asyncio.gather`.

### Why Claude Max over pay-as-you-go API billing

AlphaMind runs nine LLM agents across ~32 pipeline invocations per week. The Max 5x subscription at $100/month covers this with comfortable headroom in normal weeks, narrowing toward the weekly Opus cap in stress weeks (see [design/cost-and-rate-limit-modeling.md](../design/cost-and-rate-limit-modeling.md)). Pay-as-you-go API billing with prompt caching on stable system prompts and the synthesizer brief lands in $150–450/month — comparable, but with no cap to manage.

Max also removes cost-per-token pressure from model selection. Opus everywhere is "free" relative to Haiku — model choice becomes purely about quality and speed, significant for a system where reasoning quality directly determines P/L.

**Authentication:** Generate an OAuth token via `claude setup-token`, set `CLAUDE_CODE_OAUTH_TOKEN` in the environment. No API key needed.

### What the Agent SDK doesn't provide

- **No temperature/sampling control.** Temperature, top_p, top_k are not exposed; Claude's default sampling is used for all agents. Acceptable in practice, but rules out tuning for creative (adaptive research) vs. deterministic (PM commands) agents.
- **No raw token counting per call.** Token usage is in response metadata but less fine-grained than the raw API's `usage` object. Per-agent cost tracking is approximate (less important under flat-rate billing).
- **Abstraction over conversation internals.** The SDK manages the tool-use loop internally; detailed logging of intermediate tool calls requires working within the SDK's callback/streaming structure.

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

**Model rationale:** Sector analysts, qualitative researcher, and synthesizer do structured analytical work where Sonnet suffices. The trader and PM agents make high-stakes judgment calls (trade selection, risk evaluation, position sizing) where Opus's stronger reasoning justifies the slower speed. Under flat-rate billing the cost difference is zero — the choice is purely quality and latency.

**Model flexibility:** Starting assignments. Models swap per-agent without code changes via the `ClaudeAgentOptions` configuration parameter.

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

Each `query()` call in the Agent SDK creates a new session by default — fresh context window with no carryover. Each agent sees only what's explicitly passed, preventing cognitive anchoring between stages.

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

Data payloads (distillation output, briefs, portfolio state) go into the user message, keeping the system prompt stable across invocations while data varies.
