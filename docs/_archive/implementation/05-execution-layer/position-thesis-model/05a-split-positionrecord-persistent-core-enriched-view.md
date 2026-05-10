# 05a — Split PositionRecord into persistent core + enriched delivery view

## Goal

Split the current `PositionRecord` (which conflates persistent state with delivery-time enrichments) into two types: `PositionRecord` (persistent — `position_id`, `details` discriminated union, `status`, `direction`, `entry_timestamp`, `execution_history`, `realized_pnl_to_date_usd`, `thesis_id`, `bracket_id`, `corporate_action_adjustment_needed`, `parent_position_id`, `origin`) and `PositionView` (delivery-time wrapper that holds the persistent record plus computed fields: `current_market_value_usd`, `unrealized_pnl_usd`, `unrealized_pnl_pct`, `position_weight_pct`, `position_age_hours`, `notional_exposure_usd`, `delta_adjusted_exposure_usd`, `distance_to_target_usd`, `distance_to_stop_usd`, `risk_reward_at_current`). The current shape lets producers construct records with arbitrary computed values that drift from what the `computations/positions.py` functions would produce — there's no invariant linking the field to its computation. The split eliminates the drift class entirely: `PositionView` is *only* constructed by the assembler, which sources every computed field from the canonical computation functions.

## Reading

* `src/alphamind/portfolio_state/records/positions.py:112-186` — current `PositionRecord` mixing persistent and computed fields.
* `src/alphamind/portfolio_state/assembler.py` — current site that populates the computed fields on `PositionRecord`; will become the `PositionView` constructor.
* `src/alphamind/portfolio_state/computations/positions.py` — pure functions that compute each enriched field; the new `PositionView.from_record(record, ...)` factory delegates to these.
* `docs/design/01-data-layer/internal/portfolio-state.md:13` — design quote: "the ingestion layer applies these consumer-facing computations at each invocation" — confirms enriched fields are delivery-time projections, not persistent state.
* `docs/design/05-execution-layer/state-persistence.md` § Tier 1 Positions — the persistent shape (which the new `PositionRecord` mirrors).
* All consumer sites that read computed fields (PM input bundle, strategist input bundle, analyst input bundle, scenario tests) — these migrate from `position.unrealized_pnl_usd` to `position_view.unrealized_pnl_usd`. Roughly 50+ sites.
* <issue id="273434f7-591e-4ac2-bb85-eb601511ed4f">ALP-347</issue> (story 04a) — must land first; 05a touches the same files structurally.
* <issue id="3aac15c1-a657-4ba5-afe4-a609ea32e4f7">ALP-348</issue> (story 04b) — must land first; the `details` discriminated-union shape is the new persistent shape 05a builds on.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Architectural invariants — affirms enriched-vs-persistent split as a design correctness goal.

## Depends on

* <issue id="273434f7-591e-4ac2-bb85-eb601511ed4f">ALP-347</issue> (story 04a) — file restructure must land first to avoid concurrent edits to [positions.py](http://positions.py).
* <issue id="3aac15c1-a657-4ba5-afe4-a609ea32e4f7">ALP-348</issue> (story 04b) — Pydantic-native discriminated unions must land first; `PositionView` references `PositionRecord.details` shape.

## Scope

Source under `src/alphamind/portfolio_state/records/positions.py` (slim down `PositionRecord`), `src/alphamind/portfolio_state/views/positions.py` (new file: `PositionView`), `src/alphamind/portfolio_state/assembler.py` (assembler now produces `PositionView` not enriched `PositionRecord`). Tests and 50+ consumer sites updated.

### 1\. Slim down `PositionRecord` (persistent fields only)

Remove these fields from `PositionRecord` (move them to `PositionView`):

* `current_market_value_usd`
* `unrealized_pnl_usd`
* `unrealized_pnl_pct`
* `position_weight_pct`
* `position_age_hours`
* `notional_exposure_usd`
* `delta_adjusted_exposure_usd`
* `distance_to_target_usd`
* `distance_to_stop_usd`
* `risk_reward_at_current`

The remaining fields (per the Goal section) are pure persistent state.

### 2\. Define `PositionView`

New file `src/alphamind/portfolio_state/views/positions.py`:

```python
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated

from alphamind.portfolio_state.records.positions import PositionRecord


class PositionView(BaseModel):
    """Delivery-time projection of a PositionRecord with computed enrichments.

    Constructed by the snapshot assembler at each invocation. Computed fields
    are derived from PositionRecord + current market data + total portfolio
    value via the canonical functions in computations/positions.py. Producers
    outside the assembler must not construct PositionView directly.
    """

    model_config = ConfigDict(frozen=True)

    record: PositionRecord
    current_market_value_usd: Annotated[float, Field(allow_inf_nan=False)]
    unrealized_pnl_usd: Annotated[float, Field(allow_inf_nan=False)]
    unrealized_pnl_pct: Annotated[float, Field(allow_inf_nan=False)]
    position_weight_pct: Annotated[float, Field(allow_inf_nan=False)]  # signed per story 01e
    position_age_hours: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    notional_exposure_usd: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    delta_adjusted_exposure_usd: Annotated[float, Field(allow_inf_nan=False)]
    distance_to_target_usd: float | None
    distance_to_stop_usd: float | None
    risk_reward_at_current: float | None
```

The `record` field gives consumers transparent access to the persistent fields (e.g., `view.record.position_id`, `view.record.thesis_id`).

### 3\. Update assembler

The assembler currently constructs enriched `PositionRecord` directly. After the split, the assembler:

1. Constructs the persistent `PositionRecord` from raw OMS data.
2. Calls `compute_*` functions to derive each enriched field.
3. Constructs `PositionView(record=record, current_market_value_usd=..., ...)`.

The assembler is the *only* legitimate `PositionView` constructor. Consumer sites receive `PositionView`, not `PositionRecord`, when they want enrichments.

### 4\. Migrate consumers

Every consumer reading computed fields off `PositionRecord` (PM input bundle, strategist input bundle, analyst input bundle, scenario tests) is updated:

* Before: `position.unrealized_pnl_usd`
* After: `position_view.unrealized_pnl_usd`

For consumers reading persistent fields:

* Before: `position.position_id`
* After: `position_view.record.position_id`

Or expose convenience properties on `PositionView` (`@property def position_id(self) -> str: return self.record.position_id`) to minimize churn — operator's call.

### Out of scope

* Adding a similar split for `OrderRecord` or `BracketRecord` (those don't carry computed fields today; no drift class to fix).
* Refactoring the computation functions themselves.

## Acceptance criteria

- [ ] `PositionRecord` no longer has the 10 computed fields listed in §1 — only persistent fields remain.
- [ ] `PositionView` exists at `src/alphamind/portfolio_state/views/positions.py` with the 10 computed fields plus `record: PositionRecord`.
- [ ] `PositionView` is frozen, importable from `alphamind.portfolio_state.views.positions`.
- [ ] The assembler in `src/alphamind/portfolio_state/assembler.py` produces `PositionView` instances, not enriched `PositionRecord` instances.
- [ ] All consumers reading computed fields updated to read from `PositionView` instead of `PositionRecord`.
- [ ] All consumer tests updated; `uv run pytest -n auto` passes.
- [ ] Tests at `tests/portfolio_state/views/test_positions.py` cover: (a) PositionView happy-path construction, (b) frozen, (c) all field constraints (signed weight per 01e, finite-only per 01e), (d) `record` field gives access to persistent state.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests, after consumer migration.
* Spot-check the split: `python -c "from alphamind.portfolio_state.records.positions import PositionRecord; from alphamind.portfolio_state.views.positions import PositionView; print('current_market_value_usd' not in PositionRecord.model_fields); print('current_market_value_usd' in PositionView.model_fields)"` should print `True` `True`.
* Spot-check that no consumer reads `position.unrealized_pnl_usd` outside of the migrated PositionView path: `grep -rn '\.unrealized_pnl_usd' src/ tests/ | grep -v PositionView | grep -v assembler` should return only the PositionView definition itself.
* Lint clean per CLAUDE.md.