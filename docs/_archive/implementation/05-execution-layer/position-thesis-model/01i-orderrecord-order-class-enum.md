# 01i — OrderRecord order_class enum + audit OrderDuration members

## Goal

Two changes to `src/alphamind/portfolio_state/records/orders.py`, both in the same file:

**(A) Add** `OrderClass` enum and field. Add an `OrderClass` StrEnum (`SIMPLE | BRACKET | OCO | OTO | MLEG`) and an additive `order_class: OrderClass = OrderClass.SIMPLE` field to `OrderRecord`. The design's tier-2 vocabulary (orders-and-brackets.md § Tier 2 — Contingent orders) names these contingent-order classes; the broker-adapter doc (broker-adapter.md:79-80) confirms the Alpaca mapping (`order_class: bracket`, `order_class: mleg`). Today `OrderRecord` has `role` (purpose within bracket) and `order_type` (atomic order type) but no representation of the contingent-order class — multi-leg strategy orders are unrepresentable as a typed concept. Add a structural validator that `MLEG` order_class requires the order's instrument_spec to have `instrument_type=STRATEGY`. (Audit finding I6.)

**(B) Audit and prune** `OrderDuration` members. Decide whether to keep or remove `IOC` (immediate-or-cancel) and `FOK` (fill-or-kill) from the `OrderDuration` enum. Both are HFT-style millisecond-precision execution semantics with no current AlphaMind use case (the system runs on 4–72h trading horizons; entries are limit/stop with reasonable timeouts; exits triggered by stops are market orders). They're likely included because Alpaca's API exposes them, not because the design needs them. Per `feedback_simplify_before_building`, default to **delete** unless a concrete use case is identified during implementation. If keeping, add a docstring naming the scenarios that legitimately use them. (Audit finding I8.)

## Reading

* `src/alphamind/portfolio_state/records/orders.py:166-192` — current `OrderRecord` fields (where `order_class` field will be added).
* `src/alphamind/portfolio_state/records/orders.py:13-20` — existing `OrderRole` enum (ENTRY / TAKE_PROFIT / PRICE_STOP / TIME_STOP / CLOSE / ADD_ENTRY) — note this is leg-purpose, distinct from the contingent-class concept.
* `src/alphamind/portfolio_state/records/orders.py:22-26` — existing `OrderType` enum (MARKET / LIMIT / STOP / STOP_LIMIT) — the atomic-order-type concept.
* `src/alphamind/portfolio_state/records/orders.py:38-43` — existing `OrderDuration` enum (DAY / GTC / GTD / IOC / FOK) — the audit target for change (B).
* `docs/design/05-execution-layer/orders-and-brackets.md` § Tier 2 — Contingent orders — defines bracket / OCO / OTO; confirms these are how the design represents lifecycle-linked groups.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Multi-leg strategies — defines the `mleg` Alpaca order class for multi-leg combinations.
* `docs/design/05-execution-layer/broker-adapter.md:79-80` — Alpaca order_class values (`bracket`, `mleg`) and how they map to the OMS.
* Search `grep -rn "OrderDuration\.\(IOC\|FOK\)" src/ tests/` — to identify any existing fixture or producer that uses IOC or FOK; if any non-test site uses them, that's a counter-argument to deletion the agent must surface to the operator before proceeding.
* `tests/portfolio_state/records/test_orders.py::TestOrderRole` — pattern for adding a new enum test class.
* `tests/portfolio_state/records/test_orders.py::TestOrderDuration` — existing tests that may assert IOC/FOK membership; will need updating if those members are removed.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule applies for change (A); change (B) is a small breaking change to the enum if deletion is chosen.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates.

## Scope

Source under `src/alphamind/portfolio_state/records/orders.py` (one new enum + one field add to `OrderRecord` + cross-field validator + the IOC/FOK decision applied). Tests at `tests/portfolio_state/records/test_orders.py` (new `TestOrderClass` test class + `OrderRecord`-MLEG cross-validation tests + `TestOrderDuration` updates per the IOC/FOK decision).

### 1\. Add the `OrderClass` enum

In `src/alphamind/portfolio_state/records/orders.py`, alongside the other top-level enums:

```python
class OrderClass(StrEnum):
    """Contingent-order class per orders-and-brackets.md § Tier 2.

    SIMPLE — atomic single order; no contingent linkage. Default.
    BRACKET — entry + take-profit + stop, submitted as a unit linked to a thesis.
    OCO — one-cancels-other; pair of orders where filling one cancels the other.
    OTO — one-triggers-other; activates contingent orders only on its own fill.
    MLEG — multi-leg options strategy submitted as a single Alpaca mleg order;
           requires the instrument_spec to have instrument_type=STRATEGY.
    """

    SIMPLE = "SIMPLE"
    BRACKET = "BRACKET"
    OCO = "OCO"
    OTO = "OTO"
    MLEG = "MLEG"
```

### 2\. Add `order_class` field to `OrderRecord`

In `OrderRecord`:

```python
class OrderRecord(BaseModel):
    ...
    order_class: OrderClass = OrderClass.SIMPLE
    ...
```

Place adjacent to `order_type` (the related-but-distinct atomic-type field). Default `SIMPLE` preserves backward compatibility for existing fixtures that don't set the field.

### 3\. Add cross-field validator: MLEG ↔ STRATEGY

```python
@model_validator(mode="after")
def _validate_mleg_requires_strategy(self) -> OrderRecord:
    if self.order_class == OrderClass.MLEG:
        if self.instrument_spec.instrument_type != InstrumentType.STRATEGY:
            msg = (
                f"order_class=MLEG requires instrument_spec.instrument_type=STRATEGY; "
                f"got {self.instrument_spec.instrument_type!r}"
            )
            raise ValueError(msg)
    return self
```

This catches the structural error of declaring an MLEG order whose underlying isn't actually a multi-leg strategy.

### 4\. Audit `OrderDuration` IOC/FOK members

First, search the codebase for any existing use:

```bash
grep -rn "OrderDuration\.\(IOC\|FOK\)" src/ tests/
grep -rn "\"IOC\"\|\"FOK\"" src/ tests/  # in case of string-typed references
```

**If non-test sites use IOC or FOK:** surface to the operator; do not unilaterally delete.

**If only test fixtures (or no sites) reference them:** delete `IOC` and `FOK` enum members. Update `OrderDuration`'s docstring to document the design-supported set: `DAY`, `GTC`, `GTD` cover the 4–72h horizon; HFT-style immediate-execution semantics are out of scope. Update `tests/portfolio_state/records/test_orders.py::TestOrderDuration` to:

* Remove the IOC/FOK enum-membership assertions.
* Add a test asserting only `{DAY, GTC, GTD}` are members (so future re-additions trip a test).

If keeping (operator's call after a surfaced concrete use case): add a docstring section to `OrderDuration` naming the scenarios where IOC/FOK apply and add fixture coverage exercising at least one IOC and one FOK order.

### 5\. Module exports

Update `src/alphamind/portfolio_state/records/orders.py` (no `__all__` currently; add `OrderClass` to the natural export surface — consumers will import it directly).

### Out of scope

* Adding cross-field invariants for BRACKET / OCO / OTO (e.g., "BRACKET order_class implies the order has a position_id and bracket_id"). The relationships are mostly enforced by the surrounding `BracketRecord` already; over-constraining now blocks legitimate edge cases.
* Updating the broker adapter (<issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue>) to actually serialize `order_class` to Alpaca — this story ships the typed field; the broker adapter consumes it later.
* Adding `order_class` to `BracketRecord` — bracket-class is implied by the existence of a `BracketRecord` with `protective_legs`, no second source of truth needed.

## Acceptance criteria

- [ ] `OrderClass` StrEnum exists with five members (SIMPLE / BRACKET / OCO / OTO / MLEG) and is importable from `alphamind.portfolio_state.records.orders`.
- [ ] `OrderRecord.order_class: OrderClass = OrderClass.SIMPLE` field exists.
- [ ] `OrderClass` enum docstring documents each member's contingent-class semantics.
- [ ] All existing `tests/portfolio_state/records/test_orders.py` tests pass after the field add (additive default = SIMPLE preserves compat).
- [ ] All existing `uv run pytest -n auto` tests pass — no pre-existing test fails.
- [ ] Constructing `OrderRecord(...)` without specifying `order_class` succeeds with the field defaulting to `OrderClass.SIMPLE`.
- [ ] Constructing `OrderRecord(..., order_class=OrderClass.MLEG, instrument_spec=<EQUITY-spec>)` raises `ValueError` whose message names the actual instrument_type.
- [ ] Constructing `OrderRecord(..., order_class=OrderClass.MLEG, instrument_spec=<STRATEGY-spec with valid legs>)` succeeds.
- [ ] Constructing `OrderRecord(..., order_class=OrderClass.BRACKET, instrument_spec=<EQUITY-spec>)` succeeds (no MLEG-style cross-check applied to other classes).
- [ ] `tests/portfolio_state/records/test_orders.py` has a new `TestOrderClass` test class exercising all five enum members and StrEnum semantics, plus `TestOrderRecord` extensions for: (a) default SIMPLE on construction, (b) MLEG-without-STRATEGY rejected, (c) MLEG-with-STRATEGY accepted, (d) other classes accepted with any instrument_type.
- [ ] **IOC/FOK audit:** the grep-search results from §4 are documented in the PR description (which sites use them, if any). The decision (delete vs document) is reflected in `OrderDuration`'s code and docstring.
- [ ] If IOC/FOK deleted: `OrderDuration` membership is exactly `{DAY, GTC, GTD}`; `TestOrderDuration` asserts this exact set.
- [ ] If IOC/FOK kept: `OrderDuration` docstring names the use cases; at least one fixture each exercises an IOC and FOK order in `TestOrderDuration` or `TestOrderRecord`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/portfolio_state/records/test_orders.py -n auto -v` — confirm new `TestOrderClass` and extended `TestOrderRecord` tests pass; `TestOrderDuration` reflects the audit decision.
* Spot-check by `python -c "from alphamind.portfolio_state.records.orders import OrderClass, OrderRecord, OrderDuration; print(list(OrderClass)); print(list(OrderDuration)); print('order_class' in OrderRecord.model_fields)"`.
* Lint clean per CLAUDE.md.