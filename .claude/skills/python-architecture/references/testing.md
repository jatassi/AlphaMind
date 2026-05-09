# Testing

How tests are organised, what they exercise, what they substitute. Test architecture is application architecture: a system that's hard to test is one with bad seams, and improving the tests usually means improving the system.

---

## §J0. Classify dependencies before choosing a test strategy

**Claim.** Before deciding how to test a module, classify each of its dependencies into one of four categories. The category dictates the strategy. Skipping this step is how audits end up recommending "use a mock" or "use a Protocol" without a defensible reason for choosing one over the other.

**The four categories.**

1. **In-process.** Pure computation, in-memory state, no I/O. The dependency is just code in your address space. Strategy: don't substitute anything — call it directly. If the dependency is a separate module purely for organisation, consider whether merging it into the module under test produces a deeper (§A5) result.
2. **Local-substitutable.** Dependencies that have credible local stand-ins running inside the test process: real Postgres via testcontainers, SQLite-on-disk, a tmpfile, an in-memory filesystem, an embedded HTTP server (`responses`, `respx`, `httpx.MockTransport`). Strategy: run the real thing — or its production-equivalent stand-in — in the test. Don't define a Protocol; the substitute *is* the seam.
3. **Remote but owned.** Your own services across a network or process boundary (microservices, internal APIs, an internal queue). Strategy: define a Protocol at the consumer's boundary. Production gets the HTTP / gRPC / queue adapter; tests get an in-memory adapter. The deep module owns the logic; the transport is injected. This is the case where "ports and adapters" earns its keep — it lets logic split across a network boundary be tested as one deep unit.
4. **True external.** Third-party services you don't control (Stripe, Twilio, Slack, vendor APIs, the LLM). Strategy: wrap behind a Protocol *you own*, then provide a fake. Don't `mock.patch` the third-party library — that re-implements your assumptions about how it behaves and rots when the library changes (§J2).

**Rationale.** Each row demands a different test substitute, and the wrong choice is expensive. Local-substitutable code that's gated through a Protocol is ceremony — testing against the real Postgres is faster, more accurate, and simpler than maintaining a Protocol + two adapters. In-process code wrapped in a Protocol is even worse — you've added an indirection that exists only for tests that didn't need it. Conversely, a true-external dependency without a Protocol means tests that mock the third-party library, which is the brittlest test shape there is.

**Audit signal.** A Protocol + two adapters wrapping a database when the test suite is happy with testcontainers — over-application, ceremony for nothing. `mock.patch("stripe.Charge.create")` — under-application of the framework for a true-external dependency. A "service" that tests in-process pure logic via a Protocol — pointless indirection.

**Design signal.** When sketching a new module, list its dependencies and tag each with a category before writing any code. The categories drive the seam shape: nothing for (1), real-thing-in-test for (2), Protocol for (3) and (4). The list also exposes whether the module's coupling profile is healthy — a module that pulls dependencies from all four categories is probably doing too many things.

---

## §J1. Sociable unit tests at module / package scope

**Claim.** Test the public surface of a unit, with its real internal collaborators, against in-memory or trivial-fake substitutes for I/O. The Detroit / classicist / Cosmic Python style. Mockist tests (mock every collaborator) couple to implementation and rot during refactors.

**Rationale.** A "sociable" test exercises a meaningful slice of the system — the function plus the helpers it calls, the class plus the value objects it operates on — and asserts on behaviour, not implementation. When refactoring rearranges internals without changing behaviour, sociable tests still pass; mockist tests fail and have to be rewritten. The cost is slightly slower tests; the benefit is tests that actually protect refactoring.

**Attribution.** Martin Fowler, "Mocks Aren't Stubs" (the classicist / mockist split). Steve Freeman & Nat Pryce, *Growing Object-Oriented Software, Guided by Tests* (the canonical sociable-test reference). Cosmic Python on the same.

**When it breaks.** Genuinely independent units (a parser, a calculator) — even mockist tests work fine because there's nothing to mock. Mockist tests work for cross-system contract tests where the other side really shouldn't be invoked.

**Audit signal.** Tests that mock every collaborator of the unit under test. Tests that break on internal refactors that don't change observable behaviour. Tests that assert on the *order* of internal calls rather than the result.

**Design signal.** Define "unit" at the module or package level, not the function level. Substitute I/O at the seam (a Protocol you own); use real internal collaborators.

**Replace, don't layer.** When deepening a module (§A5) or otherwise widening the test boundary, the old per-helper unit tests become redundant once the boundary tests cover the same behaviour. Delete them — don't keep both. Layered tests at every level of internal granularity ossify the current decomposition: future refactors that change internal structure now have to update tests at every layer, which is exactly what sociable testing was supposed to prevent. The rule: tests live at the deepest module's public boundary, asserting on observable outcomes. Internal helpers may exist; tests of internal helpers should not, unless the helper is itself a deep module that other code consumes.

---

## §J2. Don't mock what you don't own

**Claim.** Wrap third-party libs behind a Protocol you own; in tests, substitute an in-memory fake of *your* Protocol. The Protocol is the contract; the fake is one of the implementations. Mocks pinned to `httpx.AsyncClient.get` are a brittle re-implementation of httpx.

**Rationale.** A mock of someone else's library encodes your assumption of how that library works. When the library changes — version upgrade, behaviour change, new error type — your mocks lie and your tests pass while the real code breaks. A fake of your own Protocol is grounded in a contract you control.

**Attribution.** Steve Freeman & Nat Pryce, "Don't mock what you don't own". Cosmic Python on the same. Justin Searls on the limitations of mocking libraries.

**When it breaks.** Genuine smoke tests that *want* to test the integration with the real library may use it directly (against testcontainers or a recording / replay tool, not mocks). The thesis is about isolated unit tests.

**Audit signal.** `mock.patch("httpx.AsyncClient.get")`. `mock.patch("sqlalchemy.orm.Session.execute")`. Anywhere a third-party module path appears as the patched target.

**Design signal.** For each I/O dependency, define a Protocol describing what your code uses. Provide one implementation that wraps the real library; in tests, provide a second implementation that's a fake.

---

## §J3. Pytest fixture discipline

**Claim.** Fixtures live in the nearest `conftest.py`. Scope deliberately — `function` is the default, `module` / `session` for expensive setup with no per-test mutation. Factory fixtures (`def make_order(**overrides): ...`) over fixed test objects. Parametrize for input variation; don't loop inside a test.

**Rationale.** Fixtures organised by scope match the cost of the work they do. Factory fixtures let each test customise just what it needs without a forest of `make_order_for_a_user_who_has_filled_orders` variants. Parametrize gives you N tests instead of one test with N internal cases — when one case fails, you see which one without scrolling logs.

**Attribution.** pytest documentation. Brian Okken (*Python Testing with pytest*). The "object mother" pattern from Freeman & Pryce.

**When it breaks.** Truly trivial test setup needs no fixture at all — inline construction is fine.

**Audit signal.** `setUp` / `tearDown` style code in pytest tests (use fixtures). Module-scope fixtures that mutate state across tests (race condition with parallelism). For-loops inside tests instead of parametrize.

**Design signal.** Factory fixtures by default. Scope at function unless the cost is real. Parametrize the inputs.

---

## §J4. Real Postgres / real SQLite-on-disk for integration; testcontainers for cross-service

**Claim.** For DB integration tests, prefer the real engine via testcontainers (Postgres) or a tmpfile SQLite. In-memory SQLite when the schema is trivially compatible. Rolled-back transactions per test for speed.

**Rationale.** Mocked databases lie. The real engine catches schema bugs, migration bugs, query-shape bugs, transaction bugs that no mock will. Testcontainers makes "the real engine in a container" cheap. Rolled-back transactions per test give you speed and isolation without recreating the schema.

**Attribution.** Testcontainers project. The "always use real DBs in tests" position from Cosmic Python and from production-engineering consensus.

**When it breaks.** Pure-domain tests that don't touch the DB don't need the DB.

**Audit signal.** SQLAlchemy session mocked in tests that exercise persistence-aware logic. In-memory SQLite for code that uses Postgres-specific features.

**Design signal.** Testcontainers Postgres for the real schema. SQLite-on-disk if dialect compatibility allows. Rolled-back transactions per test.

---

## §J5. Hypothesis for invariant-heavy domains

**Claim.** Property-based testing earns its keep where you can name invariants ("after `apply_fill`, position quantity = sum of fills", "for any valid order, round-tripping through serialization preserves equality"). Don't force it on CRUD-shaped tests.

**Rationale.** Hypothesis generates inputs your imagination wouldn't reach (boundary values, edge cases, surprising combinations). It produces minimal failing examples for debugging. The cost is learning the strategy DSL and writing invariants — payoff is high in code with mathematical structure (financial calculations, parsers, serialization round-trips, state machines).

**Attribution.** David MacIver (Hypothesis author). The QuickCheck tradition (Haskell). Hillel Wayne on property-based testing.

**When it breaks.** CRUD tests where the input space is small and the invariant is just "the data was stored and retrieved". Example-based tests are fine there.

**Audit signal.** Hand-written examples for code that has obvious mathematical invariants (commutativity, associativity, round-trip, monotonicity). No Hypothesis use in financial / numerical / serialization code.

**Design signal.** When designing a domain function, name the invariants. Use Hypothesis to test them.

---

## §J6. Determinism is an architectural property

**Claim.** Tests fail randomly because the system is non-deterministic, not because tests are flaky. Inject a `Clock` (don't call `datetime.now()` in domain code), inject a `Random` (seed it from config), inject a task-ID generator. Once the system is deterministic, replay becomes trivial — a property especially valuable for LLM systems and trading systems.

**Rationale.** Non-determinism in tests is a symptom of non-determinism in the code. Functions that call `datetime.now()` or `random.random()` directly are not unit-testable without mocking the standard library — which violates §J2 (don't mock what you don't own). Inject the source of non-determinism as a parameter, and the test substitutes a deterministic fake.

**Attribution.** Michael Feathers, *Working Effectively with Legacy Code*. Glyph on dependency injection of clocks. Functional core / imperative shell (§A1) in another guise.

**When it breaks.** Truly trivial code where the determinism injection is overhead. The thesis applies to any code under test that depends on time, randomness, or external state.

**Audit signal.** `datetime.now()` / `time.time()` / `random.random()` / `uuid.uuid4()` in code under test. Tests using `freezegun` or `mock.patch` on standard-library functions.

**Design signal.** Domain code takes `clock: Clock` and `rng: Random` as parameters (or the relevant interface). The shell instantiates the real ones; tests instantiate fakes.

---

## Cross-references

- The "fakes over mocks" position rests on `data-and-types.md §E1` (Protocols define the contract).
- Determinism (§J6) is a cousin of `foundations.md §A1` (functional core, imperative shell).
- Antipatterns (mocking third-party libs, naive datetime in code under test, fixture-scope races): `antipatterns.md`.
