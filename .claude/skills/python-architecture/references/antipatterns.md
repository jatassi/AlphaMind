# Antipatterns — audit checklist

The high-yield findings. These are the things to flag in audit mode with high confidence: each one is a known smell with a known fix, and finding any of them produces a useful audit observation.

Each entry: **what to grep for**, **why it's wrong**, **fix in one line**, **principle / reference link**.

---

## L1. God modules

**Grep for.** Top-level modules named `utils`, `helpers`, `common`, `misc`, `tools`, `lib`, `core` (when not load-bearing) with >300 lines or >5 unrelated functions.

**Why wrong.** "Utils" is a name that hides cohesion. The module accumulates whatever doesn't fit elsewhere; over time it becomes a coupling point that everything imports.

**Fix.** Split by what the contents actually do — name modules after the *concept* they handle (`text_normalisation`, `retry_policy`, `id_generation`), not their architectural role.

**Principle.** §A4 (cohesion over hierarchy), §C5 (public API discipline).

---

## L2. Side effects at import time

**Grep for.** Network calls, file writes, mutable globals being populated, `print` statements, logger configuration, environment-variable reads outside settings classes — all at module top level.

**Why wrong.** Imports must be pure. Side effects at import break testability (every import runs the side effect), break tooling (linters, type checkers, doc generators all import the module), and create import-order dependencies.

**Fix.** Move side-effecting code into functions. Run them from the entrypoint, not from import.

**Principle.** §A1 (functional core), §A2 (import graph).

---

## L3. Mutable default arguments

**Grep for.** `def f(x: list = [])`, `def f(x: dict = {})`, `def f(x: set = set())`.

**Why wrong.** The default is created once, at function-definition time, and shared across all calls. Mutating the default mutates the shared instance.

**Fix.** `def f(x: list | None = None): if x is None: x = []`.

**Principle.** Standard Python footgun; PEP 8 cousin.

---

## L4. Bare `except:` or `except Exception:` outside the outermost supervisor

**Grep for.** `except:` (bare). `except Exception:` outside top-level CLI / request / task handlers.

**Why wrong.** Swallows bugs the author didn't anticipate. Hides real problems. Makes debugging "why didn't my code do anything?" much harder.

**Fix.** Catch the specific exception class you handle. Re-raise others.

**Principle.** §F1.

---

## L5. `from x import *`

**Grep for.** `from anything import *` in production code (test code may legitimately use it for fixtures).

**Why wrong.** Pollutes the namespace, hides where symbols come from, breaks tooling, can shadow existing names silently.

**Fix.** `from x import specific_thing, other_thing`.

**Principle.** PEP 8; §A2 (the import graph as architecture).

---

## L6. Late imports as a circular-import workaround

**Grep for.** `import` statements inside function bodies whose only justification is "if I import at module level, I get a circular import".

**Why wrong.** Late imports paper over a structural problem. The cycle reflects a genuine architectural cycle (module A depends on B, B depends on A) that the late import hides.

**Fix.** Refactor to break the cycle — extract the shared code, invert one of the dependencies, or merge the modules.

**Principle.** §A2 (import graph), §C1 (boundaries).

---

## L7. Catching exceptions to drive control flow

**Grep for.** `try: x = d[k]; except KeyError: x = default` (use `.get`). `try: ...; except AttributeError: ...` (use `getattr` or `hasattr`). `try: int(s); except ValueError: ...` for routine validation.

**Why wrong.** Exceptions are for exceptional cases. Using them for control flow makes the happy path unclear, slows down the slow path (exception unwinding), and conflates "this routinely doesn't apply" with "something went wrong".

**Fix.** Use the language feature designed for the case (`.get`, `in`, explicit validation).

**Principle.** §F1.

---

## L8. Mutable dataclass without justification

**Grep for.** `@dataclass class X:` with no `frozen=True`. `@attrs.define class X:` with no `frozen=True`.

**Why wrong.** Mutability is shared state by another name. Most value-shaped types should be immutable; mutability should be a deliberate choice.

**Fix.** `@dataclass(frozen=True, slots=True)` (or `attrs.frozen`).

**Principle.** §D1.

---

## L9. Pydantic `BaseModel` for an internal domain object

**Grep for.** `BaseModel` subclasses in modules that aren't named or located like boundary modules (`*/api/*`, `*/config/*`, `*/schemas/*`, `*/io/*`, `*/adapters/*`).

**Why wrong.** Pydantic is for trust boundaries. Internal types don't need (and pay for) every-instantiation validation. Coupling internal code to Pydantic also makes the boundary disappear.

**Fix.** Convert to `@dataclass(frozen=True, slots=True)`. Keep Pydantic at the boundary only.

**Principle.** §D2.

---

## L10. Bare `str` for status / kind / type fields

**Grep for.** `status: str` parameters whose docstring lists valid values. `if status == "submitted":` repeated across files. `def transition(from_state: str, to_state: str)`.

**Why wrong.** Typos become silent bugs. Refactoring renames is dangerous. Exhaustiveness of `if`/`match` is unverifiable.

**Fix.** `class Status(StrEnum):` or `Status = Literal["submitted", "filled", ...]`.

**Principle.** §D4.

---

## L11. Naive `datetime.now()`

**Grep for.** `datetime.now()` (no `tz=` argument). `datetime.utcnow()` (deprecated, returns naive). `datetime(2024, 1, 1)` without `tzinfo`.

**Why wrong.** Naive datetimes are timezone-ambiguous and produce timezone bugs that surface only at the daylight-savings transition.

**Fix.** `datetime.now(timezone.utc)`. For testable code, inject a `Clock` (§J6).

**Principle.** §D3.

---

## L12. `float` for monetary amounts

**Grep for.** `price: float`, `amount: float`, `total: float`, `fee: float`. Database columns of type `Float` for monetary values.

**Why wrong.** Floating-point arithmetic produces rounding errors. `0.1 + 0.2 != 0.3`. Money requires exact decimal arithmetic.

**Fix.** `Decimal`. Wrap in a `Money` type with currency.

**Principle.** §D3.

---

## L13. Untyped public API

**Grep for.** Public functions (those listed in `__all__` or imported by other modules) with missing parameter or return type annotations.

**Why wrong.** Type checkers can't verify callers. The public surface contract is incomplete. Documentation drifts.

**Fix.** Add type annotations. Run `mypy --strict` or pyright in CI.

**Principle.** §E2.

---

## L14. DI container

**Grep for.** Imports of `dependency_injector`, `injector`, `inject`, `pinject`, or hand-rolled service-locator patterns.

**Why wrong.** Hides the dependency graph. Type information goes opaque. Refactoring becomes risky. Constructor injection achieves the same testability with none of the costs.

**Fix.** Compose at the entrypoint. Pass dependencies as arguments. (FastAPI's `Depends()` is the legitimate exception, §K2.4.)

**Principle.** §C4.

---

## L15. Repository pattern as 1:1 ORM wrapper

**Grep for.** A `Repository` class whose methods are 1:1 mappings to ORM calls (`get(id) -> session.get(...)`, `save(obj) -> session.add(obj); session.commit()`).

**Why wrong.** Pure renaming. No aggregate enforcement. No multi-store flexibility. Just more code.

**Fix.** Either delete the repository (use the session directly) or make it aggregate-shaped (load whole aggregate, save whole aggregate).

**Principle.** §C2.

---

## L16. `create_task` without a TaskGroup parent

**Grep for.** `asyncio.create_task(coro)` not inside an `async with asyncio.TaskGroup() as tg:` block.

**Why wrong.** Orphan tasks: no exception propagation, no cancellation propagation, possible resource leaks.

**Fix.** Wrap in a TaskGroup. The group's lifetime should match the parent's.

**Principle.** §G2.

---

## L17. External call without a timeout

**Grep for.** `httpx.get`, `httpx.post`, `requests.get`, `aiohttp.ClientSession.get`, `urlopen`, `subprocess.run`, `session.execute` — without a `timeout=` argument or `asyncio.timeout` wrapper.

**Why wrong.** Hung calls tie up resources indefinitely.

**Fix.** Set a timeout. Pick a deliberate value per call category.

**Principle.** §F5.

---

## L18. Retry on a non-idempotent operation

**Grep for.** `@retry`, `tenacity`, `backoff` decorators on functions that perform non-idempotent writes (POST without idempotency key, INSERT without ON CONFLICT, ledger writes without dedupe).

**Why wrong.** Retried non-idempotent writes produce duplicates.

**Fix.** Make the operation idempotent (idempotency key, upsert, content-addressed write) before adding retries.

**Principle.** §F4.

---

## L19. `async def` that doesn't `await` I/O

**Grep for.** `async def` functions whose body has no `await` against I/O (only computation, or only `await asyncio.sleep`).

**Why wrong.** Async without I/O is overhead. The function colours every caller for no benefit.

**Fix.** Make it sync. If it's part of an async-required interface, document the reason for the async signature.

**Principle.** §G1.

---

## L20. Free-text `logging.info(f"...")`

**Grep for.** `logging.info(f"...")`, `log.warning(f"...")` — string interpolation rather than structured fields.

**Why wrong.** Free-text logs are unsearchable, unaggregatable, and lose structure. Searching for "user 47 did X" means full-text search rather than `user_id == 47 AND action == "X"`.

**Fix.** `log.info("user.action", user_id=user_id, action=action)` with structlog.

**Principle.** §I1.

---

## L21. Mocking a third-party library directly

**Grep for.** `mock.patch("httpx.AsyncClient.get")`, `mock.patch("sqlalchemy.orm.Session.execute")`, `mock.patch("redis.Redis.get")`. Anywhere a third-party module path appears as the patched target.

**Why wrong.** Mocks pinned to someone else's API encode your assumption of how it works. Library upgrades silently break the assumption.

**Fix.** Wrap the library behind a Protocol you own. Fake the Protocol in tests.

**Principle.** §J2.

---

## L22. `Manager` / `Helper` / `Util` class suffixes

**Grep for.** `class FooManager`, `class FooHelper`, `class FooService` (used as a god class), `class FooUtility`.

**Why wrong.** "Manager" is the noun version of "do stuff" — it doesn't describe what the class is. The "Kingdom of Nouns" smell. Often a function would do.

**Fix.** Rename to what it actually is, or convert to functions.

**Principle.** §A4 (cohesion), §C3 (services as functions where possible).

---

## L23. Singletons via module-level globals

**Grep for.** Module-level mutable state used as a process-wide singleton (`_cache = {}`, `_connection = None` populated lazily).

**Why wrong.** Hard to test (state persists across tests), hard to reason about (shared mutable state), often introduces hidden ordering dependencies.

**Fix.** Pass the resource explicitly. Compose at the entrypoint. For genuine process-wide singletons (a connection pool), put them on the lifespan / app object.

**Principle.** §C4.

---

## L24. Test that hits `datetime.now()` or `random` directly

**Grep for.** Test files using `freezegun.freeze_time` or `mock.patch("datetime.datetime")` — symptoms of code under test that depends on the standard library directly.

**Why wrong.** Mocking standard-library modules violates "don't mock what you don't own". The fix is to inject a clock / RNG into the code under test.

**Fix.** Inject `Clock`, `Random`. Tests substitute deterministic fakes.

**Principle.** §J6.

---

## L25. `print` in non-CLI code

**Grep for.** `print(...)` in modules that aren't a CLI's stdout-output path.

**Why wrong.** Logging exists. `print` bypasses log levels, structure, and routing. In production, `print` output goes wherever stdout is connected, which is usually nowhere useful.

**Fix.** Use the logger.

**Principle.** §I1.

---

## L26. Pandas row iteration

**Grep for.** `df.iterrows()`, `df.itertuples()`, `for i in range(len(df)):` patterns over a DataFrame.

**Why wrong.** 10–1000× slower than vectorised equivalents.

**Fix.** Vectorise. If genuinely impossible, `.apply` (slower than vectorised, faster than iterrows).

**Principle.** §K6.1.

---

## L27. Naive `from y import x` of internal symbol

**Grep for.** Imports that reach into `_private` or undocumented internals of a third-party library.

**Why wrong.** Private APIs change without notice. Library upgrades silently break.

**Fix.** Use the public API. If the public API is missing the feature, file an issue.

**Principle.** General API discipline.

---

## L28. Auto-generated migration committed without review

**Grep for.** Alembic migrations that are clearly unmodified `alembic revision --autogenerate` output — verbose, no comments, no expand-contract structure for renames.

**Why wrong.** Auto-generation is a starting point, not the answer. It often misses non-obvious cases (rename-as-drop-and-create, computed columns, indexes).

**Fix.** Review and edit each generated migration. Apply expand-contract for renames.

**Principle.** §K7.

---

## L29. Pure helpers extracted only for testability

**Grep for.** Modules of single-line or single-expression pure functions where each helper is called from exactly one site. Test files that exhaustively cover every helper but no test that exercises the orchestrating call site. Helper names that read like "the smallest pure thing the author could carve out of an impure function".

**Why wrong.** The helpers are scaffolding, not modelling — they were extracted because pure functions are easy to test, not because they represent reusable concepts. Their tests pass; the call site that threads them together has no tests; the bugs accumulate in the seam. The refactor that "improved testability" actually moved the bug surface to the only place that wasn't tested.

**Fix.** Deepen. Collapse the helpers into their caller (or into a small private surface inside the caller's module), expose the result as one or two public entry points, and test at that boundary. Delete the now-redundant per-helper tests (`testing.md §J1` "replace, don't layer"). Keep the helpers separate only when they're genuinely reused by ≥2 unrelated callers or model a concept with a name worth defending.

**Principle.** §A5 (deep modules), §A1 (functional core — purity is necessary but not sufficient).

---

## How to use this list

In audit mode, run `scripts/antipattern_scan.py` to surface the deterministic ones (L3, L4, L5, L8, L11, L12, L17, L19, L20, L26 are detectable by static analysis). Use this list as the manual sweep for the heuristic ones — every codebase that has any of these has them in many places, so finding one example usually means there are more.

In design mode, use this list as a "things not to do" preflight — a quick sanity check that the design doesn't accidentally bake in any of these patterns.
