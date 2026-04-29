---
status: done
completed_date: 2026-04-28
commit_id: 31012ad
---

# 03 — Delta-adjusted exposure with conservative buffer and strategy aggregation

## Goal

Combine the Black-Scholes greeks (02a) and IV sourcing (02b) into the per-proposal computation that produces the `DeltaAdjustedExposure` consumed by the rule-projection layer. The function takes a `ProposedDelta` plus market inputs and library config, and returns:

- The per-leg greeks for options (or `None` for equity)
- The strategy-level net greeks (sum across legs)
- The conservative buffer applied to absolute net delta
- The signed delta-adjusted notional (equity = full notional signed by direction; options/strategies = `buffered_net_delta × spot × contract_multiplier × signed direction`)

This is the only call site that combines Black-Scholes, IV lookup, and the buffer; downstream rule contributions consume the resulting `signed_notional_usd` and `net_greeks` without reaching back into the math.

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` § Delta-adjusted exposure (all four numbered points: BS greeks, IV sourcing, conservative buffer, strategy aggregation)
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Sector concentration limits, § Options-specific limits — the consumers' interpretation of "delta-adjusted notional"; this confirms the equity vs. options/strategy treatment
- `docs/design/06-risk-guardrails/regime-adaptation.md` — context for the per-regime conservative-buffer override (`Configurable per regime; tighter regimes use higher buffer`); per-regime values land here as a literal mapping
- `docs/design/05-execution-layer/oms-commands.md` § OPEN, § ADD — confirms greeks are computed at validation time and persisted as the position's initial greeks (this story's output is what the engine persists per story 05's wiring)
- `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` (post-02a)
- `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py` (post-02b)
- `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `Greeks`, `OptionLeg`, `ProposedDelta`, `MarketInputs`, `LibraryConfig`, `DeltaAdjustedExposure`, `IvSource`

## Depends on

- 01 (canonical types)
- 02a (Black-Scholes greeks)
- 02b (IV sourcing)

## Scope

In scope:

- `src/alphamind/risk_guardrails/guardrail_evaluation/delta_adjusted.py` defining:
  - `compute_delta_adjusted_exposure(proposal: ProposedDelta, market: MarketInputs, config: LibraryConfig) -> DeltaAdjustedExposure`. Pure function.
  - Behavior by `proposal.asset_type`:
    - **Equity.** `signed_notional_usd = ± proposal.notional_usd` (sign by `proposal.direction`). `net_greeks = None`. `iv_used = None`. `iv_source = None`. `unbuffered_delta = None`.
    - **Option (single leg).** Inspect `proposal.option_legs[0]`. Look up IV via `market.iv_provider.lookup_iv(...)`. Compute `Greeks` via `bs_greeks(...)` against `market.underlying_prices[proposal.underlying]`, the looked-up IV, `market.risk_free_rate`, `time_to_expiration_years = (option_legs[0].expiration - market.as_of.date()).days / 365`, and `option_legs[0].contract_type`. Apply the conservative buffer to `|delta|`. Compute `signed_notional_usd`. `unbuffered_delta = |bs_delta|` (the leg's BS delta before buffering).
    - **Strategy (multi-leg).** Compute per-leg greeks identically to single-option. Sum legs to net greeks (delta, gamma, theta, vega each summed weighted by `leg.quantity` — multiplier-aware; see below). Apply the buffer to the absolute value of the net delta. Compute `signed_notional_usd`.
- **Per-leg greek aggregation arithmetic.** For each `OptionLeg leg` in `proposal.option_legs`:
  - `leg_signed_quantity = leg.quantity` (already signed: positive = long the leg, negative = short the leg)
  - `leg_greeks = bs_greeks(spot, leg.strike, time_to_expiration_years, r, iv, leg.contract_type)`
  - Contribution to net: `leg_signed_quantity × leg_greeks.delta` (and similarly for gamma, theta, vega)
  - `net_delta = Σ leg_signed_quantity × leg_greeks.delta` over legs; same for gamma/theta/vega.
  - `proposal.direction` for a strategy is the directional view of the net position (e.g., a long call spread has `direction=LONG`; a short call spread has `direction=SHORT`). The library does not enforce consistency between `proposal.direction` and the sign of `net_delta` — that's a strategist/PM concern, not the math layer.
- **Conservative buffer application.** Per `guardrail-evaluation.md`:
  > Absolute delta is inflated by a configurable margin (default +10%) — a computed delta of 0.45 becomes 0.495.
  - `buffered_net_delta = sign(net_delta) × (|net_delta| × (1 + buffer_fraction))` where `buffer_fraction = effective_buffer_pct / 100`.
  - `effective_buffer_pct = config.conservative_buffer_pct × _regime_buffer_multiplier(config.active_regime)`.
  - `_regime_buffer_multiplier`: a literal mapping in this module:
    ```python
    _REGIME_BUFFER_MULTIPLIERS = {
        "low-vol": 0.8,
        "normal":  1.0,
        "elevated": 1.5,
        "crisis":   2.0,
    }
    ```
    The mapping is a starting point; the design doc says "tighter regimes use higher buffer." Not committed to specific multipliers in any prior doc, so the values land here. Reviewing operators may tune via `config.execution.conservative_delta_buffer_pct` for a flat shift; per-regime variation is structural and stays in code.
  - When `config.active_regime` is not in the mapping, `_regime_buffer_multiplier` returns `1.0` and the function emits no warning (out-of-band regime labels are a structural error caught by `from_resolved_config` upstream; defensive default keeps the math layer total).
  - The buffer applies to **net** delta, not per-leg deltas. A long call spread has small net delta even when individual legs are large; the buffer should not double-count opposing legs.
- **Signed notional formula for options/strategies.**
  - `contract_multiplier = 100` — fixed (every US-listed equity option is 100 shares; no Mini-options in the asset universe).
  - `signed_notional_usd = sign × |buffered_net_delta| × spot × contract_multiplier × proposal.quantity` where:
    - `sign = +1` if `proposal.direction == LONG`, `-1` if `SHORT`
    - `proposal.quantity` is the number of strategies/contracts (the unit of `option_legs` is "per one strategy"; `quantity=3` means three of these strategies/contracts)
- **`unbuffered_delta` reporting.** For options/strategies, `unbuffered_delta = |net_delta|` (before buffer). Equity returns `None`. Audit-trail field; the projection math reads `signed_notional_usd`, not `unbuffered_delta`.
- **`iv_used` / `iv_source` reporting for strategies.** All legs of a strategy share the same `(underlying, expiration)` typically, but legs may have different strikes — and `lookup_iv` interpolates per-strike. The library reports the **mean of leg-level IVs** in `iv_used` and the **most-fallback source** across legs in `iv_source` (`SURFACE` only if every leg hit the surface; `REALIZED_VOL_FALLBACK` if any leg fell back). Audit-trail summary; consumers wanting per-leg detail iterate the legs themselves via the proposal.
- **`Action`-conditional behavior.**
  - `OPEN`, `ADD` — full computation as above. The proposal contributes new exposure.
  - `CLOSE` — `signed_notional_usd` is the **negative** of what an `OPEN` of the same instrument and quantity would produce, since closing reduces exposure. For options, the greeks are still computed (for net-position gamma/theta/vega impact) but the resulting contributions are negated by the rule-contribution layer (story 04). This story's output `signed_notional_usd` is signed by the *direction of exposure change*, not the proposal's `direction` — i.e., closing a long position has `signed_notional_usd < 0` (exposure decreases). This convention keeps the rule-contribution math uniform: `current + Σ signed_notional` for net-long projects correctly across OPEN/ADD/CLOSE.
  - `ADJUST` — modifies bracket parameters; does not change exposure. Returns `DeltaAdjustedExposure(signed_notional_usd=0.0, net_greeks=Greeks(0,0,0,0) if options else None, iv_used=None, iv_source=None, unbuffered_delta=None)`. The library treats ADJUST as exposure-neutral; the engine's downstream concerns (bracket math) are not the library's.
  - `CANCEL` — withdraws an unfilled order; the cancelled order had reserved capital but not filled exposure. Returns `signed_notional_usd=0.0` (capital release is handled by the capital rule's contribution function in story 04 reading `proposal.notional_usd`, not `signed_notional_usd`).
- Re-export `compute_delta_adjusted_exposure` from `guardrail_evaluation/__init__.py`.
- Unit tests:
  - **Equity long.** Proposal `EQUITY/LONG/notional=$5,000` with `OPEN` returns `signed_notional_usd=+5000`, `net_greeks=None`.
  - **Equity short.** Proposal `EQUITY/SHORT/notional=$3,000` with `OPEN` returns `signed_notional_usd=-3000`, `net_greeks=None`.
  - **Single-leg call OPEN.** Proposal with one ATM call, IV=0.30, T=30/365, r=0.045, spot=$100, strike=$100, `quantity=10` contracts (long), `direction=LONG`, `notional=premium_at_risk`. `bs_greeks` produces `delta≈0.55`. Buffer (medium × normal regime, default 10% buffer × 1.0 multiplier) → `buffered=0.605`. `signed_notional = +0.605 × 100 × 100 × 10 = +60,500`. Test asserts within numerical tolerance.
  - **Buffer increases absolute delta.** With `conservative_buffer_pct=10`, `regime=normal`, an unbuffered delta of `0.50` produces a buffered delta of `0.55`. Asserted to `1e-6`.
  - **Per-regime buffer.** Same call under `regime=elevated` — buffer multiplier 1.5 — produces `buffered = 0.50 × (1 + 0.10 × 1.5) = 0.575`. Per-regime mapping is wired and tested.
  - **Strategy aggregation: long call spread.** Long 100-strike call (long 1) + short 110-strike call (long -1), both 30 days to expiry, IV=0.30. Compute per-leg greeks; net delta = `bs_delta(100) - bs_delta(110)`. Test asserts the net delta is positive but smaller than either leg's delta, and the buffer is applied to the net (not each leg). `signed_notional` reflects net.
  - **Strategy aggregation: long straddle.** Long 100-call + long 100-put, ATM. Net delta ≈ 0 (call delta ~0.5, put delta ~-0.5). `signed_notional ≈ 0` with the buffer applied to the small residual. Test asserts the gamma is positive, theta is negative, vega is positive (long-vol exposure).
  - **CLOSE negation.** A `CLOSE` on a long equity position (`direction=LONG`, `notional=$5,000`, `action=CLOSE`) returns `signed_notional_usd = -5000` (exposure decreases). Same for an option `CLOSE`.
  - **ADJUST is exposure-neutral.** `signed_notional_usd=0.0`, `net_greeks=Greeks(0,0,0,0)` for options.
  - **CANCEL is exposure-neutral.** `signed_notional_usd=0.0`.
  - **Unbuffered delta is reported.** For an options proposal, `unbuffered_delta = |bs_delta|` exactly (no buffer applied).
  - **`iv_source` is `REALIZED_VOL_FALLBACK` if any leg falls back.** A two-leg strategy where one leg's strike is in the surface and the other is outside the chain: aggregate `iv_source` is `REALIZED_VOL_FALLBACK`.
  - **Multiple contracts.** `quantity=5` strategies multiplies the per-strategy notional by 5.
  - **Negative net delta on short call.** A short call (`option_legs=(OptionLeg(call, K, T, quantity=-1),)`) on a LONG-direction proposal: net delta = `-bs_delta(call)`. Buffered absolute, signed by `proposal.direction=LONG` produces `signed_notional > 0`. (The convention: `proposal.direction` is the directional intent; net greeks reflect leg signs; the math composes them as documented.)
  - **Pure function.** Two calls with equal inputs produce equal outputs.

Out of scope:

- The projection layer (story 04) — this function returns the per-proposal building blocks; per-rule contribution math is downstream.
- The entry point's orchestration loop (story 05).
- Storing greeks to the position record post-fill — that's an engine-side concern (per `oms-commands.md § OPEN — Validation metadata`).
- Per-leg `IvLookupResult.notes` aggregation; `iv_source` is the audit summary the library exposes.

## Notes

**Buffer applies once.** A common mis-implementation applies the buffer per-leg and again at net; the design doc is explicit:
> Strategy aggregation. Per-leg greeks sum to strategy-level net delta, gamma, theta, vega. The buffer applies to net delta's absolute value.

**Negative buffered delta makes no sense.** `buffered_net_delta = sign(net) × (|net| × (1 + buffer))`. The buffer is always non-negative; `sign(net)` carries through. A net delta of `-0.30` with 10% buffer becomes `-0.33`, not `-0.27`.

**Why per-regime buffer multipliers live in code, not config.** The `regime_adaptation.md` doc says "Configurable per regime; tighter regimes use higher buffer" but does not commit to specific multipliers. Per `configuration-management.md § Scope — In code`, the per-rule decision tree and adjacent structural logic live in code. The flat `conservative_delta_buffer_pct` knob in `execution.yaml` is operator-tunable for portfolio-wide shifts; per-regime variation is structural insurance against regime-skew underestimation and stays in code where it can be tested rigorously.

**Contract multiplier is 100.** US-listed equity options are 100 shares per contract. The asset universe (per `assets.yaml`) excludes the rare exceptions (Mini-options, weekly options on indices with non-100 multipliers). If the universe ever extends to instruments with different multipliers, the multiplier becomes an `OptionLeg` field; today it's a constant.

**Why CLOSE has `signed_notional_usd < 0`.** Rule-contribution math sums `current + Σ signed_notional`. For net long exposure, an OPEN-LONG adds positive; a CLOSE-LONG subtracts (the close reduces exposure). The cleanest convention is that `signed_notional_usd` carries the *direction of change*, not the proposal's `direction`. The math layer documents the convention; the rule contributions in story 04 don't need to branch on `Action`.

**Strategy `direction` interpretation.** A long call spread has `direction=LONG` (directional view); a long put spread has `direction=LONG` (defensive directional view); a long straddle has `direction=LONG` (long volatility, no directional view — but the proposal must declare a direction; convention is `LONG` for long-vol structures). The library does not enforce semantic consistency; the strategist's prompt and the proposal pre-processor's annotations cover that.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/delta_adjusted.py` exists and defines `compute_delta_adjusted_exposure(...)` and a private `_REGIME_BUFFER_MULTIPLIERS` mapping.
- [ ] `compute_delta_adjusted_exposure` is re-exported from `guardrail_evaluation/__init__.py`.
- [ ] A unit test asserts an equity-long OPEN returns `signed_notional_usd = +notional`, `net_greeks=None`.
- [ ] A unit test asserts an equity-short OPEN returns `signed_notional_usd = -notional`.
- [ ] A unit test asserts a single-leg ATM call OPEN under medium × normal regime returns a `signed_notional_usd` consistent with `bs_delta × (1 + 0.10) × spot × 100 × quantity`.
- [ ] A unit test asserts the conservative buffer (default 10%) inflates absolute delta from 0.50 to 0.55.
- [ ] A unit test asserts the elevated regime applies a 1.5× buffer multiplier per the literal mapping.
- [ ] A unit test asserts a long call spread (long lower-strike call, short higher-strike call) produces a net delta strictly between zero and either leg's delta, with the buffer applied once to the net.
- [ ] A unit test asserts an ATM straddle has near-zero net delta and positive net gamma, negative net theta, positive net vega.
- [ ] A unit test asserts `Action=CLOSE` returns `signed_notional_usd` with sign opposite to an `OPEN` of the same instrument/direction/quantity.
- [ ] A unit test asserts `Action=ADJUST` returns `signed_notional_usd=0.0` and zero net greeks.
- [ ] A unit test asserts `Action=CANCEL` returns `signed_notional_usd=0.0`.
- [ ] A unit test asserts `unbuffered_delta = |bs_delta|` for options and `None` for equity.
- [ ] A unit test asserts `iv_source = REALIZED_VOL_FALLBACK` when any strategy leg falls back.
- [ ] A unit test asserts `quantity=N` linearly scales `signed_notional_usd` by N.
- [ ] A unit test asserts `compute_delta_adjusted_exposure` is pure: equal inputs produce equal outputs.
- [ ] An unknown regime label (e.g., `"made_up"`) defaults to a `1.0` regime buffer multiplier without raising.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
