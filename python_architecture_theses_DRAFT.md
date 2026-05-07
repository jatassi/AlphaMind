# Python architecture — opinionated theses (draft for review)

**Status:** Draft for Jackson's review before building the `python-architecture` skill.
**Source:** Synthesised from five parallel research streams covering project structure & packaging, architectural patterns, data modelling & types, concurrency / errors / observability, and testing / antipatterns / app-shape patterns. Voices weighted: Hynek Schlawack, Brett Cannon, Brandon Rhodes, Raymond Hettinger, Łukasz Langa, Glyph Lefkowitz, Harry Percival & Bob Gregory ("Cosmic Python"), Gary Bernhardt, Hillel Wayne, Samuel Colvin, Sebastián Ramírez, Nathaniel J. Smith, David Beazley, Charity Majors, Brett Slatkin, Kent C. Dodds, Martin Fowler, Justin Searls, Steve Freeman & Nat Pryce.

The theses are **opinionated and unhedged**. Where two voices disagree, I've picked a side and noted the alternative. Where a thesis is load-bearing — i.e. lots of other theses depend on it — I've marked it ⭐.

The intent is that an LLM holding these theses can (a) audit a codebase and produce specific, sourced findings, and (b) sketch a greenfield system that doesn't need to be re-architected six months in.

---

## A. Foundations — the four ideas everything else rests on

### A1. ⭐ Functional core, imperative shell
Business logic lives in pure functions over plain values. I/O — DB, HTTP, time, randomness, the LLM, the broker — lives in a thin shell that loads inputs, calls the core, and writes outputs. Bernhardt's framing; the spine of *Architecture Patterns with Python* (cosmicpython.com) under different names.

The corollary: the core is trivially testable in-process with no fixtures, and parallelisable / cacheable / replayable for free. The shell is small and obvious. **The most common architectural failure in Python codebases is leaking I/O into what should be pure code** — `requests.get` inside a domain method, `datetime.now()` inside a calculation, an ORM query buried four calls deep inside business logic.

When it breaks: code that genuinely is "thin glue over an external system" (a CLI wrapper around an API, a one-shot script). For those, the shell is the program, and forcing a core/shell split is overhead.

### A2. ⭐ The import graph is the architecture
Brandon Rhodes's framing: in a dynamic language without enforced visibility, your only architecture is what your modules actually import. If `domain/order.py` imports `sqlalchemy`, you have a layered violation regardless of what your README says. Enforce the rules you want with `import-linter` in CI; don't trust prose.

Every codebase >5K lines should declare its layering as explicit `import-linter` contracts. If the import graph doesn't agree with the architecture diagram, the diagram is fiction.

### A3. ⭐ Make illegal states unrepresentable
Hillel Wayne, Edwin Brady, lifted into Python via PEPs 544 / 586 / 593. Encode constraints in types — `NewType("OrderId", UUID)`, `Literal["draft", "submitted", "filled"]`, frozen dataclasses with validating `__post_init__`, parsed-once domain primitives — so the type checker rejects the bug rather than `if`-statement-and-pray runtime checks. Pair with "parse, don't validate": untrusted input crosses the boundary exactly once into a typed value, then internal code trusts the type.

### A4. Cohesion over hierarchy; flat over nested
"Flat is better than nested" — Raymond Hettinger, the Zen, Brett Slatkin (*Effective Python*). Two to three levels of package depth is plenty. Deep trees (`alphamind.execution.orders_and_brackets.factories.builders.envelope_v2`) almost always indicate that someone created folders before there was anything to put in them. Start with a single module; promote to a package when there are three or more cohesive submodules; promote to its own distribution when there are independent consumers.

---

## B. Project skeleton — packaging, layout, tooling

### B1. `src/` layout for anything that is, or might become, a distribution
Hynek Schlawack, Brett Cannon, the PyPA. Forces editable installs to behave like real installs, prevents `import mypackage` from silently picking up a sibling directory. Cost is zero. Use it from day one — migrating later is a day-long rabbit hole of broken venvs.

When to skip: literal one-file scripts with no `pyproject.toml`. AlphaMind already does this correctly.

### B2. `pyproject.toml` is the only build / metadata file (PEP 621)
No `setup.py`, no `setup.cfg`, no `requirements.txt` checked in alongside. Tool config (ruff, mypy, pytest) goes under `[tool.*]` — one source of truth.

### B3. Package by feature, not by layer
`alphamind/orders/{domain,adapters,api}.py` beats `alphamind/{models,services,routers}/orders.py`. Cosmic Python Appendix B; the dominant view in modern Python application design (Django excepted, where the framework dictates layout). A layered tree forces every change to span every directory; a feature tree localises change.

The trap to avoid: feature packages that re-introduce internal layering as `domain/`, `infrastructure/`, `application/` subdirs when there's nothing in them. Promote to subdirectories only when files cross ~400 lines or pull in distinctly different dependencies.

### B4. `__init__.py` stays empty for application code; curated for libraries
For applications: empty. Importers say `from alphamind.orders.domain import Order`, which makes the dependency self-documenting and avoids circular-import landmines. For libraries with a stable public surface (httpx, requests-style ergonomics), curate `__init__.py` with explicit re-exports and a populated `__all__`. Requests is the cautionary tale — re-exports made every internal change a potential break.

### B5. `uv` for new projects; `uv` or Poetry for existing
The Astral toolchain has won. `uv` replaces pip + pip-tools + virtualenv + pyenv + (most of) Poetry, is 10–100× faster, and produces a `uv.lock` that is reproducible across platforms. Existing Poetry projects can stay on Poetry; greenfield should be `uv`. AlphaMind already uses `uv`.

### B6. Pin in apps, range in libraries
Apps (deployable services): commit a lockfile, pin everything transitively, `uv sync --frozen` in CI and prod. Libraries (published distributions): broad ranges (`httpx>=0.25,<1.0`) in `pyproject.toml`, no lockfile in the published artefact. Mixing these — pinned ranges in a library, fuzzy deps in an app — is one of the most reliable ways to get bitten in production.

### B7. Worktree / monorepo-of-one over polyrepo splits
For solo / small-team projects: keep everything in one repo until there's a clear external consumer. Splits are expensive to undo. AlphaMind's single-repo organisation is correct; don't be tempted to extract `alphamind-data` etc. without a real second consumer.

---

## C. Module & boundary discipline

### C1. ⭐ Hexagonal / ports-and-adapters where the domain is non-trivial; shell-only where it isn't
For systems with rules ("an order can only allocate inventory if…"), put the rules in pure domain modules and inject IO at the edges via Protocols. For systems that are mostly transformations (this ETL pipeline maps vendor JSON to a database row), skip the ceremony — a function-pipeline is fine. **Don't apply hexagonal to CRUD apps; you'll just rename Django models and call it a day.**

### C2. Repositories only when they pay rent
A `UserRepository.get(id)` that wraps `session.get(User, id)` is renaming, not abstracting. Cosmic Python's repository earns its keep when (a) it enforces aggregate boundaries — you load `Order` and its `OrderLines` together, never piecewise — or (b) you actually have multiple stores. Otherwise, the `Session` / `AsyncSession` *is* the unit of work and repository.

### C3. Service layer = use-case = transaction boundary
Not "another tier between controllers and domain." A service function (`allocate_stock(order_id, sku, qty)`) is a single use case: opens a transaction, loads aggregates, calls domain methods, commits, returns / publishes. Keep them function-shaped where possible; only promote to a class when you genuinely need shared state (rare).

### C4. Constructor injection beats DI containers
Pass dependencies as arguments. Compose at the entrypoint (`main.py`, the FastAPI app factory, the test fixture). Skip `dependency-injector`, `injector`, `inject`, etc. — they replace explicit wiring with import-time magic and obscure type information. FastAPI's `Depends()` is the legitimate exception because it's syntactic, not runtime, and the type signature still tells the truth.

### C5. Public API discipline: `__all__` + leading-underscore convention
Module's public surface = what's in `__all__` (or, lacking that, what doesn't start with `_`). Treat anything else as breakable without warning. For libraries, freezing the public surface should be a deliberate act (a tag, a deprecation cycle), not "whatever happened to be importable when 1.0 shipped."

### C6. Avoid plugin / entry-point / namespace-package architectures until you have ≥3 third-party plugins
PEP 420 namespace packages, `setuptools.entry_points`, dynamic plugin discovery — all useful when you genuinely have third parties extending your system. Premature plugin architecture is one of the highest-cost-no-benefit moves in Python; a `dict[str, Strategy]` registry in code is clearer for first-party variants.

### C7. Domain events / message bus inside a monolith: only when state machines or audit demand it
The "publish a `StockAllocated` event from the domain method" pattern (cosmic Python ch. 8–11) shines for state machines, audit logs, decoupling cross-feature side effects (email, analytics). It's overkill for "just call the function." Default to direct calls; reach for events when you find yourself wanting to add a fifth side-effect to one method.

---

## D. Data modelling

### D1. ⭐ Frozen dataclass with `slots=True` is the default internal value type
`@dataclass(frozen=True, slots=True)` for everything inside the system. Mutability is opt-in and rare. Hynek Schlawack on `attrs` and Glyph on Twisted converge here. Frozen catches "spooky action at a distance" bugs at the assignment, not three call frames later. `slots=True` cuts memory ~30% and prevents accidental attribute typos. Free.

`attrs` over `dataclass` only when you need converters, validators, or richer inheritance (rare). Don't mix — pick one and stick with it per project.

### D2. ⭐ Pydantic only at trust boundaries
HTTP request/response, file parsing, vendor API ingest, config loading, LLM tool-call validation. **Not internal domain types.** Pydantic does coercion + validation work on every instantiation; using it for already-trusted internal data wastes cycles and conflates the boundary. Convert once at ingest into a frozen dataclass; the type system carries the trust from there.

This is the single biggest "easy win" the skill should flag in audits: Pydantic models used as internal domain objects.

### D3. Domain primitives over bare ints / strs / floats
`UserId = NewType("UserId", UUID)`. `Price = NewType("Price", Decimal)`. `Symbol = NewType("Symbol", str)` with a `parse_symbol` factory. Money is `Decimal` with a currency, never `float`. Datetimes are timezone-aware always — `datetime.now(timezone.utc)`, never naive. The "primitive obsession" antipattern is endemic in finance / data Python.

### D4. `StrEnum` (3.11+) or `Literal` over bare strings for closed sets
`Status.SUBMITTED` over `"submitted"`. Typos become type errors, exhaustiveness is checkable, and `StrEnum` round-trips through JSON without a custom encoder. `Literal` for cases where you don't need identity, just narrowing.

### D5. Pick the right record type
- `NamedTuple` — ad-hoc immutable returns, especially when callers will unpack positionally.
- `TypedDict` — structural shape over dict-shaped data you don't own (e.g. third-party JSON before parsing).
- `dataclass` — domain objects with behaviour, validation, identity.
- `Pydantic.BaseModel` — boundary parsing only (D2).

The mistake to flag: dataclasses used where TypedDicts would do (over-modelling vendor JSON), or NamedTuples used where a dataclass with named fields would be clearer (the "I'm just being lightweight" reflex).

### D6. Construction via factory classmethods, not overloaded `__init__`
`Order.from_api_payload(payload)`, `Order.from_db_row(row)`, `Order.opening(symbol, qty)` over an `__init__` that branches on `if from_db: ...`. Keeps the dataclass `__init__` dumb; the factories are documented entry points.

---

## E. Interfaces & types

### E1. `Protocol` for interfaces; `ABC` only for shared implementation
PEP 544 structural typing is the right Python idiom for "anything with these methods." Reach for `ABC` only when you (a) want to reuse base-class implementation or (b) need cheap `isinstance` checks at runtime without `@runtime_checkable`'s shape-only guarantee.

The pattern to adopt: define the Protocol where it's *consumed* (next to the function that takes it), not next to the implementations. This is the dependency-inversion direction.

### E2. ⭐ `mypy --strict` (or pyright `strict`) in CI from day one
Retrofitting types is order-of-magnitude harder than starting strict. New modules: strict. Old modules: per-module strictness escalation, with a `# TODO: strict` debt list. `# type: ignore` is a code smell with a comment explaining why; CI counts them as a metric over time.

### E3. PEP 695 generic syntax (`class Stack[T]:`) on 3.12+
Replaces the `TypeVar` boilerplate. AlphaMind on 3.13 should use it.

### E4. Avoid `Any`; bound it when you must
`Any` defeats the type checker. When you genuinely don't know the shape (untyped lib, dynamic dispatch), use `object` and narrow with `isinstance` — at least the type checker forces you to acknowledge the unknown.

---

## F. Errors

### F1. Catch narrow, re-raise with `from`
Never `except Exception:` outside the outermost supervisor (CLI handler, request handler, task supervisor). Catch the specific class you actually handle. When wrapping for context, `raise DomainError("couldn't allocate stock") from e` so the chain is preserved.

### F2. Domain exceptions for expected boundary failures
Define your own `OrderRejected`, `GuardrailBreach`, `VendorRateLimited`. They live at the boundary they represent. Library exceptions (`httpx.TimeoutException`) should be caught and translated at the adapter layer — domain code doesn't depend on `httpx`.

### F3. Result / Either types are *not* Pythonic — use sparingly
This is where I'm departing from one of the research streams. Rust-style `Result[T, E]` (`returns`, `result`) reads cleanly in Rust, awkwardly in Python. Use them only at narrow seams where (a) the failure is fully expected (validation, parsing) and (b) callers must explicitly handle both branches. For everything else, exceptions with clear types are more idiomatic and cheaper.

### F4. Idempotency is a precondition for retry
Tenacity / backoff / `httpx-retries` are fine *if* the retried operation is idempotent. Retrying a non-idempotent POST is a bug factory. Either make the operation idempotent (idempotency keys, content-addressed writes, upserts) or don't retry.

### F5. Every external call has a timeout
Every. `httpx` calls, DB queries, subprocess calls, file reads from network mounts, broker API calls, LLM API calls. No timeout = potential infinite hang. Use `asyncio.timeout` (3.11+) for async, library-level timeouts for sync.

### F6. Exponential backoff with jitter; circuit breaker only when load-shed matters
Default retry policy: exponential backoff (`base * 2^attempt`) with full jitter (`random.uniform(0, base * 2^attempt)`). Circuit breakers are worth the complexity only when you have enough RPS that fast-failing rather than retrying meaningfully reduces load. For low-RPS systems, just retry.

---

## G. Concurrency

### G1. ⭐ Async only at the I/O boundary
Glyph: "Async is not for you." Async only earns its keep when you have a meaningful number of concurrent I/O operations. CPU-bound code gets nothing from async; sequential code gets nothing; "everything async because the framework is async" is overhead. Push the async boundary as far down as it actually has work to do, no further.

### G2. ⭐ Structured concurrency — `asyncio.TaskGroup` (3.11+) or never
`create_task` and forget = guaranteed silent bug. Tasks must have a known parent and be awaited. Use `async with asyncio.TaskGroup() as tg: tg.create_task(...)` (Nathaniel J. Smith's structured-concurrency argument, lifted from Trio into stdlib). Cancellation propagates correctly, exceptions surface, no orphan tasks. AlphaMind on 3.13 has zero excuse for `create_task` without a TaskGroup parent.

### G3. Reuse a single `httpx.AsyncClient` per process / lifespan
Connection-pool reuse is a 10× perf delta. Build it in a lifespan context manager, close it on shutdown. Same for DB engines, broker clients, anything with a connection pool.

### G4. Cancellation safety: `try / finally` and context managers
Anything holding a resource (file, connection, lock) goes inside an `async with`. `asyncio.shield` is for durability (flush logs before cancellation propagates), not for "I don't want to think about cancellation." Make tasks cancellation-safe by default.

### G5. `multiprocessing` for CPU-bound, threads for "blocking C extension you can't avoid"
Numpy / pandas / sklearn work that doesn't release the GIL: `ProcessPoolExecutor`. Calls into C libs that *do* release the GIL: threads are fine. Most code: neither — `asyncio.to_thread` for occasional blocking calls is enough.

---

## H. Configuration & secrets

### H1. `pydantic-settings` + environment variables
Hierarchy: defaults in code → file (TOML/YAML) → environment → secrets manager. `pydantic-settings.BaseSettings` validates and coerces. Twelve-factor for the deployable parts.

### H2. Secrets only via env vars or a secrets manager
Never in `pyproject.toml`, never in committed files, never in YAML. `.env` files are local-only and gitignored. For prod: secrets manager (AWS, Vault, 1Password CLI, OS keychain).

### H3. Config is data; behaviour lives in code
A 500-line YAML file with `if env == "prod"` conditionals is code in disguise. If config needs branching, it's a feature flag system, and feature flags belong in code with a typed flag registry, not in YAML.

---

## I. Observability

### I1. ⭐ Structured logging from day one with `structlog`
Free-text logs are a debt instrument that compounds. `structlog` produces dict-shaped events that flow into Logfire / Datadog / Honeycomb / your own SQL audit table cleanly. Bind context once (`structlog.contextvars.bind_contextvars(request_id=...)`); every subsequent log line carries it.

### I2. Log levels mean specific things
`DEBUG` — variable-level detail you'd want when reproducing a bug, off in production. `INFO` — milestone events of normal operation (request received, task completed, run started). `WARNING` — unexpected but not a failure (retry, degraded path, deprecated call). `ERROR` — operation failed; humans may need to act. `CRITICAL` — process-level invariant broken; alert. Mis-using levels makes alerting impossible.

### I3. Logs ≠ metrics ≠ traces — use all three deliberately
Logs: discrete events. Metrics: aggregates for alerting (Prometheus / OTel metrics). Traces: causality across service boundaries (OTel traces). For single-process systems: logs do most work; metrics for alerts; traces for the rare cross-service question. For distributed systems, invert: traces become primary.

### I4. "Wide events" framing (Charity Majors)
One log line per significant operation, with all context attached, beats ten thin log lines per operation. Structured-logging makes wide events natural; cardinality is good when each event has a request ID.

---

## J. Testing

### J1. ⭐ Sociable unit tests at module / package scope > isolated mock-heavy unit tests
Test the public surface of a unit, with its real internal collaborators, against in-memory or trivial-fake substitutes for I/O. The Detroit / classicist / "Cosmic Python" style. Mockist tests (mock every collaborator) couple to implementation and rot during refactors.

### J2. ⭐ "Don't mock what you don't own"
Steve Freeman & Nat Pryce; Cosmic Python. Mocks pinned to `httpx.AsyncClient.get` are a brittle re-implementation of httpx. Wrap third-party libs behind a Protocol you own; in tests, substitute an in-memory fake of *your* Protocol. The Protocol is the contract; the fake is one of the implementations.

### J3. Pytest fixture discipline
Fixtures live in the nearest `conftest.py`. Scope deliberately — `function` is the default, `module` / `session` for expensive setup with no per-test mutation. Factory fixtures (`def make_order(**overrides): ...`) over fixed test objects. Parametrize for input variation; don't loop inside a test.

### J4. Real Postgres / real SQLite-on-disk for integration; testcontainers for cross-service
For DB integration tests, prefer the real engine via testcontainers (Postgres) or a tmpfile SQLite. In-memory SQLite when the schema is trivially compatible. Rolled-back transactions per test for speed. AlphaMind specifically: integration tests against real SQLite-on-tmpfile is correct.

### J5. Hypothesis for invariant-heavy domains
Property-based testing earns its keep where you can name invariants ("after `apply_fill`, position quantity = sum of fills", "for any valid order, round-tripping through serialization preserves equality"). Don't force it on CRUD-shaped tests.

### J6. Determinism is an architectural property
Tests fail randomly because the system is non-deterministic, not because tests are flaky. Inject a `Clock` (don't call `datetime.now()` in domain code), a `Random` (seed it from config), a task-ID generator. Once the system is deterministic, replay becomes trivial — a property especially valuable for LLM systems and trading systems.

---

## K. Application-shape patterns

The architecture should fit the shape of the system. This section is the part of the skill that asks "what kind of system is this?" and applies the right pattern set.

### K1. CLI applications
Typer over Click over argparse. The command surface is a thin wrapper over a library API — the same code should be callable in-process from tests and notebooks. Exit codes mean specific things (`0` ok, `1` user error, `2` system error). Stdout is data; stderr is logs. Don't print structured logs to stdout.

### K2. Web applications / APIs
FastAPI for new APIs (Pydantic at boundary, type-driven, async-native). Django for content/admin-heavy monoliths where the framework's batteries pay off. Flask only for legacy. The framework is a detail; business logic doesn't import the framework.

### K3. Data pipelines / ETL
Stages are pure functions over typed inputs; orchestration is separate. Idempotency by stage (re-running stage 4 after a crash should produce the same result). Checkpointing by stage. Schema-on-write at the landing zone (validate vendor data into a typed table before downstream stages touch it). Roll your own only until ~5 stages with non-trivial dependencies; reach for Prefect / Dagster when DAG complexity exceeds what's clearly readable in code. Airflow only when forced by org infra.

### K4. Long-running services / daemons
Lifespan context manager owns startup / shutdown — open clients, register signal handlers (SIGTERM → graceful), supervise background TaskGroups. Health endpoint returns liveness + readiness separately. Hot reload only via process supervisor (systemd, NSSM); don't roll your own.

### K5. Agentic / LLM applications
Treat the LLM as a slow, expensive, fallible function. Three load-bearing patterns:
- **Tools as the public API of a capability** — a tool's signature is its contract; structure your code so that "what the LLM can do" is a typed, validated, side-effect-explicit surface.
- **Replay-able prompts** — every LLM call is logged with full input + output + model + version. The system can be replayed from logs, both for debugging and for evaluation.
- **Determinism in the shell** — everything around the LLM is deterministic (D6 `Clock`, J6). Then LLM non-determinism is the only thing varying, which makes evals tractable.

State machines (`pending` → `running` → `completed` / `failed` / `cancelled` with explicit transitions) over implicit booleans, especially for multi-step agentic workflows. Each transition is logged.

### K6. Numerical / scientific
Vectorise; loops over rows in pandas are an antipattern (10–1000× slowdown). Polars over pandas for >1M rows. Leave Python (Cython, Rust via PyO3, C extension) only after profiling proves a hot path; "Python is slow" without profile data is a vibe, not an engineering decision.

### K7. Database schema migrations
Alembic. Expand-contract for any production system — never rename a column in one migration; add new column → backfill → cut over reads → cut over writes → drop old column, across multiple deploys. Test migrations both directions. Lock to a single migration head; multi-head merges are bug factories.

---

## L. Antipatterns the skill should flag (audit checklist)

These are the high-yield findings — things to surface in audit mode with high confidence.

1. **God modules** named `utils`, `helpers`, `common`, `misc`, `tools`, `lib` with >300 lines or >5 unrelated functions. Split by what the contents actually do.
2. **Side effects at import time** (network calls, file writes, mutable globals populated, `print` statements). Imports must be pure.
3. **Mutable default arguments** (`def f(x: list = []):`). Use `None` and create inside.
4. **Bare `except:` or `except Exception:`** outside the outermost supervisor.
5. **`from x import *`** anywhere in production code.
6. **Late imports as a circular-import workaround** rather than a structural fix.
7. **Catching exceptions to drive control flow** (`except KeyError` to detect missing dict key — use `.get` or `in`).
8. **Mutable dataclass without justification** (`@dataclass class X:` — should be `frozen=True, slots=True` by default).
9. **Pydantic `BaseModel` for an internal domain object** that never crosses a boundary.
10. **Bare `str` for status / kind / type fields** (should be `StrEnum` or `Literal`).
11. **Naive `datetime.now()`** anywhere.
12. **`float` for monetary amounts.**
13. **Untyped public API** (function signature without return type or parameter types in a module that's imported by others).
14. **DI container** (`dependency-injector`, `injector`) where constructor injection would do.
15. **Repository pattern that's a 1:1 wrapper over the ORM** with no aggregate / multi-store justification.
16. **`create_task` without a TaskGroup parent.**
17. **External call without a timeout.**
18. **Retry on a non-idempotent operation.**
19. **`async def` that only does CPU work** (no `await` against I/O).
20. **`logging.info(f"user {user_id} did {thing}")`** — free-text where `log.info("user.action", user_id=user_id, action=thing)` is correct.
21. **Mocking a third-party library directly** (`mock.patch("httpx.AsyncClient.get")`) rather than faking a Protocol you own.
22. **`Manager` / `Helper` / `Service` / `Util` class suffixes** (the "Kingdom of Nouns" smell) where a function would do.
23. **Singletons via module-level globals** that hold mutable state.
24. **Test that hits `datetime.now()` or `random` directly** — flake waiting to happen; inject a clock / RNG.
25. **`print` in non-CLI code** (logging exists; if it's CLI output, that's stdout-data, not a log line).

---

## M. Tensions / places I deliberately took a side

Calling these out so you can push back.

- **"Result types vs exceptions"** — One research stream advocated a more aggressive Result-type stance. I took the more conservative position (F3): exceptions are the Python idiom; Result is for narrow seams. Arguably for an LLM-driven system where every tool call has expected failure modes, leaning further into Result-like patterns (e.g. `ToolResult` ADTs) is correct. I'd love your take.
- **"Repository pattern"** — I came down skeptically (C2). For systems with truly complex aggregates (an `Order` with `Lines`, `Allocations`, `Fills`), repositories earn their keep; for simple table-row mapping they don't. AlphaMind's `portfolio_state` looks aggregate-ish; might be a place where repositories make sense.
- **"Async everywhere vs async at the boundary"** — I took Glyph's "async only where it earns it" line (G1). Some Python systems (FastAPI-native, websocket-heavy) are async-everywhere by necessity. AlphaMind's continuous monitor is async-everywhere; the pipeline could be sync with async only at the IO edges.
- **"Domain events in a monolith"** — I marked these as overkill outside specific cases (C7). Cosmic Python builds toward them; some find them load-bearing for testability. I think for AlphaMind's pipeline shape (sequential, single-writer at each stage) they'd be ceremony.
- **Strictness of Pydantic-vs-dataclass split** — I took a hard line (D2). The softer line is "Pydantic everywhere, accept the perf cost for one consistent type system." I think the hard line is worth the discipline; the softer line is defensible.

---

## N. What's deliberately *not* in scope

- Specific package recommendations ("use httpx not requests") — that's library-selection, not architecture. The skill should make architectural recommendations and let library choices follow.
- Project-management practices — branching strategy, code review, ADRs. Out of scope.
- Performance optimisation — profile first, then optimise; the skill should flag obvious antipatterns (loops over pandas rows) but not chase micro-optimisations.
- Security architecture — auth, authz, secrets handling beyond H2. Adjacent skill.
- Specific deployment / IaC patterns — adjacent skill.

---

## O. How the skill should use these theses (sketch — for discussion)

Two modes, both grounded in the same theses:

**Audit mode.** Given a Python codebase, the agent:
1. Lists the top-level package structure and inspects `pyproject.toml`.
2. Runs `import-linter` (or its own import-graph analysis) to find layer violations.
3. Walks each top-level package, checking against the antipattern list (L) and the data / errors / concurrency theses (D, F, G).
4. Produces findings grouped by severity, each citing the relevant thesis and a one-liner fix.

**Design mode.** Given a problem description, the agent:
1. Asks 3–5 clarifying questions to fix the application shape (K1–K7), the trust boundaries, and the persistence / concurrency profile.
2. Sketches a feature-organised package layout (B3) with explicit boundary definitions.
3. Names the load-bearing domain primitives (D3) and the boundary types (D2).
4. Specifies the testing seam (J2) — what gets faked, what's exercised.
5. Calls out the three or four design decisions that would be hardest to reverse later, and the tradeoffs around each.

The skill itself should be opinionated — but it should also report which theses it relied on for each finding / recommendation, so the user can disagree with a thesis and see all the downstream changes.
