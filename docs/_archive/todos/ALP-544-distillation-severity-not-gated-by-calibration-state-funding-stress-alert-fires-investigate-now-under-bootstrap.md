## Symptom

Distillation modules in bootstrap calibration state (insufficient observations) emit anomaly flags at full severity (`investigate_now`), with no downgrade to reflect the underlying data uncertainty. The synthesizer receives the alert and the strategist propagates it forward as a portfolio risk, despite the alert being computed from an 11-observation bootstrap baseline.

The clearest case in this invocation: `funding_stress_alert` fired as `investigate_now` with magnitude 4.00, with all four sub-components (mmf_flow, repo_treasury_spread, sofr_ois_spread, term_repo_premium) at the 100th percentile of an 11-observation distribution — a noisy reading the module itself flagged as `bootstrap` calibration.

The tech_semis and financials researchers both manually downgraded the alert in their sector briefs, citing the bootstrap calibration:

> *"\[SA-TECH-ANOM-3\] ... composite 6.967; SOFR-OIS spread 3.56, term repo premium 2.76, all four components at 100th percentile) under bootstrap calibration with only 11 of 60 required observations, making percentile rankings unreliable. Severity: note_for_context"*

That downgrade work belongs at the distillation severity-assignment layer, not the analysis layer.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md`:

Line 673-688 — funding_stress published:

```
### q6.funding_stress | freshness ... | bootstrap — bootstrap_reason: funding_stress_min_observations: 11 < 60
alert_active: True
component_percentiles:
  mmf_flow: 100
  repo_treasury_spread: 100
  sofr_ois_spread: 100
  term_repo_premium: 100
composite_value: 6.967
Anomaly flags (1):
  - funding_stress_alert | magnitude 4.00 | severity investigate_now
```

Line 1991-1992 — synthesizer's ANOMALY FLAGS section:

```
=== ANOMALY FLAGS (195) ===
--- INVESTIGATE NOW (148) ---
- correlation_breakdown_flag:GOOG:META | magnitude 5.83 | source q7.correlation_breakdown.GOOG_META | calibration calibrated
- narrative_lag_flag | magnitude 5.83 | source q7.narrative_lag | calibration calibrated
```

The synthesizer's anomaly flag stream does carry the `calibration: calibrated` annotation per-flag — but that annotation is informational only; the severity bucket (`INVESTIGATE NOW` vs `INVESTIGATE IF PERSISTS`) is unaffected by calibration state.

## Root cause

Severity assignment in distillation modules is based on the magnitude of the underlying statistic (composite_value > threshold, components_above_percentile == 4, etc.) without consulting the calibration state. The same alert fires identically under `bootstrap`/`accumulating`/`unavailable` and `calibrated` calibration.

## Scope

After [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) establishes the three-state calibration vocabulary (`calibrated` / `accumulating` / `unavailable`), enforce a severity cap based on state:

| Calibration | Max severity allowed |
| -- | -- |
| `calibrated` | `investigate_now` (full range) |
| `accumulating` | `investigate_if_persists` (one level down) |
| `unavailable` | `note_for_context` (or suppress entirely if upstream is fully missing) |

Apply at the distillation publishing layer, uniformly across all anomaly-flag-emitting modules (q6.funding_stress, q6.market_liquidity, q7.breadth_internals, q7.intermarket_regime.\*, q7.correlation_breakdown, etc.).

## Acceptance criteria

- [ ] funding_stress_alert under bootstrap/accumulating calibration emits at `note_for_context` or `investigate_if_persists`, not `investigate_now`.
- [ ] No flag in the synthesizer's `INVESTIGATE NOW` bucket originates from a module currently in `accumulating` or `unavailable` calibration state.
- [ ] The original magnitude/composite value remains preserved in the flag payload so the severity-capping logic is transparent.
- [ ] Configuration: severity cap rules are configurable (some modules may legitimately fire `investigate_now` even with limited history if the underlying signal is structural).

## Verification

* Re-run debug-e2e; confirm funding_stress_alert appears in `INVESTIGATE IF PERSISTS` or `NOTE FOR CONTEXT` rather than `INVESTIGATE NOW`.
* Confirm sector researchers no longer need to manually downgrade the alert based on the bootstrap caveat.

## Notes

BlockedBy [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) — the severity-capping rule needs the `accumulating`/`unavailable` vocabulary that [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) establishes. Until then, the current `bootstrap` label can't unambiguously be mapped to a severity ceiling (because it conflates two different operator-action cases — one transient, one persistent).