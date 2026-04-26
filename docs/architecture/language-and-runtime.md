# Language and runtime

Python for both processes.

---

## Decision

Both the pipeline process and the continuous monitor are written in **Python**. Single language, shared domain model, shared dependencies.

---

## Rationale

### Why Python

**The pipeline process** needs:
- Async HTTP for hundreds of concurrent API calls → asyncio + httpx
- Numerical computation for distillation → numpy, pandas, TA-Lib
- LLM SDK support → first-class Anthropic/OpenAI Python clients
- Structured data modeling → dataclasses (already started in the schema work)

Python is the natural fit for every axis. The numerical ecosystem is unmatched. Every market data vendor and LLM provider ships a Python SDK first.

**The continuous monitor** needs:
- Persistent websocket connection → websockets / aiohttp
- Event loop with trigger detection → asyncio
- DB access → same ORM/driver as the pipeline

The monitor's per-tick workload is trivial (price comparison against trigger levels). Python's performance is more than adequate. Modern asyncio handles long-running websocket processes well.

### Why not split languages

The strongest argument for a single language is the **shared domain model**. The position model, thesis model, order vocabulary, fill report contract, and guardrail rules must be understood identically by both processes. Two languages means maintaining two implementations of these models — a maintenance burden with real correctness risk on a solo/small-team project.

### Why not Rust or Go for the monitor

The continuous monitor's workload is so lightweight (websocket listener, simple trigger comparisons on 5-20 orders) that the performance advantage of a systems language is meaningless. The development and maintenance cost of a split-language domain model outweighs the theoretical robustness gain.

If the monitor's workload ever scales to thousands of concurrent instruments with sub-millisecond trigger requirements, Rust becomes justified. At current scale, it isn't.

### Why not TypeScript/Node

Would work for the pipeline but loses the numerical ecosystem. Less mature libraries for technical indicators and statistical computation. LLM SDKs exist but Python's are more battle-tested. No clear advantage over Python for this workload.

---

## Runtime details

- **Python version:** 3.13+ (free-threaded build option, PEP 695 generic syntax, improved error messages, performance improvements, task groups in asyncio)
- **Async framework:** asyncio (stdlib) — no need for Trio or other alternatives given the workload
- **Package management:** uv (per [technology-selection.md](technology-selection.md)) — fast, modern, replaces pip + pip-tools + virtualenv
