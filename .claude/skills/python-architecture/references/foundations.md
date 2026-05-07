# Foundations — meta-architectural principles

These are the principles every other reference rests on. When in doubt about which way to lean on a specific question, re-read this file.

Each thesis carries: **claim**, **rationale**, **attribution**, **when it breaks**, **audit signal** (what to look for in code), **design signal** (how to apply when building new).

---

## §A1. Functional core, imperative shell

**Claim.** Business logic lives in pure functions over plain values. I/O — DB, HTTP, time, randomness, the LLM, the broker, anything non-deterministic — lives in a thin shell that loads inputs, calls the core, and writes outputs.

**Rationale.** Pure functions are trivially testable in-process with no fixtures, parallelisable / cacheable / replayable for free, and reasoned-about in isolation. The shell is small and obvious. The combination decouples *what the system does* from *how it does I/O*, which is the single biggest source of friction in real codebases.

**Attribution.** Gary Bernhardt, "Boundaries" (destroyallsoftware.com, 2012). The same idea runs through Cosmic Python under the names "domain model" + "service layer" + "adapters". John Carmack independently arrived at it ("functional style in C++").

**When it breaks.** Code that genuinely is "thin glue over an external system" — a CLI wrapper around a REST API, a one-shot script. There the shell is the program, and forcing a core/shell split is overhead for no benefit.

**Audit signal.** Look for `requests.get`, `httpx.AsyncClient`, `session.execute`, `datetime.now()`, `time.time()`, `random.random()`, `open(...)`, or `subprocess.run` deep inside what should be domain logic. Also look for domain methods that take a session, a clock, or a client as a parameter — that's an okay way to factor it, but if the *only* parameter is the client, the function is shell, not core.

**Design signal.** Sketch the system as a pipeline of pure functions before deciding where the I/O lives. Push I/O outward as far as it will go without becoming nonsensical. The shell should be visibly thinner than the core in terms of line count.

---

## §A2. The import graph IS the architecture

**Claim.** In a dynamic language without enforced visibility, the only architecture is what the modules actually import. If `domain/order.py` imports `sqlalchemy`, the system is layer-violating regardless of what the README claims.

**Rationale.** Python's permissive import system means architectural rules expressed as prose are aspirational — there is nothing stopping a developer from `from sqlalchemy import select` inside what's nominally a pure domain module. Tools that trace the actual import graph and enforce declared rules (`import-linter`, `pydeps`, `grimp`) make architecture verifiable instead of imagined.

**Attribution.** Brandon Rhodes, "When Python Practices Go Wrong" (code::dive 2019, rhodesmill.org/brandon/talks/). David Seddon (author of `import-linter`).

**When it breaks.** Single-package projects where everything genuinely is one layer. Then layering is a category error and `import-linter` has nothing to enforce.

**Audit signal.** Three things to check. First, does the project have any `import-linter` (or equivalent) configuration? If not, layer rules are purely aspirational. Second, run `analyze_imports.py` and inspect cycles — cycles are almost always architectural smells. Third, walk the imports of any module that *claims* to be domain / pure / boundary-free and see what it actually imports.

**Design signal.** Decide your layer rules at design time and write them as `import-linter` contracts before you've written any code that would violate them. The rules should be the smallest set that captures the architecture you actually want — not every possible boundary.

---

## §A3. Make illegal states unrepresentable

**Claim.** Encode constraints in types — `NewType("OrderId", UUID)`, `Literal["draft", "submitted", "filled"]`, frozen dataclasses with validating `__post_init__`, parsed-once domain primitives — so the type checker rejects the bug rather than `if`-statement-and-pray runtime checks.

**Rationale.** A bug the type checker rejects is one fewer test you have to write, one fewer code review you have to remember to do, one fewer production incident. Types compose: once `OrderId` exists, every function that takes one is automatically protected from being passed a `UserId`. Pair with **parse, don't validate**: untrusted input crosses the boundary exactly once into a typed value, then internal code trusts the type.

**Attribution.** Hillel Wayne, "Constructive vs. Predicative Data" (hillelwayne.com). The phrase "make illegal states unrepresentable" is from Yaron Minsky / OCaml; "parse, don't validate" is from Alexis King in Haskell. Lifted into Python by way of PEPs 484 / 544 / 586 / 593.

**When it breaks.** Domains where every value really is "a string" and the constraints are emergent (free-text content, user comments). Then types are scaffolding without a building.

**Audit signal.** Bare `int` IDs in function signatures (`def get_user(user_id: int)`). Bare `str` for status / kind / type fields. `dict[str, Any]` propagating through internal code where a typed structure would do. Repeated runtime checks for the same invariant in different functions.

**Design signal.** Before any business logic, write the domain primitives — `UserId`, `OrderStatus`, `Money`, `Symbol`, `Quantity`. The vocabulary of the system is the types. Convert untrusted input into these types at the boundary and never look back.

This thesis is elaborated in `data-and-types.md §D, §E`. This entry is the meta-principle.

---

## §A4. Cohesion over hierarchy; flat over nested

**Claim.** Two to three levels of package depth is plenty. Deep trees (`alphamind.execution.orders_and_brackets.factories.builders.envelope_v2`) almost always indicate folders created before there was anything to put in them. Promote a module to a package when there are three or more cohesive submodules; promote to its own distribution when there are independent consumers.

**Rationale.** Module depth is a proxy for cognitive overhead — every level of nesting is a context switch when reading, an import statement to read when navigating, a directory to descend in the file tree. Raymond Hettinger's "flat is better than nested" comes from the Zen because it's load-bearing, not decorative.

**Attribution.** Raymond Hettinger (Zen of Python, PyCon talks). Brett Slatkin, *Effective Python* item on package layout.

**When it breaks.** Genuinely large systems with multiple distinct subdomains (>50 packages). There some hierarchy is forced. Even then, the depth at which you're operating *inside* a subdomain stays flat.

**Audit signal.** Module paths longer than 3 segments after the project root. Single-file packages whose only purpose is to nest the file (`feature/sub/sub/the_actual_module.py`). Imports where the path is longer than the symbol being imported.

**Design signal.** Start with one module. Promote when it crosses ~400 lines or has three distinct concerns. Resist the urge to pre-create empty `domain/`, `application/`, `infrastructure/` directories until they have content that justifies them.

---

## §C1. Hexagonal where the domain is non-trivial; shell-only where it isn't

**Claim.** For systems with real rules ("an order can only allocate inventory if the warehouse has stock and the order is not cancelled and the customer is in good standing"), put the rules in pure domain modules and inject I/O at the edges via Protocols. For systems that are mostly transformations (vendor JSON → DB row, CSV in → cleaned CSV out), skip the ceremony — a function pipeline is fine.

**Rationale.** Hexagonal architecture pays for its overhead with testability and adaptability *only when the domain has rules to test and adapt*. Applied to a CRUD app or a thin ETL, hexagonal is renaming: the "domain" is just the database schema, the "application service" is just a function, and the "adapters" wrap libraries that have already been wrapped.

**Attribution.** Alistair Cockburn (the original "hexagonal architecture" / ports and adapters paper, 2005). Harry Percival & Bob Gregory, *Architecture Patterns with Python* (cosmicpython.com), apply it to Python. Cockburn himself is explicit that it's for systems where the domain has real rules.

**When it breaks.** Rules-less systems (CRUD, thin ETL, glue scripts). Don't apply.

**Audit signal.** Two opposite smells. (1) A "domain" module that imports `sqlalchemy` or `httpx` directly — hexagonal claimed but not enforced. (2) A repository / service / adapter layer wrapping a CRUD app where the "domain" is the same shape as the table — hexagonal applied where it shouldn't be.

**Design signal.** First decide whether the system has rules. If yes, design the domain as pure functions over types and inject I/O via Protocols defined where they're consumed. If no, skip the ceremony — a function pipeline reading from and writing to clear-shaped types is fine, and you can always add structure later when rules emerge.

---

## §C2. Repositories only when they pay rent

**Claim.** A repository pattern that wraps `session.get(User, id)` as `UserRepository.get(id)` is renaming, not abstraction. Repositories earn their keep when they (a) enforce aggregate boundaries — you load `Order` and its `OrderLines` together, never piecewise — or (b) you actually have multiple stores. Otherwise, the SQLAlchemy `Session` (or equivalent) IS the repository and the unit of work.

**Rationale.** The cost of a repository layer is doubled API surface and one more place to look when tracing data flow. The benefit is real only when there's something to enforce or swap. Cosmic Python's Chapter 2 is explicit about this; the pattern shines in the chapter where aggregates appear, not in the chapter where it's introduced as an idea.

**Attribution.** Eric Evans, *Domain-Driven Design* (the original aggregate / repository pattern). Percival & Gregory, Cosmic Python ch. 2 (the warning) and ch. 7 (the payoff).

**When it breaks.** When you have an aggregate (Order with Lines, Position with Fills, Portfolio with Orders) whose invariants must be enforced atomically. There the repository is the natural home for "load the whole aggregate" and "save the whole aggregate" and the ORM session is too low-level.

**Audit signal.** A `Repository` class whose methods are 1:1 mappings to ORM calls (`get(id) -> session.get`, `save(obj) -> session.add(obj); session.commit()`). The repo is renaming.

**Design signal.** Don't introduce a repository layer until there's an aggregate to enforce or a real second store to swap to. When you do, name it after the aggregate it loads (`OrderRepository`), and its API should be aggregate-shaped, not row-shaped.

---

## §C3. Service layer = use-case = transaction boundary

**Claim.** A "service layer" is not another tier sitting between controllers and domain. It is your application's outermost domain boundary — one function per use case, opens a transaction, loads aggregates, calls domain methods, commits, returns or publishes.

**Rationale.** Treating "Service" as a generic tier produces god classes (`UserService`, `OrderService`) that grow indefinitely. Treating each use case as a function (`allocate_stock`, `submit_order`, `cancel_order`) keeps the boundary atomic and the responsibility clear.

**Attribution.** Martin Fowler, "Service Layer" pattern (PoEAA), with the explicit framing that it's about application workflows. Cosmic Python ch. 4 applies this to Python.

**When it breaks.** When use cases share substantial state or setup. Then a class with shared state can be cleaner than functions with the same six-arg prefix. Rare in practice.

**Audit signal.** Service classes with >5 methods that share no state. Service methods that don't open and close a transaction (orphan reads, orphan writes). Service methods that delegate immediately to a single repository call — that's not a service, it's an alias.

**Design signal.** Write each use case as a function. The function name is the use case name. The signature carries the dependencies (passed in by the entrypoint composition root). The body opens a transaction, calls domain code, commits.

---

## §C4. Constructor injection beats DI containers

**Claim.** Pass dependencies as arguments. Compose at the entrypoint (`main.py`, the FastAPI app factory, the test fixture). Skip `dependency-injector`, `injector`, `inject`, and similar frameworks. FastAPI's `Depends()` is the legitimate exception because it's syntactic, not runtime, and the type signature still tells the truth.

**Rationale.** DI containers replace explicit wiring with import-time magic. The dependency graph becomes invisible, type information becomes opaque, refactoring becomes risky. Explicit constructor injection is a few more lines at the composition root in exchange for everything downstream remaining readable and analysable.

**Attribution.** Glyph Lefkowitz on "the cost of magic" (glyph.twistedmatrix.com). Cosmic Python ch. 13 demonstrates a minimal explicit DI pattern as the alternative to containers. James Shore, "Dependency Injection Demystified" (2006) — the classic argument that DI is just passing arguments.

**When it breaks.** Genuinely large applications with deep object graphs and many sites of construction. Even there, factory functions composed at the entrypoint usually beat a container.

**Audit signal.** Imports of `dependency_injector`, `injector`, `inject`, or similar. Functions that resolve dependencies from a global registry rather than from arguments. "I can't tell what this function depends on without grepping" is the smell.

**Design signal.** Composition root in `main.py` (or `app.py`, or wherever the entrypoint is). Dependencies are constructed there and passed down as arguments. For tests, construct different dependencies in the test fixture.

---

## §C5. Public API discipline: `__all__` + leading underscore

**Claim.** A module's public surface is what's listed in `__all__`. Lacking `__all__`, it's everything that doesn't start with `_`. Treat anything else as breakable without warning.

**Rationale.** Without explicit declaration of what's public, every function is public to anyone who notices it exists, and every refactor risks breaking unknown consumers. Declaring the surface cuts both ways: it tells consumers what they can rely on, and it tells maintainers what they're committed to.

**Attribution.** PEP 8 (the leading-underscore convention). Brett Cannon and Hynek Schlawack on packaging discipline.

**When it breaks.** Internal modules in applications where there is only one consumer (the rest of the same application). Then `__all__` is noise — leading underscores carry the weight.

**Audit signal.** Public modules (under a published library, or under `src/<package>/` of a project that gets imported by other projects) without `__all__`. Public modules with names like `_internal` whose contents are imported from outside. Public functions with no docstring next to private functions with full documentation — inverse-cargo-culted privacy.

**Design signal.** For libraries: every public module gets `__all__`. For applications: leading-underscore convention is enough; reserve `__all__` for the package's `__init__.py` if you re-export anything.

---

## §C6. Avoid premature plugin / namespace-package architectures

**Claim.** PEP 420 namespace packages, `setuptools.entry_points`, dynamic plugin discovery — all useful when you genuinely have third parties extending your system. Premature plugin architecture is one of the highest-cost-no-benefit moves in Python.

**Rationale.** Plugin systems trade explicit code for runtime discovery. The dependency graph becomes invisible (which plugin runs in which order? at startup or first call?), the type information becomes opaque (what's the contract a plugin satisfies?), and the failure modes proliferate (what if a plugin imports something missing? what if two plugins claim the same name?). For first-party variants, a `dict[str, Strategy]` registry in code is clearer.

**Attribution.** Glyph on "the cost of magic". The general principle of YAGNI applied to architecture.

**When it breaks.** When you genuinely have ≥3 third-party plugins that need to extend your system without modifying it. Then plugin infrastructure earns its keep. pytest is the canonical example.

**Audit signal.** Plugin / entry-point machinery for first-party variants. A `register_handler(name, fn)` system used only by code in the same repo.

**Design signal.** Start with explicit registries. Convert to `entry_points` only when third parties exist. Even then, keep the contract typed (a Protocol the plugin implements) so the boundary is checkable.

---

## §C7. Domain events / message bus only when state machines or audit demand it

**Claim.** The "publish a `StockAllocated` event from the domain method" pattern shines for state machines, audit logs, decoupling cross-feature side effects (email, analytics). It's overkill for "just call the function". Default to direct calls; reach for events when you find yourself wanting to add a fifth side-effect to one method.

**Rationale.** Events inside a monolith add ceremony — definition, serialisation (often), bus, handler registration, ordering rules — in exchange for decoupling that may not exist (handlers in the same process aren't decoupled in any meaningful sense, just indirected). The pattern earns its keep when (a) you genuinely need an audit log of what happened, (b) you have a state machine and the transitions need to be observable, or (c) you have side effects that genuinely should be added without modifying the source.

**Attribution.** Cosmic Python chapters 8–11 build the message bus pattern. Their own examples make clear it's for the cases above; I'm calling out that those cases are narrow.

**When it breaks.** Cross-process or cross-service systems where events cross machine boundaries. There they earn their keep almost always.

**Audit signal.** A message bus inside a monolith with one publisher and one handler. That's a function call with extra steps.

**Design signal.** Default to direct calls. Introduce events when you have a state machine to observe, an audit log to write, or a side-effect ecosystem to extend.

---

## Cross-references

- For the type-system mechanics behind §A3, see `data-and-types.md`.
- For the import-graph tooling and feature-vs-layer organisation behind §A2 / §C, see `packaging-and-layout.md`.
