# 03b — Typed `pl_based` payload (PLAnchorSpec) on BracketLeg

## Goal

Replace `BracketLeg.pl_based: bool` (a boolean flag with no supporting fields) with `pl_anchor: PLAnchorSpec | None` (a typed payload capturing the percentage spec, the planned-entry-price anchor, and a recalculation flag). The design (orders-and-brackets.md § P/L-based bracket legs) requires P/L-based legs to: (1) submit an equivalent absolute price computed from the planned entry price, (2) recalculate that absolute price using the actual fill price as the anchor when the entry leg fills. The current `pl_based: bool` flag preserves zero of this state — the percentage spec, the original anchor, and the recalc status are all lost. State persistence (<issue id="beaf98a0-a9fc44a8-ac46-32e50f604345">ALP-119</issue>) Phase 1 step 4 cannot implement the recalc against the current shape without parsing the legacy free-form `trigger_condition` string. This story closes the contract gap.

## Reading

* `src/alphamind/portfolio_state/records/orders.py:304-322` — current `BracketLeg` with `pl_based: bool` field.
* `docs/design/05-execution-layer/orders-and-brackets.md` § P/L-based bracket legs — defines the contract: P/L-based legs include the equivalent absolute price at submission; on entry fill, the engine recalculates using the actual fill price as anchor.
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path step 4 — names "the same Phase 1 step that transitions the bracket from pending-entry to active and submits protective legs" as the recalc site.
* `src/alphamind/portfolio_state/records/activity_log.py:171-178` — existing `BracketModificationSource` enum includes `FILL_ANCHOR_RECALCULATION`; the activity log already supports the recalc-event semantics.
* <issue id="7c6e5055-36fd-43b2-b7fe-575de26f99dc">ALP-345</issue> (story 03a) — sibling story replacing `trigger_condition` with typed payloads; this story is independent (different field) but should not conflict on the same lines.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule does NOT apply here; this is a TYPE CHANGE from `bool` to `Spec | None`.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates. Strongly suggested to dispatch *after* 03a (<issue id="7c6e5055-36fd-43b2-b7fe-575de26f99dc">ALP-345</issue>) lands to avoid concurrent edits to `BracketLeg` definition; not a hard gate but a coordination note.

## Scope

Source under `src/alphamind/portfolio_state/records/orders.py` (one new typed-payload class + replace `pl_based: bool` with `pl_anchor: PLAnchorSpec | None` + cross-field validator). Tests at `tests/portfolio_state/records/test_orders.py` (new test class `TestPLAnchorSpec` + `BracketLeg`-pl_anchor tests).

### 1\. Define the `PLAnchorSpec` payload

In `src/alphamind/portfolio_state/records/orders.py`:

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PLAnchorSpec(BaseModel):
    """P/L-anchor specification for legs defined in P/L-percentage terms.

    Per orders-and-brackets.md § P/L-based bracket legs:
    * At OPEN time, the producer specifies one of `target_pct` or `stop_pct`
      (per leg type) plus the `planned_entry_price` (the entry order's planned
      fill price). The engine submits the equivalent absolute price to the
      broker.
    * On entry fill, the engine recalculates the absolute price using
      `actual_entry_price` as the anchor. The recalculation logs as a
      `bracket_modified` event with source `FILL_ANCHOR_RECALCULATION`. Once
      complete, `recalculated_at_fill` is True.

    Sign conventions:
    * target_pct: percentage gain (e.g., 0.80 = 80% profit on premium for an
      options take-profit; 0.05 = 5% gain for an equity take-profit).
    * stop_pct: percentage loss (e.g., 0.30 = 30% loss on premium; 0.05 = 5%
      loss on equity). Always expressed as a positive magnitude.
    """

    model_config = ConfigDict(frozen=True)

    spec_type: Literal["target", "stop"]
    pct: Annotated[float, Field(gt=0, le=10.0, allow_inf_nan=False)]  # 0 < pct ≤ 1000%
    planned_entry_price: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    actual_entry_price: float | None = None
    recalculated_at_fill: bool = False

    @model_validator(mode="after")
    def _validate_recalc_consistency(self) -> PLAnchorSpec:
        if self.recalculated_at_fill and self.actual_entry_price is None:
            msg = "recalculated_at_fill=True requires actual_entry_price to be non-None"
            raise ValueError(msg)
        if not self.recalculated_at_fill and self.actual_entry_price is not None:
            msg = "actual_entry_price must be None when recalculated_at_fill=False"
            raise ValueError(msg)
        return self
```

### 2\. Update `BracketLeg`

```python
class BracketLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    leg_id: str
    leg_type: BracketLegType
    order_id: str | None
    trigger: TriggerPayload  # from story 03a
    enforcement: BracketLegEnforcement
    status: BracketLegStatus
    pl_anchor: PLAnchorSpec | None = None  # replaces pl_based: bool

    @model_validator(mode="after")
    def _validate_pl_anchor_compatibility(self) -> BracketLeg:
        # Only TAKE_PROFIT and PRICE_STOP legs can carry pl_anchor;
        # TIME_EXPIRATION and EVENT_INVALIDATION legs cannot.
        if self.pl_anchor is not None and self.leg_type not in {
            BracketLegType.TAKE_PROFIT,
            BracketLegType.PRICE_STOP,
        }:
            msg = (
                f"pl_anchor only valid on TAKE_PROFIT or PRICE_STOP legs; "
                f"got leg_type={self.leg_type!r}"
            )
            raise ValueError(msg)
        # spec_type ↔ leg_type sanity
        if self.pl_anchor is not None:
            expected = "target" if self.leg_type == BracketLegType.TAKE_PROFIT else "stop"
            if self.pl_anchor.spec_type != expected:
                msg = (
                    f"leg_type={self.leg_type!r} requires pl_anchor.spec_type={expected!r}; "
                    f"got {self.pl_anchor.spec_type!r}"
                )
                raise ValueError(msg)
        return self
```

`pl_anchor=None` indicates an absolute-price leg (the trigger payload's threshold is authoritative; no recalc on fill). `pl_anchor=PLAnchorSpec(...)` indicates a P/L-percentage leg.

### 3\. Update fixtures

Every fixture currently passing `pl_based=True` or `pl_based=False` to `BracketLeg` must:

* `pl_based=False` → omit `pl_anchor` (defaults to None) — matches the absolute-price-leg semantics.
* `pl_based=True` → construct a `PLAnchorSpec(spec_type="target"|"stop", pct=..., planned_entry_price=...)`.

### Out of scope

* Implementing the recalc operation itself (transitioning from `recalculated_at_fill=False` to `True` with `actual_entry_price` populated) — that belongs to State persistence (<issue id="beaf98a0-a9fc44a8-ac46-32e50f604345">ALP-119</issue>) Phase 1 step 4.
* Wiring the `bracket_modified` activity-log event with source `FILL_ANCHOR_RECALCULATION` to fire on recalc — also state-persistence's job.
* Cross-field validation that `actual_entry_price` is within tolerance of `planned_entry_price` — producer responsibility.

## Acceptance criteria

- [ ] `PLAnchorSpec` class exists in `alphamind.portfolio_state.records.orders` with fields per the spec above.
- [ ] `BracketLeg.pl_anchor: PLAnchorSpec | None = None` field replaces `pl_based: bool`.
- [ ] `BracketLeg._validate_pl_anchor_compatibility` validator rejects pl_anchor on TIME_EXPIRATION and EVENT_INVALIDATION legs.
- [ ] `BracketLeg._validate_pl_anchor_compatibility` validator enforces spec_type ↔ leg_type mapping (TAKE_PROFIT → "target", PRICE_STOP → "stop").
- [ ] `PLAnchorSpec._validate_recalc_consistency` validator raises when `recalculated_at_fill=True` and `actual_entry_price is None`, and vice versa.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.TAKE_PROFIT, pl_anchor=PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50), ...)` succeeds.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.PRICE_STOP, pl_anchor=PLAnchorSpec(spec_type="stop", pct=0.30, planned_entry_price=18.50), ...)` succeeds.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.TIME_EXPIRATION, pl_anchor=PLAnchorSpec(...), ...)` raises `ValueError`.
- [ ] Constructing `BracketLeg(leg_type=BracketLegType.TAKE_PROFIT, pl_anchor=PLAnchorSpec(spec_type="stop", ...), ...)` raises `ValueError`.
- [ ] Constructing `PLAnchorSpec(pct=0.0, ...)` raises `ValidationError` (`gt=0`).
- [ ] Constructing `PLAnchorSpec(pct=11.0, ...)` raises `ValidationError` (`le=10.0`).
- [ ] Constructing `PLAnchorSpec(planned_entry_price=-5.0, ...)` raises `ValidationError`.
- [ ] Constructing `PLAnchorSpec(recalculated_at_fill=True)` (without `actual_entry_price`) raises `ValueError`.
- [ ] All existing `uv run pytest -n auto` tests pass after fixture updates.
- [ ] `tests/portfolio_state/records/test_orders.py` includes a new `TestPLAnchorSpec` test class plus `BracketLeg`-pl_anchor tests covering all acceptance criteria above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/portfolio_state/records/test_orders.py -n auto -v` — confirm new tests pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.orders import PLAnchorSpec; print(PLAnchorSpec(spec_type='target', pct=0.80, planned_entry_price=18.50))"`.
* Lint clean per CLAUDE.md.