---
status: done
completed_date: 2026-04-29
commit_id: 777ef73
---

# 05 — `evaluate_proposals` entry point

## Goal

Wire the library's primitives into a single public function callers invoke. `evaluate_proposals(...)` accepts a `PortfolioStateSnapshot`, a sequence of `ProposedDelta`s, a `LibraryConfig`, and `MarketInputs`, and returns a `LibraryOutput` containing per-rule projections, per-proposal delta-adjusted-exposure records, and feature-disabled rejection records. Three callers compose this output with their own framing (validation tool, proposal pre-processor, engine T3); the library itself terminates here.

This story owns:
- Cross-field invariant checks on inputs (`option_legs is None ⇔ asset_type == EQUITY`, etc.)
- The orchestration order: feature gate → delta-adjusted exposure per proposal → batch projection
- Stable, deterministic output ordering
- The hash-stability discipline that makes `LibraryOutput` deterministic against equal inputs

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` — the full library spec; this story is its public face
- `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — the most demanding caller; the library output's `per_rule[]` and `delta_adjusted_exposure` shape feed directly into the validation tool's wrapper
- `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.A `combined_set_impact` — the second caller; pre-processor adds `breaches[]` and `contributors` on top of `per_rule[]`
- `docs/design/05-execution-layer/architecture.md` § 3 Guardrail enforcement layer — the third caller; engine integrates into the OMS write transaction
- All earlier stories in this work tree: 01 (types), 02a (BS), 02b (IV), 02c (effective limits + feature gate), 03 (delta-adjusted exposure), 04 (projection engine + rule registry)

## Depends on

- 01, 02a, 02b, 02c, 03, 04

## Scope

In scope:

- `src/alphamind/risk_guardrails/guardrail_evaluation/evaluate.py` defining `evaluate_proposals(...)` as a pure function:
  ```python
  def evaluate_proposals(
      *,
      state: PortfolioStateSnapshot,
      proposals: Sequence[ProposedDelta],
      config: LibraryConfig,
      market: MarketInputs,
  ) -> LibraryOutput:
  ```
  Behavior:
  1. **Input validation** (raises `LibraryInputError` on violation; never silently ignores):
     - Each proposal's cross-field invariants:
       - `asset_type == EQUITY` ⇔ `option_legs is None`. Mismatch raises naming the proposal ID.
       - `asset_type ∈ {OPTION, STRATEGY}` ⇒ `len(option_legs) ≥ 1`. STRATEGY requires ≥ 2.
       - `action ∈ {ADD, CLOSE, ADJUST}` ⇒ `existing_position_id is not None` and exists in `state.existing_positions`. Missing position ID raises naming proposal ID and missing position ID.
       - `action ∈ {OPEN, CANCEL}` ⇒ `existing_position_id is None`.
       - `direction == SHORT and asset_type == EQUITY and action ∈ {OPEN, ADD}` ⇒ `daily_borrow_cost_usd is not None and >= 0`. Missing borrow cost raises.
       - `notional_usd >= 0` (sign comes from direction; per story 01 convention).
       - `quantity > 0`.
     - `proposal.id` uniqueness across `proposals`. Duplicates raise.
     - `proposal.sector ∈ config.active_sectors` for `OPEN`. Other actions inherit the sector from the existing position (if `existing_position_id` is provided, `proposal.sector` should match `state.existing_positions[id].sector`; mismatch raises).
     - `state.portfolio_value_usd > 0`. (A zero-value portfolio breaks all percentage math.)
  2. **Feature gate.** Iterate proposals; for each, call `classify_feature_gate(proposal, config)`:
     - `None` → keep for projection.
     - `FeatureDisabledRejection` → add to `feature_disabled` list; do not project.
  3. **Delta-adjusted exposure.** For every proposal that passed the feature gate, call `compute_delta_adjusted_exposure(proposal, market, config)`. Collect into a list of `(proposal, dae)` tuples preserving input order.
  4. **Per-proposal `delta_adjusted` map.** Keyed on `proposal.id`; one entry per proposal that passed the feature gate. Even equity proposals get an entry (with `net_greeks=None`, `iv_used=None`, `iv_source=None`, `unbuffered_delta=None`). Feature-disabled proposals are absent from `delta_adjusted` (their rejection is in `feature_disabled`).
  5. **Batch projection.** Call `project_all(proposals_with_dae=projected_pairs, state=state, config=config)`. Receive `tuple[RuleProjection, ...]` per the registry's stable order.
  6. **Output assembly.** Construct `LibraryOutput(per_rule=projections, delta_adjusted=MappingProxyType(dae_map), feature_disabled=tuple(rejections))`.
  7. **Determinism.** Equal inputs produce equal outputs. The function is pure — no I/O, no logging side effects, no `datetime.now()`/`uuid` calls. Tests assert hash stability.

- `LibraryInputError(Exception)` — raised on input validation. Single error wrapping the aggregated list of input violations (one error per call, not one per violation, mirroring the configuration `cross_reference.py` and `semantic.py` aggregation pattern).
- Re-export `evaluate_proposals` and `LibraryInputError` from `guardrail_evaluation/__init__.py`.

- **Tests:**
  - **Single equity OPEN.** Proposal: `EQUITY/LONG/$5,000` in tech, OPEN. Synthetic state with `portfolio_value=$100K`, sector_exposure_pct[tech]=18.3, gross_pct=78. Library config: medium × normal, `effective_limits` from a synthetic ResolvedConfig. Assert:
    - `per_rule` includes `position_max_size_pct`, `sector_concentration_tech`, `net_long_pct`, `gross_exposure_pct`, `min_cash_reserve_pct`, etc.
    - `sector_concentration_tech.projected_after = 23.3`, `status=PASS` (under 25% × 70% warning threshold).
    - `delta_adjusted` has one entry keyed `"REC-1"` with `signed_notional_usd=+5000`, `net_greeks=None`.
    - `feature_disabled` is empty.
  - **Multi-proposal batch with sector breach.** Three tech longs, each $3K, totaling $9K addition into sector at 23%. Combined push to 32% — over the 25% limit. Assert `sector_concentration_tech.status=FAIL` with the right `projected_after` and `headroom_remaining < 0`.
  - **Feature-disabled short on micro profile.** Library config with `short_selling_enabled=False`. Proposal: `EQUITY/SHORT/$200`. Assert `feature_disabled` contains one rejection with `reason="shorts_disabled"`. `delta_adjusted` does not contain the rejected proposal. `per_rule` is computed against the empty post-gate proposal list (i.e., `current` values are reported with zero contributions).
  - **Options proposal under medium profile.** Single ATM call, 30 days to expiry, 5 contracts. IV surface populated for the strike. Assert:
    - `delta_adjusted["REC-1"].net_greeks` is non-None and matches the BS computation within tolerance.
    - `delta_adjusted["REC-1"].iv_source = SURFACE`.
    - `per_rule` includes `options_delta_pct`, `portfolio_theta_pct_per_day`, `portfolio_vega_pct_per_iv_point` projections reflecting the option's contribution.
  - **Options proposal with realized-vol fallback.** Same proposal, IV surface empty for the underlying; `RealizedVolEntry` provides `0.30`. Assert `delta_adjusted["REC-1"].iv_source = REALIZED_VOL_FALLBACK` and the projection still completes.
  - **Strategy proposal.** Long call spread on NVDA, two legs. Net delta computed; `per_rule.options_delta_pct.projected_after` reflects the net.
  - **CLOSE proposal.** Existing long position; CLOSE proposal references it. Assert `per_rule.gross_exposure_pct.projected_after < state.gross_pct` (gross decreases). `per_rule.sector_concentration_tech.projected_after < state.sector_exposure_pct[tech]`.
  - **ADJUST proposal.** Existing position with `action=ADJUST`. Assert all per-rule projections show `projected_after == current` (no exposure change). `delta_adjusted` entry has `signed_notional_usd=0.0`.
  - **CANCEL proposal.** Existing pending order. Assert `per_rule.pending_order_capital_pct.projected_after < current` (capital released). Other rules unchanged.
  - **Mixed batch.** One OPEN-LONG-EQUITY, one OPEN-SHORT-EQUITY, one CLOSE on existing long, one ADD on existing short, one ADJUST. Assert each is reflected in the projections per the documented arithmetic.
  - **Determinism.** Two calls to `evaluate_proposals(...)` with equal inputs produce equal `LibraryOutput`s (`==`); the library's outputs are hashable and equal hashes match.
  - **Input validation: equity with option_legs.** `EQUITY` proposal carrying `option_legs` raises `LibraryInputError`.
  - **Input validation: option without legs.** `OPTION` proposal with `option_legs=None` raises.
  - **Input validation: STRATEGY with single leg.** `STRATEGY` with `len(option_legs)==1` raises.
  - **Input validation: CLOSE with no existing position ID.** Raises.
  - **Input validation: ADD with non-existent position ID.** Raises (the position ID is not in `state.existing_positions`).
  - **Input validation: short equity OPEN without `daily_borrow_cost_usd`.** Raises.
  - **Input validation: duplicate proposal IDs.** Two proposals both with `id="REC-1"`. Raises.
  - **Input validation: portfolio value zero.** `state.portfolio_value_usd=0`. Raises.
  - **Aggregated errors.** A batch with three input violations raises `LibraryInputError` once with a message naming all three.
  - **Per-rule output ordering.** `per_rule` order matches `build_active_specs(config)` order. Two calls return the same order.
  - **Empty proposals.** `proposals=[]` produces `per_rule` with `current==projected_after` for every rule, `delta_adjusted={}`, `feature_disabled=()`. Useful for the validation tool's "headroom only" path.
  - **All proposals feature-disabled.** Three proposals on micro profile, all options. Assert `feature_disabled` has three entries, `delta_adjusted={}`, `per_rule` shows zero contribution to every rule.

Out of scope:

- The validation tool's cumulative wrapper across multiple `evaluate_proposals` calls within one agent invocation. The wrapper lives in `state-delivery.md` implementation; this library's responsibility ends at `LibraryOutput`.
- The pre-processor's `breaches[]` / signed `contributors` extraction. Pre-processor reads `per_rule` where `status != PASS` and computes contributors from `delta_adjusted` itself.
- Engine T3 transactional integration. Engine wraps `evaluate_proposals` with the OMS write transaction, returns the synchronous rejection payload, and persists greeks. None of that is library-internal.
- Logging the IV-source notes per leg. The library reports the aggregate `iv_source` per proposal; per-leg detail is implicit in `proposal.option_legs` and the IV provider's audit trail (if any). Caller-side concern.

## Notes

**Why aggregated input errors.** Mirrors `config/validation/cross_reference.py` and `semantic.py` (per stories 06a/06b of configuration management): build a list of failures, raise once with all of them. Surfacing one failure at a time forces N round-trips to discover N independent issues; aggregating surfaces the full picture in one error.

**Determinism stack-up.** Each prior story's primitives are pure; this story does not introduce non-determinism (no clock reads, no UUID generation, no map iteration over unsorted `dict`s — `dae_map` is built in proposal order and frozen via `MappingProxyType`). Tests assert `LibraryOutput == LibraryOutput` and `hash(LibraryOutput) == hash(LibraryOutput)` for equal inputs.

**Why `evaluate_proposals` does not accept a `ResolvedConfig`.** The library's contract is the narrower `LibraryConfig`. Callers adapt via `from_resolved_config` (story 02c). This keeps the library decoupled from the configuration-management package's evolution; if `ResolvedConfig` adds fields, the library is unaffected.

**Empty-proposals path is a valid call.** The validation tool's first read of headroom (before the agent has proposed anything) calls `evaluate_proposals(proposals=[])` and reads `per_rule`'s `current` values. The function must handle empty input cleanly.

**Cross-field invariants live here, not in the dataclass.** Story 01's `ProposedDelta` is constructible with mismatched fields (e.g., EQUITY + option_legs); the type system can't express the mutual exclusion without a discriminated union which adds boilerplate. The entry point is the single validation point.

**`MappingProxyType` vs. dict for `delta_adjusted`.** The output dataclass uses `Mapping[str, DeltaAdjustedExposure]`. Wrapping the internal dict in `MappingProxyType` preserves the `frozen=True, slots=True` immutability story without forcing callers to import `MappingProxyType` to read the field.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/evaluate.py` exists and defines `evaluate_proposals(...)` and `LibraryInputError`.
- [ ] `evaluate_proposals` and `LibraryInputError` are re-exported from `guardrail_evaluation/__init__.py`.
- [ ] A unit test asserts a single equity-long OPEN against a synthetic medium × normal state produces the documented `per_rule` projections and a `delta_adjusted` entry with `signed_notional_usd=+notional`.
- [ ] A unit test asserts a multi-proposal batch with combined contributions exceeding a sector limit produces `status=FAIL` on that sector with negative `headroom_remaining`.
- [ ] A unit test asserts a SHORT equity proposal under `short_selling_enabled=False` produces a `feature_disabled` entry and is absent from `delta_adjusted`.
- [ ] A unit test asserts an options OPEN with the IV surface populated produces `iv_source=SURFACE` and projections reflecting non-zero options-greeks contributions.
- [ ] A unit test asserts an options OPEN with no surface entry but realized-vol fallback available produces `iv_source=REALIZED_VOL_FALLBACK` and a successful projection.
- [ ] A unit test asserts a strategy (multi-leg) proposal projects net greeks correctly.
- [ ] A unit test asserts a CLOSE on an existing long position decreases `gross_exposure_pct.projected_after` below `state.gross_pct`.
- [ ] A unit test asserts an ADJUST proposal produces `projected_after == current` for every rule.
- [ ] A unit test asserts a CANCEL on a pending order decreases `pending_order_capital_pct.projected_after` below `state.reserved_for_pending_orders_usd / portfolio_value × 100`.
- [ ] A unit test asserts `evaluate_proposals` is deterministic — equal inputs produce equal outputs (`==` and `hash` agree).
- [ ] A unit test asserts each input-validation invariant raises `LibraryInputError` with a message naming the offending proposal ID / field.
- [ ] A unit test asserts duplicate proposal IDs raise.
- [ ] A unit test asserts `portfolio_value_usd == 0` raises.
- [ ] A unit test asserts a batch with three independent input violations raises one `LibraryInputError` listing all three.
- [ ] A unit test asserts `per_rule` order is stable (`build_active_specs` order).
- [ ] A unit test asserts `evaluate_proposals(proposals=[])` returns `delta_adjusted={}`, `feature_disabled=()`, `per_rule` with `current==projected_after` per entry.
- [ ] A unit test asserts a batch of all-feature-disabled proposals returns those proposals only in `feature_disabled` and zero contribution to `per_rule`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
