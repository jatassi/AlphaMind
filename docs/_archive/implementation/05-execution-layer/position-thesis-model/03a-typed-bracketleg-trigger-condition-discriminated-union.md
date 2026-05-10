# 03a — Typed `BracketLeg.trigger_condition` discriminated union

## Goal

Replace `BracketLeg.trigger_condition: str` (free-form string) with a discriminated union of typed payloads keyed on `leg_type`: `PriceTrigger { underlying_ticker, threshold_usd, direction: Literal["GTE","LTE"] }`, `TimeTrigger { deadline: datetime }`, `EventTrigger { description: str, condition_evaluator_id: str | None }`. The current str field stuffs four semantically distinct concepts into one untyped slot — code-smell evidence is `_parse_price_trigger` (computations/positions.py:158) and `_trigger_price_from_condition` (decision/strategist/input_bundle.py:388), both regex-parsing the string to extract a price. Brittle, lossy, and forces every consumer to re-parse strings. The typed union enables static-typed access (e.g., `leg.trigger.threshold_usd` for price triggers, `leg.trigger.deadline` for time triggers), eliminates the parse-then-validate pattern, and lets Pydantic enforce that the trigger payload matches the leg_type.

## Reading

* `src/alphamind/portfolio_state/records/orders.py:304-322` — current `BracketLeg` with `trigger_condition: str` field.
* `src/alphamind/portfolio_state/records/orders.py:62-67` — `BracketLegType` enum (TAKE_PROFIT / PRICE_STOP / TIME_EXPIRATION / EVENT_INVALIDATION) — the discriminator key.
* `src/alphamind/portfolio_state/computations/positions.py:158-167` — `_parse_price_trigger(trigger_condition: str) → float | None` regex parser. Replaced by direct attribute access on the typed payload.
* `src/alphamind/portfolio_state/computations/positions.py:182-225` — `compute_distance_to_target_usd` and `compute_distance_to_stop_usd` callers; updates to use the typed payload.
* `src/alphamind/decision/strategist/input_bundle.py:388` — `_trigger_price_from_condition(leg.trigger_condition)` — same parsing pattern; updates to typed access.
* `src/alphamind/decision/portfolio_manager/input_bundle.py:362,364` — renders `target leg.trigger_condition` and `stop leg.trigger_condition` for the PM bundle. Updates to render the typed payload appropriately.
* `src/alphamind/decision/strategist/input_bundle.py:444-450` — strategist bundle renders all four leg types' trigger conditions; updates to render typed payloads.
* `src/alphamind/scripts/verify_strategist.py:408,417` — fixture sites with `trigger_condition="price >= target"` / `"price <= stop"` strings; replace with typed `PriceTrigger` instances.
* `tests/portfolio_state/records/test_orders.py::TestBracketLegEventInvalidation` and surrounding test classes — patterns for adding a new test class for the discriminated union.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Three invalidation types — confirms the four trigger semantics: price (against underlying), time (deadline), event (qualitative condition).
* `docs/design/05-execution-layer/orders-and-brackets.md` § Options price-based stops — confirms PRICE_STOP triggers on the underlying equity stream (the `underlying_ticker` field in `PriceTrigger`).
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule does NOT apply here; this is a TYPE CHANGE. The story covers updating all consumers.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates. Recommended dispatch order: after wave 1 + 02 to minimize merge-conflict surface, but no strict dependency.

## Scope

Source under `src/alphamind/portfolio_state/records/orders.py` (replace `trigger_condition: str` with discriminated union; add three new typed-payload classes), `src/alphamind/portfolio_state/computations/positions.py` (remove `_parse_price_trigger`, update `compute_distance_to_target_usd` / `compute_distance_to_stop_usd` to typed access), `src/alphamind/decision/strategist/input_bundle.py` and `src/alphamind/decision/portfolio_manager/input_bundle.py` (update renderers). Tests at `tests/portfolio_state/records/test_orders.py` (replace string-based trigger tests with typed-payload tests) plus updates to all fixture sites.

### 1\. Define the three trigger payload classes

In `src/alphamind/portfolio_state/records/orders.py`:

```python
from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PriceTrigger(BaseModel):
    """Trigger for TAKE_PROFIT and PRICE_STOP legs.

    Evaluates against the underlying equity's real-time price stream
    (per orders-and-brackets.md § Options price-based stops). For equity
    positions, underlying_ticker is the equity itself; for options
    positions, it is the option's underlying equity.
    """

    model_config = ConfigDict(frozen=True)

    trigger_type: Literal["price"] = "price"
    underlying_ticker: str = Field(min_length=1)
    threshold_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    direction: Literal["GTE", "LTE"]


class TimeTrigger(BaseModel):
    """Trigger for TIME_EXPIRATION legs.

    Fires when wall-clock time crosses the deadline.
    """

    model_config = ConfigDict(frozen=True)

    trigger_type: Literal["time"] = "time"
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "deadline must be tz-aware UTC"
            raise ValueError(msg)
        return v


class EventTrigger(BaseModel):
    """Trigger for EVENT_INVALIDATION legs.

    Qualitative condition the analysis pipeline evaluates. The engine
    cannot enforce mechanically; the PM acts on the flag.
    """

    model_config = ConfigDict(frozen=True)

    trigger_type: Literal["event"] = "event"
    description: str = Field(min_length=1)
    condition_evaluator_id: str | None = None


TriggerPayload = Annotated[
    PriceTrigger | TimeTrigger | EventTrigger,
    Field(discriminator="trigger_type"),
]
```

### 2\. Update `BracketLeg`

```python
class BracketLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    leg_id: str
    leg_type: BracketLegType
    order_id: str | None
    trigger: TriggerPayload  # replaces trigger_condition: str
    enforcement: BracketLegEnforcement
    status: BracketLegStatus
    pl_based: bool

    @model_validator(mode="after")
    def _validate_event_invalidation(self) -> BracketLeg:
        if self.leg_type == BracketLegType.EVENT_INVALIDATION and self.order_id is not None:
            msg = "EVENT_INVALIDATION leg must have order_id as None"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_trigger_matches_leg_type(self) -> BracketLeg:
        expected_type = {
            BracketLegType.TAKE_PROFIT: "price",
            BracketLegType.PRICE_STOP: "price",
            BracketLegType.TIME_EXPIRATION: "time",
            BracketLegType.EVENT_INVALIDATION: "event",
        }[self.leg_type]
        if self.trigger.trigger_type != expected_type:
            msg = (
                f"leg_type={self.leg_type!r} requires trigger_type={expected_type!r}; "
                f"got trigger_type={self.trigger.trigger_type!r}"
            )
            raise ValueError(msg)
        return self
```

The leg_type ↔ trigger_type cross-validator catches the structural error of putting a TimeTrigger on a PRICE_STOP leg.

### 3\. Remove `_parse_price_trigger` and update consumers

In `src/alphamind/portfolio_state/computations/positions.py`:

* Delete `_parse_price_trigger` (no longer needed).
* `compute_distance_to_target_usd`: change from `_parse_price_trigger(leg.trigger_condition)` to `leg.trigger.threshold_usd if isinstance(leg.trigger, PriceTrigger) else None`.
* `compute_distance_to_stop_usd`: same pattern.

In `src/alphamind/decision/strategist/input_bundle.py`:

* Delete `_trigger_price_from_condition` (no longer needed).
* Replace caller usage with direct typed access.

In `src/alphamind/decision/strategist/input_bundle.py:444-450` (the renderer):

```python
if isinstance(target_leg.trigger, PriceTrigger):
    lines.append(f"    target: {target_leg.trigger.underlying_ticker} {target_leg.trigger.direction} ${target_leg.trigger.threshold_usd}")
elif isinstance(time_leg.trigger, TimeTrigger):
    lines.append(f"    time deadline: {time_leg.trigger.deadline.isoformat()}")
elif isinstance(event_leg.trigger, EventTrigger):
    lines.append(f'    event invalidation: "{event_leg.trigger.description}"')
```

In `src/alphamind/decision/portfolio_manager/input_bundle.py:362,364`: similar updates.

### 4\. Update fixtures

Every fixture currently passing `trigger_condition="..."` to `BracketLeg` must construct a typed payload. Examples:

```python
# Before
BracketLeg(leg_type=BracketLegType.PRICE_STOP, trigger_condition="800.0", ...)
# After
BracketLeg(
    leg_type=BracketLegType.PRICE_STOP,
    trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=800.0, direction="LTE"),
    ...
)
```

Sites to update: every test file under `tests/portfolio_state/records/test_orders.py`, `tests/portfolio_state/_fixtures.py`, `tests/risk_guardrails/breach_behavior/fixtures/builders.py`, `src/alphamind/scripts/verify_*.py`, etc. Use grep to locate.

### Out of scope

* Wiring the new trigger payloads into the broker adapter (<issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue>) — broker submission still happens via Alpaca's order parameters, not via TriggerPayload.
* Changing the trigger payload shape after the fact (e.g., adding `tolerance_pct` to PriceTrigger). The shape is what the design's three invalidation types require, no more.
* Migrating the existing string field to a deprecated alias. The change is breaking; every fixture migrates in lockstep.

## Acceptance criteria

- [ ] `PriceTrigger`, `TimeTrigger`, `EventTrigger` classes exist in `alphamind.portfolio_state.records.orders` with the documented field shapes.
- [ ] `TriggerPayload` discriminated-union alias exists, keyed on `trigger_type`.
- [ ] `BracketLeg.trigger: TriggerPayload` field replaces `trigger_condition: str`.
- [ ] `BracketLeg._validate_trigger_matches_leg_type` validator enforces the leg_type ↔ trigger_type mapping (TAKE_PROFIT/PRICE_STOP → "price", TIME_EXPIRATION → "time", EVENT_INVALIDATION → "event").
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.TAKE_PROFIT, trigger=PriceTrigger(...))` succeeds.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.PRICE_STOP, trigger=TimeTrigger(...))` raises `ValueError`.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.TIME_EXPIRATION, trigger=PriceTrigger(...))` raises `ValueError`.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.EVENT_INVALIDATION, trigger=EventTrigger(...))` succeeds.
- [ ] Constructing `PriceTrigger(threshold_usd=-5.0, ...)` raises `ValidationError`.
- [ ] Constructing `TimeTrigger(deadline=datetime.now())` (naive) raises `ValidationError`.
- [ ] Constructing `EventTrigger(description="")` raises `ValidationError`.
- [ ] `_parse_price_trigger` no longer exists in `computations/positions.py`; `_trigger_price_from_condition` no longer exists in `decision/strategist/input_bundle.py`.
- [ ] `compute_distance_to_target_usd` and `compute_distance_to_stop_usd` use typed access (`isinstance(leg.trigger, PriceTrigger)` plus attribute access); no string parsing.
- [ ] PM and strategist input bundles render the typed payloads correctly: PriceTrigger as `<ticker> <GTE/LTE> $<threshold>`, TimeTrigger as ISO timestamp, EventTrigger as quoted description.
- [ ] All existing `uv run pytest -n auto` tests pass after fixture updates — no test fails because of the type change.
- [ ] `tests/portfolio_state/records/test_orders.py` exercises: (a) all three trigger types construct happily, (b) field validators (negative threshold, naive datetime, empty description), (c) leg_type ↔ trigger_type mismatch rejected for all incompatible pairs, (d) discriminated-union deserialization round-trips correctly.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests, after fixture migration.
* Run `uv run pytest tests/portfolio_state/records/test_orders.py -n auto -v` — confirm new and updated trigger tests pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.orders import BracketLeg, PriceTrigger, BracketLegType, BracketLegEnforcement, BracketLegStatus; leg = BracketLeg(leg_id='L1', leg_type=BracketLegType.PRICE_STOP, order_id='O1', trigger=PriceTrigger(underlying_ticker='NVDA', threshold_usd=800.0, direction='LTE'), enforcement=BracketLegEnforcement.MECHANICAL, status=BracketLegStatus.ACTIVE, pl_based=False); print(leg.trigger.threshold_usd)"` should print `800.0`.
* Spot-check that consumers' renderers produce sensible string output for fixtures that previously used `trigger_condition="price >= target"`.
* Lint clean per CLAUDE.md.