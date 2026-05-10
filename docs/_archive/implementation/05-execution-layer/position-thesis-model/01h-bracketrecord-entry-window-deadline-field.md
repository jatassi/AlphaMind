# 01h — `BracketRecord` `entry_window_deadline` field

## Goal

Add an additive `entry_window_deadline: datetime | None` field to `BracketRecord` (default `None`, tz-aware UTC when populated). The design names `entry_window` as a thesis/order field the analyst proposes and the PM evaluates ("an `entry_window` deadline post-dates the catalyst timestamp when `decay_type` is `binary`" — `docs/design/04-decision-layer/portfolio-manager.md:143`); the analyst's output schema defines it. After the analyst proposes a bracket with an `entry_window`, the OMS persistence layer drops it because there's no field on `BracketRecord` to capture it. This story closes that gap by adding the field with a tz-aware-UTC validator. Consumers (OMS commands <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> OPEN handler, strategist's pending-order review, breach behavior's emergency invocation triggers) need this to enforce or surface the entry-window deadline.

## Reading

* `src/alphamind/portfolio_state/records/orders.py:325-336` — current `BracketRecord` (bracket_id, position_id, status, entry_order_id, protective_legs, modification_history, corporate_action_cancellation_reason).
* `docs/design/04-decision-layer/analyst-output-schema.md` — see `entry_window` definition in the analyst's recommendation schema (analyst-side proposal).
* `docs/design/04-decision-layer/portfolio-manager.md:140-145` — PM's timing-plausibility criterion reads `entry_window` deadline against the catalyst timestamp.
* `docs/design/04-decision-layer/strategist.md` § Pending order review — strategist evaluates entry orders' `order_age_hours` against the implied entry window.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Mandatory thesis-linked brackets — confirms the bracket binds the entry leg to its protective legs; the entry window applies to the bracket's lifecycle (the deadline at which the entry order should auto-cancel if unfilled).
* `tests/portfolio_state/records/test_orders.py` — existing test patterns for BracketRecord; add a small new test section.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule applies.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it adds one additive field to `BracketRecord`.

## Scope

Source under `src/alphamind/portfolio_state/records/orders.py` (one field add to `BracketRecord` + tz-aware validator). Tests at `tests/portfolio_state/records/test_orders.py` (extend existing `BracketRecord` test classes).

### 1\. Add the `entry_window_deadline` field

In `src/alphamind/portfolio_state/records/orders.py` `BracketRecord`:

```python
from datetime import datetime
from pydantic import field_validator

class BracketRecord(BaseModel):
    """Consumer-facing per-bracket record."""

    model_config = ConfigDict(frozen=True)

    bracket_id: str
    position_id: str
    status: BracketStatus
    entry_order_id: str
    protective_legs: tuple[BracketLeg, ...]
    modification_history: tuple[BracketLegModification, ...]
    corporate_action_cancellation_reason: str | None
    entry_window_deadline: datetime | None = None

    @field_validator("entry_window_deadline")
    @classmethod
    def _require_tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            msg = "entry_window_deadline must be tz-aware UTC when not None"
            raise ValueError(msg)
        return v
```

The field defaults to `None` so all 279 existing import sites and fixtures continue to construct `BracketRecord` without edits.

### 2\. Cross-field semantics (no validator, just docstring)

Document on the `BracketRecord` class:

* When `entry_window_deadline is not None` AND `status == PENDING_ENTRY`: the entry order should auto-cancel if `now() > entry_window_deadline` and the entry hasn't filled. Enforcement is the OMS's responsibility (story <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue>), not the record's.
* When `status in {ACTIVE, COMPLETED, DISSOLVED}`: the field is informational only — the entry has already filled (ACTIVE) or the bracket has resolved (COMPLETED/DISSOLVED).

No structural invariant added — producers that don't know the entry window can leave it None even on PENDING_ENTRY (graceful degradation; the analyst proposing the bracket is responsible for setting it).

### Out of scope

* Implementing the auto-cancel-when-deadline-passes behavior — that's the OMS commands work tree (<issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue>) Phase 2 OPEN handler.
* Adding a complementary field on `OrderRecord` (the entry order itself) — `entry_window_deadline` lives at bracket level since the bracket binds the entry to its protective legs and the deadline applies to the bracket's lifecycle.
* Cross-validating that `entry_window_deadline > entry_order.submission_timestamp` — the OPEN handler enforces this before constructing the bracket.

## Acceptance criteria

- [ ] `BracketRecord.entry_window_deadline: datetime | None = None` field exists in `alphamind.portfolio_state.records.orders`.
- [ ] Field validator requires tz-aware UTC when non-None; naive `datetime.now()` raises `ValidationError`.
- [ ] All existing `tests/portfolio_state/records/test_orders.py` tests pass after the field add (additive default = None preserves compat).
- [ ] All existing `uv run pytest -n auto` tests pass — no pre-existing test fails.
- [ ] Constructing `BracketRecord(..., entry_window_deadline=datetime(2026, 5, 6, 18, 0, tzinfo=UTC))` succeeds.
- [ ] Constructing `BracketRecord(..., entry_window_deadline=datetime(2026, 5, 6, 18, 0))` (naive) raises `ValidationError`.
- [ ] Constructing `BracketRecord(...)` without specifying `entry_window_deadline` succeeds with the field defaulting to None (most common case for legacy fixtures).
- [ ] `BracketRecord` class docstring documents the lifecycle semantics described in §2 (auto-cancel responsibility, status-dependent meaning).
- [ ] `tests/portfolio_state/records/test_orders.py` has a new test class or section exercising: (a) None default, (b) tz-aware datetime accepted, (c) naive datetime rejected, (d) PENDING_ENTRY with non-None deadline succeeds, (e) ACTIVE status with non-None deadline succeeds (informational only).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/portfolio_state/records/test_orders.py -n auto -v` — confirm new tests pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.orders import BracketRecord; print('entry_window_deadline' in BracketRecord.model_fields)"` should print `True`.
* Lint clean per CLAUDE.md.