# AlphaMind — technical architecture

**Status:** Complete
**Last updated:** 2026-03-15

---

## Purpose

Documents the technical architecture of AlphaMind: language, infrastructure, component design, and rationale. The [design spec](../design/README.md) defines *what* the system does; this directory defines *how it is built*.

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

Documented bottom-up, from system shape to specific technology:

1. **System characterization** — what are we building?
2. **Component boundaries** — deployable units and their interactions
3. **Language and runtime** — language fit for the computational profiles
4. **Data and state** — storage, querying, and cross-component sharing
5. **LLM integration** — agent orchestration, prompting, management
6. **Infrastructure** — scheduling, deployment, observability, cost
7. **Technology selection** — specific libraries, frameworks, services
