---
status: done
completed_date: 2026-04-29
commit_id: 6ecb70e
---

# 04d — Forced-reduction position selection primitives

## Goal

Land the deterministic position-selection primitives the continuous monitor invokes when a breach classified as `immediate_engine` triggers a protective CLOSE. Each primitive consumes the breaching state plus the eligible-position population and returns a `PositionSelectionResult` naming the position to close (or trim), the action prescribed (full close vs. partial trim), and the human-readable rationale that flows into the engine envelope's `position_selection_rationale` field. Five distinct breach types, each with its own deterministic selection rule per `breach-behavior.md § Position selection logic for forced reductions`.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Position selection logic for forced reductions — the authoritative per-breach-type selection criteria:
  - **Drawdown breaches (daily and cumulative):** largest unrealized loss → most liquid (highest ADV relative to position size) → full close.
  - **Position-level max loss:** close the breaching position.
  - **Short exposure breaches (when immediate, i.e., > 110% of limit):** largest short position in the most liquid name → trim to 95% of the applicable limit.
  - **Single short max size:** trim to 95% of the per-position limit.
  - **Margin call:** worst risk/reward ratio at current price (closest to invalidation, farthest from target) → most liquid → full close.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Per-rule breach response classification — the rule-to-classification mapping that determines *whether* a breach fires this story's primitives at all (immediate_engine vs. deferred_to_pm).
- `docs/design/06-risk-guardrails/breach-behavior.md` § Margin call cascade handling — the margin-call selection rule (worst risk/reward ratio at current price) and its cascade context.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Drawdown halt mode — confirms drawdown halt's tier-3 fallback uses "largest unrealized loss first" — the same drawdown selector this story exposes.
- `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord` model (carries `position_id`, `instrument`, `direction`, `size_pct_of_portfolio`, fills, etc.).
- `src/alphamind/portfolio_state/records/positions.py` — `Direction.LONG` / `Direction.SHORT` enum.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `PositionSelectionResult`, `PositionSelectionAction` (story 03).
- `src/alphamind/risk_guardrails/breach_behavior/config.py` — `BreachBehaviorConfig.forced_reduction_short_trim_target_pct_of_limit` (the 95% trim target; story 02).
- `docs/design/06-risk-guardrails/scenario-tests.md` § A6 (short squeeze — engine protective CLOSE) and § A7 (margin call cascade) and § A11 (synchronized HTB buy-in) — concrete walkthroughs the primitives must support.

## Depends on

- 02 (package skeleton + config — for the trim-target percentage)
- 03 (canonical types — `PositionSelectionResult`, `PositionSelectionAction`, `PositionRecord`)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/position_selection.py`. Tests at `tests/risk_guardrails/breach_behavior/test_position_selection.py`.

The module exposes one public function per breach type plus a small set of shared helpers. Each public function is a pure function that consumes typed records and returns a `PositionSelectionResult`. None mutate inputs.

### 1. Liquidity input shape

Position selection's tiebreaker for several breach types is "most liquid (highest ADV relative to position size)." `PositionRecord` does not carry ADV; the caller (continuous monitor) supplies it via a typed input:

```python
class PositionLiquidity(BaseModel):
    """Per-position liquidity metric used as a selection tiebreaker.

    The continuous monitor populates this from the data layer's per-symbol ADV (average
    daily volume) and the position's notional. The ratio (ADV in shares × current price /
    position notional) approximates how many days of typical volume the position represents
    — higher ratio means more liquid relative to size.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    adv_to_position_size_ratio: float       # higher = more liquid relative to size

    @field_validator("adv_to_position_size_ratio")
    @classmethod
    def _require_non_negative(cls, v: float) -> float:
        if v < 0:
            msg = f"adv_to_position_size_ratio must be >= 0; got {v}"
            raise ValueError(msg)
        return v
```

Defined in this module's file; re-exported from the package `__init__.py`.

### 2. Drawdown breach selector

```python
def select_for_drawdown_breach(
    *,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
) -> PositionSelectionResult:
    """Select the position to close in response to a drawdown breach.

    Selection rule (breach-behavior.md § Position selection logic):
      1. Largest unrealized loss in absolute USD (most negative unrealized P/L).
      2. Tiebreaker: highest adv_to_position_size_ratio (most liquid relative to size).
      3. Action: full close — partial leaves residual risk and orphaned theses.

    Args:
        open_positions: All currently-open positions. Must be non-empty.
        liquidity: Per-position liquidity metrics, one entry per position_id in open_positions.

    Returns:
        A PositionSelectionResult naming the chosen position, action=FULL_CLOSE, and a
        rationale string of the form "largest unrealized loss ($X.XX) in {ticker}; tie-broken
        by liquidity (ADV/size ratio Y.YY)" (or omitting the tie-break clause when no tie).

    Raises:
        ValueError: when open_positions is empty, when liquidity is missing entries for
            any open position, or when no position has a negative unrealized P/L (drawdown
            breach with no losing position is a structural error — drawdown was driven by
            something other than position losses).
    """
```

Selection logic:
1. Compute each position's unrealized P/L in USD. Filter to positions with `unrealized_pnl_usd < 0`.
2. Sort by `unrealized_pnl_usd` ascending (most negative first) to get the largest losses at the top.
3. If the top entry is uniquely most-loss, select it.
4. If multiple positions tie on loss (same `unrealized_pnl_usd`), break the tie by `adv_to_position_size_ratio` descending (most liquid first).
5. If both tie, fall back to `position_id` lexicographic ascending (deterministic floor).

The rationale string names the deterministic rule that selected the position. Tests assert the rationale matches the documented format.

### 3. Position-level max loss selector

```python
def select_for_position_max_loss(
    *,
    breaching_position_id: str,
    open_positions: tuple[PositionRecord, ...],
    loss_pct: float,
    limit_pct: float,
) -> PositionSelectionResult:
    """Select for a position-level max-loss breach.

    The breach is intrinsically tied to a single position — that position is the selection.
    Action: full close.

    Args:
        breaching_position_id: The position_id whose unrealized loss triggered the breach.
        open_positions: All currently-open positions. The breaching id must be present.
        loss_pct: The position's unrealized loss as a percent of cost basis (negative;
            e.g., -3.5 for a 3.5% loss). Recorded in the rationale.
        limit_pct: The active per-position max-loss limit as a percent of cost basis
            (negative; e.g., -3.0 for a -3.0% cap). Recorded in the rationale.

    Returns:
        A PositionSelectionResult with the breaching position's id, action=FULL_CLOSE, and
        a rationale string "position-level max loss breach on {ticker} (loss: -X.X% of cost,
        limit: -Y.Y%)".

    Raises:
        ValueError: when breaching_position_id is not in open_positions.
    """
```

Trivial selector: the breach identifies the position. The function exists so the position-selection module exposes a uniform per-breach-type interface, and so the rationale string is constructed in one canonical place rather than inline at the call site. `loss_pct` and `limit_pct` populate the rationale string's documented `(loss: -X.X% of cost, limit: -Y.Y%)` format. The breach detector computes these from the position's unrealized P/L and the active position-max-loss limit.

### 4. Total short exposure selector

```python
def select_for_total_short_exposure_breach(
    *,
    short_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    total_short_limit_pct_of_portfolio: float,
    config: BreachBehaviorConfig,
) -> PositionSelectionResult:
    """Select a short position to trim when total short exposure exceeds the immediate-action threshold.

    Per breach-behavior.md § Per-rule breach response classification, this selector is invoked
    only when total short exposure > config.forced_reduction_total_short_immediate_threshold_pct_of_limit
    (default 110%) of the active total-short limit. Smaller overages defer to the strategist/PM.

    Selection rule:
      1. Largest short position in dollar terms.
      2. Tiebreaker: most liquid (highest adv_to_position_size_ratio).
      3. Action: PARTIAL_TRIM to config.forced_reduction_short_trim_target_pct_of_limit (default 95%)
         of the per-position single-short limit OR the aggregate total-short limit, whichever
         the trim cures. Per the design, "trim to 95% of the applicable limit" — the applicable
         limit here is the *aggregate* total-short limit; the trim target is computed as
         (total_short_limit_pct × 0.95 - other_shorts_total) and applied as the new size of the
         selected position.

    Args:
        short_positions: All currently-open short positions. Must be non-empty.
        liquidity: Per-position liquidity metrics for each short position.
        total_short_limit_pct_of_portfolio: The active total-short limit (e.g., 30.0 for 30%).
        config: The BreachBehaviorConfig — sources the 95% trim target.

    Returns:
        A PositionSelectionResult with action=PARTIAL_TRIM and target_post_action_size_pct_of_portfolio
        equal to the computed trim target, and a rationale of the form "total short exposure
        breach (X.X% > Y.Y% threshold); largest short {ticker} trimmed to Z.Z% (95% of total cap
        Y.Y%)".

    Raises:
        ValueError: when short_positions is empty, liquidity does not cover all short positions,
            or the computed trim target is non-positive (the breach is too severe for a single-position
            trim to cure — the caller should escalate).
    """
```

Trim target arithmetic:
1. Compute `aggregate_short_pct = sum(p.size_pct_of_portfolio for p in short_positions)`. (Already > limit when this selector fires; the threshold check is the caller's responsibility.)
2. Identify the largest short by `size_pct_of_portfolio` descending; tie-break by liquidity.
3. Compute `total_short_target_pct = total_short_limit_pct × (config.forced_reduction_short_trim_target_pct_of_limit / 100.0)`. With shipped values: `30.0 × 0.95 = 28.5%`.
4. Compute `other_shorts_total_pct = aggregate_short_pct - selected_position.size_pct_of_portfolio`.
5. Compute `selected_target_pct = total_short_target_pct - other_shorts_total_pct`. If non-positive, the cure is impossible at this single-position trim — raise (the caller may select a different breach response, e.g., closing multiple positions).
6. Otherwise, return the target.

### 5. Single short size selector

```python
def select_for_single_short_max_size_breach(
    *,
    breaching_position_id: str,
    open_positions: tuple[PositionRecord, ...],
    single_short_max_pct_of_portfolio: float,
    config: BreachBehaviorConfig,
) -> PositionSelectionResult:
    """Select for a single-short max-size breach.

    The breach identifies the position. Action: PARTIAL_TRIM to 95% of the per-position limit.

    Args:
        breaching_position_id: The short position whose size exceeds the per-position limit.
        open_positions: All currently-open positions; breaching id must be present.
        single_short_max_pct_of_portfolio: The active per-position single-short limit (e.g., 3.0 for 3%).
        config: The BreachBehaviorConfig.

    Returns:
        PositionSelectionResult with action=PARTIAL_TRIM, target=single_short_max_pct ×
        config.forced_reduction_short_trim_target_pct_of_limit / 100.0 (e.g., 3.0 × 0.95 = 2.85%),
        and a rationale of the form "single short {ticker} at X.X% > Y.Y% per-position cap;
        trimmed to Z.Z% (95% of cap)".

    Raises:
        ValueError: when breaching_position_id is not in open_positions.
    """
```

### 6. Margin call selector

```python
def select_for_margin_call(
    *,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    additional_margin_required_usd: float,
    risk_reward_metric: tuple[PositionRiskReward, ...],
) -> PositionSelectionResult:
    """Select a position to close in response to a margin call.

    Per breach-behavior.md § Margin call cascade handling:
      1. Worst risk/reward ratio at current price (closest to invalidation, farthest from target).
      2. Tiebreaker: most liquid.
      3. Action: full close — partial may not satisfy the margin requirement.

    Args:
        open_positions: All currently-open positions. Must be non-empty.
        liquidity: Per-position liquidity metrics.
        additional_margin_required_usd: How much margin the broker is calling for (defensive
            input; logged in the rationale; not used in selection).
        risk_reward_metric: Per-position risk/reward ratio at current price. Lower ratio means
            worse risk/reward (closer to invalidation than to target).

    Returns:
        PositionSelectionResult with action=FULL_CLOSE and rationale of the form "margin call
        $X.XX additional margin required; worst risk/reward {ticker} (R/R ratio Y.YY) selected".

    Raises:
        ValueError: when open_positions is empty, liquidity is incomplete, or risk_reward_metric
            is missing entries for any open position.
    """
```

The `PositionRiskReward` typed input:

```python
class PositionRiskReward(BaseModel):
    """Per-position risk/reward ratio at current price.

    The continuous monitor computes this from the position's current price, its bracket's
    invalidation level (price-stop), and its target (take-profit) per breach-behavior.md
    § Margin call position selection.

    Definition: ratio = (distance to target) / (distance to invalidation). Lower means worse
    R/R (closer to invalidation than to target). Negative means the position is already past
    invalidation but hasn't been stopped yet — pathological but possible during gaps; treated
    as worst-of-the-worst.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    risk_reward_ratio: float
```

Selection: sort by `risk_reward_ratio` ascending (worst first); tiebreak by liquidity (most liquid first); final tiebreak by `position_id` lexicographic.

### 7. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_position_selection.py`. Covering each selector:

#### Drawdown selector

- **Single losing position selected:** input has 5 positions, one with -$300 P/L, others positive. Output names the single losing position, action=FULL_CLOSE, rationale matches the documented format with the ticker and loss amount.
- **Largest loss wins among multiple losers:** 3 positions with P/L `[-100, -300, -150]`. Output selects the -$300 position.
- **Liquidity tiebreak when losses tie:** two positions tie at -$300 P/L; their `adv_to_position_size_ratio` are 0.5 and 1.2. Output selects the higher ratio (most liquid).
- **Position-id tiebreak when both tie:** two positions tie at -$300 and same liquidity. Output selects the lexicographically smaller id.
- **No losing positions raises:** all positions have positive P/L. `ValueError` with "no losing positions; drawdown breach with no negative-PnL position is a structural error" or similar.
- **Empty open_positions raises:** `ValueError`.
- **Missing liquidity entry raises:** open_positions has 3 positions; liquidity covers only 2. `ValueError` naming the missing position_id.

#### Position-level max loss selector

- **Happy path:** breaching id is in open_positions; action=FULL_CLOSE; rationale names the ticker and loss percentage.
- **Missing breaching id raises:** id not in open_positions → `ValueError`.

#### Total short exposure selector

- **Happy path with shipped 30% limit and 95% trim:** four shorts at sizes [12%, 11%, 10%, 5%] (aggregate 38%, > 30 × 1.10 = 33% threshold). Largest is 12%. Trim target = 30% × 0.95 - (11% + 10% + 5%) = 28.5% - 26% = 2.5%. Output trims the 12% position to 2.5%.
- **Liquidity tiebreak when sizes tie:** two shorts both at 12%; differ in liquidity ratio. Output picks the more liquid.
- **Trim impossible — non-positive target raises:** other shorts already total 30% (e.g., aggregate 35% with one big position at 5% and others at 30%); selected position must trim below zero — `ValueError` with "trim target non-positive; single-position trim cannot cure breach".
- **Empty short_positions raises:** `ValueError`.

#### Single short size selector

- **Happy path with shipped 3% limit:** breaching position at 3.5%; action=PARTIAL_TRIM, target = 3.0 × 0.95 = 2.85%; rationale names the cap and the trim target.
- **Missing breaching id raises:** `ValueError`.

#### Margin call selector

- **Worst R/R wins:** 3 positions with R/R ratios [1.5, 0.4, 2.0]; selected is the 0.4 (worst). Action=FULL_CLOSE.
- **Liquidity tiebreak when R/R ties:** two at 0.5; differ in liquidity. Output picks the more liquid.
- **Negative R/R is worst-of-worst:** 3 positions with ratios [0.5, -0.2, 1.0]; selected is the -0.2.
- **Missing R/R entry raises:** open_positions has 3; risk_reward_metric covers only 2. `ValueError`.
- **Rationale includes additional_margin_required_usd:** rationale string contains the dollar amount the broker is calling for.

#### Cross-cutting

- **Determinism:** repeated calls with identical inputs produce equal outputs.
- **Pure functions:** no I/O, no clock reads, no global state mutation.
- **Frozen output:** assigning to a returned `PositionSelectionResult.position_id` or `.action` raises `ValidationError`.
- **Rationale strings include the deterministic rule used:** for each selector, the rationale text names the specific tiebreaker applied (e.g., "tie-broken by liquidity" or "no tiebreaker — uniquely most-loss"). This is load-bearing for the engine envelope's `position_selection_rationale` field, which the JSON Schema requires as a non-empty string matching documented selection rules.

Out of scope:
- Detection of *which* breach is in flight — that's the continuous monitor's responsibility (it evaluates portfolio state against rules and routes to the appropriate selector). This story exposes the selectors; orchestration is in stories 06 and 07.
- Computing `unrealized_pnl_usd`, `risk_reward_ratio`, or `adv_to_position_size_ratio` — those come from the data layer / portfolio-state assembler / monitor's market-data subscription. The selectors consume them as typed inputs.
- The threshold check that determines whether a total-short overage triggers immediate action vs. defers — that lives in the continuous monitor's breach-classification logic. This story's `select_for_total_short_exposure_breach` assumes the threshold is already crossed.
- Secondary breach checking on the resulting close — that's story 05b. The selectors here propose; the secondary-breach check validates before the envelope is finalized.
- The drawdown halt mode's tier-3 fallback ("the engine falls back to closing positions by largest unrealized loss first") — implemented by reusing `select_for_drawdown_breach` from a tier-3 fallback path; that orchestration belongs to story 07 or to the continuous monitor.

## Notes

**Why per-breach-type functions rather than a single dispatch function.** Each breach type has a distinct selection rule, distinct typed inputs (drawdown needs P/L; margin call needs R/R; short-exposure needs liquidity and an aggregate limit), and a distinct rationale format. A single dispatch function with a `BreachType` enum and a kitchen-sink input set obscures the per-type contract and forces all callers to construct unused fields. Five focused functions keep each call site clear and let test cases focus on one selection algorithm at a time. Per `feedback_simplify_before_building.md`.

**Why this is a single story rather than five.** Each selector is small (10–30 lines) and shares helper code (sort with deterministic tiebreakers, lookup-by-id helper, rationale-string builder). One module with five public functions plus one shared helper section is cleaner than five tiny stories with cross-references. The acceptance criteria break out per-selector to keep verification clear.

**Per `feedback_avoid_numeric_anchors.md`,** the 95% trim target and 110% immediate-action threshold come from `BreachBehaviorConfig` (story 02), not hardcoded. The shipped defaults match the design's documented values; operator tuning lifts new values without code changes.

**Per `feedback_no_inventing_component_names.md`,** function names match the design's per-breach-type wording: `select_for_drawdown_breach`, `select_for_position_max_loss`, `select_for_total_short_exposure_breach`, `select_for_single_short_max_size_breach`, `select_for_margin_call`. Internal types (`PositionLiquidity`, `PositionRiskReward`) are package-internal and named after the data they carry.

**Tiebreaker discipline.** Every selector ends with a `position_id` lexicographic tiebreaker after the design's documented rule (largest-loss, then most-liquid, then position_id). This guarantees fully-deterministic output even on adversarial inputs (multiple positions tying on every documented metric). The continuous monitor and the test suite both rely on this — the engine envelope's audit trail must reproduce identically given identical inputs.

**Rationale-string format discipline.** The rationale is a free-form human-readable string in `PositionSelectionResult.rationale`, but it must always name the specific selection rule applied so the engine envelope's audit trail reads cleanly. The acceptance criteria specify the format per selector. The implementing subagent uses string templates with clear field substitution; tests assert the format end-to-end.

**Why the trim-impossible case raises rather than returning `None`.** The continuous monitor's caller must always proceed with *some* action (a breach has fired). Raising forces the caller to handle the exceptional case explicitly — typically by selecting a different breach response (close two shorts, or escalate to a deferred response). A `None` return would silently absorb the failure and the cascade would stall.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/position_selection.py` exists and defines `PositionLiquidity`, `PositionRiskReward`, and the five public selectors (`select_for_drawdown_breach`, `select_for_position_max_loss`, `select_for_total_short_exposure_breach`, `select_for_single_short_max_size_breach`, `select_for_margin_call`).
- [ ] All public functions and types are re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] `PositionLiquidity` and `PositionRiskReward` are frozen Pydantic v2 models with the documented fields and validators.
- [ ] Drawdown selector returns the position with the largest absolute USD unrealized loss; ties broken by liquidity (descending) and then position_id (ascending); always action=FULL_CLOSE.
- [ ] Drawdown selector raises `ValueError` when no position has negative unrealized P/L.
- [ ] Drawdown selector raises `ValueError` when liquidity tuple does not cover every open position.
- [ ] Position-level max loss selector returns the breaching position id with action=FULL_CLOSE; raises `ValueError` when the id is not in open_positions.
- [ ] Total-short selector returns PARTIAL_TRIM action with target = `total_short_limit × 0.95 - other_shorts_total`; ties broken by liquidity then position_id.
- [ ] Total-short selector raises `ValueError` when the computed trim target is non-positive.
- [ ] Single-short selector returns PARTIAL_TRIM with target = `single_short_max × 0.95`; raises when breaching id is missing.
- [ ] Margin-call selector returns the position with the lowest `risk_reward_ratio`; tiebreaks by liquidity then position_id; action=FULL_CLOSE.
- [ ] Margin-call selector raises `ValueError` when liquidity or R/R coverage is incomplete.
- [ ] Each selector's rationale string names the deterministic rule applied (largest loss, most liquid, worst R/R, etc.) and includes the relevant numeric values (loss amount, R/R ratio, trim target, dollar margin amount). Verified by parametrized tests over the documented format.
- [ ] All selectors are pure (no I/O, no clock reads).
- [ ] All selectors are deterministic — 100 repeated calls with identical inputs produce identical outputs.
- [ ] Returned `PositionSelectionResult` instances are frozen.
- [ ] `BreachBehaviorConfig.forced_reduction_short_trim_target_pct_of_limit` is the source of the 95% constant (no hardcoded `0.95` literal in selection code).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
