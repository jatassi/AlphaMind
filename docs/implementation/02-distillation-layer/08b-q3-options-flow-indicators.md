---
status: in_progress
completed_date:
commit_id:
---

# 08b — Q3 options flow indicators and cross-ticker signals

## Goal

Implement the deterministic options-derived computations from `external.md § 2 From derivatives and options (quant 3)`: per-ticker options-flow classification (BTO vs. STO breakdown for puts and calls; protective vs. speculative tagging), the cross-ticker pair-trade signature detection and sector-wide sweep detection, and the ETF-IV vs. single-name-IV divergence and index-hedging vs. sector-conviction signals. Plus the low-OI volume anomaly that keys off the flow data.

## Reading

- `docs/design/02-distillation-layer/external.md` § 2 From derivatives and options (quant 3) — authoritative scope
- `docs/design/02-distillation-layer/external.md` § 3 Anomaly detection — Flow bullets
- `docs/design/02-distillation-layer/threshold-calibration.md` § Anomaly detection thresholds — `options_low_oi_volume_multiple`
- `docs/design/01-data-layer/external/quantitative.md` §§ 3a–3h — underlying quant 3 categories and the cross-ticker rationale
- `docs/design/01-data-layer/collector/storage.md` § `options_contracts`, § `options_contract_snapshots` — the tables this story reads
- Stories 04, 05, 06, 07 — framework primitives this story composes

## Depends on

- 04, 05, 06, 07.

## Scope

In scope: under `src/alphamind/distillation/q3_options.py` —

- **Options flow classification** (per `quant 3b`, `quant 3f`): per ticker × current-day window —
  - Buy-to-open vs. sell-to-open breakdown for puts and calls. Approximate BTO/STO from snapshot deltas: an opening trade increases open interest at the strike (volume_today > 0 with day-over-day OI rise); a closing trade flat-or-decreases OI. The exact attribution requires a tape feed the data layer doesn't currently collect — use the snapshot-derived heuristic and tag the output `attribution_method = "snapshot_oi_delta"` so downstream consumers know its limit.
  - Protective vs. speculative classification: a put on a name where the system holds (or known institutional ownership is high) reads as hedging, not bearish conviction. The system holding side comes from `positions` (read-only via the SQLAlchemy session); known institutional ownership is approximated from `asset_universe.float_shares` vs. retail-density heuristics — for v1, simplify to "if any position table row holds long ≥X% of the ticker's avg daily volume in shares, classify same-name puts as `protective`; otherwise `speculative`." Document the simplification.
- **Cross-ticker pair-trade signature detection** (per `quant 3g`): scan the universe for simultaneous bullish flow on one name and bearish flow on a correlated peer (correlation pair source: the intra-sector pairs from story 08d's outputs — but story 08d may run after this; resolve by reading the persisted intra-sector correlation matrix from `distillation_ticker_baseline` if available, else compute against a window of `ohlcv_bars` directly per the Q7 baseline window). Within a 30-minute window, if BTO call flow > 1.5σ on ticker A and BTO put flow > 1.5σ on a peer ticker B (correlation ≥ 0.6 over trailing 60 days), emit a `pair_trade_signature` finding naming both legs.
- **Sector-wide sweep detection** (per `quant 3g`): same direction across multiple sector names within a 30-minute window. Threshold: ≥ 3 universe names in the same sector with BTO flow > 1.5σ on the same direction (calls or puts) — emit a `sector_wide_sweep` finding.
- **ETF IV vs. single-name IV divergence** (per `quant 3h`): per sector ETF (XLK / SMH / XLF / XLE) and its constituent universe names —
  - Compute the sector-aggregate single-name IV (volume-weighted across constituents) from `options_contract_snapshots`.
  - Compute the ETF's own ATM IV (front-month, ±10% of spot).
  - Emit `etf_iv_divergence` when the ETF IV moves > 1σ relative to the aggregate single-name IV over the trailing 60-day baseline. Direction matters: ETF leading the names is a "sector view hasn't propagated" signal; names leading the ETF is the inverse.
- **Index hedging vs. sector conviction** (per `quant 3h`): when SPY/QQQ put flow and sector-ETF put flow both spike in the same window, distinguish:
  - Both direction: `macro_hedging`.
  - Sector-ETF only (SPY/QQQ flat): `sector_specific_concern`.
  - SPY/QQQ only (sector ETFs flat): `index_hedging_no_sector_view`.
  Emit one `index_vs_sector_classification` finding with the determined label and the underlying flow magnitudes.
- **Low-OI volume anomaly** (per `quant 3b` + threshold `options_low_oi_volume_multiple = 5.0`): per contract — flag when `volume_today` exceeds 5× the 20-day average on a strike whose `open_interest` is below 100 contracts. Emit `options_low_oi_volume_anomaly` with severity `investigate_now`.
- **IV-rank baseline state**: per ticker, maintain a 252-day ATM-IV history in a state table. Per the threshold-calibration doc this is described as "Per-ticker daily ATM-IV history (for IV-rank's 252-day window) is maintained separately by distillation in its own state tables." The story 03 schema does not include a dedicated table — extend `distillation_ticker_baseline` with `kind = 'atm_iv'` rows storing the rolling ATM-IV mean and the implied 252-day percentile state, OR add a small dedicated `distillation_iv_history` table if `distillation_ticker_baseline` does not fit (justify the choice in the implementation). Either approach must include an Alembic migration; if a new table is added, update story 03's documentation to reflect the addition.
- **Output assembly**: emit `OutputBlock` instances:
  - `q3.flow_classification` per ticker, sector-audience.
  - `q3.pair_trade_signature` per detected pair, both sectors' audiences (the pair often spans sectors).
  - `q3.sector_wide_sweep` per detected sweep, that sector's audience.
  - `q3.etf_iv_divergence` per sector, that sector's audience.
  - `q3.index_vs_sector_classification` per detection, all three sector audiences (universal cross-sector signal).
  - `q3.iv_rank` per ticker, sector audience.
- Unit tests:
  - Flow classification heuristic correctly tags BTO vs. STO from snapshot OI deltas under fixture conditions.
  - Protective tagging fires when a position-table row exists for the underlying; speculative otherwise.
  - Pair-trade signature fires when two correlated names show opposite-direction BTO flow above the threshold; does not fire when correlation is below 0.6.
  - Sector-wide sweep fires at exactly 3 names; does not fire at 2.
  - ETF IV divergence fires at exactly 1σ; does not fire below.
  - Index-vs-sector classification correctly assigns each of the three labels under the documented input conditions.
  - Low-OI volume anomaly fires at 5× volume × OI < 100; suppressed at OI ≥ 100 even with high volume.
  - IV-rank state writes a new row per refresh and the percentile reflects the trailing 252-day distribution.

Out of scope:
- Full options Greeks computation (the position-level Greeks computation lives in [`guardrail-evaluation.md`](../../../design/06-risk-guardrails/guardrail-evaluation.md); distillation reads the snapshot-stored Greeks directly).
- Earnings-implied moves (deferred — depends on `event_calendar` × options chain join logic; cover in a separate Phase 4 follow-up if needed; not strictly part of `external.md § 2 derivatives`).
- Options market-maker GEX / DEX modeling (`quant 3d` in the data spec; not currently implemented in distillation per `external.md` — out of v1 scope).

## Notes

The BTO/STO heuristic is approximate — Polygon's snapshot endpoint doesn't carry trade-side attribution for individual prints. The OI-delta heuristic is the standard workaround (an opening trade pair adds to OI, a closing pair reduces it). Tag the attribution_method on outputs so downstream readers know the limit.

For the protective vs. speculative classification simplification: in v1 the only "system positions" available are the system's own holdings via the `positions` table. Known-institutional ownership inference from `asset_universe.float_shares` is too noisy for a binary protective/speculative split — defer the broader heuristic to a Phase 4 refinement and document the simplification in the source.

The cross-ticker correlation source for pair-trade detection has an ordering ambiguity: story 08d (Q7) writes the intra-sector correlation matrix to state, but the orchestrator may run 08* stories in parallel. The defensive approach: this story computes its own pair correlation on the fly from a 60-day window of `ohlcv_bars` if the persisted matrix is not yet present. The redundancy is acceptable; coordinate with story 08d on the canonical source if the duplication becomes load-bearing.

The ETF IV vs. single-name IV computation requires a per-sector mapping of universe names to their sector ETF — that mapping already exists in `sector_classification.sector_etf`. Use it.

The IV-rank baseline state question (extend `distillation_ticker_baseline` vs. new `distillation_iv_history` table) should be resolved by considering: the existing `distillation_ticker_baseline` schema from story 03 has `mean`, `stdev`, `n_observations` — that fits the IV-rank usage natively (mean/stdev of trailing IV, with the percentile derived on read). Recommended: extend `distillation_ticker_baseline` and avoid the new table. Document the decision either way.

## Acceptance criteria

- [ ] BTO/STO classification heuristic tags trades from snapshot OI deltas; output carries `attribution_method = "snapshot_oi_delta"`.
- [ ] Protective vs. speculative tagging keys off `positions` table presence with documented threshold.
- [ ] Pair-trade signature detection fires on correlated-pair opposite-direction flow above threshold; respects 0.6 correlation gate.
- [ ] Sector-wide sweep detection fires at the documented 3-name threshold.
- [ ] ETF IV vs. single-name IV divergence detection fires at the documented 1σ threshold.
- [ ] Index hedging vs. sector conviction classification correctly produces all three labels.
- [ ] Low-OI volume anomaly fires at exactly the threshold (`5×` and `OI < 100`); suppressed below.
- [ ] IV-rank baseline state is persisted and the percentile reflects trailing 252-day distribution.
- [ ] Each computation emits an `OutputBlock` with the correct `block_id` and audience tags.
- [ ] Cross-sector outputs (pair-trade across sectors, index-vs-sector) carry multi-sector audience.
- [ ] Unit tests cover all of the above with hand-constructed fixtures.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
