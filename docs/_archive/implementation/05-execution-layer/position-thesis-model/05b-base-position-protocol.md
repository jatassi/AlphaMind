# 05b — Base-position `Protocol` declaring the design's "base interface"

## Goal

Add a `BasePositionProtocol` runtime-checkable Protocol declaring the design's "base position interface" (per `docs/design/05-execution-layer/position-model.md` line 3: "An instrument hierarchy with a shared base interface: downstream consumers reason about any position generically through the base, or inspect instrument-specific details when needed"). Today every consumer reaches into `PositionRecord` directly, so changes to the record shape propagate as breakage across all 50+ consumers. The Protocol formalizes the contract: consumers depending only on the base interface (direction, entry timestamp, market value, etc.) are insulated from instrument-specific schema evolution. Pure additive — no behavior changes, just an interface declaration.

## Reading

* `docs/design/05-execution-layer/position-model.md` § Base position — the design-named interface this story formalizes.
* `src/alphamind/portfolio_state/records/positions.py` — current `PositionRecord` (post-04b: with `details: PositionDetailsPayload` discriminated union).
* `src/alphamind/portfolio_state/views/positions.py` — `PositionView` (post-05a) — the consumer-facing wrapper.
* Pydantic + Protocol interaction: Pydantic models implement Protocols structurally (no inheritance needed); `runtime_checkable` enables `isinstance(x, BasePositionProtocol)` for sanity checks.
* All consumer files that currently read fields the Protocol covers — they don't change in behavior, but their type annotations CAN narrow to `BasePositionProtocol` instead of `PositionRecord` when they only need base fields.
* <issue id="3aac15c1-a657-4ba5-afe4-a609ea32e4f7">ALP-348</issue> (story 04b) — must land first; the Protocol's field set references the post-04b `details` shape.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Architectural invariants — affirms protocol-based decoupling as a goal.

## Depends on

* <issue id="3aac15c1-a657-4ba5-afe4-a609ea32e4f7">ALP-348</issue> (story 04b) — Protocol field set depends on the post-discriminated-union shape.

This story does NOT depend on 05a — Protocol can declare both `PositionRecord`'s and `PositionView`'s shared fields.

## Scope

Source under `src/alphamind/portfolio_state/protocols/positions.py` (new file) defining `BasePositionProtocol` + tests. Optional: identify 5-10 consumer sites where the type annotation can narrow to the Protocol; update those.

### 1\. Define the Protocol

New file `src/alphamind/portfolio_state/protocols/positions.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from alphamind.portfolio_state.records.positions import (
    Direction,
    PositionStatus,
)


@runtime_checkable
class BasePositionProtocol(Protocol):
    """The design's 'base position interface' — fields every position has,
    regardless of instrument type (equity / options / strategy).

    Consumers that reason about positions generically (sector exposure
    aggregation, P/L summation, lifecycle tracking) should annotate against
    this Protocol rather than the concrete PositionRecord. This insulates
    the consumer from instrument-specific schema evolution and from the
    PositionRecord vs PositionView split (story 05a).

    Per docs/design/05-execution-layer/position-model.md § Base position.
    """

    @property
    def position_id(self) -> str: ...

    @property
    def thesis_id(self) -> str | None: ...

    @property
    def bracket_id(self) -> str | None: ...

    @property
    def status(self) -> PositionStatus: ...

    @property
    def direction(self) -> Direction: ...

    @property
    def entry_timestamp(self) -> datetime | None: ...
```

The Protocol intentionally captures only the design-named "base interface" fields. Consumers needing instrument-specific or computed fields use the concrete types (`PositionRecord`, `PositionView`).

### 2\. Sanity-check structural conformance

Add tests at `tests/portfolio_state/protocols/test_positions.py` asserting:

* `PositionRecord` (an instance) is recognized by `isinstance(record, BasePositionProtocol)`.
* `PositionView` (an instance) is also recognized — by virtue of `record` access through the wrapper. (Note: this requires `PositionView` to expose property-style access to `position_id` etc., which the operator chose in story 05a §4 — convenience properties.)
* A non-position object fails the isinstance check.

### 3\. Optional: narrow consumer annotations

Identify a handful of consumer sites where the function only needs base-interface fields (e.g., `compute_sector_exposure` reads `direction` and `position_id`; pure base). Narrow their type annotations from `PositionRecord` to `BasePositionProtocol`. This is optional; do whichever the agent finds time for, no minimum.

### Out of scope

* Adding analogous Protocols for `OrderRecord`, `BracketRecord`, `ThesisRecord` — separate stories if needed.
* Migrating all 50+ consumers to the Protocol annotation — far too disruptive; the Protocol's value is forward-looking (new code uses it; legacy code stays as is).

## Acceptance criteria

- [ ] `BasePositionProtocol` exists at `src/alphamind/portfolio_state/protocols/positions.py`, runtime-checkable.
- [ ] Protocol fields cover the design's base-interface set: `position_id`, `thesis_id`, `bracket_id`, `status`, `direction`, `entry_timestamp`.
- [ ] `PositionRecord` instances pass `isinstance(record, BasePositionProtocol)`.
- [ ] `PositionView` instances (with convenience property accessors per 05a §4) also pass `isinstance`.
- [ ] Non-position objects (e.g., a dict, an `OrderRecord`) fail `isinstance`.
- [ ] Tests at `tests/portfolio_state/protocols/test_positions.py` exercise each acceptance criterion above.
- [ ] All existing `uv run pytest -n auto` tests pass — additive only.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest tests/portfolio_state/protocols/ -n auto -v` — confirm tests pass.
* Spot-check: `python -c "from alphamind.portfolio_state.protocols.positions import BasePositionProtocol; print(BasePositionProtocol.__protocol_attrs__)"` should list the field names.
* Lint clean per CLAUDE.md.