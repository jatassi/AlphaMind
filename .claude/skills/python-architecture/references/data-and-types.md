# Data and types

The shape of the data flowing through the system, and how the type system encodes its constraints. Two intertwined concerns; they share this reference because the answer to "how should I model this thing" is almost always also "what types should describe it".

---

## §D1. Frozen dataclass + slots is the default internal value type

**Claim.** `@dataclass(frozen=True, slots=True)` for everything inside the system. Mutability is opt-in, justified, and rare.

**Rationale.** Frozen catches "spooky action at a distance" bugs at the assignment site, not three call frames later. `slots=True` cuts memory by ~30% and prevents accidental attribute typos (no `obj.naem = "Alice"` silently creating a new attribute). Both are zero-cost on Python 3.10+. The combination is what `attrs` pioneered; the standard library caught up.

**Attribution.** Hynek Schlawack on the philosophy behind `attrs` (hynek.me, "Why I'm not leaving Python"). Glyph Lefkowitz on immutability in concurrent systems. Eric V. Smith (PEP 557, dataclasses).

**When it breaks.** Builder patterns where the partial object accumulates fields before being finalised — there a mutable builder class is cleaner than threading kwargs through. Hot-path numerical code where allocating a new frozen instance per iteration is wasteful — measure first; usually the cost is negligible.

**Audit signal.** `@dataclass class X:` (no `frozen=True`) for value-shaped types. Code that mutates a "value" object after construction. `__dict__` on what should be a fixed-shape record. Mixing `attrs` and `dataclass` in the same project (pick one).

**Design signal.** Every value-shaped type is `@dataclass(frozen=True, slots=True)`. Use `attrs` instead of `dataclass` only if you need converters, validators on every field, or richer inheritance — and if you do, use `attrs` consistently.

---

## §D2. Pydantic only at trust boundaries

**Claim.** Pydantic `BaseModel` is for HTTP request/response, file parsing, vendor API ingest, config loading, LLM tool-call validation. **Not internal domain types.** Convert once at ingest into a frozen dataclass; the type system carries the trust from there.

**Rationale.** Pydantic does coercion + validation work on every instantiation. Using it for already-trusted internal data wastes cycles, conflates the boundary, and pulls a heavy dependency into modules that don't need it. Pydantic v2 is faster than v1 but the cost is still real; more importantly, the conceptual cost (every layer revalidating the same data) is high.

This is the single highest-yield audit finding in most Python codebases.

**Attribution.** Samuel Colvin (Pydantic creator) on Pydantic's role at boundaries. The "parse, don't validate" framing from Alexis King, applied here as: parse once at the boundary into a typed domain object; internal code trusts the type.

**When it breaks.** Truly tiny applications where the line between "boundary" and "internal" doesn't exist — a script that takes JSON in, transforms, writes JSON out. Pydantic the whole way is fine. The thesis applies to systems with a meaningful internal logic layer.

**Audit signal.** `BaseModel` subclasses in modules that aren't named or located like boundary modules (`*/api/*`, `*/config/*`, `*/schemas/*`, `*/io/*`, `*/adapters/*`). `BaseModel` instances flowing through what should be domain code.

**Design signal.** Boundary types are Pydantic. Domain types are frozen dataclasses. The conversion is a single named function (`OrderRequest.to_domain() -> Order`) at the boundary. After the conversion, no module imports Pydantic.

---

## §D3. Domain primitives over bare ints, strs, floats

**Claim.** `UserId = NewType("UserId", UUID)`. `Price = NewType("Price", Decimal)`. `Symbol = NewType("Symbol", str)` with a `parse_symbol` factory. Money is `Decimal` with a currency, never `float`. Datetimes are timezone-aware always — `datetime.now(timezone.utc)`, never naive.

**Rationale.** "Primitive obsession" is the antipattern of treating distinct domain concepts as the same primitive type. `int` for `UserId` and `int` for `OrderId` means the type checker can't catch `get_user(order_id)`. `float` for `Price` means rounding errors in money. Naive `datetime` means timezone bugs that surface only at the daylight-savings transition. Each domain primitive is one wrapper away.

**Attribution.** Kent Beck and Ward Cunningham (the "primitive obsession" name). Hillel Wayne on type-driven design. Gary Bernhardt's "Money" type talks. Aware-datetime hygiene is consensus.

**When it breaks.** Quick scripts where the cost of the wrappers exceeds the bug surface. Internal-only code where the IDs really are interchangeable — but if they're really interchangeable, give them the same type.

**Audit signal.** `def f(user_id: int, order_id: int)` — two `int`s that mean different things. `float` for any monetary or fixed-precision quantity. `datetime.now()` (naive) anywhere. Strings for IDs that have a natural shape (UUID, ULID).

**Design signal.** Write the domain primitives first, before any business logic. The vocabulary of the system is the types.

---

## §D4. `StrEnum` (3.11+) or `Literal` over bare strings for closed sets

**Claim.** `OrderStatus.SUBMITTED` over `"submitted"`. Pick `StrEnum` when you need identity and round-trip-to-JSON; `Literal` when you only need type narrowing.

**Rationale.** Bare strings make typos type errors, exhaustiveness uncheckable, and refactors (renaming a status value) silently dangerous. `StrEnum` round-trips through JSON without a custom encoder. `Literal` is lighter when you don't need an actual enum object.

**Attribution.** PEP 663 (StrEnum). Pydantic's enum handling. The general "make illegal states unrepresentable" principle applied to status fields.

**When it breaks.** Genuinely open sets — a free-text field, a vendor-provided code with no enumeration. Then `str` with a documented format is the right type.

**Audit signal.** `if status == "submitted":` repeated across files. Status comparisons with string literals. `status: str` parameters whose docstring lists valid values.

**Design signal.** For every closed-set field, pick `StrEnum` or `Literal` at design time. Decide once per field; mixing is fine project-wide.

---

## §D5. Pick the right record type

**Claim.** Match the type to the role:
- `NamedTuple` — ad-hoc immutable returns, especially when callers will unpack positionally.
- `TypedDict` — structural shape over dict-shaped data you don't own (third-party JSON before parsing).
- `dataclass` — domain objects with behaviour, validation, identity.
- `Pydantic.BaseModel` — boundary parsing only (see §D2).

**Rationale.** Each type has a sweet spot. `NamedTuple` is unbeatable for `(x, y) = get_position()`-style returns. `TypedDict` is structural — useful when you can't or shouldn't make the producer of the data return your class. `dataclass` is the workhorse for first-class domain objects. `BaseModel` is for the boundary.

**Attribution.** Raymond Hettinger, "Dataclasses: The code generator to end all code generators" (PyCon 2018). PEP 589 (TypedDict).

**When it breaks.** When the choice is genuinely arbitrary because the use is so trivial. Then any of them is fine; consistency within the project beats local optimisation.

**Audit signal.** Dataclasses used to model vendor JSON shape (TypedDict would do). NamedTuples with eight fields (the positional unpacking advantage is gone; should be a dataclass). Dictionaries flowing through internal code where a typed record would catch typos.

**Design signal.** Decide per type, with a default of dataclass. Reach for the others when their specific advantage shows up.

---

## §D6. Construction via factory classmethods, not overloaded `__init__`

**Claim.** `Order.from_api_payload(payload)`, `Order.from_db_row(row)`, `Order.opening(symbol, qty)` over an `__init__` that branches on `if from_db: ...`. Keep the dataclass `__init__` dumb; the factories are documented entry points.

**Rationale.** A dataclass's `__init__` should assign fields; that's its job. Putting branching logic in `__init__` makes the construction surface unclear and the test surface harder. Factory classmethods name each construction path, document it, and let callers pick the right one.

**Attribution.** Effective Python (Brett Slatkin) on alternative constructors. Hynek's `attrs` factory patterns.

**When it breaks.** Classes with one obvious construction path that needs no naming. There the bare constructor is fine.

**Audit signal.** `__init__` methods with optional flags that switch between construction modes. `__post_init__` doing I/O or making decisions. Construction-from-dict logic scattered across callers instead of centralised in a `from_dict` classmethod.

**Design signal.** When designing a type whose construction comes from multiple sources (API, DB, file, in-memory), name each as a classmethod. The class definition documents the legitimate construction paths.

---

## §E1. `Protocol` for interfaces; `ABC` only for shared implementation

**Claim.** Use `Protocol` (PEP 544) for "anything with these methods." Reach for `ABC` only when you want to reuse base-class implementation or need cheap `isinstance` checks at runtime without `@runtime_checkable`'s shape-only guarantee.

**Rationale.** Protocols enable structural subtyping — the type checker accepts any object that has the right shape, with no inheritance declaration required. This breaks coupling: the consumer declares what it needs; the producer doesn't have to know about the consumer. ABCs require inheritance, which couples the implementation to the abstraction. The pattern to adopt: define the Protocol where it's *consumed* (next to the function that takes it), not next to the implementations.

**Attribution.** PEP 544 (Ivan Levkivskyi, Jukka Lehtosalo, Łukasz Langa, Guido van Rossum). The general "structural over nominal" position from typed languages with structural subtyping (TypeScript, OCaml).

**When it breaks.** When you genuinely want to provide shared implementation in the abstraction itself. Then ABC. When you need `isinstance` to be cheap, ABC. When the runtime really needs to know "is this a Foo?", `@runtime_checkable` Protocol or ABC.

**Audit signal.** ABC subclasses with no concrete shared methods — that's a Protocol with extra steps. Protocols defined in the same module as the implementation rather than next to the consumer.

**Design signal.** Define Protocols at the seam. Define them where they're consumed. Implementations are passed in (constructor injection, §C4). For tests, fake the Protocol.

---

## §E2. `mypy --strict` (or pyright `strict`) from day one

**Claim.** Run a strict type checker in CI from the beginning. Retrofitting types is order-of-magnitude harder than starting strict. New modules: strict. Old modules: per-module strictness escalation, tracked as a debt list.

**Rationale.** Gradual typing is real and works, but it works *forward* much better than *backward*. New code starting strict is cheap; existing code becoming strict is a months-long project that touches everything. The other reason: a strict checker turns "I forgot to type this parameter" into a build failure, which is exactly when you want to know.

**Attribution.** Guido van Rossum on typing posture. Jukka Lehtosalo on mypy's strict mode. Eric Traut on pyright. Łukasz Langa.

**When it breaks.** Heavy interaction with poorly-typed third-party libraries (numpy until very recently, pandas, some vendor SDKs). Use type stubs where available; `# type: ignore[no-typed]` with a comment for what the missing stub costs you.

**Audit signal.** No `[tool.mypy]` or `[tool.pyright]` section in `pyproject.toml`. `[tool.mypy] strict = false`. Many `# type: ignore` without explanatory comments. Public modules without complete type signatures.

**Design signal.** From day one: `[tool.mypy] strict = true`. Per-module relaxations only for libraries you genuinely can't type, tracked in `[[tool.mypy.overrides]]` with a comment.

---

## §E3. PEP 695 generic syntax on Python 3.12+

**Claim.** Use `class Stack[T]:` and `def first[T](xs: list[T]) -> T:` syntax instead of the older `T = TypeVar("T")` pattern. AlphaMind on 3.13 should use it everywhere.

**Rationale.** PEP 695 generic syntax is concise, scope-aware, and reads like generics in other typed languages. The old `TypeVar` pattern still works but is verbose, scope-confusing, and now legacy.

**Attribution.** PEP 695 (Eric Traut, Jelle Zijlstra).

**When it breaks.** Python <3.12. There the old syntax is mandatory.

**Audit signal.** New code on 3.12+ using `TypeVar`.

**Design signal.** Use the new syntax. If supporting older Python is required, document it; otherwise default to new syntax.

---

## §E4. Avoid `Any`; bound it when you must

**Claim.** `Any` defeats the type checker. When you genuinely don't know the shape, prefer `object` (forces narrowing via `isinstance`) or a Protocol describing what you actually use.

**Rationale.** `Any` is contagious — it propagates through function signatures and erases type information for callers. `object` is safer because the checker forces you to acknowledge unknowns; a Protocol is better still because it constrains the unknown to what you actually use.

**Attribution.** mypy documentation on `Any` vs. `object`. PEP 484. Guido van Rossum's typing-related talks.

**When it breaks.** Truly dynamic dispatch (a JSON parser, a deserialiser, a shim across heterogeneous data). There `Any` is the right type for the unknown column.

**Audit signal.** `Any` in public function signatures (especially returns). `dict[str, Any]` propagating through internal code.

**Design signal.** Reach for `object` first, narrow with `isinstance` or a Protocol. Reach for `Any` only when the dynamism is genuine and confined.

---

## Cross-references

- The hexagonal pattern that puts these types at the boundary vs. domain split: `foundations.md §C1`.
- Antipatterns in this dimension (mutable dataclasses, naive datetimes, float-money, `Any` propagation): `antipatterns.md`.
