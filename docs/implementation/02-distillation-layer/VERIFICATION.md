# Distillation Verification Runbook

Authoritative operator procedure for verifying AlphaMind's external
distillation layer end-to-end: orchestrator runs to completion, all
six state tables update, all three sector outputs and the
correlation/regime brief render, the regime label persists, and the
invocation archive is populated. Cross-referenced by story
`13-end-to-end-verification.md`.

---

## Pre-conditions

Complete all of the following before running any verification step:

- [ ] Bootstrap has been run to completion:
  `DATABASE_PATH=<path> python -m alphamind.collector bootstrap`
- [ ] At least one collector cycle has populated `macro_observations` with
  the VIX series (`series_id = "VIXCLS"`); the regime classifier needs
  at least one VIX observation to emit a `calibrated` regime block.
- [ ] The `DATABASE_PATH` environment variable points to the AlphaMind
  SQLite database, or the database exists at the platform default:
  - Windows: `%USERPROFILE%\AlphaMind\data\alphamind.db`
  - macOS / Linux: `~/AlphaMind/data/alphamind.db`
- [ ] The `config/distillation.yaml` and `config/assets.yaml` files are
  present in the working directory (the verification scripts read them
  for thresholds and the universe scope).

The distillation orchestrator is callable directly via
`alphamind.distillation.orchestrator.run_external_distillation`; the
verification scripts invoke it on the operator's behalf, so the
distillation phase does not need to be wired into the production
pipeline scheduler before this runbook is useful.

---

## Sequence

Run the three scripts in the order below. Each script exits 0 on pass
and 1 on fail. Treat any non-zero exit as a "stop and investigate"
event before proceeding to the next step.

```
1. uv run python scripts/verify_distillation.py
2. uv run python scripts/verify_bootstrap_calibration_mix.py
3. uv run python scripts/verify_regime_transition.py --lookback-days 7
```

---

## Step 1 — End-to-end distillation verification

**Script:** `scripts/verify_distillation.py`

**What it checks:**
- The orchestrator's `DistillationOutputs.sector_outputs` contains all
  three sector audiences (`sector_tech_semis`, `sector_financials`,
  `sector_energy`); each carries non-empty text and a non-empty ticker
  tuple.
- `correlation_regime_brief.text` is non-empty and contains a `[CR-1]`
  reference (the regime line per
  `docs/design/02-distillation-layer/external.md` § Output format).
- `universal_regime_label["regime_label"]` is one of the four
  documented labels (`low_vol_compression`, `vol_expansion`,
  `crisis_spike`, `vol_normalization`).
- All six distillation state tables have at least one row written
  within the last 5 minutes (`distillation_ticker_baseline`,
  `distillation_pair_lag`, `distillation_contract_history`,
  `distillation_event_history`, `distillation_regime_state`,
  `distillation_composite_state`).
- The invocation-archive directory contains
  `tech_semis_sector.md`, `financials_sector.md`, `energy_sector.md`,
  `correlation_regime_brief.md`, and `regime.md`.

**Run:**
```
uv run python scripts/verify_distillation.py
```

**Expected output (passing):**
```
======================================================================
AlphaMind Distillation End-to-End Verification
======================================================================

[ Sector outputs ]
  sector_energy                  blocks=1
  sector_financials              blocks=1
  sector_tech_semis              blocks=1

[ Universal regime ]
  regime_label = vol_normalization

[ Aggregate counts ]
  total_blocks = 4
  total_anomalies = 1
  bootstrap_block_count = 1
  freshness range = 2026-04-25T12:00:00+00:00 .. 2026-04-25T12:00:00+00:00

[ State-table freshness ]
  distillation_ticker_baseline        rows=78    most_recent=2026-04-25T12:00:00Z   OK
  distillation_pair_lag               rows=4     most_recent=2026-04-25T12:00:00Z   OK
  distillation_contract_history       rows=0     most_recent=(no rows)              STALE
  ...

[ Invocation archive ]
  dir = /Users/<you>/AlphaMind/archive/2026-04-25/<invocation>/distillation
  tech_semis_sector.md                OK
  financials_sector.md                OK
  energy_sector.md                    OK
  correlation_regime_brief.md         OK
  regime.md                           OK

[ PLACEHOLDER GAPS ]
  Phase 2 categories that the orchestrator currently routes to a no-op block list.
  These produce empty output today; integration is deferred to follow-up stories.
  - q1: Q1 (story 08a) ...
  - q3: Q3 (story 08b) ...
  - q6: Q6 (story 08c) ...
  - q7: Q7 (story 08d) ...

======================================================================
RESULT: PASS — all structural assertions hold.
======================================================================
```

**Failure interpretation:**

| Failure code                          | Probable cause |
|---------------------------------------|----------------|
| `sector-outputs-missing`              | Orchestrator returned fewer than three sector outputs. Check for a sector-assembly exception in logs. |
| `sector-output-empty-text`            | The sector roster is empty (check `sector_classification` rows for the missing audience) or every block was filtered out. |
| `cr-brief-empty`                      | Correlation/regime brief assembler returned no text — likely no blocks reached the synthesizer audience. |
| `cr-brief-missing-cr1-reference`      | The brief does not include the regime line. Check that `regime.label` was emitted and reached the brief assembler. |
| `regime-label-unknown`                | Regime classifier emitted a label not in the four-tier vocabulary. Check `RegimeLabel` and the persistence CHECK constraint. |
| `state-table-stale`                   | A distillation state table has no row in the last 5 minutes. Check the corresponding refresh primitive in story 07. |
| `archive-file-missing`                | The orchestrator did not write its archive files. Check the archive root path and write permissions. |

---

## Step 2 — Bootstrap calibration mix

**Script:** `scripts/verify_bootstrap_calibration_mix.py`

Run after one full distillation invocation immediately following
collector bootstrap. The script reads
`distillation_ticker_baseline` and `distillation_pair_lag` and reports
the calibration-state distribution per `baseline_kind`.

**What it asserts (qualitative bands):**

| Kind        | Expected calibrated share |
|-------------|---------------------------|
| `volume`    | ≥ 80% (high-frequency, calibrated after one month per warm-up estimate) |
| `atr`       | ≥ 80% (same) |
| `spread`    | ≥ 80% (same) |
| `sentiment` | ≤ 50% (vendor backfill varies; mostly bootstrap initially) |
| `lead_lag`  | ≤ 50% (event-driven; mostly bootstrap until ~10 cycles per pair) |

The bands are deliberately broad. The warm-up estimate is qualitative,
not a numeric SLA, per
`docs/design/02-distillation-layer/threshold-calibration.md`
§ Warm-up duration estimate.

**Run:**
```
uv run python scripts/verify_bootstrap_calibration_mix.py
```

**Expected output (passing):**
```
======================================================================
AlphaMind Distillation — Bootstrap Calibration Mix
======================================================================

[ Calibration Mix ]
  kind          total  calibrated  bootstrap  unavailable    share  band
  volume          78          75          3            0    96.2%  [80%.._] OK
  atr             78          75          3            0    96.2%  [80%.._] OK
  spread          78          75          3            0    96.2%  [80%.._] OK
  sentiment       78           5         73            0     6.4%  [_..50%] OK
  lead_lag         4           1          3            0    25.0%  [_..50%] OK

======================================================================
RESULT: PASS — calibration distribution within expected bands.
======================================================================
```

**Failure interpretation:**

| Failure code                                | Probable cause |
|---------------------------------------------|----------------|
| `calibration-distribution-out-of-band`      | One kind's calibrated share is outside the documented band. Either the warm-up window has not elapsed, or a refresh primitive is mis-tagging output. |
| `no-baseline-rows`                          | Both `distillation_ticker_baseline` and `distillation_pair_lag` are empty. Run the orchestrator at least once after bootstrap. |

---

## Step 3 — Regime state-machine invariants

**Script:** `scripts/verify_regime_transition.py`

Reads `distillation_regime_state` rows for the last
`--lookback-days N` (default 7) and asserts the four invariants from
story 09 hold:

1. Every consecutive label change records `prior_label`.
2. Every `confirmed` row was preceded by enough rows at the same label
   to satisfy `regime_transition_confirmed_invocations`.
3. Every `early-strong` row carries
   `indicator_agreement_count >= regime_transition_indicator_agreement_min`.
4. Every `early-weak` row carries
   `indicator_agreement_count < regime_transition_indicator_agreement_min`.

Run this after a market event suspected to cause a regime shift to
confirm the state machine recorded the transition correctly.

**Run:**
```
uv run python scripts/verify_regime_transition.py --lookback-days 7
```

**Failure interpretation:**

| Failure code                          | Probable cause |
|---------------------------------------|----------------|
| `missing-prior-label-on-change`       | Regime-state row's `prior_label` field is empty on a label change. Check the persistence layer. |
| `confirmed-without-prior-runlength`   | A `confirmed` transition fired without enough prior consecutive rows at the same label. Check `compute_transition_state`. |
| `early-strong-low-agreement`          | An `early-strong` row's `indicator_agreement_count` is below the configured minimum. |
| `early-weak-high-agreement`           | An `early-weak` row's `indicator_agreement_count` is at or above the configured minimum (would have classified as `early-strong`). |
| `unknown-regime-label`                | Persisted `regime_label` is not one of the four documented strings. Schema CHECK should prevent this — investigate the migration. |
| `unknown-transition-state`            | Persisted `transition_state` is not one of `stable`/`early-weak`/`early-strong`/`confirmed`. Same as above. |

---

## Mental check

A passing verification looks roughly like this on a typical mid-week
mid-cycle invocation:

- **Regime label:** `vol_normalization` (post-spike normalization is
  the most common steady-state observation; outside obvious crisis
  windows it is the modal label).
- **Sector outputs:** three documents, each ~10–30 KB. Each starts
  with the sector header (e.g. `# Sector brief: Tech & Semis`) and
  ends with one anomaly summary block followed by a freshness line.
- **Anomaly summary:** typically 0–3 anomalies per audience on a
  quiet day; a single `investigate_now` flag is unusual and worth
  reading. A run with zero anomalies across all three sectors is
  fine — it means the day was quiet.
- **Bootstrap block count:** 0–5 immediately after one month of
  paper-trading; the count drops to ≤ 2 (regime placeholder + maybe
  one event-driven block) once event-driven baselines mature.
- **Freshness range:** all three sector outputs share the same
  `freshness_min` to within a few seconds — every block reads from
  the same Class B snapshot taken at the start of the invocation.

If the regime label is `crisis_spike` and you have not seen a major
risk-off move in the prior 24 hours, treat it as a calibration
incident and run `verify_regime_transition.py` to confirm the
invariants still hold.

---

## Calibration warm-up timeline

The warm-up estimate from
`docs/design/02-distillation-layer/threshold-calibration.md`
§ Warm-up duration estimate, condensed for the runbook:

| Baseline kind                       | Time to mostly-calibrated |
|-------------------------------------|---------------------------|
| Volume / ATR / spread per ticker    | ~20 trading days (~1 month) |
| Sentiment per ticker                | 1–2 invocations after vendor backfill (varies) |
| Gap-fill rate per ticker            | Event-driven; ~6 months for major caps, longer for the long tail |
| Extended-hours confirmation         | Event-driven; similar to gap-fill |
| Lead-lag pair estimates             | ~1–2 months (10 cycles per pair) |

Steady state: high-frequency baselines universally `calibrated` after
one month; event-driven baselines coexist with sector-pooled
fallbacks indefinitely for less-active names.

---

## Known limitations

The orchestrator (story 12) currently routes the q1 / q3 / q6 / q7
phase-2 categories to a placeholder helper that returns an empty
block list. This is documented in
`alphamind.distillation.orchestrator._PHASE_2_PLACEHOLDER_GAPS`. As a
result:

- `verify_distillation.py` will see partial output: the regime block
  always exists, q12 corporate-actions and qualitative-derived blocks
  exist when their inputs are present, but Q1 technicals / volume /
  gap, Q3 options, Q6 macro, and Q7 cross-asset blocks are absent.
- The verification script does **not** treat the placeholder gap as a
  failure. Its essential structural assertions (three sector outputs
  exist, brief exists, regime label valid, archive files written, six
  state tables touched) cover the orchestrator's seven-phase contract;
  the gap is surfaced in a `PLACEHOLDER GAPS` section of the summary
  so the operator sees it explicitly rather than reading partial
  output as a regression.

The follow-up that closes this gap is tracked in the distillation
backlog as the "Phase-2 wiring" follow-up to story 12. When those
categories integrate, their entries drop out of
`_PHASE_2_PLACEHOLDER_GAPS` and the verification summary's
`PLACEHOLDER GAPS` section shrinks correspondingly with no script
changes required.
