---
status: not_started
completed_date:
commit_id:
---

# 07 — Guardrail validation tool

## Goal

Land the deterministic agent-callable validation tool that the analyst, strategist, and PM use to pre-check proposals against guardrail state during reasoning. The tool composes the guardrail-evaluation library's per-rule projection primitives, layers cumulative-impact tracking across multiple calls within a single agent invocation, and emits the canonical typed I/O contract with `overall`, `per_rule`, `delta_adjusted_exposure`, `greeks`, `cumulative_impact_note`, and `failure_guidance` fields. Produces the verbatim contract documented in [`state-delivery.md § Guardrail validation tool`](../../../design/06-risk-guardrails/state-delivery.md#guardrail-validation-tool).

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — the authoritative I/O contract; input/output schemas, behavioral contract (cumulative tracking + state reset + shared math + failure guidance).
- `docs/design/06-risk-guardrails/guardrail-evaluation.md` — the library this tool composes; canonical per-rule output object, primitives (active regime parameter resolution, feature-flag early-exit, delta-adjusted exposure, per-rule projection and evaluation), caller-orchestration table.
- `docs/design/04-decision-layer/analyst.md` § Pre-submission guardrail validation — the analyst's two-layer approach (headroom context + validation tool); cumulative-impact behavior across multiple proposals.
- `docs/design/04-decision-layer/strategist.md` § Pre-submission guardrail validation — same tool, applied to ADD/CLOSE/REDUCE actions.
- `docs/design/04-decision-layer/portfolio-manager.md` § Pre-submission guardrail check on PM modifications — same tool, applied to PM sizing modifications.
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Per-rule enforcement summary — which rules are checked at T1 (same set the validation tool checks).
- `src/alphamind/portfolio_state/records/capital.py` — `RiskZone`, `RegimeLabel` typed enums.
- `src/alphamind/portfolio_state/records/positions.py` — `Direction`, `InstrumentType` typed enums.
- `02-package-skeleton-and-config.md` — `state_delivery/validation_tool.py` is the target module; `StateDeliveryConfig` is loaded but does not currently carry tool-specific knobs.

## Depends on

- 02 (package skeleton + config)
- **Cross-feature dependency:** the `guardrail-evaluation` work tree ships the per-rule projection primitive (`evaluate_per_rule(...)`), the active regime parameter resolution primitive, the feature-flag early-exit primitive, and the Black-Scholes greeks computation. The library's typed `PerRuleResult` output object is the same shape this tool emits per-rule. For story 07 to dispatch, the guardrail-evaluation work tree's stories that produce these primitives and the `PerRuleResult` shape must be `done`. The orchestrator (this work tree's `ORCHESTRATOR.md`) calls out this gate.

(03/04*/05/06 are NOT dependencies — story 07 is independently composable on top of 02 and the guardrail-evaluation library.)

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/validation_tool.py`. Tests at `tests/risk_guardrails/state_delivery/test_validation_tool.py`.

### 1. Input contract — typed value objects

```python
class ValidationInstrument(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    asset_type: InstrumentType   # equity | option | strategy
    direction: Direction         # long | short
    # Options-only fields, populated only when asset_type == OPTION
    strike: float | None = None
    expiration: datetime | None = None
    contract_type: Literal["call", "put"] | None = None
    # Strategy-only field, populated only when asset_type == STRATEGY
    legs: tuple[ValidationStrategyLeg, ...] | None = None

    @model_validator(mode="after")
    def _validate_options_fields(self) -> ValidationInstrument:
        if self.asset_type == InstrumentType.OPTION:
            missing = [f for f in ("strike", "expiration", "contract_type") if getattr(self, f) is None]
            if missing:
                msg = f"OPTION asset_type requires {missing}"
                raise ValueError(msg)
        if self.asset_type == InstrumentType.STRATEGY and (self.legs is None or not self.legs):
            msg = "STRATEGY asset_type requires non-empty legs"
            raise ValueError(msg)
        return self


class ValidationStrategyLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    direction: Direction
    asset_type: InstrumentType
    strike: float | None = None
    expiration: datetime | None = None
    contract_type: Literal["call", "put"] | None = None
    quantity: int                # contracts (or shares for equity legs in strategies)


class ValidationSize(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: int                # shares for equity, contracts for options/strategies
    dollar_value: float          # at current market price for equity; premium for options
    premium_at_risk_usd: float | None = None  # options/strategies only

    @model_validator(mode="after")
    def _validate_premium(self) -> ValidationSize:
        if self.dollar_value < 0:
            msg = f"dollar_value must be non-negative; got {self.dollar_value}"
            raise ValueError(msg)
        if self.premium_at_risk_usd is not None and self.premium_at_risk_usd < 0:
            msg = f"premium_at_risk_usd must be non-negative when set; got {self.premium_at_risk_usd}"
            raise ValueError(msg)
        return self


class ValidationAction(StrEnum):
    OPEN = "OPEN"
    ADD = "ADD"
    CLOSE = "CLOSE"
    ADJUST = "ADJUST"


class ValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: ValidationInstrument
    size: ValidationSize
    action: ValidationAction
```

The renderer-side typed input mirrors the design's `validate_guardrail(instrument, size, action)` signature. Naming uses `Validation*` prefix so the value objects do not collide with similar-shape types in the OMS or pre-processor work trees.

### 2. Output contract — typed value objects

The output's per-rule shape is the canonical `PerRuleResult` from the guardrail-evaluation library. This story re-exports it (or imports it under the same name); it does not redeclare:

```python
from alphamind.risk_guardrails.guardrail_evaluation import PerRuleResult
```

If the guardrail-evaluation library defines its canonical type with a different name (e.g., `RuleProjection`), this story imports it under that name and the output contract uses the library's name. Resolve at implementation time once the library's stories land.

```python
class ValidationGreeks(BaseModel):
    model_config = ConfigDict(frozen=True)

    delta: float
    gamma: float
    theta: float
    vega: float


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    overall: Literal["PASS", "FAIL"]
    per_rule: tuple[PerRuleResult, ...]
    delta_adjusted_exposure: float
    greeks: ValidationGreeks | None              # populated for OPTION and STRATEGY actions only
    cumulative_impact_note: str
    failure_guidance: str | None                 # populated only when overall == "FAIL"
    proposal_index_in_invocation: int            # 1-indexed; first call returns 1, second returns 2, ...
```

The output's `proposal_index_in_invocation` is part of the cumulative-tracking surface — exposed so the agent can see how many calls have run and reason about the cumulative-impact note.

### 3. Cumulative-tracking state object

The validation tool tracks per-invocation cumulative state across calls. The state is held by a `ValidationToolState` object that the agent constructs once at the start of its invocation and passes to each call:

```python
class ValidationToolState(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    invocation_id: str
    starting_snapshot: PortfolioStateSnapshot
    starting_risk_budget: RiskBudgetConsumption
    starting_active_risk_parameters: ActiveRiskParameterSet
    profile_feature_flags: ProfileFeatureFlags
    accumulated_deltas: tuple[ProjectedDelta, ...] = ()      # one entry per validated proposal so far

    def with_accepted_proposal(self, delta: ProjectedDelta) -> ValidationToolState:
        return self.model_copy(update={"accumulated_deltas": (*self.accumulated_deltas, delta)})


class ProjectedDelta(BaseModel):
    """A previously-validated proposal's projected impact, used for cumulative tracking."""
    model_config = ConfigDict(frozen=True)

    instrument: ValidationInstrument
    size: ValidationSize
    action: ValidationAction
    delta_adjusted_exposure: float
    greeks: ValidationGreeks | None
    proposal_index: int


class ProfileFeatureFlags(BaseModel):
    model_config = ConfigDict(frozen=True)

    options_enabled: bool
    short_selling_enabled: bool
```

The state is **frozen** — every accepted proposal returns a new state via `with_accepted_proposal(...)`. This avoids mutation surprises and keeps the call chain deterministic.

The state lifecycle:

- **Construction:** the agent's prompt-assembly stage builds the initial `ValidationToolState` from the snapshot, the resolved profile, and the active risk parameters. Index counter starts at zero (no accepted proposals yet); first call's `proposal_index_in_invocation` is `1`.
- **Per call:** the tool reads the state, runs the projection (accounting for `accumulated_deltas`), returns a `ValidationResult`. On `PASS`, the caller invokes `state = state.with_accepted_proposal(...)` to add the proposal to the cumulative set. On `FAIL`, the caller decides whether to revise and re-call (in which case the failed proposal is NOT added to `accumulated_deltas` — the agent revises and the next call starts fresh against the prior accepted set).
- **Reset:** at agent-invocation boundaries, the next agent (analyst → strategist → PM) builds a fresh `ValidationToolState` with an empty `accumulated_deltas`. The design's "State reset: tracking resets at agent-invocation boundaries" rule.

### 4. Tool function

```python
def validate_guardrail(
    *,
    request: ValidationRequest,
    state: ValidationToolState,
) -> ValidationResult
```

Procedure:

1. **Feature-flag early-exit.** If `request.instrument.asset_type == OPTION` and `state.profile_feature_flags.options_enabled is False`, return:
   ```
   ValidationResult(
       overall="FAIL",
       per_rule=(),
       delta_adjusted_exposure=0.0,
       greeks=None,
       cumulative_impact_note=_format_cumulative_impact_note(state),
       failure_guidance="Options trading is disabled for this portfolio profile.",
       proposal_index_in_invocation=len(state.accumulated_deltas) + 1,
   )
   ```
   Same for `direction == SHORT` when `short_selling_enabled is False`. The early-exit produces no per-rule entries — disabled features are blocking by feature, not by rule.

2. **Compose with the guardrail-evaluation library.** Call the library's `evaluate_per_rule_projection(...)` (or whatever the library exposes; resolve naming at implementation time) with:
   - `current_state` derived from `state.starting_snapshot` (positions, sector exposure, directional exposure, drawdown, capital).
   - `proposed_deltas` = `state.accumulated_deltas + (this_request_as_delta,)` — the cumulative set including this call's request.
   - `active_regime_parameters` = `state.starting_active_risk_parameters`.
   - `profile_feature_flags` = `state.profile_feature_flags`.
   The library returns the canonical `tuple[PerRuleResult, ...]` plus the per-instrument `delta_adjusted_exposure` and `greeks` (for options/strategies).

3. **Determine `overall`.** PASS if every `PerRuleResult.status == PASS`; FAIL if any is FAIL. WARNING-status entries do not flip overall to FAIL — the design's WARNING zone is informational, not blocking.

4. **Populate `cumulative_impact_note`.** Always present. Helper:

   ```python
   def _format_cumulative_impact_note(state: ValidationToolState) -> str:
       prior = len(state.accumulated_deltas)
       this_index = prior + 1
       if prior == 0:
           return (
               f"This is proposal #{this_index} in this invocation. "
               f"No prior proposals affect headroom calculations."
           )
       return (
           f"This is proposal #{this_index} in this invocation. "
           f"Cumulative impact of proposals #1–{prior} is included in headroom calculations."
       )
   ```

5. **Populate `failure_guidance` (FAIL only).** When `overall == FAIL`, walk the failed `per_rule` entries and produce a guidance string per the design's worked example: e.g., `"Reduce size by 15% to pass sector concentration"`. The guidance helper takes the failed entries and the request, and returns:

   - For a single-rule failure caused by sector concentration, options delta, theta, vega, or per-position size: `"Reduce size by ~{pct}% to pass {rule_label}"` where `pct` = the percentage reduction needed to bring the projected value back inside the limit. Computation: `pct = (projected_after - limit) / projected_after * 100`, rounded to the nearest integer.
   - For a single-rule failure caused by net-long, net-short, or gross exposure: `"Reduce size by ~{pct}% to pass {rule_label}, or substitute a lower-delta instrument"`.
   - For a single-rule failure caused by capital: `"Insufficient deployable capital ({available_usd:,.0f}); reduce dollar value to ~${suggested_usd:,.0f} or fewer"`.
   - For a multi-rule failure: `"Multiple rules would breach: {comma-separated rule_labels}. Reducing size by ~{pct}% addresses {primary rule}; revise instrument or skip to address others."`. The `primary rule` is the most-overage rule.
   - For feature-disabled failures (early-exit path): the static text in step 1.

   The guidance is a best-effort suggestion, not a binding contract — the design's "Starting point, not binding" wording. The tool surfaces enough specificity that the agent can revise; deciding what to actually do is the agent's call.

6. **Return** the `ValidationResult` with `proposal_index_in_invocation` = `len(state.accumulated_deltas) + 1`.

The function is pure: same `(request, state)` inputs produce the same output. It does NOT mutate `state` — the caller invokes `state.with_accepted_proposal(...)` if it accepts the result.

### 5. Helper: `_request_as_projected_delta`

Internal helper that converts a `ValidationRequest` to a `ProjectedDelta` value object — used both during projection (step 2) and when the caller accepts the proposal:

```python
def _request_as_projected_delta(
    request: ValidationRequest,
    delta_adjusted_exposure: float,
    greeks: ValidationGreeks | None,
    proposal_index: int,
) -> ProjectedDelta
```

Pure function; no side effects.

### 6. Tests

Tests at `tests/risk_guardrails/state_delivery/test_validation_tool.py`:

- **Input validation:**
  - `ValidationInstrument(asset_type=OPTION, ...)` missing `strike` / `expiration` / `contract_type` raises `ValueError`.
  - `ValidationInstrument(asset_type=STRATEGY, legs=None)` raises `ValueError`.
  - `ValidationSize(dollar_value=-100)` raises `ValueError`.
  - `ValidationSize(premium_at_risk_usd=-50)` raises `ValueError`.

- **Feature-flag early-exit:**
  - Equity OPEN on a profile with `options_enabled=False` does NOT trigger the options early-exit (only option-typed instruments do).
  - Option OPEN on a profile with `options_enabled=False` returns FAIL with no per-rule entries; `failure_guidance` is the static disabled-features text; the guardrail-evaluation library is never called.
  - Short OPEN on a profile with `short_selling_enabled=False` returns FAIL with no per-rule entries; same shape.

- **Cumulative tracking:**
  - First call on an empty state returns `proposal_index_in_invocation=1` and `cumulative_impact_note` reads "No prior proposals affect headroom calculations."
  - After accepting a 5%-of-portfolio long (adds to `accumulated_deltas`), second call returns `proposal_index_in_invocation=2` and `cumulative_impact_note` mentions "Cumulative impact of proposals #1–1 is included".
  - Two individually-PASSING proposals where each contributes 12% to net-long exposure (current 40%, limit 60%) — first PASSES (projected 52%), second FAILS (projected 64%) because the cumulative state includes the first.
  - Failed proposals do NOT add to `accumulated_deltas` (the caller doesn't invoke `with_accepted_proposal`); revising and re-calling produces a fresh projection against the prior accepted set.

- **State reset across agents:** the test constructs a `ValidationToolState`, runs three accepted proposals, then constructs a fresh `ValidationToolState` (simulating analyst → strategist boundary) and confirms the new state's `accumulated_deltas` is empty and `proposal_index_in_invocation` for the first call is `1`.

- **Library composition (mock):** a stub `evaluate_per_rule_projection` returning hand-crafted `PerRuleResult` tuples confirms the validation tool correctly:
  - Sets `overall="PASS"` when every entry is PASS (or PASS + WARNING).
  - Sets `overall="FAIL"` when any entry is FAIL.
  - Surfaces the library's per-rule values verbatim in the output.

- **Failure guidance:**
  - Single sector-concentration failure → guidance includes `"Reduce size by ~X%"` and the rule label.
  - Single net-long failure → guidance includes the substitute-instrument suggestion.
  - Capital failure → guidance shows the available dollar value and a suggested smaller dollar value.
  - Multi-rule failure → guidance lists all failing rule labels with the primary-rule reduction percentage.
  - PASS results have `failure_guidance is None`.

- **Greeks population:**
  - `asset_type=EQUITY` actions return `greeks=None`.
  - `asset_type=OPTION` actions return a populated `ValidationGreeks` (mocked from the library's response).
  - `asset_type=STRATEGY` actions return a populated `ValidationGreeks` (net values from the library's response).

- **Determinism:** identical `(request, state)` inputs produce byte-identical `ValidationResult` outputs.

- **`with_accepted_proposal` immutability:** calling `state.with_accepted_proposal(delta)` returns a new state; the original state's `accumulated_deltas` is unchanged.

Out of scope:
- Implementing the per-rule projection math, Black-Scholes greeks computation, or active regime parameter resolution — those live in the guardrail-evaluation library (sibling work tree).
- Implementing the engine's T3 authoritative check — that lives in the execution-layer guardrail enforcement layer (separate work tree).
- Implementing the proposal pre-processor's combined-set check — that lives in the proposal-pre-processor work tree.
- Wiring the tool into the analyst / strategist / PM agent's tool registration — that lives in each agent's runtime story.
- Persisting validation-tool calls to the activity log — the tool is a per-invocation reasoning helper, not a persisted decision; calls are not logged.
- Per-rule custom failure-guidance templates beyond the documented set — additions land when an operator or feedback-loop signal demands them.

## Notes

The validation tool is a thin orchestration layer over the guardrail-evaluation library. The library owns the math; the tool owns the cumulative tracking, the typed I/O, the `cumulative_impact_note`, the `failure_guidance`, and the feature-flag early-exit semantics. This separation follows the design's "caller orchestration" table — each caller composes the library's per-rule output with its own framing.

The frozen-state-with-copy pattern (`ValidationToolState.with_accepted_proposal`) avoids the surprise of an agent thinking it just validated a proposal but the next call's projection not accounting for it. The explicit accept step makes the cumulative-tracking decision visible: revising a failed proposal does not silently consume headroom; only acceptance does.

Per `feedback_simplify_before_building.md`, the `failure_guidance` helper handles only the documented templates. Generic catch-all guidance ("Try a different size or instrument") is worse than a null guidance — surfacing nothing is the failure-guidance's null state. When a new failure mode emerges from feedback-loop data, this helper is extended with a new template, not preemptively over-generalized.

Per `feedback_no_inventing_component_names.md`, the typed I/O names mirror the design's section labels: `validate_guardrail` is the design's tool name; `ValidationResult` is the output contract's documented shape; the per-rule entry uses the guardrail-evaluation library's canonical type without rename. The `ValidationToolState` and `ProjectedDelta` typed records exist because the design's "Cumulative tracking" behavior requires a state object — the names follow from the behavior.

Per `feedback_avoid_numeric_anchors.md`, no zone thresholds, regime multipliers, or rule limit values appear in this story's code. The validation tool composes typed inputs from `state.starting_active_risk_parameters` and the guardrail-evaluation library's per-rule output. Every numeric value in the output flows from upstream typed inputs.

Per `feedback_per_producer_schema.md`, the validation tool's typed I/O is its own per-producer contract, separate from the proposal pre-processor's bundle schema and the engine's T3 envelope schema. All three callers compose the same `PerRuleResult` from the library; each wraps it in its own framing per-producer.

Per `feedback_no_decision_trails.md`, the validation tool emits failure guidance positively. The guidance text says what to do ("Reduce size by ~15%"), not what failed historically or which rules conflicted in the past. The guidance is forward-looking and actionable.

Cross-feature dependency callout (load-bearing): this story depends on the guardrail-evaluation work tree's stories that produce `evaluate_per_rule_projection`, the `PerRuleResult` canonical output, the regime parameter resolution primitive, the feature-flag early-exit primitive, and the Black-Scholes greeks computation. Until those land, story 07 is implementable against a stub library — but the production implementation must wire to the real library before story 08's end-to-end verification runs. The orchestrator surfaces this gate before dispatching story 07.

The design's "shared math" property is preserved through library composition: a proposal passing the validation tool also passes the engine's T3 check (barring state drift between T1 reasoning and T3 execution, which the synchronous-rejection feedback path handles). The validation tool does not duplicate the math — it composes the same primitives.

The activity log integration referenced in `state-delivery.md` § Cross-constraint impact visibility ("Each check returns projected per-rule headroom...") is implicit — the tool returns the projection synchronously to the agent; persisting tool-call outputs to the activity log is not required because the resulting envelope (PM-issued command) carries the validation-time greeks and per-rule context per `oms-commands.md` § Greek computation at validation time.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` exists with `validate_guardrail`, `ValidationRequest`, `ValidationResult`, `ValidationToolState`, `ValidationInstrument`, `ValidationStrategyLeg`, `ValidationSize`, `ValidationAction`, `ValidationGreeks`, `ProjectedDelta`, `ProfileFeatureFlags` exposed (re-exported from `state_delivery/__init__.py`).
- [ ] All typed value objects are frozen Pydantic v2 models with the documented fields and validators.
- [ ] `ValidationInstrument(asset_type=OPTION, ...)` requires `strike` / `expiration` / `contract_type`; raises `ValueError` if any is missing.
- [ ] `ValidationInstrument(asset_type=STRATEGY, legs=None)` raises `ValueError`.
- [ ] `ValidationSize(dollar_value=-x)` raises `ValueError`.
- [ ] Option OPEN on `options_enabled=False` profile returns `overall="FAIL"`, empty `per_rule`, and the static `failure_guidance` for disabled features; the guardrail-evaluation library is not called (verified via mock).
- [ ] Short OPEN on `short_selling_enabled=False` profile returns same shape.
- [ ] First call on empty state returns `proposal_index_in_invocation=1` and `cumulative_impact_note` reads the no-prior-proposals text; subsequent calls increment the index and update the note.
- [ ] Two PASSING proposals that together would breach a rule produce: first PASS, second FAIL — the cumulative state correctly accounts for the first proposal's impact.
- [ ] Failed proposals do not get added to `accumulated_deltas` (the caller controls acceptance via `state.with_accepted_proposal`).
- [ ] `state.with_accepted_proposal(delta)` returns a new `ValidationToolState` with `accumulated_deltas + (delta,)`; the original state's `accumulated_deltas` is unchanged.
- [ ] `overall="PASS"` when every `per_rule` entry is PASS or WARNING; `overall="FAIL"` when any entry is FAIL.
- [ ] `failure_guidance is None` on PASS; non-None and matching one of the documented templates on FAIL (single sector, single directional, capital, multi-rule).
- [ ] `greeks is None` for EQUITY actions; populated for OPTION and STRATEGY actions.
- [ ] Repeated `validate_guardrail(request, state)` calls produce byte-identical `ValidationResult` outputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
