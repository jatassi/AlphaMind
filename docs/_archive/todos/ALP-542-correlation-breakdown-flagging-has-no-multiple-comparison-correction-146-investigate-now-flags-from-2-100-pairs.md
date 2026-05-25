## Symptom

The `q7.correlation_breakdown` module tests every ticker-pair in the active universe for a 3σ deviation between short-window and long-window correlation. With \~66 active tickers, that's \~2,145 pairs tested per invocation. At a 3σ threshold under the null hypothesis of no breakdown, the expected false-positive count is approximately `2145 × 2 × (1 - Φ(3)) ≈ 2145 × 0.0027 ≈ 5.8` per-tail or \~12 two-tailed.

This invocation produced **146** `investigate_now` **flags** — well within an order of magnitude of the expected false-positive rate, suggesting most flagged pairs are statistical noise from running thousands of tests without correction.

The synthesizer received all 146 flags as `investigate_now` severity, indistinguishable from the genuine 5.83σ GOOG:META breakdown that the adaptive researcher correctly identified as a real signal.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md`:

* Line 1991: `=== ANOMALY FLAGS (195) ===`
* Lines 1992-2140: `--- INVESTIGATE NOW (148) ---` — 148 individual `correlation_breakdown_flag` entries (146 pairs + 1 narrative_lag + 1 overdue_lag)
* σ distribution: only \~10 pairs exceed 4.5σ; the bulk (130+) sit between 3.0 and 4.0σ — right at the noise floor for an uncorrected 3σ test.

Universe sizing from `resolved_config.json`:

* tech: 24 tickers
* semis: 16
* financials: 17
* energy: 9
* Total: 66 tickers → 66×65/2 = 2,145 unique pairs

Math check: P(|deviation| ≥ 3σ) under null = 0.0027; expected false positives = 2145 × 0.0027 ≈ 5.8 one-tail, \~12 two-tail.

Even accounting for legitimate within-sector correlation structure (which would yield more genuine breakdowns), 146 is in the noise-dominated regime.

## Root cause

No multiple-comparison correction is applied. The 3σ threshold is treated as if it were a single-test threshold, but in practice we're running 2k+ tests per invocation.

## Scope

Apply a multiple-comparison correction at the publishing layer of `q7.correlation_breakdown`. Two reasonable approaches:

**Option A — Benjamini-Hochberg FDR (recommended):**
Rank all pairs by p-value, apply BH at a configurable FDR (e.g., 0.05). Yields \~5-15 flags with controlled expected false discovery rate.

**Option B — Bonferroni-equivalent threshold scaling:**
Scale the σ threshold by √(2 log N_pairs) ≈ √(2 × log 2145) ≈ √15.3 ≈ 3.9. So require \~3.9σ + 3.0 ≈ 6.9σ for genome-wide significance. Conservative; would have caught only GOOG:META + GOOGL:META + COIN:MSFT in this invocation.

**Option C — Adaptive threshold with locus correction:**
Set base threshold at 3σ but require corroboration (same-side ticker appears in ≥2 pairs OR sector-internal consistency) for any flag below the Bonferroni-corrected threshold. Most ergonomic for downstream agents.

Recommended: Option A. Output the FDR-adjusted q-value alongside the deviation σ so downstream can re-threshold if needed.

## Acceptance criteria

- [ ] `correlation_breakdown` module publishes a configurable correction method (BH-FDR default).
- [ ] In a debug-e2e invocation with a synthetic universe of size N, the count of `investigate_now` correlation_breakdown flags is bounded — for inv-20260518T111140Z replay (N=66 tickers, 2,145 pairs), expect ≤25 flags at FDR=0.05.
- [ ] Each published flag includes both the raw deviation σ AND the FDR-adjusted q-value (or equivalent corrected metric).
- [ ] The top 10 highest-σ flags in inv-20260518T111140Z (GOOG:META 5.83σ, GOOGL:META 5.72σ, COIN:MSFT 5.05σ, etc.) remain published — corrections shouldn't kill real signal at the high-σ tail.

## Verification

* Re-run debug-e2e; confirm flag count drops from 146 to ≤25 (BH-FDR at 0.05) or ≤5 (Bonferroni).
* Confirm GOOG:META 5.83σ still surfaces as `investigate_now`.
* Confirm the adaptive researcher's locus investigation still finds the META centroid (downstream behavior unchanged for genuine signal).

## Notes

Coordinates with MATH-1 (data-alignment guard reduces false positives upstream) and MATH-3 (locus aggregation reduces effective N for downstream). All three should ship; this issue is the statistical-rigor leg. Without it, every e2e invocation will continue to fire \~100 phantom flags regardless of data quality.