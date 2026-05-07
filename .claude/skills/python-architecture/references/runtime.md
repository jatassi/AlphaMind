# Runtime — errors, concurrency, configuration, observability

The four runtime concerns are bundled because they're co-cited often and they share a common spine: **explicit, structured, supervised**. Errors that can be caught and re-raised with context; concurrency that can be cancelled and joined; config that can be validated and reloaded; logs that can be queried.

---

## §F. Errors

### §F1. Catch narrow, re-raise with `from`

**Claim.** Never `except Exception:` (or worse, bare `except:`) outside the outermost supervisor — the CLI handler, the request handler, the task supervisor. Catch the specific class you actually handle. When wrapping for context, use `raise DomainError("couldn't allocate stock") from e` so the chain is preserved.

**Rationale.** Broad `except` clauses swallow bugs the author didn't anticipate. The chain-preserving `from` clause turns "an error happened somewhere" into "this happened because that happened" — visible in the traceback, debuggable in production.

**Attribution.** PEP 3134 (Ka-Ping Yee, exception chaining). Brett Cannon on exception hygiene.

**When it breaks.** The outermost supervisor of a long-running process legitimately catches `Exception` to log and continue (without it the process dies). Mark it with a comment.

**Audit signal.** `except Exception:` outside the top-level supervisor. `raise X` inside an `except Y:` block without `from`. Catching exceptions to drive control flow (`except KeyError` to detect missing dict key — use `.get` or `in`).

**Design signal.** Define your domain exceptions next to the operations that raise them. Translate library exceptions to domain exceptions at the adapter layer.

### §F2. Domain exceptions for expected boundary failures

**Claim.** Define your own `OrderRejected`, `GuardrailBreach`, `VendorRateLimited`. They live at the boundary they represent. Library exceptions (`httpx.TimeoutException`) get caught and translated at the adapter layer — domain code doesn't depend on `httpx`.

**Rationale.** The exception type is part of the API. When domain code catches `httpx.TimeoutException`, the entire dependency tree below knows about httpx. Translating at the adapter (`except httpx.TimeoutException as e: raise VendorUnavailable(...) from e`) keeps the domain dependency-free.

**Attribution.** Cosmic Python on adapter-layer translation. Robert C. Martin on dependency direction.

**When it breaks.** When the domain genuinely is the library (a thin shell over httpx is fine to expose httpx errors). Same caveat as §C1.

**Audit signal.** Domain modules importing exception types from third-party libraries. `try` blocks in domain code catching library-specific exceptions.

**Design signal.** Each adapter defines and translates exceptions for its boundary. Domain code only sees domain exceptions.

### §F3. Result / Either types are not Pythonic — use sparingly

**Claim.** Rust-style `Result[T, E]` (`returns`, `result`) reads cleanly in Rust, awkwardly in Python. Use them only at narrow seams where (a) the failure is fully expected (validation, parsing) and (b) callers must explicitly handle both branches. For everything else, exceptions with clear types are more idiomatic and cheaper.

**Rationale.** Python's exception machinery is well-supported by every library, IDE, and debugger. Rust-style Results require pattern-matching ceremony that fights the language. Where they earn their keep is at boundaries where you want callers to *be forced* to handle both outcomes — typically a narrow ingest path, or LLM tool outputs where "tool said no" is a normal control-flow case.

**Attribution.** Position taken in this skill, against an aggressive Result-everywhere stance from one strain of functional Python practice. The conservative position aligns with PEP authors and CPython core devs.

**When it breaks.** LLM-driven systems where every tool call has expected failure modes the controller must inspect. There leaning into Result-like ADTs (a `ToolResult` union of success and named failures) is correct.

**Audit signal.** `returns.result` or `result` import in many modules — the pattern is being used as a global discipline rather than a local seam.

**Design signal.** Default to exceptions. Reach for Result-like types at narrow seams where the caller genuinely must inspect the outcome.

### §F4. Idempotency is a precondition for retry

**Claim.** Tenacity, backoff, `httpx-retries` are fine *if* the retried operation is idempotent. Retrying a non-idempotent POST is a bug factory. Either make the operation idempotent (idempotency keys, content-addressed writes, upserts) or don't retry.

**Rationale.** Naive retry on a non-idempotent write produces double-charges, double-orders, double-fills — the classic distributed-systems bug. Idempotency keys (a client-generated unique key sent with the request) are the standard fix on the application side; upserts and content-addressed storage are the fix on the persistence side.

**Attribution.** Stripe's idempotency-key pattern (canonical reference). Pat Helland's "Life beyond distributed transactions" and "Idempotence is not a medical condition".

**When it breaks.** Genuinely safe-to-retry operations (GETs, idempotent PUTs, reads) need no idempotency key. Don't add ceremony.

**Audit signal.** `@retry` decorator on a function that does a non-idempotent write with no idempotency key. Retry-on-everything decorators applied as a default.

**Design signal.** Mark each operation as idempotent or not at design time. Retries are wired only to operations marked idempotent. For non-idempotent operations that need retry, design in idempotency keys from the start.

### §F5. Every external call has a timeout

**Claim.** Every. `httpx` calls, DB queries, subprocess calls, file reads from network mounts, broker API calls, LLM API calls. No timeout = potential infinite hang. Use `asyncio.timeout` (3.11+) for async, library-level timeouts for sync.

**Rationale.** Default timeouts vary by library and are sometimes infinity. A hung external call ties up resources (connections, threads, async tasks) until the process restarts. The cost of adding a timeout is one parameter; the cost of missing one is observable downtime.

**Attribution.** SRE / production-engineering consensus. `httpx` ships with a default timeout for exactly this reason; `requests` did not, and produced a generation of horror stories.

**When it breaks.** Operations that legitimately may take arbitrarily long (a streaming query, an interactive subprocess). There the timeout is "no progress for N seconds", not "complete in N seconds".

**Audit signal.** `httpx` / `requests` / `aiohttp` calls without `timeout=`. DB sessions without statement timeouts. `subprocess.run` without `timeout=`. `await some_io()` not wrapped in `asyncio.timeout`.

**Design signal.** Default timeouts are a settings concern; pick reasonable values per call category (fast: 5s, normal: 30s, slow: minutes), document them.

### §F6. Exponential backoff with jitter; circuit breaker only when load-shed matters

**Claim.** Default retry policy: exponential backoff (`base * 2^attempt`) with full jitter (`random.uniform(0, base * 2^attempt)`). Circuit breakers are worth the complexity only when you have enough RPS that fast-failing rather than retrying meaningfully reduces load.

**Rationale.** Exponential backoff prevents thundering-herd retry storms when an upstream recovers. Jitter spreads retry timing so clients don't synchronise. Circuit breakers are a different mechanism for a different problem — they protect a downed service from being overloaded by retries during recovery, which only matters at scale.

**Attribution.** AWS Architecture Blog, "Exponential Backoff and Jitter". Michael Nygard, *Release It!* (the canonical circuit-breaker reference).

**When it breaks.** Low-RPS systems (a few req/min) don't need circuit breakers. Genuinely fast-recovering services may not need backoff. Pick deliberately.

**Audit signal.** Linear-delay retries (`time.sleep(1)` in a retry loop). Same retry interval across all attempts. Circuit breakers in low-RPS systems where they add ceremony without benefit.

**Design signal.** Exponential + jitter is the default. Circuit breakers only when the load-shed argument is real.

---

## §G. Concurrency

### §G1. Async only at the I/O boundary

**Claim.** Async earns its keep when there are concurrent I/O operations and nowhere else. CPU-bound code gets nothing from async; sequential code gets nothing; "everything async because the framework is async" is overhead.

**Rationale.** Async colours functions — once `async def`, callers must `await`, and the colouring spreads. The benefit (concurrent I/O via cooperative multitasking) only appears when there are multiple I/O operations that can overlap. Without that, async is overhead: more complex code, harder debugging, more constraints on what libraries you can use.

**Attribution.** Glyph Lefkowitz, "Unyielding" and other async-cost essays. Bob Nystrom, "What color is your function?" (the original framing). David Beazley on the cost of async.

**When it breaks.** Genuinely I/O-concurrent systems (web frameworks, websocket-heavy services, parallel-API-call systems like AlphaMind's analysis layer). There async-everywhere is correct.

**Audit signal.** `async def` functions that don't `await` anything. `async def` for CPU-bound work. Heavy use of `asyncio.run` to bridge sync code to async libraries when the sync code itself doesn't need async.

**Design signal.** Identify where I/O concurrency is real. Async there. Sync everywhere else, with `asyncio.run` only at the entrypoint that needs it.

### §G2. Structured concurrency — `asyncio.TaskGroup` (3.11+) or never

**Claim.** `create_task` and forget = guaranteed silent bug. Tasks must have a known parent and be awaited. Use `async with asyncio.TaskGroup() as tg: tg.create_task(...)`. Cancellation propagates correctly, exceptions surface, no orphan tasks.

**Rationale.** A bare `asyncio.create_task(coro)` produces a task whose lifetime is unbounded. If it raises an exception, the exception is logged at task-collection time (often after the original code path has moved on) and not propagated. If the parent is cancelled, the task isn't. Structured concurrency makes the parent responsible for the children — exceptions surface, cancellation propagates, no leaks.

**Attribution.** Nathaniel J. Smith, "Notes on structured concurrency, or: Go statement considered harmful" (vorpus.org). Trio (Smith's library) was the first Python implementation; `asyncio.TaskGroup` brought it into stdlib.

**When it breaks.** Truly fire-and-forget background work where exceptions and cancellation genuinely don't matter (a metric increment, a log write). Even there, supervised is safer.

**Audit signal.** `asyncio.create_task(...)` not inside a `TaskGroup` context. `asyncio.gather(...)` (the older pattern; works but less safe — exceptions don't always propagate cleanly). Background-task registries hand-rolled.

**Design signal.** Every async task lives inside a `TaskGroup`. The group's lifetime is the parent's lifetime. Library-level: provide `TaskGroup`-friendly APIs.

### §G3. Reuse a single `httpx.AsyncClient` per process / lifespan

**Claim.** Connection-pool reuse is a 10× perf delta. Build the client in a lifespan context manager, close it on shutdown. Same for DB engines, broker clients, anything with a connection pool.

**Rationale.** Per-request `AsyncClient` creation throws away the TCP connection pool, the TLS session cache, and the HTTP/2 multiplexing state. Reuse keeps them. This is one of the most common "my service is slow" findings.

**Attribution.** httpx documentation on the `Client` pattern. Tom Christie (httpx author) on connection reuse.

**When it breaks.** Short-lived scripts (the connection cost is paid once). Tests that need isolation between cases (use a per-test client).

**Audit signal.** `async with httpx.AsyncClient() as client:` inside a per-request handler. `httpx.get(...)` (the module-level convenience function) in production code.

**Design signal.** Single client per process, owned by the lifespan / app factory. Inject it where needed.

### §G4. Cancellation safety: `try/finally` and context managers

**Claim.** Anything holding a resource (file, connection, lock) goes inside an `async with` or has its cleanup in a `try / finally`. `asyncio.shield` is for durability (flush logs before cancellation propagates), not for "I don't want to think about cancellation."

**Rationale.** Cancellation is propagated by raising `asyncio.CancelledError` at the next `await` point. If a task is holding a resource and gets cancelled mid-operation, `finally` is what runs. Code without `try/finally` or context managers leaks resources on cancellation.

**Attribution.** Nathaniel J. Smith on cancellation safety. Python asyncio docs on `CancelledError`.

**When it breaks.** Code that genuinely cannot be safely cancelled mid-flight (committing a half-written file). Use `asyncio.shield` deliberately for those cases, with a comment.

**Audit signal.** `await` between `open(...)` and `f.close()`. Connection cleanup outside a `finally`. `asyncio.shield` without an explanatory comment.

**Design signal.** Default to `async with`. Use `try/finally` where context managers don't fit. Reserve `shield` for the durability case.

### §G5. `multiprocessing` for CPU-bound, threads for "blocking C extension you can't avoid"

**Claim.** Numpy / pandas / sklearn work that doesn't release the GIL: `ProcessPoolExecutor`. Calls into C libs that *do* release the GIL: threads are fine. Most code: neither — `asyncio.to_thread` for occasional blocking calls is enough.

**Rationale.** The GIL is the constraint. Threads don't help CPU-bound Python code; they do help GIL-releasing C extensions. Processes help CPU-bound Python code at the cost of pickling (and the inability to share state via memory). The free-threaded build (3.13+, opt-in) changes this calculus, but most code shouldn't depend on it yet.

**Attribution.** David Beazley on the GIL. CPython core devs on the free-threaded build. Standard lib documentation.

**When it breaks.** Scaling-out scientific code may benefit from multiprocessing even when GIL-released; pure-Python multiprocessing has overhead that may exceed the speedup for small workloads.

**Audit signal.** `threading.Thread` for CPU-bound Python work (no speedup). `multiprocessing` for code that's mostly I/O (use async). `asyncio.to_thread` calls that wrap CPU-bound work (use a process executor).

**Design signal.** Identify the workload type before choosing. CPU-bound Python → processes. GIL-releasing C → threads. Most everything else → async or sync.

---

## §H. Configuration and secrets

### §H1. `pydantic-settings` + environment variables

**Claim.** Hierarchy: defaults in code → file (TOML/YAML) → environment → secrets manager. `pydantic-settings.BaseSettings` validates and coerces. Twelve-factor for the deployable parts.

**Rationale.** A typed, validated settings object beats `os.environ.get("FOO", "default")` scattered through the codebase. Pydantic-settings reads from env, dotenv, and files, with type coercion and validation. The boundary is clear: untrusted env strings on one side, typed `Settings` instance on the other.

**Attribution.** The Twelve-Factor App (12factor.net) on env-based config. Pydantic-settings docs. Ronacher on the "config object" pattern.

**When it breaks.** Trivial scripts (one env var). Use `os.environ` directly.

**Audit signal.** `os.environ.get(...)` calls scattered through code. Config loaded ad hoc per module. Untyped config dictionaries.

**Design signal.** One `Settings(BaseSettings)` class. Pass it (or fields from it) explicitly. Load once at the entrypoint.

### §H2. Secrets only via env vars or a secrets manager

**Claim.** Never in `pyproject.toml`, never in committed files, never in YAML. `.env` files are local-only and gitignored. For prod: secrets manager (AWS Secrets Manager, HashiCorp Vault, 1Password CLI, OS keychain).

**Rationale.** Committed secrets are a recurring cause of credential compromise. The attack surface is the entire git history of every clone. Env vars and secrets managers keep secrets out of source control.

**Attribution.** Twelve-Factor App. OWASP on credential management.

**When it breaks.** Test secrets that aren't real secrets (a fake API key for an integration test) can live in fixture files. Mark them clearly.

**Audit signal.** API keys, passwords, tokens in committed files. `.env` files in git. Hardcoded credentials in code.

**Design signal.** Secrets are pulled from env or a secrets manager at startup. The settings class names them; the values are populated by the environment.

### §H3. Config is data; behaviour lives in code

**Claim.** A 500-line YAML file with `if env == "prod"` conditionals is code in disguise. If config needs branching, it's a feature flag system, and feature flags belong in code with a typed flag registry, not in YAML.

**Rationale.** Config-as-code (in YAML) loses type checking, IDE support, and refactoring tools. Code-as-code keeps them. The line is: data values that change per environment go in config; logic that decides what to do based on environment goes in code.

**Attribution.** The general "configuration is code" antipattern critique. Glyph on the cost of dynamic configuration.

**When it breaks.** Genuine cross-cutting feature flags that need to be toggled at runtime without a deploy. There a flag service is the right answer; YAML is still wrong.

**Audit signal.** Conditionals on environment variables inside YAML / TOML. Config files with logic-shaped keys (`"if_user_is_admin"`).

**Design signal.** Config carries values. Code carries decisions.

---

## §I. Observability

### §I1. Structured logging from day one with `structlog`

**Claim.** Free-text logs are a debt instrument that compounds. `structlog` produces dict-shaped events that flow into Logfire / Datadog / Honeycomb / a SQL audit table cleanly. Bind context once (`structlog.contextvars.bind_contextvars(request_id=...)`); every subsequent log line carries it.

**Rationale.** Free-text logs are unsearchable, unaggregatable, and duplicate across formats. Structured logs produce a uniform shape that downstream tools can index, query, and alert on. The cost of starting structured is one library; the cost of retrofitting is rewriting every log line in the codebase.

**Attribution.** Hynek Schlawack (structlog author). Charity Majors on observability. Datadog / Honeycomb / OpenTelemetry on structured event logs.

**When it breaks.** Quick scripts that print to stderr for a human are fine with `print` or basic `logging`. The thesis applies to systems that produce logs anyone has to query.

**Audit signal.** `logging.info(f"user {user_id} did {thing}")` — free-text where `log.info("user.action", user_id=user_id, action=thing)` is correct. Multiple log formats in the same project. No structured-logging library.

**Design signal.** `structlog` configured at startup. Bound context (request ID, run ID, agent ID) at the entrypoint. Every log line is a dict event with named keys.

### §I2. Log levels mean specific things

**Claim.** `DEBUG` — variable-level detail, off in production. `INFO` — milestone events of normal operation. `WARNING` — unexpected but not a failure. `ERROR` — operation failed; humans may need to act. `CRITICAL` — process-level invariant broken; alert.

**Rationale.** When levels are interchangeable, alerting becomes impossible. The team can't tell whether `WARNING` is "this happens 1000x/min and is fine" or "this happens once and we should look". Discipline at definition time prevents this.

**Attribution.** Standard lib `logging` documentation, taken seriously. SRE consensus on level semantics.

**When it breaks.** No real exception. Internal tools may collapse INFO/WARNING; production systems should not.

**Audit signal.** `WARNING` for routine events. `INFO` for failures. `ERROR` without an actionable signal. Levels chosen by mood rather than meaning.

**Design signal.** Document level meanings in the project. Apply them consistently. Alert on `ERROR` and `CRITICAL`; tolerate `WARNING`; ignore `INFO` in alerting.

### §I3. Logs ≠ metrics ≠ traces

**Claim.** Logs: discrete events. Metrics: aggregates for alerting (Prometheus / OTel metrics). Traces: causality across service boundaries (OTel traces). For single-process systems: logs do most work, metrics for alerts, traces for the rare cross-service question. For distributed systems, invert: traces become primary.

**Rationale.** Each tool answers a different question. Logs answer "what happened?". Metrics answer "how much / how often?". Traces answer "what called what, and what was the latency?". Treating them as interchangeable produces high-cardinality metrics, log-as-trace systems that lose causality, and unalertable systems.

**Attribution.** Charity Majors, "Observability Engineering". OpenTelemetry's three-pillar framing.

**When it breaks.** Systems where one of the three genuinely doesn't apply (a single-process script needs no traces; a stateless function may need no logs).

**Audit signal.** Metrics being read for individual events (high cardinality). Logs being summed for alerting (use metrics). Traces in a single-process system that doesn't have causality questions.

**Design signal.** Pick the right tool per question. Single-process systems start with logs and metrics; add traces only when distributed.

### §I4. "Wide events" framing

**Claim.** One log line per significant operation, with all context attached, beats ten thin log lines per operation. Structured-logging makes wide events natural; cardinality is good when each event has a request ID.

**Rationale.** Thin log lines (`"started"`, `"loaded user"`, `"called service"`, `"finished"`) are aggregate noise when separated, and re-stitching them in the analytics tool is painful. One wide event per operation, with `duration`, `user_id`, `outcome`, `error_kind`, `bytes`, etc., is queryable and analysable directly.

**Attribution.** Charity Majors and the Honeycomb team. The OTel "span events" pattern.

**When it breaks.** Long-running operations with progress milestones genuinely deserve multiple log lines. Mark them with the same span / trace ID.

**Audit signal.** Log statements for entry / exit / "starting" / "finished" with no payload. Log fanout where one operation produces dozens of correlated lines.

**Design signal.** Per significant operation, build a wide event during the operation and log it once at the end with the full context.

---

## Cross-references

- The principle of "core pure / shell impure" that underlies most of these (§F1 narrow exceptions, §G1 async at the boundary, §H3 config is data) is `foundations.md §A1`.
- Antipatterns in this dimension (`except Exception`, `create_task` orphans, free-text logs, hardcoded secrets, missing timeouts): `antipatterns.md`.
