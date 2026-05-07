<!--
This is a scaffold for design briefs. Use it as a starting point; deviate
where the design demands a different shape. The brief is short — typically
1-3 pages. It assumes the reader knows Python and is making implementation
decisions; it is NOT a tutorial.

Sections marked OPTIONAL can be cut if not relevant. Some designs need
none of the optional sections (a CLI tool with a flat shape); some need
all of them (a complex agentic system).

Inline comments throughout (the <!-- ... -->) are guidance for the agent
filling in the template. Strip them before delivering.
-->

# Design — `<system or module name>`

**Date:** `<YYYY-MM-DD>`
**Shape:** `<CLI / web API / data pipeline / long-running daemon / agentic / numerical / library / mixed>`
**Status:** `<draft for review | ready to implement>`

---

## 1. What we're building

<!--
2-3 paragraphs in plain prose. Cover:
  - The problem the system solves, in one sentence the user would agree with
  - The trust boundaries (what comes in, what goes out, what's untrusted)
  - The persistence story in one sentence
  - The concurrency profile in one sentence

This is the section that catches misunderstanding. If the user reads this
and disagrees, the rest of the brief is wasted; spend the lines getting it
right.
-->

---

## 2. Principles guiding this design

<!--
Of the eight load-bearing principles in SKILL.md §2, name the two or three
that are central to this design. State them in one line each, with the
principle ID. The user should be able to push back on any of them and see
the downstream change.

Example shape:
  - **P1 (functional core, imperative shell):** central, because the rules
    around order allocation are non-trivial and we want them testable in
    isolation.
  - **P3 (illegal states unrepresentable):** central, because order status
    drives the whole state machine; encoding it as a Literal/StrEnum from
    the start prevents typo-driven bugs.
  - **P5 (Pydantic at boundary, frozen dataclass inside):** central,
    because we're parsing vendor JSON at ingest and the conversion is the
    natural trust seam.

Don't list all eight. Three is plenty.
-->

---

## 3. Package layout

<!--
A directory tree, feature-organised by default. Concrete enough to start
typing. Use code-block formatting.

Don't pre-create empty subdirectories. Show what's there from day one.

If the layout is non-trivial, follow with one paragraph explaining the
choice (why feature-organised, where the seams are, where shared code
lives, what the import rules are).
-->

```
src/<project>/
├── pyproject.toml
├── <feature_a>/
│   ├── __init__.py        # empty
│   ├── domain.py          # pure functions, frozen dataclasses, Protocols
│   ├── adapters.py        # I/O against the Protocols
│   └── api.py             # entrypoint
└── ...
```

<!--
If you want the boundaries enforced, include the import-linter contract:

[importlinter]
root_package = "<project>"

[[importlinter.contracts]]
name = "Domain doesn't import infrastructure"
type = "forbidden"
source_modules = ["<project>.<feature>.domain"]
forbidden_modules = ["sqlalchemy", "httpx", "<project>.shared.db"]
-->

---

## 4. Types

<!--
The single highest-leverage section. Show concrete types, not abstract
descriptions. The user should be able to copy these into a file and start
working.

Cover at minimum:
  - Identifier types (NewType wrappers)
  - Closed-set fields (StrEnum / Literal)
  - Domain primitives (Money, Symbol, etc.)
  - Boundary types (Pydantic in / dataclass internal / conversion function)

Use Python code blocks. Include type annotations. Include the conversion
function signature if it's a boundary.
-->

```python
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import NewType, Literal
from uuid import UUID
from dataclasses import dataclass

# Identifier types
OrderId = NewType("OrderId", UUID)
UserId  = NewType("UserId",  UUID)

# Closed-set fields
class OrderStatus(StrEnum):
    DRAFT     = "draft"
    SUBMITTED = "submitted"
    FILLED    = "filled"
    CANCELLED = "cancelled"

# Domain primitives
@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: Literal["USD", "EUR", "GBP"]

# Internal domain type
@dataclass(frozen=True, slots=True)
class Order:
    id: OrderId
    user: UserId
    status: OrderStatus
    submitted_at: datetime  # always tz-aware
    total: Money
```

<!--
For each boundary, add the inbound type and the conversion function:

```python
from pydantic import BaseModel

# Boundary type — Pydantic for validation only
class OrderRequest(BaseModel):
    user_id: UUID
    total_amount: Decimal
    currency: str

    def to_domain(self) -> Order:
        return Order(
            id=OrderId(uuid4()),
            user=UserId(self.user_id),
            status=OrderStatus.DRAFT,
            submitted_at=datetime.now(timezone.utc),
            total=Money(self.total_amount, currency=self.currency),  # validate currency
        )
```
-->

---

## 5. Testing seam

<!--
For each I/O-bearing Protocol the design introduces, name what tests
substitute. Be concrete: "fake the OrderRepository Protocol with an
in-memory implementation" is a seam; "use mocks" is not.

Include one example fake skeleton if it clarifies what's expected.
-->

```python
from typing import Protocol

class OrderRepository(Protocol):
    def get(self, order_id: OrderId) -> Order | None: ...
    def save(self, order: Order) -> None: ...

# In tests:
class FakeOrderRepository:
    def __init__(self) -> None:
        self._store: dict[OrderId, Order] = {}
    def get(self, order_id: OrderId) -> Order | None:
        return self._store.get(order_id)
    def save(self, order: Order) -> None:
        self._store[order.id] = order
```

<!--
If the system has multiple I/O seams (HTTP client, broker, LLM, queue),
list each Protocol and the testing strategy for it.

If the design uses real-DB integration tests (testcontainers, SQLite on
disk), name that here.
-->

---

## 6. Hardest-to-reverse decisions

<!--
Three or four decisions that are expensive to undo, with the tradeoffs.
The user gets to make these consciously now; everything else can be
adjusted later.

Each decision: name the choice taken, name the alternative, state the
reason for the pick, and state what would force a rethink.

Examples of decisions that go here:
  - Sync entrypoint vs. async-from-the-top
  - SQLite vs. Postgres
  - Single repo vs. distribution split
  - Whether to commit to events / message bus
  - Whether to introduce DI framework (default: no)
  - Choice of LLM SDK
-->

### 6.1 `<decision name>`

**Picked.** `<the choice>`

**Alternative.** `<the road not taken>`

**Why.** `<one or two sentences. Cite the principle or reference thesis if it informs the call.>`

**Reconsider if.** `<the condition that would change the answer>`

<!-- Repeat 3-4 times. -->

---

## 7. Open decisions OPTIONAL

<!--
Decisions deliberately deferred — explicit so the user knows what's not
yet locked in. Different from "hardest-to-reverse" in that these are
genuinely flexible later.

Example:
  - "Persistence engine for the audit log: leaning SQLite for now;
    revisit when monthly volume exceeds 10M events."
  - "Whether to extract the validation layer as its own module: defer
    until the second consumer appears."

Skip this section if everything's decided.
-->

---

## 8. Implementation starter OPTIONAL

<!--
If the user asked for a starter scaffold rather than just a sketch, this
section provides actual files to create. Otherwise omit.

Format suggestion: file-tree-with-content, one block per file, marked
clearly so the user can copy-paste.

Don't include boilerplate the user can generate (uv init output, pytest
config) unless the configuration is non-default.
-->

---

<!--
Pre-deliverable check (from SKILL.md §7):

  - [ ] Every recommendation cites a principle (P1-P8) or reference thesis.
  - [ ] Every "don't" has a concrete "do this instead".
  - [ ] The shape is named explicitly.
  - [ ] Domain primitives and boundary types are concrete code, not prose.
  - [ ] Testing seam is named with concrete Protocols and fakes.
  - [ ] Hardest-to-reverse decisions are explicit.
  - [ ] No generic Python tutorial content.

Strip all <!-- ... --> comments before delivering.
-->
