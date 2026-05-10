# 01g — `PositionFill` `live_execution_estimate` + sign convention docs

## Goal

Add an additive `live_execution_estimate: LiveExecutionEstimate | None` field to `PositionFill` (default `None`, populated only in paper mode by the paper-evaluation harness) plus a new `LiveExecutionEstimate` typed payload defined in `records/positions.py`. The design (`docs/design/05-execution-layer/architecture.md` § Fill report contract) requires every paper-mode fill to carry an estimated spread / impact / regulatory-fee drag and a live-adjusted fill price; today the field is missing from `PositionFill` so paper-mode fills lose the harness annotation entirely. Document the sign convention for `slippage` (negative = price improvement, positive = paid more than reference) and `fees` (always positive — cost). New consumers (paper harness, feedback-loop diagnostics) need to differentiate raw paper fills from live-adjusted estimates without reaching outside the typed record.

## Reading

* `src/alphamind/portfolio_state/records/positions.py:49-58` — current `PositionFill` (fill_timestamp, fill_price, fill_quantity, slippage, fees) — frozen Pydantic with no live-execution metadata.
* `src/alphamind/portfolio_state/records/positions.py:130` — `PositionRecord.execution_history: tuple[PositionFill, ...]` consumer site.
* `docs/design/05-execution-layer/architecture.md:122` — defines `live_execution_estimate` (paper mode only): "metadata attached by the paper-evaluation harness — estimated spread, impact, regulatory-fee drag, and a live-adjusted fill price. Absent in live mode, where execution costs are real."
* `docs/design/05-execution-layer/paper-evaluation-harness.md` — defines what the harness estimates: spread (bid-ask gap impact), market impact (size-driven price move), regulatory fees (SEC/FINRA/exchange dust), live-adjusted fill price (raw price + spread + impact + regulatory drag).
* `docs/design/05-execution-layer/state-persistence.md` § Fill record — confirms the metadata field is part of the persisted fill record (line 156).
* `tests/portfolio_state/records/test_positions.py::TestPositionFill` — existing happy-path test (line ~97); extend with new field.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule applies.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it adds one additive field plus a new typed nested model.

## Scope

Source under `src/alphamind/portfolio_state/records/positions.py` (new `LiveExecutionEstimate` model + one field add to `PositionFill` + docstring). Tests at `tests/portfolio_state/records/test_positions.py` (extend `TestPositionFill`).

### 1\. Define the new `LiveExecutionEstimate` typed payload

In `src/alphamind/portfolio_state/records/positions.py`, add adjacent to `PositionFill`:

```python
class LiveExecutionEstimate(BaseModel):
    """Paper-mode harness estimate of live-execution drag for a single fill.

    Attached by the paper-evaluation harness to fills produced in paper mode.
    Absent in live mode (the broker reports actual costs separately). All four
    fields use the same sign convention as the parent PositionFill:
    * estimated_spread_usd: positive (cost). The estimated bid-ask spread the
      live order would have crossed.
    * estimated_impact_usd: positive (cost). The estimated market-impact drag
      from order size.
    * estimated_regulatory_fees_usd: positive (cost). SEC/FINRA/exchange fees
      Alpaca reports at EOD via the activity feed, not per-fill.
    * live_adjusted_fill_price: the raw paper fill price minus (for buys) or
      plus (for sells) the per-share equivalent of the three drag components.
      Always finite; can be above or below the raw fill_price depending on
      direction.
    """

    model_config = ConfigDict(frozen=True)

    estimated_spread_usd: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    estimated_impact_usd: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    estimated_regulatory_fees_usd: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    live_adjusted_fill_price: Annotated[float, Field(allow_inf_nan=False)]
```

### 2\. Add `live_execution_estimate` field to `PositionFill`

In `PositionFill`:

```python
class PositionFill(BaseModel):
    """Bare-minimum execution audit per position.

    Sign conventions:
    * slippage: signed. Positive when the fill price was worse than the
      reference price at submission (buy filled higher / sell filled lower).
      Negative when the fill price was better (price improvement). Reference
      price is the limit price for limit orders, and the mid-quote at
      submission for market orders.
    * fees: always positive (cost — broker, regulatory, exchange).
    """

    model_config = ConfigDict(frozen=True)

    fill_timestamp: datetime
    fill_price: float
    fill_quantity: float
    slippage: float
    fees: Annotated[float, Field(ge=0.0)]  # was unbounded float; now ge=0
    live_execution_estimate: LiveExecutionEstimate | None = None
```

The `fees: float` annotation is tightened to `ge=0` (per the documented sign convention). This is technically a constraint addition that could break a fixture passing negative fees — but no legitimate negative-fees fixture should exist (negative fees would mean the broker pays you).

### 3\. Defaults reasoning

* `live_execution_estimate: LiveExecutionEstimate | None = None` — None in live mode (where broker actual costs are authoritative); populated by the paper harness in paper mode. Existing fixtures construct `PositionFill` without this field, so the None default preserves compatibility.

### Out of scope

* Wiring the paper harness to actually populate `live_execution_estimate` — that's the paper-evaluation harness work tree (<issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>).
* Computing the live-adjusted price formula — also the harness's responsibility.
* Adding a `mode: Literal["paper", "live"]` field to `PositionFill` — `live_execution_estimate is not None` already serves as the discriminator.

## Acceptance criteria

- [ ] `LiveExecutionEstimate` is a new frozen Pydantic model in `alphamind.portfolio_state.records.positions` with four fields per the spec above.
- [ ] `PositionFill.live_execution_estimate: LiveExecutionEstimate | None = None` field exists.
- [ ] `PositionFill.fees` is typed as `Annotated[float, Field(ge=0.0)]` (constraint added per documented sign convention).
- [ ] All existing `tests/portfolio_state/records/test_positions.py::TestPositionFill` tests pass after the field add (additive default = None preserves compat).
- [ ] All existing `uv run pytest -n auto` tests pass — no pre-existing test fails.
- [ ] Constructing `LiveExecutionEstimate(estimated_spread_usd=-0.1, ...)` raises `ValidationError` (cost must be `>= 0`).
- [ ] Constructing `LiveExecutionEstimate(estimated_impact_usd=float('nan'), ...)` raises `ValidationError` (`allow_inf_nan=False`).
- [ ] Constructing `LiveExecutionEstimate(live_adjusted_fill_price=float('inf'), ...)` raises `ValidationError`.
- [ ] Constructing `PositionFill(..., fees=-1.0)` raises `ValidationError` (newly enforced cost-only constraint).
- [ ] Constructing `PositionFill(..., live_execution_estimate=LiveExecutionEstimate(...))` (paper-mode case) succeeds.
- [ ] Constructing `PositionFill(...)` without `live_execution_estimate` (live-mode case) succeeds with the field defaulting to None.
- [ ] `PositionFill` class docstring includes the slippage and fees sign-convention paragraphs.
- [ ] `LiveExecutionEstimate` class docstring includes the cost/sign-convention paragraph.
- [ ] `tests/portfolio_state/records/test_positions.py` includes a new `TestLiveExecutionEstimate` test class exercising: (a) happy path, (b) negative-cost rejected for each of the three cost fields, (c) inf/nan rejected, (d) frozen.
- [ ] `tests/portfolio_state/records/test_positions.py::TestPositionFill` is extended to test: (a) None default, (b) populated estimate field, (c) negative-fees rejected.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/portfolio_state/records/test_positions.py -n auto -v` — confirm `TestPositionFill` and `TestLiveExecutionEstimate` pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.positions import LiveExecutionEstimate, PositionFill; print(set(PositionFill.model_fields.keys()))"` includes `live_execution_estimate`.
* Lint clean per CLAUDE.md.