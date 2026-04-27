# Mid-pipeline failure handling

**Policy: no checkpoint, no resume.** When an invocation aborts partway through — any agent failure per [llm-agent-failure-handling.md](llm-agent-failure-handling.md), a data-layer abort per [api-failure-handling.md](01-data-layer/api-failure-handling.md), or an infrastructure error — the pipeline discards in-flight state and waits for the next scheduled trigger. Successfully-completed agent outputs persist as diagnostic records tagged with the aborted invocation ID and failure point, but are never replayed.

## Why no resume

Three reasons, each sufficient:

**Fresh context windows are a core design principle.** Each invocation runs on current distillation output, portfolio state, and guardrail headroom. Replaying a prior invocation's analyst brief contradicts the "no cognitive anchoring between stages" rationale in [design-decisions.md](design-decisions.md) and biases downstream reasoning against current data.

**Briefs decay fast at this horizon.** On a 4–72 hour horizon, a sector brief produced two hours ago is stale against current prices, news, and anomaly flags. Replaying it is the same "no stale fallback" violation [api-failure-handling.md](01-data-layer/api-failure-handling.md) rules out at the data layer. The next scheduled invocation regenerating from current data *is* the retry mechanism.

**The continuous monitor covers safety between invocations.** Position exposure between scheduled runs is covered by the monitor's independent breach detection and protective CLOSE authority ([05-execution-layer/architecture.md](05-execution-layer/architecture.md)). The LLM pipeline is not the safety mechanism. A clean abort is operationally equivalent to skipping a cycle, which the system already tolerates.

## Execution layer is unaffected

The OMS, execution gateway, and activity log have independent persistence guarantees. Two invariants hold regardless of pipeline outcome:

- **Collect phase is fully applied or not at all.** Fills from prior invocations are collected and committed before any LLM runs (see [two-phase invocation model](05-execution-layer/architecture.md#two-phase-invocation-model)). An abort after collect leaves fills correctly committed; an abort during collect leaves them uncommitted and they re-collect at the next invocation start.
- **Commands submitted before the abort remain committed.** The PM submits sequentially with synchronous validation. If the PM aborts after submitting N commands, those N are committed; the remainder are not. Commands are individually idempotent at the engine, so this partial-batch outcome is a normal operational state.

---

*Cross-references:*

- LLM agent failure handling: [llm-agent-failure-handling.md](llm-agent-failure-handling.md)
- API failure handling (fresh-or-abort principle): [01-data-layer/api-failure-handling.md](01-data-layer/api-failure-handling.md)
- Two-phase invocation model: [05-execution-layer/architecture.md](05-execution-layer/architecture.md)
- Fresh context windows principle: [design-decisions.md](design-decisions.md)
- Continuous monitor safety authority: [05-execution-layer/architecture.md](05-execution-layer/architecture.md)
