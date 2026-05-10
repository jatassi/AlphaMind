# 01f — `OptionGreeks` freshness metadata + sign convention docs

## Goal

Add three additive fields to `OptionGreeks` capturing the design's required staleness/refresh semantics: `as_of_timestamp: datetime` (tz-aware UTC, when the greeks were last computed), `iv_used: float` (the implied volatility used in the Black-Scholes refresh, persisted for audit/replay), and `refresh_failed: bool` (whether the last refresh attempt failed and the greeks fall back to the prior value with a widened uncertainty buffer). Document the sign convention for `theta` (negative for long options, positive for shorts). Consumers (continuous monitor § 4d, guardrail evaluation, paper harness) need the freshness metadata to know whether to widen uncertainty or escalate to emergency invocation. Without `as_of_timestamp`, downstream code can't tell 30-second-old greeks from 30-minute-old greeks.

## Reading

* `src/alphamind/portfolio_state/records/positions.py:38-46` — current `OptionGreeks` (delta, gamma, theta, vega) — frozen Pydantic with no metadata.
* `src/alphamind/portfolio_state/records/positions.py:74-86` — `OptionsPositionDetails` which embeds `greeks: OptionGreeks`.
* `src/alphamind/portfolio_state/records/positions.py:98-109` — `StrategyPositionDetails` with `strategy_greeks: OptionGreeks` (per-strategy aggregated greeks).
* `docs/design/05-execution-layer/architecture.md` § 4d — defines refresh cadence (15min scheduled + 2% move-based), the IV-fetch failure policy ("brief retry on IV fetch failure matching the adapter's submission retry pattern; on exhaustion, continue with the last successful greeks under a widened derivation uncertainty buffer and record `greeks_refresh_failed` in the activity log").
* `docs/design/06-risk-guardrails/guardrail-evaluation.md` — the library that consumes greeks for OPEN/ADD validation; needs to know whether the greeks it received are stale.
* `docs/design/05-execution-layer/paper-evaluation-harness.md` — paper harness uses greeks for derived option pricing; freshness affects uncertainty buffer.
* `tests/portfolio_state/records/test_positions.py::TestOptionGreeks` — existing happy-path test (line ~79); extend with new fields.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule applies; new fields default to safe values so existing fixture builders continue to work.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it adds three additive fields to an existing nested type in `records/positions.py`.

## Scope

Source under `src/alphamind/portfolio_state/records/positions.py` (three field adds + docstring). Tests at `tests/portfolio_state/records/test_positions.py` (extend `TestOptionGreeks`).

### 1\. Add freshness metadata fields

In `OptionGreeks`:

```python
from datetime import datetime
from pydantic import field_validator

class OptionGreeks(BaseModel):
    """Greeks for an options position.

    Sign conventions (per Black-Scholes textbook):
    * delta: positive for long calls (0 to 1), negative for long puts (-1 to 0).
      For short positions, the parent OptionsPositionDetails sign-flips externally
      via the position-level direction; this record stores the *long-equivalent*
      delta of the contract itself.
    * gamma: always positive (curvature of delta wrt underlying).
    * theta: NEGATIVE for long options (decay reduces option value over time);
      consumers needing the position-level theta must sign-flip for short positions.
    * vega: always positive (sensitivity to IV; higher IV always raises long
      option prices).
    """

    model_config = ConfigDict(frozen=True)

    delta: float
    gamma: float
    theta: float
    vega: float

    # Freshness metadata (added per architecture.md § 4d)
    as_of_timestamp: datetime | None = None
    iv_used: float | None = None
    refresh_failed: bool = False

    @field_validator("as_of_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            msg = "as_of_timestamp must be tz-aware UTC when not None"
            raise ValueError(msg)
        return v

    @field_validator("iv_used")
    @classmethod
    def _require_positive_iv(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            msg = f"iv_used must be > 0 when not None; got {v}"
            raise ValueError(msg)
        return v
```

### 2\. Defaults reasoning

* `as_of_timestamp: datetime | None = None` — None during construction is allowed (legacy code paths and per-fixture); production-path producers must populate it. Tooling that asserts freshness (continuous monitor § 4d, paper harness) reads this and treats `None` as "unknown freshness — apply max uncertainty."
* `iv_used: float | None = None` — None when the greeks were derived from a non-Black-Scholes source or when refresh hasn't completed. The Black-Scholes refresh path always populates it.
* `refresh_failed: bool = False` — defaults to False (presumed-fresh). The continuous monitor sets this `True` when refresh exhausts retries and the greeks fall back to the prior value.

### 3\. Docstring updates

Update the `OptionGreeks` class docstring (per the code in §1) to include the sign convention block. Cross-reference `architecture.md § 4d` for refresh cadence details.

### Out of scope

* Adding refresh-cadence enforcement to OptionGreeks itself — that's the continuous monitor's job (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* Adding a separate `GreeksHistoryRecord` for tracking refresh history — that's a future story; this one only captures the most-recent-refresh metadata.
* Computing the greeks themselves — handled by `risk_guardrails/guardrail_evaluation` (Black-Scholes library, <issue id="ddaa28ff-1eff-4a23-b40a-6691aae97123">ALP-134</issue>, *done*).
* Wiring the `refresh_failed` flag into the activity log's `greeks_refresh_failed` event — that's the continuous monitor's responsibility.
* Modifying `OptionsPositionDetails` or `StrategyPositionDetails` to surface the embedded greeks' freshness — consumers can reach in directly via `position.options_details.greeks.as_of_timestamp`.

## Acceptance criteria

- [ ] `OptionGreeks.as_of_timestamp: datetime | None = None` field exists, with field validator requiring tz-aware UTC when non-None.
- [ ] `OptionGreeks.iv_used: float | None = None` field exists, with field validator requiring `> 0` when non-None.
- [ ] `OptionGreeks.refresh_failed: bool = False` field exists.
- [ ] All three new fields default to None / False so existing `OptionGreeks(delta=..., gamma=..., theta=..., vega=...)` constructions continue to succeed.
- [ ] Constructing `OptionGreeks(..., as_of_timestamp=datetime.now())` (naive datetime) raises `ValidationError`.
- [ ] Constructing `OptionGreeks(..., as_of_timestamp=datetime.now(tz=UTC))` (tz-aware) succeeds.
- [ ] Constructing `OptionGreeks(..., iv_used=0.0)` raises `ValidationError`.
- [ ] Constructing `OptionGreeks(..., iv_used=0.45)` succeeds.
- [ ] Constructing `OptionGreeks(..., iv_used=-0.1)` raises `ValidationError`.
- [ ] `OptionGreeks` class docstring includes the four sign-convention paragraphs (delta, gamma, theta, vega).
- [ ] All existing `uv run pytest -n auto` tests pass — no pre-existing test fixture breaks.
- [ ] `tests/portfolio_state/records/test_positions.py::TestOptionGreeks` exercises: (a) all-None freshness defaults, (b) populated freshness fields succeed, (c) naive-datetime rejected, (d) zero/negative iv_used rejected, (e) refresh_failed True when freshness fields are populated, (f) frozen-model — can't mutate after construction.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus the new freshness tests.
* Run `uv run pytest tests/portfolio_state/records/test_positions.py -n auto -v` — confirm new tests pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.positions import OptionGreeks; print(set(OptionGreeks.model_fields.keys()))"` should include `as_of_timestamp`, `iv_used`, `refresh_failed`.
* Lint clean per CLAUDE.md.