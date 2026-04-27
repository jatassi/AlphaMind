# LLM agent failure handling

When an LLM agent fails to produce usable output — timeout, malformed output, context overflow, model API error, or tool-use error — the pipeline needs a deterministic policy. This doc defines that policy per agent and per failure type.

## Design principle — fail closed, not open

Mirrors [api-failure-handling.md](01-data-layer/api-failure-handling.md), one step further: an invocation producing degraded reasoning is worse than one producing no output. Bad reasoning becomes executed positions that persist into subsequent cycles. After retries exhaust, any agent failure aborts the invocation. Every LLM agent is Critical — no Important or Optional tier.

The continuous monitor's safety authority is independent of the LLM pipeline. Positions retain bracket coverage during aborts, the monitor continues evaluating breaches on live quote data, and emergency invocation triggers fire even after a recent scheduled invocation aborted. Skipping one scheduled invocation costs 2–6 hours — acceptable for the 4–72 hour thesis horizon.

## Failure taxonomy

Five failure modes, distinct detection and response:

**Timeout** — agent did not return within its latency budget. Causes: slow model, rate limiting, stuck tool-use loop (most relevant to the adaptive researcher). Detected by per-agent `asyncio.wait_for`.

**Malformed output** — response fails structural validation. Causes: prose around the JSON, dropped required field, invalid enum, invented reference ID, empty string. Detected by post-call schema validation against published JSON Schemas (analyst, strategist) or lightweight structural checks for agents without formal schemas.

**Context overflow** — context exceeded the model window or per-invocation budget cap. Causes: oversized distillation payload, adaptive researcher's loop accumulated more output than budgeted, synthesizer's combined inputs grew unbounded. Detected pre-call by token counting (preferred) or post-call by API error.

**Model API error** — infrastructure-level error from the model provider: 5xx, network failure, auth, rate limit. Distinct from LLM behavioral failures because retry without modification is appropriate.

**Tool-use error** — a tool returned an error or unexpected payload. Examples: `retrieve_brief` cannot find the ID, `validate_guardrail` raises unexpectedly, an adaptive research tool times out. Distinct from a tool returning a *failure result* the agent should reason about (`validate_guardrail` returning FAIL is a normal result).

## Detection

**Schema validation is the primary structural check.** Every agent with a formal schema (analyst per [analyst-output-schema.md](04-decision-layer/analyst-output-schema.md), strategist per [strategist-output-schema.md](04-decision-layer/strategist-output-schema.md), PM per [pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md)) has its output validated immediately on receipt. The schema is authoritative — validation failure is a structural error regardless of how plausible the content looks.

Every OMS command extracted from a PM envelope additionally validates against [oms-command-schema.md](05-execution-layer/oms-command-schema.md) before reaching the engine. The envelope schema's `commands` array references the OMS schema via `$ref`; both checks run at the LLM output validation seam.

Agents without a formal schema (domain researchers, qualitative, adaptive, synthesizer) get lighter checks: required sections present, reference IDs follow the typed prefix convention, no obvious truncation. **Invented reference IDs are a structural error in any agent's output** — references are the contract through which downstream agents drill into source material; an invented reference is unverifiable.

**Token counting before model invocation** catches context overflow before an API error. The pipeline computes input tokens with the model's tokenizer and refuses to dispatch if input exceeds the window minus a safety margin.

**Latency budgets are per-agent and configurable**, set at deployment from observed latency with a 2–3× multiplier on the typical to absorb slow runs without false timeouts.

## Recovery semantics

Retry shape per failure type:

- **Model API error** — multiple retries with exponential backoff, then abort. Same shape as Critical-tier API retries in [api-failure-handling.md](01-data-layer/api-failure-handling.md).
- **Timeout** — single retry with doubled latency budget, then abort. Persistent timeout often indicates a deeper problem (rate limiting, model degradation, infinite tool-use loop) that more retries won't fix.
- **Malformed output** — one corrective retry within the same context (below), then abort.
- **Context overflow** — no retry; abort. Inputs are too large — a structural problem to investigate.
- **Tool-use error** — idempotent tools (`retrieve_brief`, `validate_guardrail`) get one retry, then abort the agent; non-idempotent tools (none currently exposed) are not retried.

### Schema repair (single corrective retry)

When output fails schema validation, the pipeline constructs a corrective system message with the validator's error list and the offending output, and re-invokes the agent **within the same conversation context**. Same-context preserves the agent's reasoning while making the failure explicit; a fresh context loses the analytical work and biases toward repeating the original framing.

If the corrective retry also fails, the agent is failed. No further retries — iterative repair converges on the LLM gaming the schema rather than fixing the underlying reasoning.

The schema-repair retry uses the same model. Model swapping mid-invocation is not configured and would not address structural schema issues.

### Context overflow is a hard failure

Every agent has bounded inputs by design: domain researchers get a fixed distillation slice, the qualitative researcher gets a fixed baseline payload, the adaptive researcher's loop is bounded by its 25-call / 4,000-token budget ([adaptive-research.md](03-analysis-layer/adaptive-research.md)), the synthesizer gets the union of bounded upstream briefs, decision agents get the synthesis plus guardrail header.

Overflow indicates configuration drift, unbounded growth upstream, or a model-window assumption that no longer holds — investigation, not runtime mitigation. The pipeline does not prune inputs, drop briefs, or interrupt tool-use loops to fit budget; that silently produces a degraded decision.

The adaptive researcher's bounded loop is the one place where "incomplete investigation" is normal — but only when the agent recognizes the budget and produces its synthesis. Forced interruption is a failure, not a graceful exit.

## Failure response — every agent is Critical

Every LLM agent is Critical: any unrecoverable failure aborts the invocation. Applies uniformly to domain researchers, qualitative, adaptive, synthesizer, analyst, strategist, PM. No peer-recovery path.

Each agent's output is load-bearing for the next stage. Proceeding with a missing input means the downstream agent reasons over a misleading or partial picture without an explicit signal that the absence is structural.

The analyst and strategist run in parallel with non-overlapping mandates (new entries vs. position management). Tempting to treat them as peer-recoverable — let the PM proceed with whichever survives. Rejected: a PM issuing commands on new entries with no fresh assessment of existing positions (or vice versa) decides on a partial portfolio view.

The adaptive researcher is the closest defensible exception — its absence means uninvestigated anomalies rather than missing baseline context. Still Critical: the agent exists *because* surface signals warrant deeper investigation, and skipping it means deciding on surface signals alone in exactly the case where the system flagged them as needing more.

The PM is Critical for a separate reason: without it, no commands issue. Upstream state persists for diagnostic inspection but does not become committed decisions.

## Abort semantics

"Abort" carries the same meaning as in [api-failure-handling.md](01-data-layer/api-failure-handling.md): cancel the invocation, log the failure, wait for the next trigger. Outputs from agents that succeeded before a downstream failure persist for diagnostics but are not replayed — fresh context windows regenerate briefs from current data.

### Continuous monitor interaction

Pipeline aborts do not affect the continuous monitor. Bracket coverage, breach detection on live quote data, and the monitor's protective CLOSE authority via engine-originated command envelopes ([05-execution-layer/architecture.md](05-execution-layer/architecture.md)) all continue. The monitor's degradation conditions are upstream data dependencies (Q1 live quote feed especially), not LLM availability.

### Emergency invocations

When the monitor triggers an emergency invocation (regime jump, multi-rule breach, margin call — [breach-behavior.md](06-risk-guardrails/breach-behavior.md)), fail-closed still applies. An emergency invocation hitting any unrecoverable agent failure aborts; the monitor's mechanical response authority handles the breach via engine-originated CLOSE commands. No extra retries or relaxed validation under stress.

## Alerting

Any unrecoverable agent failure aborts and warrants immediate notification.

High-severity condition: persistent malformed output from one agent across consecutive invocations indicates prompt drift (system prompt no longer matches schema) or model regression. The first is fixable in-prompt; the second may require a model swap.

Alerting infrastructure is scoped to the Phase 4 monitoring and alerting design.

---

*Cross-references:*

- API failure handling (sibling pattern): [01-data-layer/api-failure-handling.md](01-data-layer/api-failure-handling.md)
- LLM output validation mechanism (detection, parsing, corrective-retry construction): [testing/llm-output-validation.md](testing/llm-output-validation.md)
- LLM integration architecture and agent inventory: [../architecture/llm-integration.md](../architecture/llm-integration.md)
- Analyst output schema: [04-decision-layer/analyst-output-schema.md](04-decision-layer/analyst-output-schema.md)
- Strategist output schema: [04-decision-layer/strategist-output-schema.md](04-decision-layer/strategist-output-schema.md)
- PM command envelope contract: [04-decision-layer/portfolio-manager.md](04-decision-layer/portfolio-manager.md)
- Adaptive research budget: [03-analysis-layer/adaptive-research.md](03-analysis-layer/adaptive-research.md)
- Continuous monitor: [05-execution-layer/architecture.md](05-execution-layer/architecture.md)
- Emergency invocations: [06-risk-guardrails/breach-behavior.md](06-risk-guardrails/breach-behavior.md)
