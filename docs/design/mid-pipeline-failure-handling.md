# Mid-pipeline failure handling

**Policy: no checkpoint, no resume.** When a pipeline invocation aborts partway through — any agent failure per [llm-agent-failure-handling.md](llm-agent-failure-handling.md), a data-layer abort per [api-failure-handling.md](01-data-layer/api-failure-handling.md), or an infrastructure error — the pipeline process discards the in-flight invocation state and waits for the next scheduled trigger. Partial agent outputs that completed successfully are persisted as diagnostic records (tagged with the aborted invocation ID and the failure point) but are never replayed in a later invocation.

## Why no resume

Three reasons, each sufficient:

**Fresh context windows are a core design principle.** Each invocation runs on a fresh view of the world: current distillation output, current portfolio state, current guardrail headroom. Replaying a prior invocation's analyst brief into a later invocation contradicts the "no cognitive anchoring between stages" rationale established in [design-decisions.md](design-decisions.md) and biases downstream reasoning against current data.

**Briefs decay fast at this horizon.** On a 4–72 hour decision horizon, a sector brief produced two hours ago is already stale against current prices, news, and anomaly flags. Replaying it is the same "no stale fallback" violation that [api-failure-handling.md](01-data-layer/api-failure-handling.md) rules out at the data layer. The next scheduled invocation regenerating upstream briefs from current data *is* the retry mechanism — no separate resume path is needed or wanted.

**The continuous monitor covers safety between invocations.** The concern a resume mechanism might address — "existing positions are exposed while we wait for the next scheduled run" — is already covered by the continuous monitor's independent guardrail breach detection and protective CLOSE authority (see [05-execution-layer/architecture.md](05-execution-layer/architecture.md)). The LLM pipeline is not the safety mechanism; the monitor is. A pipeline that aborts cleanly and waits is operationally equivalent to a pipeline that skips a scheduled cycle, which the system is already designed to tolerate.

## Execution layer is unaffected

The OMS, execution gateway, and activity log have their own persistence guarantees and are not affected by pipeline aborts. Two specific invariants hold regardless of pipeline outcome:

- **Collect phase is always fully applied or not applied at all.** Fills from prior invocations are collected and committed to the OMS before any LLM agent runs (see [two-phase invocation model](05-execution-layer/architecture.md#two-phase-invocation-model)). A mid-pipeline abort after the collect phase leaves those fill reports correctly committed; an abort during collect leaves them uncommitted and they are collected again at the start of the next invocation.
- **Commands submitted before the abort remain committed.** The PM submits commands sequentially at the end of its run with synchronous validation between each. If the PM aborts after submitting N commands, those N are committed and reflected in portfolio state; the remainder are never submitted. Commands are individually idempotent at the engine (see the forthcoming OMS idempotency spec), so this partial-batch outcome is a normal operational state, not a corruption.

---

*Cross-references:*

- LLM agent failure handling: [llm-agent-failure-handling.md](llm-agent-failure-handling.md)
- API failure handling (fresh-or-abort principle): [01-data-layer/api-failure-handling.md](01-data-layer/api-failure-handling.md)
- Two-phase invocation model: [05-execution-layer/architecture.md](05-execution-layer/architecture.md)
- Fresh context windows principle: [design-decisions.md](design-decisions.md)
- Continuous monitor safety authority: [05-execution-layer/architecture.md](05-execution-layer/architecture.md)
