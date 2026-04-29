---
status: done
completed_date: 2026-04-29
commit_id: d87a3a0
---

# 06 — End-to-end golden tests

## Goal

A small fixture-based test suite exercising `evaluate_proposals` end-to-end against scenarios drawn from the design docs' worked examples. These tests pin the library's behavior across realistic compositions (state + proposals + config + market inputs) so unrelated future edits to any individual primitive cannot silently regress the integration.

The tests use the **shipped configuration tree** to produce realistic `ResolvedConfig` → `LibraryConfig` adapters; only the portfolio-state snapshot, proposal set, and market inputs are constructed inline. This catches drift between the library and the upstream configuration cascade.

## Reading

- `docs/design/06-risk-guardrails/scenario-tests.md` — narrative scenarios used as source material for the golden tests; this story's fixtures are inspired by the same shapes (realistic regime/profile combinations, breach scenarios, regime-transition mechanics)
- `docs/design/06-risk-guardrails/guardrail-evaluation.md` — the full library contract these tests exercise
- `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — the validation tool's expected output shape; the golden tests build it from the library output to verify the library-to-tool composition holds
- `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.A — the pre-processor's expected output; one golden test builds the `breaches[]` and `contributors` extraction by hand to confirm the library output covers those needs
- `config/profiles/medium.yaml`, `config/regimes/normal.yaml`, `config/regimes/elevated.yaml`, `config/regimes/crisis.yaml` — used as the source of truth for cascade values
- `src/alphamind/config/loaders.py`, `resolver.py` — the load and compose path the golden tests reuse
- All earlier stories in this work tree

## Depends on

- 01, 02a, 02b, 02c, 03, 04, 05

## Scope

In scope:

- `tests/risk_guardrails/guardrail_evaluation/test_e2e_golden.py` containing the fixture infrastructure plus the scenario tests below.
- **Fixture infrastructure:**
  - `_load_library_config(profile, regime, mode="normal", overlays=())` — helper that loads the shipped config tree, calls the resolver, and adapts to `LibraryConfig` via `from_resolved_config`. Returns the adapted config.
  - `_make_iv_provider(quotes_by_underlying, realized_vol_by_underlying)` — helper that constructs a `FixtureIvProvider` from a flat list of `(underlying, strike, expiration, contract_type, iv)` tuples plus a `{underlying: realized_vol}` dict. Reduces test boilerplate.
  - `_make_state(...)` — helper that constructs a `PortfolioStateSnapshot` with sane defaults; tests pass overrides for the fields that vary.
  - All helpers are private to the test module; no production-code import of test fixtures.
- **Golden tests** (each is one `pytest` function, named after the scenario):

  1. **`test_micro_long_only_book_passes_full_validation`.** Micro profile (`options_enabled=False`, `short_selling_enabled=False`), normal regime. State: $1,500 portfolio, eight equity longs in tech/semis ranging $50–$200 each, no shorts, no options, gross 60%, sector tech 35% / semis 25%. Proposals: two new tech longs of $75 each. Assert:
     - `per_rule` includes only the rules in scope at micro (no options, no short rules).
     - `sector_concentration_tech.projected_after = 35 + (150 / 1500 × 100) = 45` — `FAIL` (over 25% limit). Test pins the FAIL.
     - `feature_disabled` is empty.
     - `delta_adjusted` has two equity entries with `net_greeks=None`.
     - The `RuleProjection.unit` strings match the rule registry's documented units.

  2. **`test_medium_options_proposal_pass`.** Medium profile, normal regime. State: $50,000 portfolio, ten positions across all four sectors, gross 90%, no options. Proposal: one ATM call on NVDA, 30 days out, 5 contracts, `notional_usd=$1,000` premium. Surface populated for NVDA. Assert:
     - `delta_adjusted["REC-1"].iv_source = SURFACE`.
     - `delta_adjusted["REC-1"].net_greeks` matches the BS computation with conservative buffer applied (regime multiplier 1.0).
     - `per_rule.options_delta_pct.projected_after` reflects the option's contribution to delta-adjusted exposure.
     - `per_rule.portfolio_theta_pct_per_day.projected_after` is more negative than `current` (long call has negative theta).
     - All rules `PASS`.

  3. **`test_medium_options_proposal_fail_on_vega_under_elevated`.** Medium profile, elevated regime (vega limit cut to 0.7%). State: $50,000 portfolio, six existing positions including two long-vol options (current `portfolio_vega_pct_per_iv_point = 0.6`). Proposal: long ATM straddle on AAPL, 14 days out, 4 contracts each leg. Assert:
     - `delta_adjusted["REC-1"].net_greeks.vega > 0` (long vol).
     - `per_rule.portfolio_vega_pct_per_iv_point.status = FAIL` — projected exceeds 0.7% × 95% hard-block threshold.
     - `delta_adjusted["REC-1"].iv_source = SURFACE`.
     - The conservative buffer is the elevated-regime multiplier (1.5×).

  4. **`test_crisis_regime_immediate_tightening_creates_breach`.** Medium profile, crisis regime (gross cut to 60%). State: gross 85%. Proposal: any small new position. Assert:
     - `per_rule.gross_exposure_pct.current = 85`.
     - `per_rule.gross_exposure_pct.limit = 60` (after the crisis × 0.5 cascade).
     - `per_rule.gross_exposure_pct.status = FAIL` — already over the limit before the proposal.
     - The proposal's contribution makes it more over the limit.
     - This test pins the cascade transparency: the library shows the breaching state, the engine layer responds to it.

  5. **`test_short_proposal_with_borrow_cost`.** Medium profile, normal regime. State: $50,000 portfolio, one existing short of $1,500 with $0.50/day borrow cost (current `daily_borrow_cost_pct = 0.001`). Proposal: short $2,000 in TSLA equity with `daily_borrow_cost_usd=$1.00`. Assert:
     - `delta_adjusted["REC-1"].signed_notional_usd = -2000`.
     - `per_rule.borrow_cost_budget_pct_per_day.current ≈ 0.001`, `projected_after ≈ 0.001 + (1.00/50000 × 100) = 0.003`.
     - `per_rule.total_short_pct.projected_after = 3 + 4 = 7`.
     - `per_rule.net_short_pct.projected_after` reflects the new short.

  6. **`test_strategy_long_call_spread_aggregates_correctly`.** Medium profile, normal regime. State: $50K, no options. Proposal: long call spread on NVDA — long 950-strike call, short 1000-strike call, 30 days out, 3 contracts. NVDA spot=$925, IV from surface 0.30. Assert:
     - `delta_adjusted["REC-1"].net_greeks.delta ∈ (0, 1)` — net long but smaller than the lower-strike leg's delta.
     - `delta_adjusted["REC-1"].net_greeks.theta < 0` (long net) but smaller magnitude than a single long call's theta.
     - `per_rule.options_delta_pct.projected_after` reflects the net delta-adjusted contribution.

  7. **`test_close_releases_capital_and_reduces_gross`.** Medium profile, normal regime. State: $50K, one $5K long position in tech, gross 80%, sector tech 12%. Proposal: CLOSE the position. Assert:
     - `delta_adjusted["REC-1"].signed_notional_usd = -5000`.
     - `per_rule.gross_exposure_pct.projected_after = 80 - 10 = 70`.
     - `per_rule.sector_concentration_tech.projected_after = 12 - 10 = 2`.
     - `per_rule.min_cash_reserve_pct.projected_after > current` (capital released).
     - All rules `PASS`.

  8. **`test_validation_tool_wrapper_composition`.** Synthetic test that builds the library output and demonstrates how the validation tool would compose it: take `per_rule`, derive `overall = "FAIL" if any status == FAIL else "PASS"`, generate `failure_guidance` from the worst-FAIL rule's overage, and produce a `cumulative_impact_note` reflecting the proposal index. Test asserts the wrapper composition is straightforward — i.e., the library output covers everything the validation tool needs without requiring the library to know about cumulative state. Pins the library-to-tool seam.

  9. **`test_pre_processor_breaches_extraction`.** Synthetic test that builds the library output for a 3-proposal batch and demonstrates how the pre-processor would extract `breaches[]` and signed `contributors`: filter `per_rule` to entries where `status != PASS`, then for each compute per-proposal contribution by re-running `RuleSpec.contribute(...)` against each proposal individually. Asserts the contributors sum to the breaching rule's combined contribution. Pins the library-to-pre-processor seam.

  10. **`test_engine_t3_synchronous_rejection_payload`.** Synthetic test that builds the library output for a single failing proposal and demonstrates how the engine would build the synchronous rejection payload to return to the PM: name the breached rule(s), name the current/limit/projected/headroom values, name the suggested modification (`reduce size by X% to pass`). Test pins the contract via the library output's structure.

  11. **`test_determinism_against_shipped_config`.** Load the medium × normal config twice via the resolver; build identical `LibraryConfig`s; run `evaluate_proposals` twice on identical inputs; assert `output_a == output_b` and `hash(output_a) == hash(output_b)`. Catches any non-determinism that creeps in (e.g., dict iteration order, non-frozen aggregation).

- **Test infrastructure constraints:**
  - Tests do not mutate `config/*.yaml`. The shipped config is read-only fixture material.
  - Tests do not import private (`_`-prefixed) members from the library; the public API is sufficient.
  - Tests run against the **shipped** config — not synthetic ResolvedConfigs — for compositions 1–7, 11. Compositions 8–10 use synthetic outputs because the test asserts the *seam* shape, not behavior under config.

Out of scope:

- The validation tool's own unit tests — those land with the `state-delivery.md` implementation.
- The pre-processor's own unit tests — those land with `proposal-pre-processor.md` implementation.
- Engine T3 integration tests — those land with `architecture.md` § 3 implementation.
- Performance benchmarks. The library is small (O(rules × proposals) projection); a single-evaluation perf test would be premature.
- Property-based / fuzz tests. The fixture-based golden tests are the v1 surface; property tests are a future addition gated on a specific failure mode the goldens missed.

## Notes

**Why "shipped config, synthetic inputs."** Loading the actual `config/` tree exercises the resolver/adapter chain; constructing portfolio state and proposals inline keeps tests focused and readable. This mirrors the configuration-management story 08 pattern (`tests/config/test_end_to_end_loader.py`) — load shipped YAML, assert resolver behavior; use synthetic Pydantic instances for failure-injection tests.

**Why scenarios 8–10 use synthetic outputs.** The library has no caller-side composition logic; the goldens 8–10 demonstrate that the library's output is *sufficient* for the documented callers. They construct synthetic `LibraryOutput` instances and walk through the composition steps, asserting the seam. This is the only place the library and its callers interact in test code; the actual caller implementations land in their own work trees.

**No regression-pinning of specific numerical values beyond breach status.** The exact `projected_after` for a regime-tightened sector cap depends on the multiplier values committed in `config/regimes/*.yaml`. Tests assert relationships (`projected_after > current`, `status = FAIL`), not absolute numbers, where possible. Where tests need to pin a specific value (e.g., `medium × normal × tech sector limit = 25%`), they read the value from the resolver's output and assert against it — keeping the test resilient to legitimate config edits.

**Why this story is one test file, not many.** Twelve scenarios cohere as the library's golden surface. Splitting per file fragments the discoverability ("where do we test the strategy aggregation?"). One file with named functions is the right granularity.

## Acceptance criteria

- [ ] `tests/risk_guardrails/guardrail_evaluation/test_e2e_golden.py` exists.
- [ ] The eleven named tests above are present and pass:
  - `test_micro_long_only_book_passes_full_validation`
  - `test_medium_options_proposal_pass`
  - `test_medium_options_proposal_fail_on_vega_under_elevated`
  - `test_crisis_regime_immediate_tightening_creates_breach`
  - `test_short_proposal_with_borrow_cost`
  - `test_strategy_long_call_spread_aggregates_correctly`
  - `test_close_releases_capital_and_reduces_gross`
  - `test_validation_tool_wrapper_composition`
  - `test_pre_processor_breaches_extraction`
  - `test_engine_t3_synchronous_rejection_payload`
  - `test_determinism_against_shipped_config`
- [ ] Tests load the shipped `config/` tree via `alphamind.config.loaders` + `compose_config` for scenarios 1–7 and 11.
- [ ] Tests do not mutate `config/*.yaml`.
- [ ] Tests do not import library private (`_`-prefixed) members.
- [ ] All assertions reference rule IDs and field names verbatim from earlier stories' specifications.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
