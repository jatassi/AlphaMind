# Audit — `buggy_app`

**Date:** 2026-05-06
**Scope:** Full codebase at `/src/buggy_app`; all modules, no constraints.
**Constraints noted:** None. This is a small internal tool for synchronous order submission and risk checking.

---

## 1. Calibration

The codebase is a three-layer order-management system:
- `core/` — domain logic (orders, risk checking)
- `api/` — HTTP handlers
- `storage/` — persistence adapter

Layout is feature-flat (appropriate for a small system). `pyproject.toml` declares `httpx`, `pydantic`, and `sqlalchemy` as dependencies. The code claims to separate domain from adapters but the actual imports show significant violations. No `import-linter` configuration exists, so layer rules are aspirational.

---

## 2. Scripts run

- **package_overview:** 8 modules; 1 god-module candidate (`core/risk.py` at 462 lines, containing 300+ stub helpers); all modules lack `__all__`.
- **analyze_imports:** 4 edges; 1 critical cycle (`core/orders ↔ storage/repo`); no layer-violation enforcement (no `import-linter`).
- **antipattern_scan:** 17 findings across 11 antipattern categories, dominated by primitive-obsession patterns (L12, L11), mutable defaults (L3), async misuse (L16, L19), and structural cycles (L6).

---

## 3. Findings — load-bearing

### 3.1 Cycle between domain and storage violates layering and breaks architecture

**Where.** `core/orders.py` line 5 imports `buggy_app.storage.repo.save_order`; `storage/repo.py` line 4 imports from `core/orders` (via late import inside function).

**Why it matters.** **P2 (The import graph IS the architecture)** — the only architecture is what modules actually import. This cycle violates the core principle of layered architecture: domain must not import adapters. The late import in `repo.py` is a workaround that papers over the real problem rather than solving it. Early detection and enforcement via `import-linter` would have prevented this.

**Fix.** Invert the dependency: move `save_order` logic to the domain, inject a `Repository` Protocol into the domain function, and implement it in `storage/repo.py`. Alternatively, have the API layer orchestrate both domain call and storage call (if domain logic is truly thin).

---

### 3.2 Broad exception handling swallows errors in domain logic

**Where.** `core/orders.py` line 38: `except Exception: pass`.

**Why it matters.** **P1 (Functional core, imperative shell)** and **§F1 (catch narrow)** — swallowing `Exception` at this call site hides bugs and makes debugging impossible. The domain method `submit_order` silently fails if `save_order` raises anything. This is outside the outermost supervisor (no CLI/request handler context), so broad catching is not justified.

**Fix.** Catch specific exceptions or let them propagate. If the intent is to tolerate storage failures gracefully, use domain-specific exceptions and handle them explicitly at the boundary: `except RepositoryError: return Order(..., status="pending_save_retry")`.

---

### 3.3 Mutable dataclass without frozen=True allows accidental state mutation

**Where.** `core/orders.py` line 9-16: `class Order` is a mutable dataclass.

**Why it matters.** **P3 (make illegal states unrepresentable)** via **§D1 (frozen dataclass + slots)** — `Order` is a value type that should be immutable. The absence of `frozen=True` allows code to mutate order state after construction, breaking reasoning about order invariants. This is especially dangerous in a domain object.

**Fix.** Add `@dataclass(frozen=True, slots=True)` to the `Order` class definition.

---

### 3.4 Pydantic BaseModel used for internal domain type, not at boundary

**Where.** `core/orders.py` line 20: `class Position(BaseModel)`.

**Why it matters.** **P5 (frozen dataclass + slots internally; Pydantic only at trust boundaries)** — `Position` is an internal domain type (no indication it's an HTTP request/response or parsed from untrusted input). Using `BaseModel` couples the domain to Pydantic, pays validation overhead on every instantiation of already-trusted data, and is one of the highest-yield refactors in Python codebases.

**Fix.** Replace `class Position(BaseModel)` with `@dataclass(frozen=True, slots=True) class Position`, then convert trusted input into `Position` at the API boundary (via a `PositionRequest` Pydantic model with a `.to_domain()` method).

---

### 3.5 Bare str for status field with no type-driven enforcement

**Where.** `core/orders.py` line 16: `status: str`; values like `"submitted"` are free text.

**Why it matters.** **P3 (make illegal states unrepresentable)** via **§D4** — the `status` field accepts any string, making typos undetectable and refactoring dangerous. Code comparing `status == "submitted"` (not present here but likely elsewhere) is a source of silent bugs.

**Fix.** Define a `Status = Literal["submitted", "filled", ...]` or `class Status(StrEnum)` and use it: `status: Status`.

---

### 3.6 Async tasks created without TaskGroup supervision (fire-and-forget bug)

**Where.** `api/handlers.py` lines 16–17 in `submit_async`: two `asyncio.create_task` calls with no parent `TaskGroup`.

**Why it matters.** **P7 (async only at the I/O boundary; structured concurrency or none)** via **§G2** — orphan tasks (created without a `TaskGroup`) will not propagate exceptions, will not be cancelled when the parent is cancelled, and are a guaranteed source of silent bugs. This is a structural defect in concurrent systems.

**Fix.** Wrap both `create_task` calls in `async with asyncio.TaskGroup() as tg:` and use `tg.create_task()`. Cancellation and exceptions will propagate correctly.

---

## 4. Findings — high-yield

### L3: Mutable default arguments (2 instances)

`core/risk.py` lines 8 and 15: `blocked_symbols: list = []` and `exposures: dict = {}` are shared across calls.

**Fix.** Replace each with `None` sentinel: `blocked_symbols: list | None = None; if blocked_symbols is None: blocked_symbols = []`.

---

### L8: Mutable dataclass without frozen=True (1 instance)

`core/orders.py` line 9: `class Order:` already covered in §3.3 above.

---

### L9: Pydantic BaseModel for internal domain type (1 instance)

`core/orders.py` line 20: `class Position(BaseModel)` already covered in §3.4 above.

---

### L11: Naive datetime without timezone (1 instance)

`core/orders.py` line 33: `submitted_at=datetime.now()` is timezone-naive and ambiguous across timezones.

**Fix.** Replace with `submitted_at=datetime.now(timezone.utc)` (or inject a `Clock` protocol for testability, see §J6).

---

### L12: Float for monetary amounts (3 instances)

`core/orders.py` lines 14, 26, 23: `price: float`, `quantity: int` parameter in function, and `cost_basis: float` in `Position`.

**Why it matters.** Floating-point arithmetic fails for money: `0.1 + 0.2 != 0.3` in binary FP.

**Fix.** Use `Decimal` for all money fields. Wrap in a `Price = NewType("Price", Decimal)` or a `Money` dataclass with currency.

---

### L16: `create_task` without TaskGroup (2 instances)

`api/handlers.py` lines 16–17 already covered in §3.6 above.

---

### L19: Async def with no await (2 instances)

`api/handlers.py` lines 14 and 20: `async def submit_async(...)` and `async def _send_email(...)` contain no `await` statements.

**Why it matters.** Functions marked `async` should await something; if they don't, they should be synchronous. This confuses callers about concurrency guarantees.

**Fix.** Either add `await` calls (if the functions genuinely need to be async) or remove `async` and call them directly from `submit_async` synchronously. If `submit_async` needs to spawn background work, use the `TaskGroup` pattern (§3.6).

---

### L20: Free-text f-string log (1 instance)

`core/risk.py` line 22: `log.warning(f"Risk check failed for {order.symbol} qty={order.quantity}")` is free-text and unstructured.

**Why it matters.** Logs should be structured (JSON, key=value) for querying and alerting. Free-text logs are human-readable but unmachine-readable.

**Fix.** Use structured logging: `log.warning("risk_check_failed", symbol=order.symbol, quantity=order.quantity)` (with `structlog` or similar).

---

### L25: print() in non-CLI modules (2 instances)

`core/risk.py` line 27: `print(f"DEBUG: {order}")`. `storage/repo.py` line 5: `print(f"saved {order.order_id}")`.

**Why it matters.** `print()` goes to stdout, bypasses logging, can't be configured, and pollutes output in production.

**Fix.** Use `logging.debug(...)` / `logging.info(...)` instead.

---

### L5: Star import (1 instance)

`api/handlers.py` line 4: `from buggy_app.core.orders import *`.

**Why it matters.** Pollutes namespace, hides where symbols come from, breaks tooling, and can shadow names silently.

**Fix.** Import explicitly: `from buggy_app.core.orders import Order, submit_order`.

---

### L10: Bare str for status field

`core/orders.py` line 16 already covered in §3.5 above.

---

### L4: Broad except

`core/orders.py` line 38 already covered in §3.2 above.

---

## 5. Findings — worth knowing

### No `import-linter` configuration; layer rules are aspirational

The project has no `[importlinter]` section in `pyproject.toml`. This means the declared layer boundaries (domain/adapters) are prose only, and the cycle discovered by `analyze_imports.py` will not trigger a CI failure. Worth adding when this grows beyond a single file.

**Fix.** Add a `pyproject.toml` section:
```toml
[importlinter]
root_package = "buggy_app"

[[importlinter.contracts]]
name = "Domain doesn't import storage"
type = "forbidden"
source_modules = ["buggy_app.core"]
forbidden_modules = ["buggy_app.storage"]
```

---

### Missing `__all__` declarations on all modules

All 8 modules lack `__all__`, so the public surface is implicit (everything not starting with `_`). For a small tool this is acceptable, but worth adopting if the tool is ever published or consumed by other projects.

---

### `httpx` call without timeout (1 instance)

`api/handlers.py` line 10 in `fetch_market_data`: `client.get(f"https://example.com/quotes/{symbol}")` has no `timeout=` parameter.

**Why it matters.** **§F5** — missing timeouts are the leading cause of hung requests. If the remote service hangs, this coroutine will hang indefinitely.

**Fix.** Add `timeout=30` (or a configurable value): `await client.get(..., timeout=30)`.

---

## 6. What looked good

The codebase is small enough that the violations are isolated and fixable in a day or two. The separation of concerns (api / core / storage) is intentional and mostly enforced, aside from the cycle. Type hints are present on function signatures and class fields, which is a good foundation.

---

## 7. Punch list

Priority ordering balances load-bearing violations against ease of fix:

1. **[structural]** Resolve the cycle between `core/orders` and `storage/repo`. Extract a `Repository` Protocol, define it in the domain layer, implement it in storage. 1–2 hours. (Blocks P2 enforcement.)
2. **[quick win]** Fix L3 mutable defaults in `check_position_limits` and `check_concentration` (2 instances). 15 minutes.
3. **[quick win]** Fix L25 `print()` calls (2 instances) → `logging` calls. 10 minutes.
4. **[quick win]** Fix L12 `float` for money (3 instances) → `Decimal`. 30 minutes.
5. **[quick win]** Fix L11 naive `datetime.now()` (1 instance) → `datetime.now(timezone.utc)`. 10 minutes.
6. **[quick win]** Fix L8 mutable dataclass `Order` → `@dataclass(frozen=True, slots=True)`. 5 minutes.
7. **[quick win]** Fix L4 broad exception handling in `submit_order` (catch specific exceptions or remove the try/except). 15 minutes.
8. **[quick win]** Fix L9 `Position` Pydantic → frozen dataclass. 20 minutes.
9. **[quick win]** Fix L5 star import in `api/handlers.py`. 5 minutes.
10. **[quick win]** Fix L19 async-no-await (2 instances); decide if `submit_async` and `_send_email` should be sync or if background work should use TaskGroup. 20 minutes.
11. **[quick win]** Fix L16 `create_task` without TaskGroup. 10 minutes.
12. **[quick win]** Add `timeout=` to `httpx` call. 5 minutes.
13. **[forward-looking]** Add `import-linter` configuration to prevent future cycles. 30 minutes.
14. **[low-priority]** Structure logging via `structlog` (L20). Deferrable until observability matters more.

Items 2–12 can all be done in a 3–4 hour session. Item 1 (the cycle) should be done first because it enables P2 enforcement.

---

## 8. Open questions for the user

None. The findings are specific and actionable. The codebase is small enough that all recommendations can be implemented and tested in a single day.
