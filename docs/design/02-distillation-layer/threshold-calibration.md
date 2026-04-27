# Threshold calibration

How the distillation layer's tunable thresholds are set, refreshed, and validated, and how the layer behaves before rolling state has accumulated. Outputs depend on threshold choices consumers treat as given; this doc separates static from rolling and defines warm-up behavior so the system produces useful outputs from day one.

Companion docs cover *what* is computed ([external.md](external.md), [internal.md](internal.md)).

---

## Scope

**In scope.** Every numeric or categorical threshold inside the distillation layer that gates an anomaly flag, regime classification, persistence baseline, or divergence detection — and that doesn't have a canonical home elsewhere.

**Out of scope** (owned elsewhere):

| Threshold class | Authoritative spec |
|---|---|
| Per-rule guardrail limit values | [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md) |
| Regime multipliers on guardrail limits | [regime-adaptation.md](../06-risk-guardrails/regime-adaptation.md) |
| Escalation breakpoints (warning / critical / hard-block) | [breach-behavior.md](../06-risk-guardrails/breach-behavior.md) |
| Emergency invocation trigger thresholds | [breach-behavior.md § Emergency invocation trigger](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) |
| Thesis-dependency risk flag threshold | [internal.md § 9b](internal.md) (deferred to risk guardrails) |
| LLM agent inclusion thresholds (analyst, strategist) | [analyst.md](../04-decision-layer/analyst.md), [strategist.md](../04-decision-layer/strategist.md) |
| API failure tier classification thresholds | [api-failure-handling.md](../01-data-layer/api-failure-handling.md) |

---

## Calibration classes

Every threshold falls into one of three classes determining where it lives and how it changes.

### Class A — Static configuration constants

Operator-tunable scalars, one value per universe. Live in `config/distillation.yaml`, reload at the start of each invocation per [configuration-management.md § Reload model](../configuration-management.md#reload-model). Examples: anomaly z-score thresholds, regime VIX boundaries, the 5pp prediction-market delta, lookback windows. Reviewed by the operator on a calibration cadence (see [Update process](#update-process)); never auto-tuned.

### Class B — Computed rolling state

Per-ticker, per-pair, or per-contract baselines the layer itself produces and persists between invocations. Refreshed at the start of each invocation's distillation phase. Examples: 20-day trailing volume average per ticker, trailing sentiment distribution per ticker, lead-lag timing per pair, prediction-market deltas per contract. The lookback driving the computation *is* configured (Class A); the resulting state is derived, not configured.

### Class C — Hybrid

Class A lookback window produces Class B state, evaluated against a Class A threshold to produce a Class B flag. Most anomaly detections are Class C: "volume exceeds *N* stdevs from the *W*-day trailing mean," where *N* and *W* are Class A and the resulting flag is Class B.

---

## Static configuration thresholds

All values in `config/distillation.yaml`, universe-wide. Dual portfolio profiles ([rules-and-limits.md § Dual portfolio profiles](../06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles)) differ in capital and feature flags, not in what this layer computes.

### Anomaly detection thresholds

| Threshold | Default | Rationale |
|---|---|---|
| `volume_anomaly_sigma` | 2.5 | Volume exceeding 2.5σ above the 20-day trailing mean. At this level, the per-ticker per-day false-positive rate is ~1.2% — across 65 tickers, that's roughly one nuisance flag per ticker every 3 invocations, which fits inside the adaptive research layer's 3–5 thread budget. Lower would swamp triage; higher would miss legitimate institutional flow events. |
| `price_move_atr_multiple` | 1.5 | A daily move exceeding 1.5× the 14-period ATR. ATR-normalized so cross-ticker comparison is meaningful (per [external.md § 1](external.md#1-normalization-and-formatting)). At 1.5× ATR, the move is at the edge of the historical daily range. |
| `options_low_oi_volume_multiple` | 5.0 | Single-day options volume exceeding 5× the 20-day average on a strike with open interest below 100 contracts. The low-OI filter is essential — a 5× volume spike on a deep-liquidity strike is noise; the same spike on a strike that didn't exist yesterday is a positioning signal. |
| `block_trade_min_shares` | 10,000 | Block prints at or above this size are tagged as institutional flow. Mirrors the threshold cited in [quantitative.md § 2c](../01-data-layer/external/quantitative.md). |
| `block_trade_min_notional_usd` | 500,000 | OR'd with the share threshold — either condition tags the print. The notional threshold catches large prints on lower-priced names. |
| `dark_pool_one_sided_window_minutes` | 60 | Sustained one-sided dark pool flow detection window. Sixty minutes is long enough to filter routine VWAP execution patterns, short enough that detection lands within the same invocation as the flow. |
| `earnings_revision_cluster_count` | 3 | Three or more analyst revisions in the same direction within `earnings_revision_cluster_days`. Three is the smallest count that meaningfully indicates institutional repricing rather than a single house update. |
| `earnings_revision_cluster_days` | 5 | Trailing window for the cluster detection. |
| `macro_surprise_percentile` | 90 | Macro releases (CPI, PCE, NFP, etc.) flagged when the surprise component is in the top 10% of its trailing 24-month distribution. Calibrates per-indicator without requiring per-indicator tuning. |
| `funding_stress_component_alert_count` | 2 | Funding stress composite alerts when 2 of 4 components (SOFR-OIS spread, repo-Treasury spread, term repo premium, MMF flow) are simultaneously above their individual alert percentiles. A single component breach is noise; two simultaneous breaches indicate real funding pressure. |
| `funding_stress_component_percentile` | 90 | Per-component alert level — top 10% of trailing 60-day distribution. |
| `market_liquidity_alert_percentile` | 10 | Market-wide liquidity composite alerts when in the bottom 10% of trailing 60-day distribution (lower percentile = worse liquidity). |
| `news_price_divergence_window_hours` | 12 | Window for cross-referencing news sentiment against subsequent price action. Twelve hours captures the typical institutional digestion window without bleeding into the next trading day. |

### Regime classification boundaries

| Threshold | Default | Rationale |
|---|---|---|
| `regime_low_vol_vix_max` | 14.0 | Upper bound of low-vol compression regime. Mirrors [regime-adaptation.md § Regime classification mapping](../06-risk-guardrails/regime-adaptation.md#regime-classification-mapping). |
| `regime_normal_vix_min` | 14.0 | Lower bound of normal regime. |
| `regime_normal_vix_max` | 22.0 | Upper bound of normal regime. |
| `regime_elevated_vix_min` | 22.0 | Lower bound of elevated regime. |
| `regime_elevated_vix_max` | 35.0 | Upper bound of elevated regime. |
| `regime_crisis_vix_min` | 35.0 | Lower bound of crisis regime. |
| `regime_term_structure_backwardation_threshold` | 0.0 | VX1 minus VIX spot. Negative = backwardation. The composite regime classification (quant 11f) requires backwardation as a confirming signal for crisis, distinguishing a VIX spike from a sustained crisis. |
| `regime_vvix_high_percentile` | 80 | VVIX percentile rank against trailing 1-year. Above this is the "high VVIX" half of the [VIX/VVIX matrix](external.md#4-persistent-state-and-composites). |
| `regime_vvix_low_percentile` | 30 | Below this is the "low VVIX" half. The deliberate gap between 30 and 80 leaves an indeterminate band where the matrix label is unstable; agents see the underlying VVIX percentile in that case rather than a forced binary. |

### Regime transition confidence

The layer emits a regime label every invocation. When the label changes, downstream guardrails respond differently for early-stage vs. confirmed transitions ([regime-adaptation.md § Transition mechanics](../06-risk-guardrails/regime-adaptation.md#transition-mechanics)).

| Threshold | Default | Rationale |
|---|---|---|
| `regime_transition_confirmed_invocations` | 2 | A label change is `confirmed` after the new label has held for 2 consecutive invocations. Otherwise it is `early`. Two invocations is roughly 4 hours during market hours — long enough to filter intraday VIX whipsaw, short enough that confirmed transitions still drive timely loosening or tightening. |
| `regime_transition_indicator_agreement_min` | 3 | Within an `early` transition, the change is reported as `early-strong` if at least 3 of 4 underlying indicators (VIX level, term structure shape, VVIX, realized vol) agree with the new label, and `early-weak` otherwise. Tightening always fires on `early-*` per [regime-adaptation.md](../06-risk-guardrails/regime-adaptation.md); loosening waits for `confirmed`. |
| `regime_skip_emergency_trigger` | true | Whether a regime label that skips a level (low-vol → elevated, normal → crisis, low-vol → crisis) triggers an emergency invocation. Wired to [breach-behavior.md § Emergency invocation trigger](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger). |

### Lead-lag and narrative-lag

Maintains trailing lead-lag timing estimates per pair (Class B) and flags overdue lag responses against operator-set bounds (Class A).

| Pair | Default lag bound | Rationale |
|---|---|---|
| `lead_lag_funding_to_credit_max_days` | 3 | Funding-market stress (SOFR-OIS, repo) typically reaches credit spreads within 1–3 days. Beyond 3 days the relationship is unreliable. |
| `lead_lag_credit_to_equity_max_days` | 3 | Credit spread moves typically reach equity within 1–3 days at the index level, longer at the single-name level. |
| `lead_lag_semis_to_tech_max_days` | 2 | Semis lead the broader tech complex on cyclical inflections; the relationship resolves within 1–2 sessions. |
| `lead_lag_financials_to_market_max_days` | 1 | Financials lead the market on growth/risk repricing; the relationship is fast (intraday to next session). |
| `lead_lag_commodity_to_energy_equity_max_days` | 1 | Crude futures lead energy equities; the lag is intraday to one session. |
| `lead_lag_overdue_lead_sigma` | 1.5 | The lead must have moved at least 1.5σ for the lag to be considered overdue. A small lead move that the lag hasn't tracked is not a divergence — it's noise. |

| Narrative lag | Default | Rationale |
|---|---|---|
| `narrative_lag_correlation_shift_sigma` | 1.5 | Correlation matrix shift exceeding 1.5σ relative to its trailing 60-day variance. Below this, intra-day correlation noise dominates; above it, a real regime shift is likely. |
| `narrative_lag_media_silence_hours` | 12 | Window during which financial media coverage is checked for narrative pickup. If the correlation has shifted but media in this window contains no coverage of the underlying regime, narrative lag is flagged. |

### Persistence and percentile windows

These are the lookback windows that drive Class B rolling state.

| Window | Default days | Used by |
|---|---|---|
| `volume_baseline_days` | 20 | Per-ticker rolling volume mean and stdev (anomaly denominator) |
| `atr_baseline_days` | 14 | Per-ticker ATR (price-move normalization) |
| `spread_baseline_days` | 20 | Per-ticker spread mean (liquidity baseline) |
| `correlation_short_days` | 20 | Intra-sector pairwise correlation matrix (short window) |
| `correlation_long_days` | 60 | Intra-sector pairwise correlation matrix (long window) and correlation-stability baseline |
| `sentiment_baseline_days` | 60 | Per-ticker trailing sentiment distribution for percentile calibration |
| `sentiment_min_observations` | 30 | Minimum observations before per-ticker sentiment baseline is treated as calibrated; below this, cross-sectional pooled baseline is used and the output is tagged `bootstrap` |
| `gap_fill_baseline_days` | 252 | Per-ticker gap-fill history (one trading year) |
| `gap_fill_min_events` | 30 | Minimum gap events before per-ticker gap-fill rate is treated as calibrated |
| `extended_hours_confirmation_days` | 90 | Per-ticker extended-hours confirmation rate window |
| `extended_hours_min_events` | 20 | Minimum extended-hours events before per-ticker rate is treated as calibrated |
| `prediction_market_history_days` | 30 | Per-contract trailing probability history retained for trajectory display |
| `funding_stress_baseline_days` | 60 | Per-component funding-stress percentile baseline |
| `market_liquidity_baseline_days` | 60 | Market-wide liquidity composite percentile baseline |

### Prediction market delta

| Threshold | Default | Rationale |
|---|---|---|
| `prediction_market_delta_pp_threshold` | 5.0 | Inter-invocation probability shift exceeding 5 percentage points is flagged. Mirrors the value cited in [qualitative.md § 3](../01-data-layer/external/qualitative.md) and [external.md § 4](external.md#4-persistent-state-and-composites). At 5pp, the move is large enough to outpace bid-ask noise on the more liquid contracts (Polymarket, Kalshi) without requiring further calibration. |
| `prediction_market_low_liquidity_volume_min_usd` | 10,000 | Contracts with 24-hour notional volume below this are tagged low-liquidity; their deltas are computed but discounted in downstream weighting. |

---

## Bootstrap policy

The layer must produce outputs from day one, before per-ticker rolling state has accumulated. Two facts force the policy:

1. Baseline windows above are 20–252 days. Waiting for full calibration would block paper trading for a year on the gap-fill baseline alone.
2. Decisions built on stale or missing data are worse than decisions skipped — fail-closed per [api-failure-handling.md § Design principle](../01-data-layer/api-failure-handling.md#design-principle--no-degraded-decisions).

Produce outputs with **explicit confidence tagging**; fall back to **cross-sectional priors** when per-ticker data is insufficient; let downstream consumers weight accordingly. Mirrors the `Signal quality: HIGH | MODERATE | LOW | DEGRADED` flag on domain-researcher briefs and the `quality: complete | partial | stale | unavailable` flag on adaptive-research tool outputs.

### Per-output tagging

Each distillation output block carries a `calibration_state` field:

| Value | Meaning |
|---|---|
| `calibrated` | All inputs reached their `_min_observations` thresholds. Output is produced from per-ticker / per-pair / per-contract rolling state. |
| `bootstrap` | At least one input is below its observation minimum. Output uses a cross-sectional pooled fallback; missing inputs listed in the block's `bootstrap_reason` field. |
| `unavailable` | The fallback itself cannot be computed (e.g., no universe-wide gap-fill events yet). Output is omitted; downstream consumers see no signal rather than a misleading one. |

Anomaly detections, regime classifications, and lead-lag flags carry the tag. Per-ticker sentiment percentiles, gap-fill rates, and extended-hours confirmation rates are most likely to surface in `bootstrap` state during the first months.

### Cross-sectional priors

When per-ticker state is insufficient, the layer substitutes a universe-wide prior:

| Computation | Bootstrap fallback |
|---|---|
| Per-ticker volume baseline | Sector-pooled mean/stdev. Tech/semis (~35), financials (~15), energy (~15) provide stable priors. |
| Per-ticker ATR baseline | Sector-pooled mean/stdev. |
| Per-ticker sentiment percentile | Universe-pooled distribution. Per-ticker calibration captures that TSLA's sentiment range is wider than JPM's (per [qualitative.md § 2a](../01-data-layer/external/qualitative.md)); the pooled prior loses that nuance, which is why the bootstrap tag matters downstream. |
| Per-ticker gap-fill rate | Sector-pooled rate from the prior trading year, refreshed monthly. |
| Per-ticker extended-hours confirmation rate | Universe-pooled rate, refreshed monthly. Cold-start default 50% (no information). |
| Per-pair lead-lag timing | Class A `_max_days` bounds act as the prior. Trailing estimates begin updating after the first 10 observed pair events. |
| Per-contract prediction-market delta | The 5pp threshold is universe-wide — no per-contract bootstrap needed. |

### Downstream propagation

The `calibration_state` tag flows into the analysis layer the same way `signal_quality` does. Domain researchers consuming bootstrap-tagged anomalies surface that in their `Signal quality` line; the synthesizer carries it into the unified snapshot; analyst and strategist see it in their input bundles and weight conviction accordingly — a 2.5σ volume anomaly on a `bootstrap` baseline is weaker than the same anomaly on a `calibrated` baseline. "Produce with tag" rather than "abort" is the right contract; LLM agents' own conviction calibration is the natural recipient of graded confidence.

### Warm-up duration estimate

For a paper-trading deployment starting from cold state:

- Volume / ATR / spread baselines per ticker: `calibrated` after 20 trading days (~1 month).
- Sentiment baseline per ticker: `calibrated` after the vendor's 30-day backfill plus 30 trading days of in-system observation. Most vendors backfill at API connection, so typically `calibrated` within 1–2 invocations.
- Gap-fill rate per ticker: event-driven. Major caps often reach 30 events within 6 months; smaller names may take a year. Sector-pooled fallback is steady state for the long tail.
- Extended-hours confirmation rate: event-driven, similar to gap-fill.
- Lead-lag pair estimates: meaningful after ~10 cycles per pair, typically 1–2 months.

Steady state: high-frequency baselines (volume, ATR, spread, sentiment) universally `calibrated` after one month; event-driven baselines (gap-fill, extended-hours) coexist with sector-pooled fallbacks indefinitely for less-active names.

---

## Update process

### Class B refresh (automated, every invocation)

Refresh at the start of each invocation's distillation phase, before anomaly detection runs. Incremental — append yesterday's point, drop the oldest — so cost is bounded regardless of run history.

Failures surface through the fail-closed path per [api-failure-handling.md](../01-data-layer/api-failure-handling.md). A refresh that cannot complete (e.g., DB unavailable) aborts the invocation rather than running anomaly detection on a stale baseline.

### Class A review (operator-driven, periodic)

Default cadence: **monthly** during paper trading, **quarterly** once live, plus on-demand whenever a structured trigger fires.

**Structured triggers — review immediately when:**

- Adaptive research's per-cycle thread budget is consistently saturated by anomaly flags from a single class (e.g., volume anomalies dominating triage). The relevant `_sigma` or `_multiple` is too loose.
- Multiple invocations with zero anomalies of a class across the universe over a window where market activity should produce some. Threshold is too tight.
- Continuous-monitor emergency invocations fire repeatedly without underlying market state warranting it (false positive on `regime_skip_emergency_trigger`).
- A regime transition that should have been `confirmed` is consistently `early`, or vice versa. `regime_transition_confirmed_invocations` is mistuned.
- Feedback-loop output (Phase 4, [project-tracker.md](../../project-tracker.md)) shows a class of anomaly flags has poor predictive value for thesis outcomes.

**Review procedure** (each is a YAML edit landing at the next invocation's reload, per [configuration-management.md § Reload model](../configuration-management.md#reload-model)):

1. Examine the activity log and adaptive-research outputs over 2–4 weeks of paper-trading data.
2. Compute the empirical flagging rate for the threshold class (flags per ticker per day, or flags per cycle for global thresholds).
3. Compare the empirical rate to the expected rate at the configured value.
4. Adjust; reload validation runs at the next invocation; change takes effect.

No automated tuning. The Phase 4 feedback loop is the natural source of empirical inputs at step 2 once it ships; the edit itself remains an operator action — the trade-off (flag rate vs. signal quality) depends on the system's current capacity to investigate.

**No silent threshold mutation.** A threshold change is a config change with a config-change activity log entry. Motivating observation, old value, and new value live in the operator's calibration log — notes paired with the YAML diff in version control.

---

## Validation invariants

Run as part of the configuration-management semantic self-test ([configuration-management.md § Validation](../configuration-management.md#validation)) when `config/distillation.yaml` is loaded. Failure aborts the invocation and alerts the operator.

| Invariant | Reason |
|---|---|
| All `*_sigma` and `*_multiple` thresholds ≥ 0 | Negative thresholds are nonsensical for one-sided anomaly detection. |
| All `*_days`, `*_minutes`, `*_hours` windows ≥ 1 | A non-positive lookback is structurally invalid. |
| `*_min_observations` ≤ corresponding `*_baseline_days` | The minimum sample size cannot exceed the window size. |
| `regime_low_vol_vix_max == regime_normal_vix_min` | Adjacent regimes share their boundary. |
| `regime_normal_vix_max == regime_elevated_vix_min` | Same. |
| `regime_elevated_vix_max == regime_crisis_vix_min` | Same. |
| `regime_low_vol_vix_max < regime_normal_vix_max < regime_elevated_vix_max` | Strict monotonicity of regime VIX ceilings. |
| `regime_vvix_low_percentile < regime_vvix_high_percentile` | The VIX/VVIX matrix's low/high boundaries cannot cross. |
| `funding_stress_component_alert_count` in [1, 4] | The composite has 4 components; the alert count must select between 1 and all of them. |
| `funding_stress_component_percentile` in [50, 100] | A component-alert percentile below 50 inverts the meaning of "stress." |
| `market_liquidity_alert_percentile` in [0, 50] | Liquidity alerts fire on the low end; a percentile above 50 inverts the meaning. |
| `prediction_market_delta_pp_threshold` in (0, 100] | Probability shifts are bounded. |
| `regime_transition_confirmed_invocations` ≥ 1 | A confirmation horizon of zero would make every label change `confirmed`, eliminating the early/confirmed distinction. |

---

## Where each threshold lives

Mirrors the [configuration-management.md § Runtime vs. deploy-time classification](../configuration-management.md#runtime-vs-deploy-time-classification) split.

**`config/distillation.yaml`** — every Class A threshold above. Reloaded at the start of every invocation. Operator edits take effect at the next scheduled trigger; no restart required.

**Distillation-state DB tables** — every Class B rolling baseline:

- `distillation_ticker_baseline` — per-ticker volume/ATR/spread/sentiment-distribution rolling state
- `distillation_pair_lag` — per-pair lead-lag timing estimates and event counts
- `distillation_contract_history` — per-contract prediction-market trailing probability series
- `distillation_event_history` — per-ticker gap events and extended-hours events with outcomes
- `distillation_regime_state` — current regime label, indicator-agreement count, invocations-held counter
- `distillation_composite_state` — funding-stress and market-liquidity composite trailing distributions

Schema definitions belong with the persistence schema in the execution layer's state-persistence work; listed here only to make the storage destination explicit.

**Code constants** — none. Sign conventions and percentile bounds at 0/100 are enforced by the validation invariants above, not hidden in code.

---

## Cross-references

- Computations these thresholds gate: [external.md](external.md) (anomaly detection §3, persistent state §4), [internal.md](internal.md)
- Class A storage and reload: [configuration-management.md](../configuration-management.md)
- Regime label downstream consumers: [regime-adaptation.md](../06-risk-guardrails/regime-adaptation.md), every analysis-layer agent (universal context broadcast per [external.md § 4](external.md#4-persistent-state-and-composites))
- `regime_skip_emergency_trigger` consumer: [breach-behavior.md § Emergency invocation trigger](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger)
- Signal-quality conventions the bootstrap tag aligns with: [domain-researchers/tech-semis.md](../03-analysis-layer/domain-researchers/tech-semis.md), [qualitative-research.md](../03-analysis-layer/qualitative-research.md), [adaptive-research.md](../03-analysis-layer/adaptive-research.md)
- Fail-closed principle: [api-failure-handling.md § Design principle](../01-data-layer/api-failure-handling.md#design-principle--no-degraded-decisions)
- Operator review feedback source (when shipped): [project-tracker.md § Phase 4](../../project-tracker.md)
