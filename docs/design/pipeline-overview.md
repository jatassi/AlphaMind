# Pipeline architecture

A scheduled trigger (cron) executes a sequential pipeline across five layers. Each LLM stage runs in a fresh context window to prevent upstream bias from leaking downstream. Risk guardrails cross-cut the decision and execution layers.

```
Trigger → Data Layer → Distillation Layer → Analysis Layer → Decision Layer → Execution Layer
                                                                    ↑                ↑
                                                              Risk Guardrails ───────┘
```

### Layer overview

| Layer | Type | Purpose |
|-------|------|---------|
| Cron trigger | Infrastructure | Activates the pipeline on schedule |
| [Data layer](01-data-layer/README.md) | Programmatic | Hits APIs, collects raw external data; reads internal portfolio state from execution layer database |
| [Distillation layer](02-distillation-layer/README.md) | Programmatic | Normalizes data, computes indicators, flags anomalies, derives portfolio metrics |
| [Analysis layer](03-analysis-layer/README.md) | Hybrid (LLM + tools) | Domain researchers analyze, adaptive research investigates, synthesizer produces unified snapshot with source references |
| [Decision layer](04-decision-layer/README.md) | Hybrid (LLM + deterministic) | Analyst and strategist read synthesis (retrieve source briefs on demand), produce structured recommendations; proposal pre-processor merges and annotates outputs deterministically; portfolio manager evaluates annotated proposals, adjusts, executes |
| [Execution layer](05-execution-layer/README.md) | Programmatic | Order management, Alpaca broker adapter (paper and live via URL swap), position tracking, P/L accounting, thesis tracking |
| [Risk guardrails](06-risk-guardrails/README.md) | Programmatic | Hard-coded constraints enforced at trader advisory, PM judgment, and engine hard-stop levels |
