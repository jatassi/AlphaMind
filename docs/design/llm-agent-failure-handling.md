# LLM agent failure handling

When an LLM agent fails to produce usable output — timeout, malformed output, context overflow, model API error, or tool-use error — the pipeline needs a deterministic policy for how to proceed. This document defines that policy per agent and per failure type.

## Design principle — fail closed, not open

Mirrors [api-failure-handling.md](01-data-layer/api-failure-handling.md), and goes one step further: an invocation that produces degraded reasoning is worse than one that produces no output. Bad reasoning becomes executed positions and persists into subsequent cycles. After retries are exhausted, any agent failure aborts the invocation. There is no Important or Optional tier for LLM agents — the equivalent gradation in the API spec exists because a few data categories have specific structural reasons their absence is tolerable; no LLM agent in this pipeline meets that bar.

The continuous monitor's safety authority is independent of the LLM pipeline. Existing positions retain their bracket coverage during pipeline aborts, the monitor continues evaluating breach conditions on live quote data, and emergency invocation triggers can fire even when the most recent scheduled invocation aborted. Skipping one scheduled invocation costs 2–6 hours until the next trigger — operationally acceptable for the 4–72 hour thesis horizon.

## Failure taxonomy

Five distinct failure modes with different detection and response shapes:

**Timeout** — the agent did not return within its latency budget. Causes: model is slow, model is rate-limited, the agent is stuck in a tool-use loop (most relevant to the adaptive researcher). Detected by the pipeline process's per-agent `asyncio.wait_for` wrapper.

**Malformed output** — the response does not pass structural validation. Causes: emitted prose before or after the JSON object, dropped a required field, used an invalid enum value, invented a reference ID, returned an empty string. Detected by post-call schema validation against the published JSON Schemas (analyst, strategist) or by lightweight structural checks for agents without formal schemas.

**Context overflow** — the conversation context exceeded the model's window or the agent's per-invocation budget cap. Causes: distillation payload larger than expected, adaptive researcher's tool-use loop accumulated more output than budgeted, synthesizer's combined input briefs grew unbounded. Detected pre-call by token counting (preferred) or post-call by API error.

**Model API error** — infrastructure-level error from the model provider: 5xx, network failure, authentication issue, rate limit. Distinct from LLM behavioral failures because retry without modification is appropriate.

**Tool-use error** — a tool the agent invoked returned an error or unexpected payload. Examples: `retrieve_brief` cannot find the requested ID, `validate_guardrail` raises an unexpected exception, an adaptive research tool times out. Distinct from a tool returning a *failure result* the agent should reason about (`validate_guardrail` returning FAIL is a normal result, not a tool-use error).

## Detection

**Schema validation is the primary structural check.** Every agent with a formal output schema (analyst per [analyst-output-schema.md](04-decision-layer/analyst-output-schema.md), strategist per [strategist-output-schema.md](04-decision-layer/strategist-output-schema.md), PM per [pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md)) has its output validated against that schema immediately on receipt. The schema is authoritative — failure to validate is a structural error regardless of how plausible the content looks.

Every OMS command extracted from a PM envelope additionally validates against [oms-command-schema.md](05-execution-layer/oms-command-schema.md) before it reaches the engine. The envelope schema's `commands` array references the OMS command schema via `$ref`; both checks run at the LLM output validation seam.

Agents without a formal output schema (domain researchers, qualitative researcher, adaptive researcher, synthesizer) receive lighter checks: required sections present, reference IDs follow the typed prefix convention, no obvious truncation. **Invented reference IDs are a structural error in any agent's output** — references are the contract through which downstream agents drill into source material, and an invented reference makes the citation unverifiable.

**Token counting before model invocation** catches context overflow before the model returns an error. The pipeline process computes input token count using the model's tokenizer and refuses to dispatch the call if the input alone exceeds the model's window minus a safety margin.

**Latency budgets are per-agent and configurable**, set during deployment based on observed agent latency with a 2–3× multiplier on the typical to allow for slow runs without false timeouts.

## Recovery semantics

Retry shape per failure type:

- **Model API error** — multiple retries with exponential backoff, then abort. Same shape as Critical-tier API retries in [api-failure-handling.md](01-data-layer/api-failure-handling.md).
- **Timeout** — single retry with a doubled latency budget, then abort. Persistent timeout often indicates a deeper problem (rate limiting, model degradation, infinite tool-use loop) that more retries won't fix.
- **Malformed output** — one corrective retry within the same context (see below), then abort.
- **Context overflow** — no retry; abort. Inputs are too large; this is a structural problem to investigate, not a runtime to mitigate.
- **Tool-use error** — idempotent tools (`retrieve_brief`, `validate_guardrail`) get one retry, then abort the agent; non-idempotent tools (none currently exposed to LLM agents) are not retried.

### Schema repair (single corrective retry)

When an agent's output fails schema validation, the pipeline process constructs a corrective system message containing the validator's error list and the offending output, and re-invokes the agent **within the same conversation context**. Same-context preserves the agent's prior reasoning while making the structural failure explicit; a fresh context loses the analytical work and biases toward repeating the original framing of the inputs.

If the corrective retry also fails, the agent is treated as failed. No further retries. The pattern is "give the agent one chance to read its own mistake," not an iterative repair loop — iterative repair tends to converge on the LLM gaming the schema rather than fixing the underlying reasoning.

The schema-repair retry uses the same model. Model swapping mid-invocation is not configured and would not address the schema issue, which is structural rather than capability-bounded.

### Context overflow is a hard failure

Every agent has bounded inputs by design: domain researchers receive a fixed-shape distillation slice for their sector, the qualitative researcher receives a fixed-shape baseline payload, the adaptive researcher's tool-use loop is bounded by its 25-call / 4,000-token budget (see [adaptive-research.md](03-analysis-layer/adaptive-research.md)), the synthesizer receives the union of bounded upstream briefs, and decision agents receive the synthesizer brief plus a guardrail header.

Overflow therefore indicates either a configuration drift, an unbounded growth somewhere upstream, or a model-window assumption that no longer holds. None of these are recoverable in the next pipeline invocation — they require investigation. The pipeline process does not prune inputs, drop briefs, or interrupt tool-use loops to keep an agent within budget; any such mitigation silently produces a degraded decision that is exactly what fail-closed semantics exist to prevent.

The adaptive researcher's bounded loop is the one place where "incomplete investigation" is the agent's normal operating mode — but only when the agent itself recognizes it has used its budget and produces its synthesis. Forced interruption by the pipeline process is a failure, not a graceful exit.

## Failure response — every agent is Critical

Every LLM agent in the pipeline is treated as Critical: any unrecoverable failure aborts the invocation. This applies uniformly to the domain researchers, the qualitative researcher, the adaptive researcher, the synthesizer, the analyst, the strategist, and the portfolio manager. There is no Important or Optional tier and no peer-recovery path.

The reasoning is uniform: each agent's output is load-bearing for the next stage, and proceeding with a missing input means the downstream agent reasons over a misleading or partial picture without an explicit signal that the absence is structural. A pipeline that aborts on a single agent failure is preferable to one that produces decisions over a silently degraded view.

The analyst and strategist run in parallel and have non-overlapping mandates (new entries vs. position management). It is tempting to treat them as peer-recoverable — if one fails, let the PM proceed with the other's output. Rejected for the same reason as everywhere else: a PM that issues commands based on new entries while having no fresh assessment of existing positions (or vice versa) is making decisions on a partial portfolio view. Better to abort and re-run at the next scheduled invocation, when both agents will produce fresh outputs against fresh data.

The adaptive researcher is the closest case to a defensible exception — its absence means uninvestigated anomalies rather than missing baseline context, and the synthesizer's contradiction-flagging on the upstream briefs partially substitutes. It is still Critical. The "uninvestigated anomaly" phrasing understates the loss: the adaptive researcher exists precisely because the surface signals warrant deeper investigation, and proceeding without it means deciding on the surface signals alone in exactly the case where the system already flagged them as needing more.

The PM is Critical for a separate reason: without it, no commands are issued. Pipeline state up to that point (briefs, proposals, validation results) is persisted for diagnostic inspection but does not become committed decisions. The next invocation produces fresh briefs and fresh proposals; the failed PM's intermediate state is not replayed.

## Abort semantics

"Abort" carries the same meaning as in [api-failure-handling.md](01-data-layer/api-failure-handling.md): cancel the current pipeline invocation entirely, log the failure, wait for the next scheduled trigger. Agents that succeeded before a downstream agent failed have their outputs persisted for diagnostic purposes but are not replayed in the next invocation — fresh context windows mean each invocation regenerates its briefs from current data.

### Continuous monitor interaction

Pipeline aborts do not affect the continuous monitor. Existing positions retain their bracket coverage, breach detection on live quote data continues, and the monitor retains its protective CLOSE authority via engine-originated command envelopes (see [05-execution-layer/architecture.md](05-execution-layer/architecture.md)). The monitor's degradation conditions are upstream data dependencies (Q1 live quote feed in particular), not LLM availability.

### Emergency invocations

When the continuous monitor triggers an emergency invocation (regime jump, multi-rule breach, margin call — see [breach-behavior.md](06-risk-guardrails/breach-behavior.md)), the same fail-closed rule applies. An emergency invocation that hits any unrecoverable agent failure aborts; the monitor's mechanical response authority handles the breach via engine-originated CLOSE commands. Emergency invocations do not get more retries or relaxed validation — degraded reasoning under stress conditions is exactly the failure mode that motivates fail-closed semantics.

## Alerting

Any unrecoverable agent failure aborts the invocation and warrants immediate human notification. There is no per-agent severity gradation because all agents are Critical.

A specific high-severity condition: persistent malformed output from a single agent across consecutive invocations indicates either prompt drift (the system prompt no longer matches the schema) or model regression. Both warrant immediate investigation. The first is fixable in-prompt; the second may require a model swap.

Specific alerting infrastructure is scoped to the Phase 4 monitoring and alerting design and not defined here.

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
