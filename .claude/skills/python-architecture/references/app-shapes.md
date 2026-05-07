# Application shapes

The architecture should fit the shape of the system. This reference is the "what kind of system is this?" pass — identify the shape, then apply the shape-specific theses on top of the cross-cutting principles. Some antipatterns are antipatterns only in a particular shape; what's correct for a CLI is wrong for a long-running daemon.

For each shape: typical traits, framework / library defaults, and shape-specific theses.

---

## §K1. CLI applications

**Traits.** Invocation-driven, short-lived, stdin/stdout/stderr I/O, exit codes, often wraps a library API.

**Defaults.** **Typer** over Click over argparse. Typer earns its keep through type-hint-driven argument parsing — `def cmd(name: str, count: int = 1):` and the parser is generated. Click is fine and widely-known; argparse only when you cannot add a dependency.

**§K1.1. The CLI is a thin wrapper around a library API.**
The same code should be callable in-process from tests and notebooks. Don't put logic in the Click / Typer command function; have it call into a regular function defined elsewhere. The command function does argument parsing, the library function does the work.

**§K1.2. Exit codes mean specific things.**
`0` ok, `1` user error (bad input, file not found), `2` system error (network down, can't write to disk), other codes for domain-specific failure modes if you have them. Reserve them deliberately and document them.

**§K1.3. Stdout is data; stderr is logs.**
Tools that pipe to other tools depend on this. Don't print log lines to stdout; they pollute pipelines. Use stderr for human-readable progress, stdout for machine-readable output.

**§K1.4. Don't print structured logs to stdout.**
Even when the CLI writes JSON to stdout, the JSON is the output, not log lines. Logs go to stderr or a log file.

**Audit signal.** Click commands that do non-trivial work directly in the function body. Mixed-format output (logs and data on the same stream). Implicit exit codes (no `sys.exit`, returning from main).

**Design signal.** Define the library API first. The CLI is a separate module that imports and orchestrates.

---

## §K2. Web applications and APIs

**Traits.** Long-running, HTTP-fronted, request-response (or websocket), framework-driven.

**Defaults.** **FastAPI** for new APIs (Pydantic at boundary, type-driven, async-native). **Django** for content/admin-heavy monoliths where the framework's batteries pay off. **Flask** only for legacy.

**§K2.1. Pydantic at the boundary; frozen dataclasses inside.**
The HTTP request → Pydantic model → conversion → frozen dataclass → domain function. The reverse for responses. The framework is a detail; the domain doesn't import it.

**§K2.2. Background tasks run in a TaskGroup, not via the framework's "background_task" feature.**
FastAPI's `BackgroundTasks` is fine for fire-and-forget after the response is sent (analytics, emails). For anything you need to be sure ran, structured concurrency (§G2). For long-running jobs that outlive a request, an actual job queue (Celery, RQ, Arq, or a hand-rolled `asyncio.Queue` with a TaskGroup supervisor).

**§K2.3. The framework is a detail.**
Business logic lives in functions and classes that don't import FastAPI. Routes are thin (parse → call domain → format response). Routes can change framework without touching the domain.

**§K2.4. `Depends()` is the legitimate exception to no-DI-containers (§C4).**
FastAPI's `Depends()` is syntactic, not runtime. The type signature still tells the truth. Use it freely.

**Audit signal.** Business logic in route handlers. Pydantic models flowing through internal code (§D2). Framework imports in domain modules. `BackgroundTasks` for work that needs to be guaranteed to run.

**Design signal.** Routes are thin. The composition root (`main.py` / app factory) wires up dependencies. Pydantic at the boundary; frozen dataclasses inside.

---

## §K3. Data pipelines / ETL / batch processing

**Traits.** Stage-oriented (ingest → transform → load), often run on a schedule, idempotency matters, schemas evolve.

**Defaults.** **Roll your own** until ~5 stages with non-trivial dependencies. **Prefect** or **Dagster** when DAG complexity exceeds what's clearly readable in code. Airflow only when forced by org infra.

**§K3.1. Stages are pure functions over typed inputs.**
Each stage takes a typed input, returns a typed output. No I/O inside the stage; I/O at the orchestration layer (or at clearly-named adapter boundaries). This is §A1 (functional core, imperative shell) applied to pipelines.

**§K3.2. Idempotency by stage.**
Re-running stage 4 after a crash should produce the same result. Implement via content-addressed writes, upserts, or staged outputs. Without idempotency, retries corrupt state.

**§K3.3. Checkpointing by stage.**
Save the output of each stage to durable storage. The next stage reads from there, not from the previous stage's in-memory result. Recovery from a crash is "re-run from the last checkpoint".

**§K3.4. Schema-on-write at the landing zone.**
Validate vendor data into a typed table before downstream stages touch it. Catch schema drift at ingest, not three stages downstream when a calculation produces wrong numbers. Pydantic at the landing zone is canonical.

**§K3.5. Don't reach for Airflow / Dagster / Prefect prematurely.**
A 10-stage pipeline written in Python with explicit `def run_pipeline(): step1(); step2(); ...` plus checkpointing is often clearer than the same pipeline expressed as a DAG framework's primitives. Reach for the framework when you genuinely need its features (cron-like scheduling, retries, observability dashboards, parallel fan-out).

**Audit signal.** Stages with hidden I/O (a "transform" stage that calls the database). No checkpointing — a crash mid-pipeline forces re-running from the start. No schema validation at ingest. Premature Airflow.

**Design signal.** Type the data between stages. Checkpoint between stages. Validate at the landing zone. Choose the orchestrator for the actual complexity, not the imagined complexity.

---

## §K4. Long-running services / daemons

**Traits.** Always-on, supervised by an OS-level process manager (systemd, NSSM, Docker), lifecycle-aware, signal-handling.

**Defaults.** **systemd** on Linux, **NSSM** on Windows, **Docker** in containers. Avoid supervisord (unmaintained); avoid rolling-your-own.

**§K4.1. Lifespan context manager owns startup / shutdown.**
Open clients (httpx, DB, broker), register signal handlers, supervise background TaskGroups, all in a single `async with lifespan():` block. On shutdown: cancel the TaskGroup, close clients, flush logs. Frameworks like FastAPI and Litestar provide a lifespan hook; for hand-rolled daemons, write one.

**§K4.2. SIGTERM is graceful shutdown; SIGKILL is the OS giving up.**
Catch SIGTERM (and SIGINT in dev), trigger graceful shutdown, complete in-flight work or save state, exit cleanly. The supervisor sends SIGTERM first, waits a configured timeout, then SIGKILL. Aim to be done before SIGKILL.

**§K4.3. Health endpoints are liveness + readiness, separately.**
Liveness: "is the process alive?". Readiness: "can it accept work?". A service might be alive but not ready (still loading state, waiting for a downstream). Distinguishing them lets the supervisor distinguish "restart" from "wait".

**§K4.4. Hot reload only via process supervisor.**
The supervisor restarts the process on config change. Don't roll your own hot-reload inside the process — too many ways to leak state. The exception is dev mode (`uvicorn --reload`), which is a different program.

**Audit signal.** Daemons without a signal handler. Daemons whose "shutdown" is `sys.exit(0)` mid-work. Background tasks not in a TaskGroup. Health endpoints that always return 200 regardless of state.

**Design signal.** Lifespan context manager from day one. Signal handlers wired in startup. Health endpoints that reflect real state.

---

## §K5. Agentic / LLM applications

**Traits.** Tool-using, multi-turn, expensive per call, non-deterministic, evolving capabilities.

**§K5.1. Treat the LLM as a slow, expensive, fallible function.**
Per-call latency is seconds to tens of seconds. Cost per call is real. Failure modes include: refusal, malformed output, hallucination, rate limit, timeout. The architecture must accommodate all of these as routine.

**§K5.2. Tools are the public API of a capability.**
What the LLM can do is the set of tools you've defined. Each tool's schema is a contract. Treat tool definitions with the same rigor as any other public API: typed parameters, validated inputs, named errors, observable outcomes. The tool's signature is the contract; structure your code so the side effects each tool can have are explicit.

**§K5.3. Replay-able prompts.**
Every LLM call is logged with full input + output + model + version. The system can be replayed from logs. This is critical for debugging (why did the agent decide X?), evaluation (did the new prompt change behaviour?), and post-mortem analysis (what went wrong on run #847?).

**§K5.4. Determinism in the shell.**
Everything around the LLM is deterministic — clock, randomness, IDs, ordering (§J6). LLM non-determinism is the only thing varying, which makes evaluations tractable: rerun the same input under different prompts, compare outputs.

**§K5.5. State machines over implicit booleans.**
Multi-step agentic workflows have states (`pending` → `running` → `completed` / `failed` / `cancelled`) with explicit transitions. Each transition is logged. Don't model state with booleans (`is_running`, `is_failed`); model it with a `Status` enum and a transition function.

**§K5.6. Treat tool outputs as untrusted input.**
A tool whose output is consumed by the LLM — and possibly by downstream code that the LLM directs — is a boundary. Validate the tool output's shape; sanity-check ranges; refuse on out-of-spec.

**Audit signal.** LLM calls without full logging. Booleans where a status enum should be. Tools without typed contracts. Side effects from tool calls that aren't visible in the tool's signature. Non-deterministic shell (random, time, ordering not injected).

**Design signal.** Define the tools first. Define the state machine first. Determinism in the shell. Logging of every LLM call.

---

## §K6. Numerical / scientific

**Traits.** Heavy numpy / pandas / sklearn use, performance-sensitive, often notebook-developed before being productionised.

**§K6.1. Vectorise; loops over rows in pandas are an antipattern.**
Per-row Python loops in pandas (`for i, row in df.iterrows()`) are 10–1000× slower than the vectorised equivalent. Vectorise (`df["x"] = df["a"] * df["b"]`); use `.apply` only when there's no vectorised form.

**§K6.2. Polars over pandas for >1M rows.**
Polars is built around lazy evaluation and a query optimiser; pandas is eager and row-shaped. For large data, Polars is faster, more memory-efficient, and has better defaults (no automatic type coercion, explicit nulls). For small data and existing codebases, pandas is fine.

**§K6.3. Leave Python only after profiling.**
"Python is slow" is a vibe; "this `for` loop in this function is slow because the profiler shows it" is data. Cython, Numba, Rust via PyO3, C extensions — all real options once profiling proves a hot path. Don't pre-optimise.

**§K6.4. Notebooks are scratchpads, not production code.**
Code that's live in a notebook gets refactored into a module before it ships. The notebook can import from the module and demonstrate; the module is what runs in production.

**Audit signal.** `.iterrows()` or row-by-row Python in pandas. Untyped DataFrames flowing through the system (no pandera, no schema). Notebook code copy-pasted into production with no refactor. Cython / Numba added without profiling data.

**Design signal.** Vectorise from the start. Schema the DataFrames. Notebooks for exploration; modules for code.

---

## §K7. Database schema migrations

Migrations are a cross-cutting concern, not a shape per se, but they have shape-like character: they have their own tooling, their own pitfalls, and they affect every other shape that uses persistence.

**§K7.1. Alembic.**
The de facto standard. Auto-generation is a starting point, not the answer; review the generated migration before committing.

**§K7.2. Expand-contract for any production system.**
Never rename a column in one migration. The pattern is: add the new column → backfill → cut over reads → cut over writes → drop the old column, across multiple deploys. Each step is independently revertable.

**§K7.3. Test migrations both directions.**
`alembic upgrade head` and `alembic downgrade -1` should both work, on real data. Migrations that can't be reverted are operational risk.

**§K7.4. Lock to a single migration head.**
Multi-head merges happen when two branches both add migrations. Resolve them deliberately; don't let the multi-head state persist.

**Audit signal.** Migrations that rename in one step. Migrations that don't have a downgrade. Multi-head migration state in the repo. Auto-generated migrations committed without review.

**Design signal.** Migration discipline from day one. Document the expand-contract pattern in a `docs/migrations.md`. Test migrations as part of CI.

---

## Cross-references

- Most cross-cutting principles (foundations, data-and-types, runtime, testing) apply across all shapes.
- Shape-specific antipatterns (CRUD apps over-applying hexagonal, agentic systems with non-deterministic shells): `antipatterns.md`.
