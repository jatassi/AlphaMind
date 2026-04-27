# Language and runtime

Python for both processes.

---

## Decision

Both processes are written in **Python**: single language, shared domain model, shared dependencies.

---

## Rationale

### Why Python

**The pipeline process** needs:
- Async HTTP for hundreds of concurrent API calls → asyncio + httpx
- Numerical computation for distillation → numpy, pandas, TA-Lib
- LLM SDK support → first-class Anthropic/OpenAI Python clients
- Structured data modeling → dataclasses (already started in the schema work)

Python's numerical ecosystem is unmatched, and every market data vendor and LLM provider ships a Python SDK first.

**The continuous monitor** needs:
- Persistent websocket connection → websockets / aiohttp
- Event loop with trigger detection → asyncio
- DB access → same ORM/driver as the pipeline

Per-tick workload is trivial (price comparison against trigger levels); Python's performance is adequate.

### Why not split languages

The position model, thesis model, order vocabulary, fill report contract, and guardrail rules must be understood identically by both processes. Two languages means two implementations — a maintenance burden with real correctness risk on a solo/small-team project.

### Why not Rust or Go for the monitor

The monitor's workload (websocket listener, trigger comparisons on 5-20 orders) makes the systems-language performance advantage meaningless. If it ever scales to thousands of concurrent instruments with sub-millisecond requirements, Rust becomes justified.

### Why not TypeScript/Node

Loses the numerical ecosystem; less mature libraries for technical indicators and statistical computation. Python's LLM SDKs are more battle-tested.

---

## Runtime details

- **Python version:** 3.13+ (free-threaded build option, PEP 695 generic syntax, improved error messages, asyncio task groups)
- **Async framework:** asyncio (stdlib)
- **Package management:** uv (per [technology-selection.md](technology-selection.md)) — replaces pip + pip-tools + virtualenv
