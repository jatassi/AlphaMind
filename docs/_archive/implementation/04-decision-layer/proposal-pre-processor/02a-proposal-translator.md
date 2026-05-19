# 02a — Proposal translator

## Goal

Ship `translate_recommendation_to_proposed_delta(recommendation, snapshot)` and `translate_position_assessment_to_proposed_delta(assessment, snapshot)` — pure functions that map an analyst `Recommendation` or a non-hold strategist `PositionAssessment` to the guardrail-evaluation library's `ProposedDelta` shape. These are the input adapters for §1.A's `evaluate_proposals` call (story 03 consumes them). The translators populate every `ProposedDelta` field directly from the agent-emitted record and the supplied portfolio snapshot — no resolver callbacks, no clock reads, no I/O.

## Reading

* `docs/design/04-decision-layer/proposal-pre-processor.md` § Inputs, § §1.A `combined_set_impact` — the basis section's `analyst_proposal_ids` and `strategist_action_ids` derive from the records this translator consumes; understand which records are eligible.
* `docs/design/04-decision-layer/analyst-output-schema.md` `$defs/recommendation`, `$defs/instrument`, `$defs/position_size` — the analyst-side input shape.
* `docs/design/04-decision-layer/strategist-output-schema.md` `$defs/position_assessment`, `$defs/action_parameters`, `$defs/close_parameters`, `$defs/reduce_parameters`, `$defs/add_parameters`, `$defs/adjust_bracket_parameters` — the strategist-side input shape; note the `recommended_action` enum.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` `_request_to_library_proposal`, `_library_notional_usd`, `_build_option_legs`, `_make_option_leg`, `_lookup_existing_position` — canonical translator pattern. Story 02a adapts these to the persisted-output shape.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `ProposedDelta`, `OptionLeg`, `Action`, `AssetType`, `Direction`, `ContractType` shapes; cross-field invariants the translator must satisfy.
* `src/alphamind/decision/analyst/models.py` — `Recommendation`, `Instrument` union, `PositionSize`, `Sector`.
* `src/alphamind/decision/strategist/models.py` — `PositionAssessment`, the `ActionParameters` discriminated union, `Sector`.
* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree, story 01) — confirm the typed-model surface this story does NOT consume directly (the translator returns `ProposedDelta`, not the bundle's `BasisSection` — story 03 wraps the call site).

## Depends on

* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree) — establishes the package and `__init__.py` to extend.

## Scope

In scope, all under `src/alphamind/decision/proposal_pre_processor/translator.py`. Tests at `tests/decision/proposal_pre_processor/test_translator.py`.

### 1\. Public function — analyst recommendation translator

```python
def translate_recommendation_to_proposed_delta(
    recommendation: Recommendation,
    *,
    snapshot: PortfolioStateSnapshot,
) -> ProposedDelta:
    """Convert an analyst Recommendation to the guardrail-evaluation library's ProposedDelta shape.

    The recommendation always represents an OPEN action — analyst recommendations
    are new entries, never additions/closes (existing-position management is the
    strategist's responsibility). The library validates this invariant.
    """
```

Field mapping:

* `id` ← `recommendation.recommendation_id` (already `REC-N` shape).
* `underlying` ← `recommendation.underlying`.
* `sector` ← `recommendation.sector` (string value of the imported `Sector` enum, e.g., `"tech"`).
* `direction` ← derived from `recommendation.instrument.direction` (`"long"` / `"short"` → `Direction.LONG` / `Direction.SHORT`).
* `asset_type` ← derived from `recommendation.instrument.asset_type` (`"equity"` / `"option"` / `"strategy"` → `AssetType.EQUITY` / `AssetType.OPTION` / `AssetType.STRATEGY`).
* `notional_usd` ← `recommendation.position_size.premium_at_risk` for option/strategy (when populated), else `recommendation.position_size.dollar_value`.
* `quantity` ← `float(recommendation.position_size.quantity)`.
* `option_legs` ← `None` for equity; for option, a one-tuple built from `instrument.{strike, expiration, contract_type, direction}` and `position_size.quantity`; for strategy, a tuple built from `instrument.legs[]`. Reuse the `_build_option_legs` / `_make_option_leg` shape from validation_tool, adapted to read directly from the `Recommendation`'s instrument variant.
* `action` ← `Action.OPEN`.
* `existing_position_id` ← `None` (analyst recommendations are always OPEN).
* `daily_borrow_cost_usd` ← For SHORT EQUITY: read from `snapshot.existing_positions` if a same-ticker SHORT EQUITY position exists (use that position's `daily_borrow_cost_usd`); otherwise raise `TranslatorError("borrow cost unavailable for new short on TICKER — caller must seed snapshot or skip translation")`. For all other cases: `None`.
* `reserves_capital` ← `True` if `recommendation.entry_order.type` is `"limit"` or `"stop_limit"`, else `False`.

### 2\. Public function — strategist position-assessment translator

```python
def translate_position_assessment_to_proposed_delta(
    assessment: PositionAssessment,
    *,
    snapshot: PortfolioStateSnapshot,
) -> ProposedDelta:
    """Convert a non-hold strategist PositionAssessment to ProposedDelta shape.

    Caller is responsible for filtering hold actions before calling this function;
    a hold-action assessment raises TranslatorError. Adjust-bracket actions are
    accepted and translated to Action.ADJUST (zero-exposure-change projection).
    """
```

Field mapping:

* `id` ← `assessment.assessment_id` (already `SA-N` shape).
* `underlying` ← `assessment.underlying`.
* `sector` ← `assessment.sector`.
* `direction` ← derived from the existing position at `snapshot.existing_positions[assessment.position_id].direction`.
* `asset_type` ← derived from the existing position at `snapshot.existing_positions[assessment.position_id].asset_type`.
* `notional_usd` and `quantity` ← derived from `assessment.action_parameters` per the discriminated union — for `close_parameters`: full or partial close based on `quantity` field (`"all"` → existing position's full `notional_usd` / `quantity`; numeric → that subset); for `reduce_parameters`: partial (`quantity` and pro-rated `notional_usd` from the existing position); for `add_parameters`: `additional_dollar_value` and `additional_quantity`; for `adjust_bracket_parameters`: `notional_usd=0.0`, `quantity=0.0` (adjust-bracket has no exposure change).
* `option_legs` ← `None` for equity positions; for option/strategy, reuse the existing position's option_legs (read from `snapshot.existing_positions[position_id]`'s `current_greeks` neighborhood — note that the library's `ExistingPosition` doesn't carry option_legs verbatim; this story may need to use a simpler path: `option_legs=None` works for ADD/CLOSE on the library side because the library reads greeks from the existing position record). **Surface to operator if this proves under-specified — see Surfacing conditions in parent.**
* `action` ← per the action-parameters discriminator: `close_parameters` → `Action.CLOSE`; `reduce_parameters` → `Action.CLOSE` (per parent decision D); `add_parameters` → `Action.ADD`; `adjust_bracket_parameters` → `Action.ADJUST`.
* `existing_position_id` ← `assessment.position_id`.
* `daily_borrow_cost_usd` ← `None` (the library re-reads from the existing position record for shorts).
* `reserves_capital` ← `False` (the existing position already reserves capital where applicable; the action-side flag is for new pending-order entries the analyst proposes).

### 3\. Translator-error class

```python
class TranslatorError(Exception):
    """Raised when a record cannot be translated to ProposedDelta.

    Cases:
    - Hold-action PositionAssessment passed to translate_position_assessment_to_proposed_delta.
    - PositionAssessment.position_id not present in snapshot.existing_positions.
    - SHORT EQUITY recommendation with no same-ticker short in the snapshot
      (no borrow cost available; caller must seed or skip).
    """
```

### 4\. Helpers

Internal helpers may be private (prefix `_`):

* `_build_option_legs_from_recommendation(recommendation: Recommendation) -> tuple[OptionLeg, ...] | None` — adapted from validation_tool's `_build_option_legs` to read from the `Recommendation` instrument variants.
* `_resolve_close_quantity_and_notional(close_params: CloseParameters, *, existing: ExistingPosition) -> tuple[float, float]` — handles the `quantity = "all"` vs. numeric case.
* `_action_from_recommended_action(recommended_action: str) -> Action` — handles the parent's pre-resolved decisions C and D (adjust-bracket → ADJUST; close + reduce → CLOSE; add → ADD).

### 5\. Public re-exports

Add `translate_recommendation_to_proposed_delta`, `translate_position_assessment_to_proposed_delta`, `TranslatorError` to `src/alphamind/decision/proposal_pre_processor/__init__.py`.

### Out of scope

* Histogram computations (story 02b).
* Conflict detection (story 02c).
* Calling `evaluate_proposals` (story 03).
* Mode-dispatch and bundle assembly (story 04).

## Acceptance criteria

- [ ] `translate_recommendation_to_proposed_delta` round-trips a long-equity recommendation: input `Recommendation(direction="long", asset_type="equity", quantity=100, dollar_value=15000.0)` produces `ProposedDelta(direction=LONG, asset_type=EQUITY, notional_usd=15000.0, quantity=100.0, option_legs=None, action=OPEN)`.
- [ ] A long-call-option recommendation produces `ProposedDelta` with `asset_type=OPTION`, `option_legs=(OptionLeg(contract_type=CALL, strike=..., expiration=..., quantity=+N),)` (positive — long), `notional_usd` equal to `position_size.premium_at_risk`.
- [ ] A multi-leg strategy recommendation produces `option_legs` of length matching `instrument.legs`, with each leg's `quantity` signed by direction.
- [ ] A SHORT EQUITY recommendation against a snapshot containing a same-ticker SHORT EQUITY existing position picks up that position's `daily_borrow_cost_usd`; against a snapshot with no such position, raises `TranslatorError`.
- [ ] A `limit`-type entry order recommendation produces `reserves_capital=True`; a `market`-type produces `False`.
- [ ] `translate_position_assessment_to_proposed_delta` on a `close` action with `quantity="all"` produces `ProposedDelta(action=CLOSE)` whose `quantity` and `notional_usd` match the existing position's full size.
- [ ] On a `reduce` action, the translator emits `Action.CLOSE` (per parent decision D) with the partial quantity and pro-rated notional.
- [ ] On an `adjust-bracket` action, the translator emits `Action.ADJUST` with `notional_usd=0.0` and `quantity=0.0`.
- [ ] On an `add` action, the translator emits `Action.ADD` with the `add_parameters.additional_*` quantities, `existing_position_id` populated.
- [ ] A `hold`-action `PositionAssessment` passed to `translate_position_assessment_to_proposed_delta` raises `TranslatorError`.
- [ ] A `PositionAssessment` whose `position_id` is missing from `snapshot.existing_positions` raises `TranslatorError`.
- [ ] The translated `ProposedDelta` instances are accepted by `evaluate_proposals` without raising `LibraryInputError` (pair-test against a fixture snapshot + library config + market inputs).
- [ ] `tests/decision/proposal_pre_processor/test_translator.py` exists and passes under `uv run pytest tests/decision/proposal_pre_processor/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/decision/proposal_pre_processor/test_translator.py -n auto`. The pair-test (translator output → library input) is the integration check that validates the cross-field invariants the library enforces (`option_legs is None ⇔ asset_type == EQUITY`, `existing_position_id required for ADD/CLOSE/ADJUST`, etc.) — if it passes, the translator's output is shape-correct.
