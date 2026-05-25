## Symptom

When a single ticker (META, in this invocation) is the actual source of a correlation dislocation, the `q7.correlation_breakdown` module emits one flag per affected pair instead of aggregating to a single "ticker X is the locus" finding. The synthesizer is then shown N independent-looking flags that actually express one underlying signal.

The adaptive researcher correctly identified this manually in `[AR-1]`:

> *"META appears as the locus node in at least 15 correlation breakdown pairs across both tech and financials sectors (D-54, D-69, D-78, D-92, D-102, D-119, D-123, D-127, D-138, D-145, D-155, D-162, D-168 through D-175) — confirms this is a META-specific trajectory, not sector-wide dislocation"*

That aggregation work belongs at the distillation layer, before the synthesizer sees the flag stream, not at the adaptive layer after the fact.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

Locus ticker occurrence counts in the 146 `correlation_breakdown` flags from `analysis/synthesizer/user_message.md`:

| Ticker | Pairs flagged | Selected flag IDs |
| -- | -- | -- |
| META | 20 | CR-87, CR-83, CR-67, CR-91, CR-102, CR-67, CR-127, CR-128, CR-129, CR-130, CR-131, CR-132, CR-133, CR-134, CR-135, CR-138, CR-145, CR-155, CR-162 ... |
| XYZ (Block) | 12 | CR-30, CR-36, CR-48, CR-74, CR-117, CR-125, CR-133, CR-139, CR-144, CR-150, CR-157, CR-160 |
| C (Citi) | 10+ | CR-22, CR-26, CR-62, CR-63, CR-64, CR-65, CR-66, CR-67, CR-68, CR-69, CR-70, CR-71, CR-72, CR-73, CR-74 |
| MA | \~12 | CR-101, CR-111, CR-112, CR-113, CR-114, CR-115, CR-116, CR-117, CR-86, CR-90 |
| MCHP | \~10 | CR-57, CR-59, CR-94, CR-118, CR-119, CR-120, CR-121, CR-122, CR-123, CR-124, CR-125 |

After locus aggregation, these 60+ raw flags would collapse to \~5 locus findings (META, XYZ, C, MA, MCHP, plus a small residual of cross-locus pairs).

The adaptive researcher's `[AR-1]` finding shows what the aggregated output should look like:

* Locus: META
* Pair count: 15+ (D-54, D-69, D-78, ..., D-175)
* Magnitude: 5.83σ (max across the locus)
* Pattern: tech and financials sectors
* Conclusion: META-specific trajectory, not sector dislocation

## Root cause

The σ-test in `q7.correlation_breakdown` iterates over pairs but doesn't post-process the resulting flag stream to detect locus structure. Each pair is treated as an independent finding, which is correct at the statistical level but wrong at the signal level — pair-wise correlations are not independent when a single ticker's return path shifts.

## Scope

Add a locus-aggregation pass after the σ-test but before publishing:

1. Count, per ticker, how many flagged pairs it appears in.
2. If `pair_count >= locus_threshold` (e.g., 3), emit a single `correlation_locus_flag` for that ticker with:
   * Locus ticker
   * Pair count
   * Max σ across pairs
   * Affected partner tickers (list, grouped by sector)
   * Cross-sector spread (e.g., "2 sectors" or "tech-only")
3. Suppress the underlying per-pair flags from the main `investigate_now` stream; retain them as supporting evidence within the locus flag.
4. Pairs not part of any locus continue to emit individual pair flags.

Aggregation threshold (3) should be configurable.

## Acceptance criteria

- [ ] `q7.correlation_breakdown` publishes a `correlation_locus_flag` for any ticker appearing in ≥3 pair-wise breakdowns.
- [ ] Per-pair flags for locus-aggregated tickers are demoted from `investigate_now` to `evidence_within_locus` (or suppressed entirely).
- [ ] In a re-run of inv-20260518T111140Z, META would appear as a single locus flag with \~20 supporting pairs rather than 20 independent flags.
- [ ] Downstream synthesizer + adaptive researcher continue to find the same META-locus signal but via the structured flag rather than via manual pattern recognition.

## Verification

* Re-run debug-e2e; spot-check the synthesizer's `user_message.md` for a new `### LOCUS FLAGS ===` section listing META, XYZ, C, MA, MCHP.
* Confirm the per-pair `correlation_breakdown_flag` count drops by the locus-pair total (60+ flags become \~5 locus + residual pairs).
* Confirm the adaptive researcher's `[AR-1]`-equivalent investigation still launches but uses the pre-built locus list rather than manual pair counting.

## Notes

Locus aggregation is the cleanest single fix for the "146 flags → unactionable" problem. Combined with MATH-1 (data-alignment guard) and MATH-2 (multiple-comparison correction), the expected flag count for a 66-ticker universe drops from 146 to \~10-20 actionable items.