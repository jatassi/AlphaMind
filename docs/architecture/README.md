# AlphaMind — technical architecture

**Status:** Complete
**Last updated:** 2026-03-15

---

## Purpose

This directory documents the technical architecture of AlphaMind: language choices, infrastructure decisions, component design, and the rationale behind each. The [design spec](../design/README.md) defines *what* the system does; this directory defines *how it is built*.

---

## Documents

| Document | Status | Description |
|----------|--------|-------------|
| [System characterization](system-characterization.md) | Complete | What kind of system AlphaMind is — runtime profiles, architectural concerns, computational characteristics |
| [Component boundaries](component-boundaries.md) | Complete | Two processes (pipeline + continuous monitor) sharing a database |
| [Language and runtime](language-and-runtime.md) | Complete | Python for both processes — shared domain model, unmatched ecosystem fit |
| [Data and state](data-and-state.md) | Complete | SQLite (WAL mode), four state categories, SQLAlchemy ORM for portfolio state / Core for bulk data |
| [LLM integration](llm-integration.md) | Complete | Claude Agent SDK with Max OAuth token, per-agent model/prompt/tool config, async orchestration |
| [Infrastructure](infrastructure.md) | Complete | APScheduler v3, NSSM Windows Service supervision, structured logging, SQLite audit trail |
| [Technology selection](technology-selection.md) | Complete | Consolidated dependency list, version constraints, and exclusion rationale |

---

## Decision process

Architecture decisions are documented bottom-up, starting from high-level system shape and working toward specific technology selection:

1. **System characterization** — what are we actually building?
2. **Component boundaries** — what are the deployable units and how do they interact?
3. **Language and runtime** — what languages fit the computational profiles?
4. **Data and state** — how is state stored, queried, and shared across components?
5. **LLM integration** — how do agents get orchestrated, prompted, and managed?
6. **Infrastructure** — scheduling, deployment, observability, cost
7. **Technology selection** — specific libraries, frameworks, and services
