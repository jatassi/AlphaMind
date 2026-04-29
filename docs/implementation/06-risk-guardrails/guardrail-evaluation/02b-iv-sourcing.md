---
status: in_progress
completed_date:
commit_id:
---

# 02b — IV sourcing with realized-vol fallback

## Goal

Implement the implied-volatility lookup the library uses to feed Black-Scholes. The primary source is the data pipeline's IV surface (`options_contract_snapshots` populated by the Polygon collector); when no surface entry exists for the proposed strike/expiration, fall back to the underlying's trailing 30-day realized volatility. The library reports which source supplied the IV via the `IvSource` enum so callers and the feedback loop can segment outcomes by IV provenance.

This story owns the `IvProvider` Protocol, an abstract `lookup_iv(...)` contract, and a fixture-backed reference implementation suitable for tests. The Polygon-backed production implementation lands when the options collector is built (gated on the deferred `Q2 microstructure` and live `options_contract_snapshots` work in the data layer); this story provides the seam.

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` § Delta-adjusted exposure §2 IV sourcing — surface interpolation primary, trailing 30-day realized volatility fallback
- `docs/design/01-data-layer/collector/storage.md` § `options_contract_snapshots` — `implied_volatility`, `delta`, `gamma`, `theta`, `vega`, `underlying_price` per `(snapshot_ts, contract_ticker)`; this is the production IV-surface backing store
- `docs/design/02-distillation-layer/external.md` § "From derivatives and options (quant 3)" — distillation's IV-related computations (ATM IV percentile, IV rank); these are downstream consumers, not inputs to this lookup
- `docs/design/02-distillation-layer/threshold-calibration.md` — bootstrap policy for cold-start data (per-output `calibration_state` tag); the realized-vol fallback follows the same pattern
- `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `MarketInputs.iv_provider`, `IvSource` defined in 01

## Depends on

- 01 (canonical types)

## Scope

In scope:

- `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py` defining:
  - `IvProvider` (`typing.Protocol`):
    ```python
    class IvProvider(Protocol):
        def lookup_iv(
            self,
            *,
            underlying: str,
            strike: float,
            expiration: date,
            contract_type: ContractType,
            as_of: datetime,
        ) -> IvLookupResult: ...
    ```
  - `IvLookupResult` (frozen dataclass): `implied_volatility: float`, `source: IvSource`, `notes: str | None`. `notes` is `None` when the lookup hits the surface cleanly; populated with a short tag (e.g., `"strike_interpolated"`, `"expiration_extrapolated"`, `"realized_vol_fallback_no_chain"`, `"realized_vol_fallback_strike_outside_chain"`) when the lookup falls back or interpolates non-trivially.
  - `IvLookupError` (`Exception`): raised when neither the surface nor the realized-vol fallback can produce a value. Conditions:
    - The underlying has no options chain in `options_contract_snapshots` AND no historical price series sufficient for a 30-day realized-vol estimate.
    - The contract type is `CALL`/`PUT` but the surface contains no rows of that type for the underlying within the lookback window AND realized-vol fallback is unavailable.
- `FixtureIvProvider` (concrete implementation in the same module, used by tests and as a placeholder until the Polygon collector is built):
  - Constructor accepts a `Mapping[str, IvSurfaceEntry]` keyed on `underlying` and a `Mapping[str, RealizedVolEntry]` keyed on `underlying`.
  - Implements `lookup_iv(...)` per the surface→fallback→error chain below.
  - `IvSurfaceEntry` (frozen dataclass): `underlying: str`, `quotes: tuple[IvQuote, ...]`. Each `IvQuote` carries `strike`, `expiration`, `contract_type`, `implied_volatility`. Mirrors a stripped slice of `options_contract_snapshots`.
  - `RealizedVolEntry` (frozen dataclass): `underlying: str`, `trailing_30d_realized_vol: float`. Mirrors a per-underlying reduction over the underlying's price-volume series.
- `lookup_iv` algorithm (also documented in the production implementation contract):
  1. **Surface lookup with strike interpolation.** Filter quotes to `contract_type == requested type` and `expiration == requested expiration`. If the requested strike is bracketed by surface strikes (i.e., there exist `strike_low <= requested <= strike_high` in the filtered set), linearly interpolate IV in strike. `notes = "strike_interpolated"` when the requested strike is not exact.
  2. **Strike outside chain (same expiration).** If quotes exist for the requested expiration but the requested strike is outside the chain bounds (deeper ITM/OTM than any listed), trigger the realized-vol fallback. `notes = "realized_vol_fallback_strike_outside_chain"`. Rationale: extrapolating IV smile beyond the listed strikes is unreliable; the conservative buffer covers the underestimate.
  3. **Expiration not in surface.** If quotes exist for the underlying and contract type but not for the requested expiration, find the two bracketing expirations available and **linearly interpolate in calendar days to expiration** at the requested strike (after step-1 strike interpolation against each bracketing expiration). `notes = "expiration_interpolated"`. If the requested expiration is beyond the surface's longest-dated quote, trigger the realized-vol fallback (`notes = "realized_vol_fallback_expiration_extrapolated"`).
  4. **No surface for underlying or contract type.** Trigger the realized-vol fallback with `notes = "realized_vol_fallback_no_chain"`.
  5. **Realized-vol fallback.** Return `IvLookupResult(implied_volatility=trailing_30d_realized_vol, source=IvSource.REALIZED_VOL_FALLBACK, notes=...)`. The trailing-30-day realized vol is read from `RealizedVolEntry`; production-side computation (`std(daily log returns) × √252`) lives with the data pipeline, not this library.
  6. **No surface and no realized vol.** Raise `IvLookupError("no IV available for {underlying} {strike}/{expiration}/{contract_type}")`.
- `lookup_iv` is total over its `IvProvider` Protocol contract — every successful path returns `IvLookupResult` with positive `implied_volatility`. The `_d1`/`_d2` consumers in `bs_greeks` already raise `ValueError` on non-positive IV; that contract is preserved.
- Update `MarketInputs` in `types.py` so its `iv_provider: IvProvider` field has a complete type (was a forward declaration in 01).
- Re-export `IvProvider`, `IvLookupResult`, `IvLookupError`, `FixtureIvProvider`, `IvSurfaceEntry`, `IvQuote`, `RealizedVolEntry` from `guardrail_evaluation/__init__.py`.
- Unit tests:
  - **Exact-strike, exact-expiration hit.** Surface entry exists for `(strike, expiration, contract_type)`; lookup returns `IvLookupResult(implied_volatility=stored_value, source=SURFACE, notes=None)`.
  - **Strike interpolation.** Surface contains `strike=100, IV=0.30` and `strike=110, IV=0.40` for the requested expiration/contract; lookup at `strike=105` returns `IV=0.35` with `notes="strike_interpolated"`.
  - **Strike outside chain.** Surface strikes span `[100, 110]`; lookup at `strike=80` returns `IvLookupResult(source=REALIZED_VOL_FALLBACK, notes="realized_vol_fallback_strike_outside_chain")`.
  - **Expiration interpolation.** Surface contains chains at `expiration=2026-05-15` (30 days out, IV=0.28) and `expiration=2026-06-15` (60 days out, IV=0.32) at the requested strike; lookup at `expiration=2026-05-30` (45 days out) interpolates linearly in days to `IV=0.30` with `notes="expiration_interpolated"`.
  - **Expiration extrapolated.** Lookup at an expiration beyond the surface's longest-dated quote returns realized-vol fallback with `notes="realized_vol_fallback_expiration_extrapolated"`.
  - **No chain, realized vol available.** Underlying has no surface entries; lookup returns realized-vol fallback with `notes="realized_vol_fallback_no_chain"`.
  - **No chain, no realized vol.** Underlying has neither surface nor realized-vol entry; lookup raises `IvLookupError`.
  - **Source enum.** `IvSource.SURFACE` for clean and interpolated surface hits; `IvSource.REALIZED_VOL_FALLBACK` whenever the realized-vol path supplies the value.
  - **Returned IV is positive.** Every successful test returns `implied_volatility > 0`; the `bs_greeks` contract from 02a is preserved.
  - **Contract-type filter.** Surface contains only put quotes for the underlying; lookup with `contract_type=CALL` falls back to realized vol with the appropriate notes.

Out of scope:

- The Polygon-backed production `IvProvider` reading from `options_contract_snapshots`. The Polygon options collector is in `Forward-trigger entries` per `project-tracker.md` § Backlog; the production adapter lands when that work fires. The library is unblocked because `FixtureIvProvider` covers the test path, and the production adapter trivially conforms to the Protocol.
- Realized-vol computation. The trailing-30-day realized-vol value is read from `RealizedVolEntry`; computing it from the underlying's daily price series is the data pipeline's responsibility (per the existing equity-price storage in `storage.md`).
- IV-rank, IV-percentile, ATM-IV-history. These are distillation-layer computations downstream of the surface; the library only consumes raw IV.
- Caching. The IV provider is called O(N proposals) per evaluation; an underlying-keyed in-memory cache is a future optimization gated on a perf signal, not a v1 feature.

## Notes

**Why a Protocol, not a class hierarchy.** The library's only contract is "give me an IV for this contract." The fixture and Polygon implementations are unrelated implementations of the same interface; a Protocol expresses that without forcing inheritance.

**Realized-vol-as-IV is conservative on most-of-the-time and aggressive in events.** The IV-vs-realized spread is normally positive (vol risk premium), so substituting realized for IV underestimates premium and overestimates delta-adjusted exposure — favorable for a guardrail. Around earnings or FOMC, IV expands ahead of the event while realized lags, so realized-vol fallback would underestimate the premium and underestimate the buffered delta. The surface is the primary source for exactly this reason; the fallback is the cold-start safety net per the [bootstrap pattern in threshold calibration](../../../design/02-distillation-layer/threshold-calibration.md).

**Why expiration interpolation but not strike extrapolation.** Term-structure interpolation in time-to-expiration is well-behaved (linear in T or log-T is a standard approximation); strike-skew extrapolation requires a model (SVI, SABR, or similar) that the library does not own. Strikes outside the chain trigger fallback; expirations within the bracketing range interpolate.

**Linear-in-calendar-days expiration interpolation.** The simplest defensible choice. A more sophisticated approach interpolates in `√T` (preserves Black-Scholes's vol-time scaling), but for the proposal-validation horizon (typically 14–90 days to expiration), linear-in-days is within ~50bp of √T-interpolation — well inside the conservative-buffer envelope.

**`notes` is informational.** The library writes `notes` so callers can audit *how* the IV was sourced (clean surface hit vs. interpolated vs. fallback). The validation tool's logging consumes this; the projection math does not branch on `notes`.

**`FixtureIvProvider` is production-grade for tests, not for live trading.** Tests construct `IvSurfaceEntry` and `RealizedVolEntry` instances inline; the production code path uses the Polygon-backed implementation written when the collector lands. Both conform to the same `IvProvider` Protocol so the library's tests assert behavior that holds in production.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py` exists.
- [ ] `IvProvider` is a `typing.Protocol` with the documented `lookup_iv(...)` signature.
- [ ] `IvLookupResult`, `IvSurfaceEntry`, `IvQuote`, `RealizedVolEntry` are `dataclass(frozen=True, slots=True)`.
- [ ] `IvLookupError` is an `Exception` subclass with a clear message format.
- [ ] `FixtureIvProvider` implements `IvProvider` and follows the surface→fallback→error chain.
- [ ] `IvProvider`, `IvLookupResult`, `IvLookupError`, `FixtureIvProvider`, `IvSurfaceEntry`, `IvQuote`, `RealizedVolEntry` are re-exported from `guardrail_evaluation/__init__.py`.
- [ ] `MarketInputs.iv_provider` types check against the implemented Protocol (the forward declaration from 01 is replaced).
- [ ] A unit test asserts an exact-strike, exact-expiration surface hit returns `IvSource.SURFACE`, `notes=None`.
- [ ] A unit test asserts strike interpolation linearly between two listed strikes; `notes="strike_interpolated"`.
- [ ] A unit test asserts strike outside the chain falls back to realized vol with `notes="realized_vol_fallback_strike_outside_chain"`.
- [ ] A unit test asserts expiration interpolation between two listed expirations is linear in calendar days; `notes="expiration_interpolated"`.
- [ ] A unit test asserts expiration beyond the surface's longest-dated quote falls back to realized vol with `notes="realized_vol_fallback_expiration_extrapolated"`.
- [ ] A unit test asserts no-chain underlying with realized vol available returns the fallback with `notes="realized_vol_fallback_no_chain"` and `IvSource.REALIZED_VOL_FALLBACK`.
- [ ] A unit test asserts no-chain underlying with no realized vol raises `IvLookupError`.
- [ ] A unit test asserts contract-type mismatch (e.g., only puts in the surface, requesting a call) falls back to realized vol.
- [ ] A unit test asserts every successful return has `implied_volatility > 0`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
