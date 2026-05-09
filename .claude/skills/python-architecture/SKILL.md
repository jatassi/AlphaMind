---
name: python-architecture
description: Opinionated audit and design guidance for Python systems. Use when auditing, refactoring, or restructuring an existing Python codebase, or when designing a new module, service, or pipeline. Distilled from Hynek Schlawack, Brandon Rhodes, Raymond Hettinger, Glyph Lefkowitz, the Cosmic Python authors, Hillel Wayne, Gary Bernhardt, and Nathaniel J. Smith.
---

# Python architecture

For audits and designs at the package / system level. Not for line-level style (use a linter), library selection, security, deployment, or micro-optimisation. The skill bundles three scripts that produce structural facts (`scripts/`), eight reference files of opinionated theses (`references/`), and two output templates (`assets/`). Read this file end-to-end on first use; consult references on demand.

The skill optimises for three goals at once: **testability** (good seams, sociable tests, faked owned dependencies), **maintainability** (small interfaces, clear layer boundaries, types that catch bugs at the boundary), and **AI- and human-navigability** (deep modules whose implementation lives near its interface, so understanding one concept doesn't require chasing imports across the tree). When recommendations conflict, the deepest principle wins; otherwise prefer the option that improves all three.

## 1. Pick a mode

**Audit** — an existing codebase or package. Triggers: "review this code", "what should I clean up", "is this Pythonic", "refactor this", "audit my project". Goes to §3.

**Design** — a new module, service, or pipeline. Triggers: "how should I structure", "design a module that", "I'm starting a new service", "scaffold a package for". Goes to §4.

If the request is too small for either (a single file, a few hundred lines, no package structure), this skill is overkill — handle it directly with general Python knowledge. If the request is library selection ("httpx vs requests"), it's adjacent — answer it but don't activate the audit/design machinery.

## 2. Load-bearing principles

These nine govern everything else. Each has a one-paragraph summary here and a deeper reference. When a finding or recommendation rests on one of them, name it explicitly so the user can disagree with the principle and see the downstream change.

**P1. Functional core, imperative shell.** Business logic lives in pure functions over plain values. I/O — DB, HTTP, time, randomness, the LLM, the broker — lives in a thin shell that loads inputs, calls the core, and writes outputs (Bernhardt; the spine of Cosmic Python under different names). The most common architectural failure in real Python codebases is leaking I/O into what should be pure code: `requests.get` inside a domain method, `datetime.now()` inside a calculation, an ORM query buried four calls deep. → `references/foundations.md §A1`.

**P2. The import graph IS the architecture.** In a dynamic language without enforced visibility, the only architecture is what the modules actually import (Brandon Rhodes). If `domain/order.py` imports `sqlalchemy`, you have a layered violation regardless of what the README says. Enforce the rules you want with `import-linter` in CI; don't trust prose. → `references/foundations.md §A2`.

**P3. Make illegal states unrepresentable.** Encode constraints in types — `NewType("OrderId", UUID)`, `Literal["draft", "submitted", "filled"]`, frozen dataclasses with validating `__post_init__`, parsed-once domain primitives — so the type checker rejects the bug rather than `if`-statement-and-pray runtime checks (Hillel Wayne, Edwin Brady). Pair with **parse, don't validate**: untrusted input crosses the boundary exactly once into a typed value, then internal code trusts the type. → `references/data-and-types.md §D, §E`.

**P4. Hexagonal where the domain is non-trivial; shell-only where it isn't.** For systems with real rules ("an order can only allocate inventory if…"), put the rules in pure domain modules and inject I/O at the edges via Protocols (Cosmic Python). For systems that are mostly transformations (vendor JSON → DB row), skip the ceremony — a function pipeline is fine. **Don't apply hexagonal to CRUD apps; you'll just rename Django models.** → `references/foundations.md §C1`.

**P5. Frozen dataclass + slots internally; Pydantic only at trust boundaries.** `@dataclass(frozen=True, slots=True)` is the default internal type (Hynek Schlawack). Pydantic `BaseModel` is for HTTP request/response, file parsing, vendor API ingest, config loading, LLM tool-call validation — *not* internal domain types. Convert once at ingest into a frozen dataclass; the type system carries the trust from there. This is the single highest-yield audit finding in most Python codebases. → `references/data-and-types.md §D1, §D2`.

**P6. `mypy --strict` (or pyright `strict`) from day one.** Retrofitting types is order-of-magnitude harder than starting strict (Guido van Rossum on typing posture). New modules: strict. Old modules: per-module strictness escalation with a debt list. `# type: ignore` is a code smell with a comment explaining why. → `references/data-and-types.md §E2`.

**P7. Async only at the I/O boundary; structured concurrency or none.** Async earns its keep when there are concurrent I/O operations and nowhere else (Glyph: "async is not for you"). CPU-bound code gets nothing from async; sequential code gets nothing. And once async, structured concurrency is mandatory: every `create_task` lives inside an `asyncio.TaskGroup` (Nathaniel J. Smith). Fire-and-forget is a guaranteed silent bug. → `references/runtime.md §G`.

**P8. Sociable unit tests; fakes over mocks.** Test the public surface of a unit with its real internal collaborators, against in-memory or trivial-fake substitutes for I/O — the Detroit / classicist / Cosmic Python style. Mockist tests (mock every collaborator) couple to implementation and rot during refactors. **Don't mock what you don't own** (Freeman & Pryce): wrap third-party libs behind a Protocol you own, then fake the Protocol. → `references/testing.md §J1, §J2`.

**P9. Deep modules: small interface, large implementation.** A module is *deep* when its public surface is small relative to the complexity it hides; *shallow* when the interface area approaches the implementation (Ousterhout, *A Philosophy of Software Design*). Shallow modules are the dual failure mode of P1's functional-core enthusiasm: a swarm of pure helper functions extracted "for testability" can leave the real bugs in the call sites that thread them together. Optimise for the smallest interface that still expresses the contract; let depth absorb complexity that doesn't need to be visible. Deep modules are also more navigable for both humans and LLM agents — fewer files to bounce between to understand one concept. → `references/foundations.md §A5`.

## 3. Audit workflow

The agent's job in audit mode is to produce **specific, sourced, prioritised findings** — not a generic Python style review. Every finding cites the principle or reference thesis it rests on, and pairs with a one-line fix. Generic praise ("good use of type hints") is filler; cut it.

### 3.1 Establish scope

Before touching code, agree with the user on:

1. **What to audit.** A single package, a layer, the whole repo, or a specific concern (e.g. "just look at concurrency")?
2. **Depth.** A 30-minute walk-through versus a multi-hour deep audit produces very different reports.
3. **Known constraints.** Frameworks the user can't change, deployment targets, deliberate decisions they don't want re-litigated.

If the user has dropped you in without scoping, ask. A scoped audit is useful; an unscoped one drifts into vague advice.

### 3.2 Ground yourself in the codebase

Read the surface before the substance:

- `pyproject.toml` — Python version, dependency manager, tool config (`[tool.ruff]`, `[tool.mypy]`, `[tool.pytest]`), package layout (src/ vs flat), declared scripts.
- The top-level `README.md` and any `docs/architecture/` content. Note what the code *claims* to be; the gap between claim and reality is half the audit.
- The top-level package layout (`ls src/<package>/`). Is it organised by feature or by layer?

This is the calibration pass. Don't form opinions yet.

### 3.3 Run the structural scripts

Each script writes JSON to stdout. Read the JSON; let Claude form findings.

```bash
python .claude/skills/python-architecture/scripts/package_overview.py <src_path>
python .claude/skills/python-architecture/scripts/analyze_imports.py <src_path>
python .claude/skills/python-architecture/scripts/antipattern_scan.py <src_path>
```

`package_overview.py` reports per-module line counts, declared `__all__`, and a categorised import summary (stdlib / third-party / first-party). Look for: god modules, modules with no `__all__` that are imported widely, modules importing across declared layer boundaries.

`analyze_imports.py` reports the import graph as an adjacency list, plus detected cycles. Cycles are almost always architectural smells — a domain module importing from an adapter, a "utils" module importing from a feature.

`antipattern_scan.py` reports the deterministic antipatterns from `references/antipatterns.md`: mutable default arguments, bare `except:`, naive `datetime.now()`, `from x import *`, `float` for monetary fields (heuristic), Pydantic `BaseModel` outside boundary modules (heuristic), `asyncio.create_task` without a TaskGroup parent (heuristic). Each finding has a file, line, and antipattern ID.

The scripts produce **data**. The findings come from interpreting the data against the principles in §2 and the references.

### 3.4 Walk the audit dimensions

The structural scripts produce a frame, but they don't catch everything. Two complementary modes:

- **Dimensional walk (default).** Walk the dimensions below in order. Each one is anchored to a reference and produces specific findings. Use when scripts have run, the codebase is medium-to-large, and you want coverage.
- **Friction walk (alternative or supplement).** Read the codebase like a new contributor — pick a feature and follow its execution path from entrypoint to outcome. Note where comprehension stalls: where understanding one concept requires bouncing between many small files, where module boundaries seem to fight you, where the test suite doesn't cover the path you're reading. The friction *is* the signal. Use when scripts haven't been run yet, the codebase is small enough to walk, or as a sanity-check pass after the dimensional walk has produced its list — friction often surfaces shallow-module / over-decomposition findings (§A5) that the structural scripts can't see.

Either way, work the dimensions in this order. Earlier dimensions surface findings that change later dimensions, so don't shuffle.

1. **Layout & boundaries** (`references/packaging-and-layout.md`, `references/foundations.md §C`). Feature vs. layer organisation? `__init__.py` policy? Visible boundary between domain and adapters? Cycles in the import graph? Module depth — interfaces small relative to implementations, or shallow swarms of single-purpose helpers (§A5)?
2. **Data & types** (`references/data-and-types.md`). Frozen dataclasses internally? Pydantic only at boundaries? Domain primitives over bare ints/strs? `StrEnum`/`Literal` for closed sets? Aware datetimes? `Decimal` for money? `mypy --strict`?
3. **Runtime** (`references/runtime.md`). Errors caught narrowly with `from`-chained re-raises? Domain exceptions for expected failures? Every external call has a timeout? Async only at the boundary, with `TaskGroup`? Structured logging? Config via `pydantic-settings`?
4. **Testing** (`references/testing.md`). For each module's dependencies, the right strategy per category — in-process / local-substitutable / remote-but-owned / true-external (§J0)? Sociable unit tests at module scope? Fakes for owned Protocols, not patches on third-party libs? Real DB for integration tests? `Clock` injected, randomness seeded? Hypothesis where invariants exist?
5. **Application shape** (`references/app-shapes.md`). Identify the system's shape (CLI, web API, data pipeline, long-running daemon, agentic/LLM, numerical) and consult the shape-specific section. Some antipatterns are only antipatterns in a particular shape.

### 3.5 Produce the report

Use `assets/audit_report_template.html` as the scaffold; produce a single self-contained HTML file. Save as `audit-<package>-<YYYY-MM-DD>.html` in the working directory unless the user specifies otherwise. Deviate from the template where the audit's findings demand a different shape — but keep the section ordering, since it leads with visuals to frame the system before per-finding detail.

Each finding has:

- **What** — the issue, in one sentence (the card heading).
- **Where** — file path(s) and line numbers, or "package-wide".
- **Why it matters** — one or two sentences citing the principle (P1–P9) or reference thesis.
- **Fix** — one-line concrete change. If non-trivial, point to a reference section that explains how.

Each finding card carries badges: a **severity badge** (load-bearing / high-yield / worth-knowing) and one or more **principle badges** (P1–P9 or the reference thesis ID like A5, J0). When a small Mermaid diagram or code snippet makes the issue clearer than prose, embed it inside the card's `.embed` block.

Group findings by severity:

- **Load-bearing** — a violation of P1–P9 or a structural pathology (cycle, layer violation, god module, primitive obsession spanning the system, shallow-module swarm). Worth scheduling deliberate work. Rendered as full finding cards.
- **High-yield** — local antipatterns that are easy to fix and pay off immediately (mutable defaults, naive datetimes, missing timeouts). Rendered as a grouped table with counts and example file:line refs — too many for cards to add value.
- **Worth knowing** — observations that aren't problems today but will become problems if the system grows in a particular direction. Rendered as compact cards.

End the report with a **punch list**: ordered items, each tagged with two cost dimensions, balancing load-bearing-ness against ease of fix. The user wants a list they can start working from, not an inventory.

The two dimensions:

- **Complexity** (qualitative — a structural property of the change, verifiable by inspection):
  - `Trivial` — no design knowledge required; mechanical change verifiable by syntax (lint autofix, regex replace, type annotation).
  - `Low` — requires understanding one module's contract.
  - `Medium` — requires understanding one subsystem's design.
  - `High` — requires understanding two or more subsystems' interactions, or a cross-cutting concern (type system, layer responsibilities).
  - `Extreme` — requires re-deriving the system's architecture; multiple subsystems must be redesigned in concert.
- **Blast radius** (quantitative — files touched):
  - `Surgical` — 1 file
  - `Local` — 2–5 files
  - `Module` — 6–20 files
  - `Subsystem` — 21–100 files
  - `System-wide` — >100 files

**Do not anchor on time-based estimates** (hours, days, weeks). Agents systematically misestimate durations because LLM-driven work compresses time non-linearly relative to human work, and time estimates rot the moment a fix turns out harder than expected. Complexity + blast radius describes the work without that hazard, and both dimensions are verifiable from the change itself rather than from prediction.

**Mandatory visualisations** (every audit produces these, near the top of the report):

- **Architecture overview** — Mermaid `flowchart` of the module / package dependency graph, current vs proposed. Mark cycles and layer violations on the current side with the `violation` class; mark targets and boundaries on the proposed side. Keep node count manageable (~15 per side); use subgraphs to group when larger.
- **Package layout** — side-by-side text trees with diff styling (`add` / `rem` / `ren` / `new` spans). Inline `note` spans annotate non-obvious moves.

**Optional visualisations** (include only when the audit surfaces relevant findings; otherwise remove the entire section *and* its TOC entry):

- **Data model** — Mermaid `erDiagram`, current vs proposed, when domain-type or schema findings are central.
- **Data flow** — Mermaid `flowchart` with subgraphs for stages (ingest / core / egress), when the system is pipeline-shaped or has boundary-leak findings worth showing.
- **Module-depth chart** — horizontal bars showing public-symbol-vs-implementation ratio per module, when over-decomposition / shallow-module findings (§A5) are central. The template provides the `.depth-chart` markup; populate one row per module under audit.

The template uses Mermaid (CDN) for diagrams and Prism (CDN) for code highlighting. The file degrades gracefully offline — diagram source remains readable as plain text. Verify Mermaid syntax by pasting each diagram into mermaid.live before delivering.

### 3.6 What the audit is NOT

- Not a style review. The linter handles style.
- Not a security review.
- Not a performance review (beyond flagging obvious antipatterns like row-by-row pandas).
- Not a "rewrite it in $LANG" review.
- Not generic praise. If everything in a dimension is fine, say so in one sentence and move on.

## 4. Design workflow

The agent's job in design mode is to produce a **concrete, opinionated sketch** — package layout, named domain primitives, named boundary types, named testing seam, and the three or four hardest-to-reverse decisions surfaced for the user. Not a tutorial; not a generic "here's how to structure a Python project". The sketch should be specific enough to start typing.

### 4.1 Clarify the system shape

Before sketching, ask 3–5 questions. The questions in `references/design-questions.md` cover the standard ground; pick the ones that matter for this design. The minimum:

- **Application shape** (`references/app-shapes.md §K1–K7`) — CLI, web API, pipeline, daemon, agent, numerical, library? The shape changes nearly everything downstream.
- **Trust boundaries** — where does untrusted input enter? Where does data leave the system?
- **Persistence** — what state survives a restart, and where does it live?
- **Concurrency profile** — is this episodic, long-running, or a mix? Many concurrent operations or sequential?
- **Constraints the user has already fixed** — Python version, framework, deployment target, persistence engine. Don't re-litigate these.

Don't ask all five every time. Ask what's load-bearing for *this* design.

### 4.2 Pick the load-bearing principles

Not every principle from §2 applies to every design. Decide which two or three are central. For most systems, P1 (functional core / imperative shell), P3 (illegal states unrepresentable), and P9 (deep modules) are central. P4 (hexagonal) is central only for systems with non-trivial domain rules. P7 (async/structured concurrency) is central only for I/O-concurrent systems. State which ones you're designing around so the user can push back if they want to optimise for something else.

### 4.3 Sketch the package layout

Feature-organised by default (P2). Concretely:

```
src/<project>/
├── pyproject.toml
├── <feature_a>/
│   ├── __init__.py        # empty for app code; curated for libraries
│   ├── domain.py          # pure functions, frozen dataclasses, Protocols
│   ├── adapters.py        # I/O against the Protocols
│   └── api.py             # HTTP / CLI / pipeline entrypoint
├── <feature_b>/
│   └── ...
└── shared/                # only what's truly used by ≥2 features
```

Subdivide a feature only when files cross ~400 lines or pull in distinctly different dependencies. Empty `domain/`, `infrastructure/`, `application/` subdirs in a feature with three files are pure ceremony.

For the boundaries that matter, write the import-linter contract:

```toml
[importlinter]
root_package = "<project>"

[[importlinter.contracts]]
name = "Domain doesn't import infrastructure"
type = "forbidden"
source_modules = ["<project>.<feature>.domain"]
forbidden_modules = ["sqlalchemy", "httpx", "<project>.shared.db"]
```

### 4.4 Name the types

The single highest-leverage move in design mode: write the domain primitives and boundary types *concretely*, not abstractly. "Use NewType for IDs" is filler; `OrderId = NewType("OrderId", UUID)` is design. The user should be able to copy these into a file and start working.

Cover at minimum:
- **Identifier types** — `UserId`, `OrderId`, etc.
- **Closed-set fields** — `OrderStatus = Literal["draft", "submitted", "filled"]` or `class OrderStatus(StrEnum): ...`.
- **Domain primitives** — `Money` (with currency), `Symbol`, `Quantity` — wherever primitive obsession is a likely failure mode.
- **Boundary types** — Pydantic models for the inbound surface, frozen dataclasses for the corresponding internal type, and the named conversion function (`OrderRequest.to_domain() -> Order`).

### 4.5 Specify the testing seam

Classify each external dependency into one of the four categories from `references/testing.md §J0` — in-process / local-substitutable / remote-but-owned / true-external — and name the substitute that follows from the category. For local-substitutable dependencies (Postgres, SQLite, filesystem) name the stand-in directly; don't introduce a Protocol if testcontainers or an embedded server already gives you the seam. For remote-owned and true-external dependencies, name the Protocol and the in-memory fake: "Fake the `OrderRepository` Protocol with an in-memory implementation" is a seam; "use mocks" is not. Include one example fake skeleton if useful.

### 4.6 Surface the hardest-to-reverse decisions

End the brief with three or four decisions that are expensive to undo, with the tradeoffs. Examples: choice of persistence engine, sync-vs-async at the entrypoint, whether to commit to events / message bus, whether to introduce a DI framework, monorepo vs. distribution split. The point is to give the user the smallest set of decisions they need to make consciously *now*; everything else can be adjusted later.

### 4.7 Produce the brief

Use `assets/design_brief_template.md` as scaffolding; rearrange or skip sections that don't apply. The brief is short — typically 1–3 pages. It is *not* a tutorial; it assumes the reader knows Python and is making implementation decisions.

## 5. Working with the references

References are the authoritative source for the theses; this file is navigation. When in doubt about why a thesis holds, read the reference, not memory. The references organise theses with a uniform structure: claim, rationale, attribution, when it breaks, audit signal (what to look for in code), design signal (how to apply when building new). The audit and design signals are the operational version of each thesis.

## 6. What this skill is NOT for

- **Library selection** — `httpx` vs `requests`, `Polars` vs `pandas`. Architectural recommendations should let library choices follow.
- **Security architecture** — auth, authz, threat models. Adjacent skill.
- **Deployment / IaC** — process supervision, Docker, Kubernetes, Terraform. Adjacent skill.
- **Performance optimisation** beyond flagging the obvious antipatterns.
- **Line-level style** — formatting, naming, docstring conventions. Use a linter.
- **Migrations from one framework to another** — too project-specific. The skill can inform the design of the destination, not the migration plan.

## 7. Pre-deliverable checklist

Before handing back the audit report or design brief:

**Content:**

- [ ] Every finding or recommendation cites a principle (P1–P9) or a specific reference thesis.
- [ ] Every "don't" has a paired concrete "do this instead". No bare prohibitions.
- [ ] Severity / priority is explicit. The user shouldn't have to infer what to do first.
- [ ] No generic Python advice that isn't specific to this codebase or design.
- [ ] No findings drawn from memory of "best practice" without a thesis to anchor them.
- [ ] If the audit found nothing significant in a dimension, that's stated in one sentence — not padded.
- [ ] The final ordering / next-action recommendation is the punch list the user can start working from.

**Audit HTML deliverable:**

- [ ] Architecture overview (Mermaid current-vs-proposed) is present.
- [ ] Package layout (current-vs-proposed file tree with diff styling) is present.
- [ ] Optional visualisations (data model / data flow / module-depth chart) are included only when findings demand them; otherwise the section *and* its TOC entry are removed.
- [ ] Every Mermaid block has valid syntax (paste into mermaid.live to verify before delivery).
- [ ] Severity is encoded as both a text badge and a visual cue (left-border colour on the finding card).
- [ ] Principle citations appear as `.badge.principle` badges on each finding, not just inline references.
- [ ] Each punch-list item carries a **complexity** badge (`Trivial`/`Low`/`Medium`/`High`/`Extreme`) and a **blast-radius** badge (`Surgical`/`Local`/`Module`/`Subsystem`/`System-wide`); no item carries a time-based estimate.
- [ ] All `FILL:` comments and unused OPTIONAL section scaffolding are stripped.
- [ ] Saved as `audit-<package>-<YYYY-MM-DD>.html`.

## 8. Sources

The references catalogue specific citations per thesis. The voices most heavily weighted in this skill are:

- **Hynek Schlawack** (hynek.me, attrs maintainer) — packaging, dataclasses, frozen-by-default, structured logging via structlog.
- **Brandon Rhodes** (rhodesmill.org, Architecture Patterns with Python contributor) — import graph as architecture, anti-patterns talks, vertical slice organisation.
- **Raymond Hettinger** (PyCon talks) — flat over nested, dataclasses, the right record type for the job.
- **Glyph Lefkowitz** (glyph.twistedmatrix.com, Twisted) — async cost, immutability, OOP design.
- **Harry Percival & Bob Gregory** (cosmicpython.com, *Architecture Patterns with Python*) — hexagonal, repositories with aggregates, service layers, domain events.
- **Gary Bernhardt** (destroyallsoftware.com) — functional core, imperative shell.
- **Hillel Wayne** (hillelwayne.com) — type-driven design, parse-don't-validate, illegal states unrepresentable.
- **Nathaniel J. Smith** (vorpus.org) — structured concurrency, Trio.
- **Samuel Colvin** (Pydantic) — validation at trust boundaries.
- **Sebastián Ramírez** (FastAPI, Typer) — type-driven web/CLI, the `Depends()` exception to the no-DI-container rule.
- **David Beazley** (dabeaz.com) — concurrency cost, GIL.
- **Charity Majors** (honeycomb.io) — wide-event observability.
- **Steve Freeman & Nat Pryce** (*Growing Object-Oriented Software, Guided by Tests*) — don't mock what you don't own, fakes over mocks.
- **Brett Slatkin** (*Effective Python*) — concrete idioms, flat over nested.
