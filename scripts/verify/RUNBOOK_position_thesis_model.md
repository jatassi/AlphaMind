# Position-Thesis Model Type-Layer Verification Runbook

Operator workflow for the ALP-336 verification artifact that proves the
position-thesis-model work tree (ALP-122) is internally consistent and
contract-correct. Run after any change to `src/alphamind/portfolio_state/`,
`src/alphamind/execution/thesis_model/`, or
`src/alphamind/execution/position_model/`.

## Purpose

`scripts/verify/verify_position_thesis_model.py` is a **Phase 0 — Type-layer
self-check** that must pass before any live-data or SDK-using verification
phase. It exercises:

- The three pure-function utilities from ALP-333/334/335 (bracket-thesis
  coverage, thesis-resolution classifier, strategy-payoff functions).
- The seven additive-field changes from ALP-337–343 (float
  `time_expectation_hours`, negative `position_weight_pct`, OptionGreeks
  freshness metadata, `PositionFill.live_execution_estimate`, bracket
  `entry_window_deadline`, `OrderClass`/MLEG validator, thesis
  `position_size_rationale`).
- The enum relocation fix from ALP-344 (`RegimeLabel` moved to
  `risk_guardrails/`; identity preserved via capital.py re-export).
- The typed discriminated-union payloads from ALP-345/346 (`BracketLeg`
  trigger and `PLAnchorSpec` cross-validators).
- The structural reorganisation from ALP-347/348 (`events/`, `aggregates/`
  subpackages; Pydantic discriminated-union at parse time).
- The architectural splits from ALP-349/350/351 (`PositionRecord` vs
  `PositionView`; `BasePositionProtocol`; `ThesisHealthSnapshot` lifecycle).

No SDK calls; no DB reads. All 39 cases should complete in under 10 seconds.

## Prerequisites

1. **`uv sync` completed** — `uv run` is the entry point.
2. The work tree's integration branch (`jackson/alp-122-position-thesis-model`)
   must be reachable from HEAD (all 15 predecessor stories landed).
3. No live market data or broker connection needed.

## Run

```bash
# Quick smoke check — prints verdict block only.
uv run python scripts/verify/verify_position_thesis_model.py

# With per-scenario detail (useful when triaging a specific FAIL).
uv run python scripts/verify/verify_position_thesis_model.py --verbose
```

## Expected output

A clean run prints (exit code 0):

```
=== verify_position_thesis_model verdict ===
Wave 1 utilities (01a/01b/01c):             PASS (12/12 cases)
Wave 1 additive fields (01d-01j):           PASS (14/14 cases)
Wave 2 boundary fix (02):                   PASS (2/2 cases)
Wave 3 typed payloads (03a/03b):            PASS (4/4 cases)
Wave 4 structural (04a/04b):                PASS (3/3 cases)
Wave 5 architectural (05a/05b/05c):         PASS (4/4 cases)
-----------------------------------------------
Overall: PASS (39/39 cases)
Wall-clock: 0.Xs
```

On any sub-scenario FAIL, the overall status is `FAIL`, exit code is 1, and
a `=== Failure details ===` section prints the failing scenario label plus the
full traceback. The failure detail is printed regardless of `--verbose`.

## Failure-mode triage

| Wave | Responsible stories | Triage |
|---|---|---|
| Wave 1 utilities (01a/01b/01c) | ALP-333, ALP-334, ALP-335 | Check `src/alphamind/execution/thesis_model/coverage.py`, `resolution.py`, and `src/alphamind/execution/position_model/strategy_payoff.py`. Confirm the function signatures and return types match the story acceptance criteria. |
| Wave 1 additive fields (01d-01j) | ALP-337, ALP-338, ALP-339, ALP-340, ALP-341, ALP-342, ALP-343 | Check `src/alphamind/portfolio_state/records/theses.py`, `positions.py`, `orders.py` for the relevant field validator. The FAIL detail names the scenario (e.g., `01d-time_expectation_float_parses`); find the matching story by the `01x` prefix. |
| Wave 2 boundary fix (02) | ALP-344 | Import `RegimeLabel` from `alphamind.risk_guardrails.regime_adaptation.types` directly. If that fails, ALP-344 did not land. If identity check fails, `capital.py` is re-exporting a different object — check the re-export chain. |
| Wave 3 typed payloads (03a/03b) | ALP-345, ALP-346 | Check `BracketLeg`'s `_validate_trigger_matches_leg_type` and `_validate_pl_anchor_compatibility` model validators in `orders.py`. |
| Wave 4 structural (04a/04b) | ALP-347, ALP-348 | If events/aggregates imports fail, ALP-347 did not land the subpackage `__init__.py`. If discriminated-union test fails, ALP-348 did not apply the `PositionDetailsPayload` discriminator. |
| Wave 5 architectural (05a/05b/05c) | ALP-349, ALP-350, ALP-351 | If `current_market_value_usd` is still on `PositionRecord`, ALP-349 did not land. If isinstance check fails, ALP-350's `BasePositionProtocol` is not `runtime_checkable` or `PositionRecord` doesn't expose the required properties. If `ThesisComponent` still has `supporting_signals`, ALP-351 did not move it to `ComponentHealthEntry`. |

## References

- `scripts/verify/RUNBOOK_end_to_end_verification.md` — central e2e runbook; this
  script runs as Phase 0 before the data-layer checks.
- ALP-122 parent issue — position-thesis-model feature overview and
  pre-resolved decisions (including the offline / no-SDK constraint).
- ALP-333 through ALP-351 — 15 sub-stories whose deliverables this script
  exercises.
- `src/alphamind/execution/thesis_model/` — ALP-333/334 coverage + resolution
  utilities.
- `src/alphamind/execution/position_model/` — ALP-335 strategy-payoff
  utilities.
- `src/alphamind/portfolio_state/records/` — Tier 1 records (positions,
  theses, orders, cash).
- `src/alphamind/portfolio_state/events/` — Tier 2 event records (ALP-347).
- `src/alphamind/portfolio_state/aggregates/` — Tier 3 derived records
  (ALP-347).
- `src/alphamind/portfolio_state/views/` — delivery-time projections
  (ALP-349/351).
- `src/alphamind/portfolio_state/protocols/` — structural Protocol definitions
  (ALP-350).
- `src/alphamind/risk_guardrails/regime_adaptation/types.py` — canonical
  `RegimeLabel` home post-ALP-344.
- `tests/scripts/test_verify_position_thesis_model.py` — unit tests for this
  script's wave functions; run with `uv run pytest
  tests/scripts/test_verify_position_thesis_model.py -n auto`.
