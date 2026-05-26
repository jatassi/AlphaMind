# Strategy direction category error on guardrail-evaluation proposal types (ProposedDelta / ExistingPosition)

The guardrail-evaluation library's input types carry a required position-level `direction` that is a category error for multi-leg options strategies — the proposal-side counterpart of the `PositionRecord` fix in [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category). This issue makes those fields honestly optional.

## Symptom

`ProposedDelta.direction` and `ExistingPosition.direction` (`guardrail_evaluation/types.py`, the `guardrail_evaluation.Direction` enum) and `ValidationInstrument.direction` (`state_delivery/validation_tool.py`, the `positions.Direction` enum) are required `Direction` fields. For a multi-leg options strategy a position-level long/short `direction` is meaningless — the same category error [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) fixes on the persisted `PositionRecord`. The producers fabricate a value: `submit_envelope/process.py` hard-codes `Direction.LONG` for a strategy `ValidationInstrument`; `proposal_pre_processor/translator.py` takes a strategy's direction from its first leg; `library_snapshot.py` maps the persisted placeholder through `_DIRECTION_TO_LIBRARY`.

[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story 01d already made the guardrail *math* leg-derived (strategy directional exposure and greeks come from per-leg data), so runtime behavior is already correct. What remains is the *field shape*: the boundary types still require a `direction` no strategy consumer reads meaningfully.

## Why filed separately

[ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) is scoped to the persisted `PositionRecord` / `PositionView`. The guardrail-evaluation library-input types and the validation-tool request types are a distinct record family with their own lifecycle; folding them into [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) roughly doubles that work tree's audit surface. This was decided out of scope there ([ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) pre-resolved decision E) and recorded as the agreed follow-on.

## Dependencies

**Hard-gated on** [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end)**.** [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) wave 2 (stories 02, 03a–03c, 04) actively reshapes every producer file in this issue's scope — `submit_envelope/process.py`, `proposal_pre_processor/translator.py`, `library_snapshot.py`, `validation_tool.py`. Do not start until [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) is `Done` and merged to `main`, and implement against the post-[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) merged code as ground truth — the Scope below describes the pre-[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end)-wave-2 shape as orientation, not as a contract.

## Scope

Three required `direction` fields become optional; their producers emit `None` for a strategy; their consumers are audited for strategy-`None` handling. This is one mypy-green change — there is no decoupling seam, so the type flips, producers, and consumers land together in a single PR.

**(1) Library boundary types —** `guardrail_evaluation/types.py`**.** `ProposedDelta.direction` and `ExistingPosition.direction` become `Direction | None`. `None` is the value for a multi-leg strategy; equity and single-leg options keep a non-`None` `Direction`. Update the dataclass docstrings to state the optional shape and that a strategy's directional sign lives in its per-leg / net-greeks data, not this field. Do not add a `__post_init__` validator — these dataclasses are deliberately constructible without runtime validation (module docstring: cross-field invariants are checked at the entry point).

**(2) Validation-tool request type —** `state_delivery/validation_tool.py`**.** `ValidationInstrument.direction` becomes `Direction | None` (the `positions.Direction` enum). `ValidationStrategyLeg.direction` stays required — per-leg directions are real. Extend `ValidationInstrument._validate_options_fields` (or a sibling `model_validator`) so the field is required for `EQUITY` / `OPTIONS` and `None` for `STRATEGY`. The `_DIRECTION_TO_LIBRARY` lookups in `_request_to_library_proposal` and `_lookup_existing_position` become `None`-tolerant (`None` maps to `None`); `_lookup_existing_position` then matches a strategy existing position on ticker + asset-type with `direction == None` on both sides. `_build_option_legs` / `_make_option_leg` narrow on the validator's `OPTIONS ⇒ direction not None` guarantee. The `is_short` reads in `_disabled_feature_guidance` and `_needs_borrow_cost` already evaluate correctly for `None` (equality comparisons against `Direction.SHORT`) — confirm, do not rewrite. If the `validate_guardrail` MCP tool's agent-facing schema text instructs the agent to supply `direction` for a strategy, update it so a strategy request omits the field.

**(3) Producer —** `library_snapshot.py`**.** The `ExistingPosition` construction in the `to_library_snapshot` loop emits `direction=None` for a strategy position (gate on `pos.instrument_type is InstrumentType.STRATEGY`) instead of `_DIRECTION_TO_LIBRARY[pos.direction]`. Equity / single-leg options keep the mapped `Direction`.

**(4) Producer —** `proposal_pre_processor/translator.py`**.** `_direction_from_instrument` returns `Direction | None`, returning `None` for an `InstrumentStrategy` instead of the first-leg direction. `translate_recommendation_to_proposed_delta` passes that through. `translate_position_assessment_to_proposed_delta` already reads `existing.direction`, which carries `None` for a strategy once `ExistingPosition.direction` is optional — no separate change there.

**(5) Producer —** `submit_envelope/process.py`**.** `_open_instrument_kwargs` and `_add_instrument_kwargs` emit `direction=None` for a `STRATEGY` instrument instead of the hard-coded `Direction.LONG`. Update the docstrings describing the `Direction.LONG`-by-convention placeholder.

**(6) Consumer audit —** `guardrail_evaluation/`**.** Confirm the eight `proposal.direction` read sites (`feature_gate.py`, `delta_adjusted.py`, `evaluate.py`, `rules/capital.py`, `rules/shorts.py`) handle a strategy's `None`. All but one are identity comparisons (`is` / `is not Direction.SHORT|LONG`) that already treat `None` correctly. `delta_adjusted.py` computes `direction_sign` from `proposal.direction` then discards it for a strategy (the sign is overridden by `net_greeks.delta`) — restructure so `direction` is read only on the EQUITY / single-leg OPTION paths where it is guaranteed non-`None`, and update the comment referencing the "inert `LONG` placeholder ([ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) decision C)". Add a cross-field check at the `evaluate.py` entry point — `direction is None` iff `asset_type is STRATEGY` — alongside the existing `option_legs` / `asset_type` invariant, so a malformed proposal fails fast.

## Out of scope

`PositionRecord.direction` / `PositionView.direction` and their reads — [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category). `submit_envelope/dispatch.py`'s `position.direction` reads consume the persisted `PositionRecord` and belong to [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) story 02d ([ALP-609](https://linear.app/alphamind-jatassi/issue/ALP-609/02d-decision-layer-strategy-aware-direction-reads)), not this issue. The metadata-only `ValidationInstrument` placeholder for `ADJUST` commands (`process.py`, built as `EQUITY` + `Direction.LONG`) is not a strategy and is left unchanged.

## Coordination

[ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) story 02a ([ALP-606](https://linear.app/alphamind-jatassi/issue/ALP-606/02a-risk-guardrails-strategy-aware-direction-reads)) also edits `library_snapshot.py` — it migrates the `pos.direction` reads onto [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category)'s `position_direction()` accessor. Whichever of [ALP-603](https://linear.app/alphamind-jatassi/issue/ALP-603/strategy-direction-category-error-on-guardrail-evaluation-proposal) / [ALP-606](https://linear.app/alphamind-jatassi/issue/ALP-606/02a-risk-guardrails-strategy-aware-direction-reads) lands second rebases the `ExistingPosition` construction block; both converge on "strategy ⇒ `direction=None`". This issue does not depend on `position_direction()` — it determines strategy-ness from `pos.instrument_type`.

## Reading

* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `ProposedDelta`, `ExistingPosition`, the `Direction` enum.
* `src/alphamind/risk_guardrails/guardrail_evaluation/delta_adjusted.py` § `compute_delta_adjusted_exposure` — the one consumer needing a restructure.
* `src/alphamind/risk_guardrails/guardrail_evaluation/feature_gate.py`, `evaluate.py`, `rules/capital.py`, `rules/shorts.py` — the already-`None`-safe consumer read sites.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` — `ValidationInstrument`, `ValidationStrategyLeg`, `_DIRECTION_TO_LIBRARY`, `_request_to_library_proposal`, `_lookup_existing_position`, `_build_option_legs`.
* `src/alphamind/risk_guardrails/library_snapshot.py` § the `to_library_snapshot` loop's `ExistingPosition` construction.
* `src/alphamind/decision/proposal_pre_processor/translator.py` — `_direction_from_instrument` and the two `ProposedDelta` builders.
* `src/alphamind/decision/portfolio_manager/submit_envelope/process.py` — `_open_instrument_kwargs`, `_add_instrument_kwargs`.
* `ALP-591` — the `PositionRecord` counterpart; mirror its optional-direction shape.

## Acceptance criteria

* `ProposedDelta.direction` and `ExistingPosition.direction` are typed `Direction | None`; constructing either with `direction=None` succeeds.
* `ValidationInstrument.direction` is typed `Direction | None`; a `STRATEGY` instrument validates with `direction` omitted and rejects a non-`None` `direction`; an `EQUITY` / `OPTIONS` instrument still requires it; `ValidationStrategyLeg.direction` is unchanged (required).
* `library_snapshot.py` produces `ExistingPosition(direction=None)` for a strategy position and a non-`None` `Direction` for equity / single-leg options.
* `translator.py` produces `ProposedDelta(direction=None)` for an `InstrumentStrategy` recommendation; `_direction_from_instrument` no longer reads `legs[0].direction`.
* `submit_envelope/process.py` builds a `STRATEGY` `ValidationInstrument` with `direction=None`; no `Direction.LONG` literal remains on a strategy construction path.
* `_lookup_existing_position` resolves a strategy existing position for an ADD/CLOSE/ADJUST `ValidationRequest` whose instrument has `direction=None`.
* No guardrail-evaluation consumer fabricates a long/short for a strategy proposal; `delta_adjusted.py` reads `proposal.direction` only where it is non-`None`.
* The `evaluate.py` entry point rejects a proposal whose `direction is None` does not agree with `asset_type is STRATEGY`.
* The CLAUDE.md lint chain — `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` — passes clean.
* New / extended tests for each producer and the type flip pass under `uv run pytest --testmon -n auto`.

## Verification

Intermediate runs use `uv run pytest --testmon -n auto`; the pre-PR final verification runs the full suite (`uv run pytest -n auto`). Spot-check by constructing a strategy `ProposedDelta` / `ExistingPosition` / `ValidationInstrument` with `direction=None` and confirming `evaluate_proposals` / `validate_guardrail` project it without raising. Tests to extend: `tests/risk_guardrails/guardrail_evaluation/test_types.py` and `test_delta_adjusted.py`; `tests/risk_guardrails/state_delivery/test_validation_tool.py`; `tests/risk_guardrails/test_library_snapshot.py`; `tests/decision/proposal_pre_processor/test_translator.py`; `tests/decision/portfolio_manager/test_submit_envelope_package_surface.py`. The `scripts/verify_debug_e2e.py` pipeline gate covers the integrated path once [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) wave 2 seeds a strategy end-to-end.

## Related

[ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) — the `PositionRecord` counterpart of this category error; this is the agreed follow-on ([ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) pre-resolved decision E). [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) — made the guardrail consumers leg-derived; the hard gate.
