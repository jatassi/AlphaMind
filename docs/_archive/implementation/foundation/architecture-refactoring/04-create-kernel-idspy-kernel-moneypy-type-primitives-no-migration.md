# 04 — Create `_kernel/ids.py` + `_kernel/money.py` (type primitives, no migration)

## Goal

Introduce the type primitives that the audit's L4 finding requires for a money-handling system: `NewType` aliases for every named identifier (`OrderId`, `PositionId`, `BracketId`, `CommandId`, `AlpacaOrderId`, `ClientOrderId`, `EnvelopeId`, `InvocationId`, `ThesisId`, `Symbol`, `OccSymbol`) in `alphamind/_kernel/ids.py`, and `Money` / `Price` Decimal-backed primitives in `alphamind/_kernel/money.py`. This story creates the types and tests them in isolation; the migration of consumer code to use these types is split across stories 05a (IDs) and 05b (Money) so the type creation lands quickly and the migrations dispatch in parallel.

The audit calls this "the single highest-yield finding system-wide": zero `Decimal` in 133K LOC of trading code, zero `NewType` for IDs, 115 `float`-for-money sites.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L4 — full motivation
* `.claude/skills/python-architecture/references/data-and-types.md` § D2 (parse-don't-validate), § D3 (Money primitive shape)
* `.claude/skills/python-architecture/references/foundations.md` § A3 — make illegal states unrepresentable
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decisions (G) — system-wide migration scope
* `src/alphamind/_kernel/__init__.py` — empty package created by 01b; this story populates it further
* `src/alphamind/_kernel/regime.py`, `_kernel/calibration.py` — sibling modules created by 02a; mirror their style and `__all__` discipline

## Depends on

* 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/` must exist with `regime.py` / `calibration.py` as the shape template

## Scope

In scope: two new files under `src/alphamind/_kernel/`. Tests at `tests/_kernel/test_ids.py` and `tests/_kernel/test_money.py`. **No migration of consumers** — that's stories 05a and 05b.

### 1\. `_kernel/ids.py`

```python
"""NewType aliases for identifiers across AlphaMind.

Each alias is a distinct type for the type checker but a plain str at
runtime; passing an EnvelopeId where a PositionId is expected fails
mypy --strict.
"""
from __future__ import annotations
from typing import NewType
from uuid import UUID

OrderId = NewType("OrderId", str)
PositionId = NewType("PositionId", str)
BracketId = NewType("BracketId", str)
CommandId = NewType("CommandId", str)
AlpacaOrderId = NewType("AlpacaOrderId", str)
ClientOrderId = NewType("ClientOrderId", str)
EnvelopeId = NewType("EnvelopeId", str)
InvocationId = NewType("InvocationId", str)
ThesisId = NewType("ThesisId", str)
Symbol = NewType("Symbol", str)
OccSymbol = NewType("OccSymbol", str)

__all__ = [...]
```

Each NewType also gets a constructor function with the regex/format validation that today lives at Pydantic model boundaries. Example:

```python
import re
_ENVELOPE_ID_RE = re.compile(r"^ENV-(REC|SA|SA-ORD)-[0-9]+$")

def envelope_id(value: str) -> EnvelopeId:
    """Construct an EnvelopeId, validating the regex pattern once at the boundary."""
    if not _ENVELOPE_ID_RE.match(value):
        raise ValueError(f"invalid envelope id: {value!r}")
    return EnvelopeId(value)
```

Constructor functions for every ID with a known regex pattern (envelope IDs, command IDs at minimum). For Symbol/OccSymbol, validation rules come from the asset universe — defer to story 05a where consumers are migrated and the right validation source surfaces.

### 2\. `_kernel/money.py`

```python
"""Money and Price primitives.

Money: USD amount, NewType("Money", Decimal). Construct via `money(value)`
which accepts str/int/Decimal and validates non-negative for typical use
(operations producing negative amounts — e.g., losses — pass via constructor
that allows negative explicitly).

Price: NewType("Price", Decimal). Construct via `price(value)` validating > 0.

The Alpaca SDK returns monetary fields as str; parse-at-boundary via
money(broker_str). Internal arithmetic stays on Decimal.
"""
from __future__ import annotations
from decimal import Decimal
from typing import NewType

Money = NewType("Money", Decimal)
Price = NewType("Price", Decimal)

def money(value: str | int | Decimal) -> Money: ...
def price(value: str | int | Decimal) -> Price: ...
def signed_money(value: str | int | Decimal) -> Money: ...  # for losses, debits, etc.

__all__ = ["Money", "Price", "money", "price", "signed_money"]
```

Provide arithmetic helpers if useful (`add_money(a, b) -> Money`, `multiply_price_qty(p, q) -> Money`) — but only those the migration stories will actually use. Don't pre-design a full Money algebra.

### 3\. Tests

* `tests/_kernel/test_ids.py` — each NewType is a distinct type at type-check time (test via `mypy --strict` on the test file); constructor validation accepts valid IDs and rejects invalid; the alias is `str` at runtime so existing string operations work.
* `tests/_kernel/test_money.py` — `money("100.50")` returns `Money` typed value, `Decimal("100.50")` at runtime; `money("abc")` raises; arithmetic preserves precision (`money("0.1") + money("0.2") == money("0.3")`).

### Out of scope

Migration of any consumer to use these types — stories 05a (IDs) and 05b (Money). This story creates and tests the types in isolation.

## Acceptance criteria

- [ ] `src/alphamind/_kernel/ids.py` exists with all 11 NewType aliases and constructor functions for envelope/command IDs.
- [ ] `src/alphamind/_kernel/money.py` exists with `Money`, `Price`, `money()`, `price()`, `signed_money()` defined.
- [ ] Both modules have populated `__all__`.
- [ ] Both modules import zero first-party (`alphamind.*`) modules (stdlib + Pydantic if needed only — but stdlib `Decimal` + `NewType` should suffice).
- [ ] `tests/_kernel/test_ids.py` and `tests/_kernel/test_money.py` exist and pass under `uv run pytest -n auto`.
- [ ] `mypy --strict` passes on `src/alphamind/_kernel/`.
- [ ] `money("0.1") + money("0.2")` returns `Money` equal to `money("0.3")` (Decimal arithmetic, no binary drift).
- [ ] `uv run lint-imports` continues to pass (kernel-leaf contract from story 03 holds).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

Import smoke-test: `python -c "from alphamind._kernel.ids import OrderId, PositionId, EnvelopeId; from alphamind._kernel.money import money, price; print(money('100.50') + money('0.25'))"` prints `Decimal('100.75')`. Synthetic mypy check: `def f(o: PositionId) -> None: ...` called with `f(envelope_id("ENV-REC-1"))` fails mypy with an incompatible-type error (the two NewTypes are distinct).