# 06 — Comprehensive E2E verification + runbook

## Goal

Ship `scripts/verify_position_thesis_model.py` (offline, no SDK, no DB), `scripts/RUNBOOK_position_thesis_model.md` (per-feature operator workflow), and a phase-0 insertion into `scripts/RUNBOOK_end_to_end_verification.md`. The verification script exercises every deliverable from the work tree's 15 sub-stories — the three pure-function utilities (01a/01b/01c), the seven additive-field changes (01d–01j), the boundary fix (02), the typed payloads (03a/03b), the structural reorg (04a/04b), and the architectural splits (05a/05b/05c). Wall-clock target: under 10 seconds. Failures here invalidate every downstream layer, so the runbook places this feature first as a "type-layer self-check."

## Reading

* All 15 predecessor stories' acceptance criteria (<issue id="21e03751-3502-4f74-986f-eb509bca83b8">ALP-333</issue> through <issue id="c0a200d1-0dce-41fe-a8da-ea175c3154cc">ALP-351</issue>) — define the behaviors the script must exercise.
* `scripts/RUNBOOK_end_to_end_verification.md` — existing central runbook the script must slot into; insert "Phase 0 — Type-layer self-check" before Phase 1 (Data).
* `scripts/RUNBOOK_pm.md` and `scripts/RUNBOOK_strategist.md` — per-feature runbook examples to mirror (Purpose / Prerequisites / Run / Expected output / Failure-mode triage table / References).
* `scripts/verify_pm.py` and `scripts/verify_strategist.py` — verify-script examples for argparse / verdict-block conventions.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (F) — confirms the offline / no-SDK constraint and the phase-0 placement.

## Depends on

All 15 predecessor stories in the work tree:

* <issue id="21e03751-3502-4f74-986f-eb509bca83b8">ALP-333</issue> (01a — bracket-thesis coverage validator)
* <issue id="59e2a7d6-3a65-4e31-811b-a62add12d66d">ALP-334</issue> (01b — thesis-level resolution classifier)
* <issue id="38042ec3-2b83-4085-b4ff-9c59f4205f55">ALP-335</issue> (01c — strategy payoff utilities)
* <issue id="8cb4e733-ffce-41e6-afe5-e824568d870d">ALP-337</issue> (01d — `time_expectation_hours` retype to float)
* <issue id="05bd2a75-2eeb-4851-8990-4a930c054d15">ALP-338</issue> (01e — drop position_weight_pct upper bound)
* <issue id="22f531bb-4a30-4f16-8f7b-b20a6aa3883e">ALP-339</issue> (01f — OptionGreeks freshness metadata)
* <issue id="1ce95a3b-f321-4a9e-9729-8cc1b4aa0dc4">ALP-340</issue> (01g — PositionFill live_execution_estimate)
* <issue id="91febf86-602b-4455-b999-9b447a1d64d5">ALP-341</issue> (01h — BracketRecord entry_window_deadline)
* <issue id="49a1bd4c-d4ce-4fae-a1b6-4113ed9ee37f">ALP-342</issue> (01i — OrderRecord order_class enum)
* <issue id="d6dafa14-bb41-4367-9983-c9ce9a1809ff">ALP-343</issue> (01j — ThesisRecord position_size_rationale)
* <issue id="2d1cd9b2-12f0-49fc-ae3d-6c64a6733638">ALP-344</issue> (02 — move risk-guardrail enums out of [capital.py](http://capital.py))
* <issue id="7c6e5055-36fd-43b2-b7fe-575de26f99dc">ALP-345</issue> (03a — typed BracketLeg.trigger discriminated union)
* <issue id="34f2b785-a5c1-4ccb-8efc-e88b7171be1f">ALP-346</issue> (03b — typed pl_anchor payload)
* <issue id="273434f7-591e-4ac2-bb85-eb601511ed4f">ALP-347</issue> (04a — restructure records/ into records/events/aggregates)
* <issue id="3aac15c1-a657-4ba5-afe4-a609ea32e4f7">ALP-348</issue> (04b — Pydantic-native discriminated unions)
* <issue id="19198927-66c7-4eaf-843e-c856ba231a9e">ALP-349</issue> (05a — split PositionRecord into persistent + view)
* <issue id="d16c4b4e-e561-4e37-8a6d-c614a90f9cf9">ALP-350</issue> (05b — base-position Protocol)
* <issue id="c0a200d1-0dce-41fe-a8da-ea175c3154cc">ALP-351</issue> (05c — supporting_signals lifecycle refactor)

## Scope

Source files: `scripts/verify_position_thesis_model.py` (new), `scripts/RUNBOOK_position_thesis_model.md` (new), `scripts/RUNBOOK_end_to_end_verification.md` (one-section insertion).

### 1\. Verification script scenarios

`scripts/verify_position_thesis_model.py` exposes a thin argparse CLI (`--help`, no required args; supports `--verbose` for detailed per-scenario output). On run, the script exercises (organized by predecessor story):

1. **Wave-1 utilities** (covers 01a, 01b, 01c):
   * Construct a 3-leg bracket (TAKE_PROFIT + PRICE_STOP + TIME_EXPIRATION) with a thesis whose components correctly cover each leg via `linked_bracket_leg_id`. Call `validate_bracket_thesis_coverage(bracket, thesis)`; expect no raise. Then construct a deliberately-broken variant (one leg uncovered); expect `ValueError`.
   * Call `classify_thesis_resolution` on five canonical inputs covering each of the four resolution categories plus the empty-tuple ValueError.
   * Construct each of the five canonical strategy structures from <issue id="38042ec3-2b83-4085-b4ff-9c59f4205f55">ALP-335</issue> (long bull call vertical, long bear put vertical, iron condor, long straddle, long strangle) and call all three payoff functions; assert closed-form expected values.
2. **Wave-1 additive fields** (covers 01d, 01e, 01f, 01g, 01h, 01i, 01j):
   * Construct a `ThesisRecord` with `time_expectation_hours=48.0` (float) and `expected_resolution_at = generation_timestamp + timedelta(hours=48)`; assert it parses; assert string `"48"` rejected.
   * Construct a short-position `PositionView` (or PositionRecord pre-05a) with `position_weight_pct = -3.5`; assert it parses (post-01e).
   * Construct an `OptionGreeks` with `as_of_timestamp` populated (tz-aware UTC) and `iv_used = 0.45`; assert success; assert `iv_used = 0.0` rejected.
   * Construct a `PositionFill` with `live_execution_estimate=LiveExecutionEstimate(...)`; assert success; assert negative-fees rejected.
   * Construct a `BracketRecord` with `entry_window_deadline` set; assert success; assert naive datetime rejected.
   * Construct an `OrderRecord` with `order_class=OrderClass.MLEG, instrument_spec=<STRATEGY>`; assert success; assert MLEG-with-EQUITY rejected.
   * Construct a `ThesisRecord` with `position_size_rationale="..."`; assert success; assert empty string rejected.
3. **Wave-2 boundary fix** (covers 02):
   * `from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel` succeeds.
   * Identity check: `from alphamind.portfolio_state.records.capital import RegimeLabel as A; from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel as B; assert A is B`.
4. **Wave-3 typed payloads** (covers 03a, 03b):
   * Construct `BracketLeg(leg_type=PRICE_STOP, trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=800.0, direction="LTE"))`; assert success.
   * Construct `BracketLeg(leg_type=PRICE_STOP, trigger=TimeTrigger(...))`; assert ValueError.
   * Construct `BracketLeg(leg_type=TAKE_PROFIT, pl_anchor=PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50))`; assert success.
   * Construct `BracketLeg(leg_type=TIME_EXPIRATION, pl_anchor=PLAnchorSpec(...))`; assert ValueError.
5. **Wave-4 structural** (covers 04a, 04b):
   * `from alphamind.portfolio_state.events import ActivityLogEntry` succeeds (post-04a).
   * `from alphamind.portfolio_state.aggregates import RiskBudgetEntry` succeeds.
   * `from alphamind.portfolio_state.records.activity_log import ActivityLogEntry` continues to succeed (backward-compat shim).
   * Construct `PositionRecord(details={"instrument_type": "BOGUS"})`; assert ValidationError fires at parse time (post-04b discriminated union).
6. **Wave-5 architectural** (covers 05a, 05b, 05c):
   * `PositionRecord` no longer carries `current_market_value_usd` (asserted via `'current_market_value_usd' not in PositionRecord.model_fields`).
   * `PositionView` carries the computed fields.
   * `isinstance(position_record, BasePositionProtocol)` returns True (post-05b).
   * `ThesisComponent` no longer carries `supporting_signals`; `ThesisHealthSnapshot.health_for_component(...)` works.

### 2\. Final verdict block

```
=== verify_position_thesis_model verdict ===
Wave 1 utilities (01a/01b/01c):     PASS (12/12 cases)
Wave 1 additive fields (01d-01j):   PASS (14/14 cases)
Wave 2 boundary fix (02):           PASS (2/2 cases)
Wave 3 typed payloads (03a/03b):    PASS (4/4 cases)
Wave 4 structural (04a/04b):        PASS (4/4 cases)
Wave 5 architectural (05a/05b/05c): PASS (4/4 cases)
-----------------------------------------------
Overall: PASS (40/40 cases)
```

On any sub-scenario FAIL, overall is FAIL and exit code 1; PASS exits 0. Per-scenario failure detail prints under `--verbose` or in any FAIL case regardless of `--verbose`.

### 3\. Per-feature runbook

`scripts/RUNBOOK_position_thesis_model.md` — operator workflow following the structure of `scripts/RUNBOOK_pm.md`. Sections: Purpose / Prerequisites / Run / Expected output / Failure-mode triage table (one row per wave with the responsible story IDs) / References.

### 4\. End-to-end runbook insertion

In `scripts/RUNBOOK_end_to_end_verification.md`, insert "Phase 0 — Type-layer self-check" before Phase 1, update the §Phase map table to add `| 0 | Types | position_thesis_model | No | <10s |`, and add `verify_position_thesis_model | (none) | 0 | 0 |` to the §Cost summary table.

### Out of scope

* Running the new verifier in CI — the script is operator-runnable and slot-1 in the e2e runbook; CI integration is a separate concern.
* Constructing fixtures beyond the canonical structures named in the predecessor stories — the verifier is a smoke test, not a sweep.
* Live-SDK or live-broker scenarios.

## Acceptance criteria

- [ ] `scripts/verify_position_thesis_model.py` exists and is invocable as `uv run python scripts/verify_position_thesis_model.py`.
- [ ] On a clean main branch with all 15 predecessor stories landed, the script exits 0 and prints "Overall: PASS (40/40 cases)".
- [ ] Wall-clock for the full run is under 10 seconds on a development laptop.
- [ ] `--verbose` flag prints per-scenario detail; `--help` prints usage.
- [ ] Any sub-scenario FAIL exits with code 1 and prints triage-friendly error detail.
- [ ] Each wave's sub-scenarios are clearly labeled in the verdict block by responsible story IDs.
- [ ] `scripts/RUNBOOK_position_thesis_model.md` exists with the standard sections.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` has a new "Phase 0 — Type-layer self-check" section before Phase 1 with the Phase map and Cost summary tables updated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run python scripts/verify_position_thesis_model.py` — confirm "Overall: PASS" verdict, exit 0, sub-10-second wall-clock.
* Run `uv run python scripts/verify_position_thesis_model.py --verbose` — confirm per-scenario detail prints.
* Read the per-feature runbook and execute the operator workflow; confirm every step works.
* Read the updated end-to-end runbook Phase 0 section and confirm tables render correctly.
* Spot-check by intentionally regressing one predecessor's deliverable and re-running; confirm the verifier FAILs with a clear triage-friendly message naming the responsible story.
* Lint clean per CLAUDE.md.