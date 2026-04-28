---
status: in_progress
completed_date:
commit_id:
---

# 16 — Class A flag-rate empirical reporter

## Blocker (2026-04-27)

This story is blocked on the same substrate gap that blocks stories 14a and 15. A subagent dispatched against the spec verified the gap and stopped before improvising. Concrete preconditions absent from the codebase:

1. **No `activity_log` SQL table** in `src/alphamind/persistence/models.py`. The reporter's primary data source per § Data sources is "query the activity log for entries whose event_type identifies them as anomaly flags." `models.py` defines 29 SQLAlchemy tables (data-layer collector + the six distillation-state tables from story 03) but nothing matching the activity-log entry shape from `state-persistence.md § Activity log entries`. The portfolio-state `activity_log.py` is in-memory `EventSource` records for fills/brackets/margin events — wrong vocabulary and not SQL-backed.
2. **No `invocations` SQL table.** The reporter's "Invocations in window" header line and per-cycle rate computation both read from this table. Neither it nor a counterpart exists.
3. **No file-archive fallback.** Per the dispatch prompt's "Possible blocker (assess and decide)" section, the documented fallback is `infrastructure.md § Layer 2 invocation archive` — the per-invocation file tree at `%USERPROFILE%\AlphaMind\archive\<date>\<time>_<trigger>/`. The writer of those files is the distillation orchestrator (story 12, `run_external_distillation()`). Story 12 frontmatter is `status: not_started` — the orchestrator has not shipped, and no other code path writes those archive directories. There is no archive on disk to scan.
4. **`AnomalyFlag` exists only in memory.** The dataclass is constructed inside per-category indicator code (`q1/anomalies.py`, `q12_corporate_actions.py`, `qualitative_derived.py`, etc.) and aggregated in `distillation/aggregation.py`, but every flag is discarded at end-of-invocation. No persistence side effect, no archive write, no event emission.

The story's own § Data sources clause anticipates the per-category-stories gap ("If the per-category stories (08*) are not yet implemented, the reporter prints 'no data — flag event types not yet registered' for the affected classes and exits 0") but that fallback assumes the activity-log substrate exists. With both data sources absent, there is no synthetic-fixture path that exercises the production code path — the script would have nothing to query.

Resolution paths the orchestrator should pick from (operator picks):

- **Add a precursor story** for `activity_log` + `invocations` SQL tables + a flag-emission hook in the per-category indicator output path, then re-dispatch 16 unchanged. This is the same precursor that unblocks 14a and 15.
- **Sequence after story 12.** Once the orchestrator lands the file-archive writer, 16 can scan the file tree for flags. This requires the per-category indicators to also write their flag list into the archive (today they only return in-memory blocks).
- **Defer 16** until the threshold-calibration review process is actually being run (paper-trading evaluation phase). The script is operator-side review tooling; it has no production-pipeline consumer.

When this is resolved, set `status: not_started` and remove this section.

## Goal

Implement the operator's empirical-rate measurement tool — step 2 of the threshold-calibration design doc's "Review procedure." Given a threshold class and a window, compute the actual flagging rate observed over that window (flags per ticker per day, or flags per cycle for global thresholds), so the operator can compare it against the expected rate at the configured value and decide whether the threshold needs tightening or loosening. Land the tool as a CLI script under `scripts/` matching the existing `verify_*.py` pattern.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — Review procedure section, especially step 2 (compute the empirical flagging rate) and step 3 (compare to expected rate)
- `docs/design/02-distillation-layer/threshold-calibration.md` § Static configuration thresholds — the threshold classes the reporter groups flags by (anomaly detection, regime classification, lead-lag, narrative-lag, prediction market)
- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — `calibration_state` tag the reporter splits flag counts by, so the operator sees `bootstrap`-tagged flag rates separately from `calibrated`-tagged
- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — Structured trigger #1 (single-class saturation) and #2 (universe-wide silence), the two triggers this report directly addresses
- `docs/design/05-execution-layer/state-persistence.md` § Activity log entries — entry shape and event-type catalog (the report counts entries; the per-category indicator stories will define which event types correspond to which threshold classes)
- `docs/architecture/infrastructure.md` § Layer 2 invocation archive — per-invocation file tree the report can also scan if activity-log granularity is too coarse for a flag class
- `scripts/verify_bootstrap.py`, `scripts/verify_ongoing_collection.py` — existing CLI script patterns this script mirrors (CLI conventions, `--db-path` argument, exit codes)
- Stories `04-calibration-state-and-bootstrap.md`, `05-output-envelope.md`, `10-anomaly-aggregation-partitioning.md` — the upstream artifacts that produce the flag-bearing entries this report aggregates

## Depends on

- 04 (Calibration state and bootstrap fallback framework) — the report splits flag counts by `calibration_state`; the framework is the source of the tag.
- 05 (Output envelope and structured-text formatter) — the `OutputBlock` shape and `AnomalyFlag` carrier are upstream of the activity-log entries the report aggregates.
- 10 (Anomaly aggregation and per-consumer partitioning) — the `AnomalySummary` structure that ultimately populates the activity log; the reporter's group-by logic mirrors story 10's audience grouping.

## Scope

In scope:

- A new CLI script `scripts/report_flag_rates.py` with the following surface:

  ```
  uv run python scripts/report_flag_rates.py \
    --window-days 28 \
    [--db-path PATH] \
    [--threshold-class CLASS] \
    [--ticker TICKER] \
    [--output FORMAT]
  ```

  - `--window-days`: required. Trailing window size in days (operator review default per the design doc is 2–4 weeks; default the script to 28 to give a four-week trailing view).
  - `--db-path`: defaults to the configured `main.yaml` database path.
  - `--threshold-class`: optional filter. Accepts one of `anomaly_detection`, `regime_classification`, `lead_lag`, `narrative_lag`, `prediction_market`, or `all` (default).
  - `--ticker`: optional filter restricting per-ticker flag types to a single ticker.
  - `--output`: one of `text` (default, human-readable) or `json` (machine-readable for piping into a follow-up review).

- **Report content** (text format):

  ```
  Class A flag-rate report
    Window: 2026-03-29 to 2026-04-26 (28 days)
    Universe: 65 tickers
    Invocations in window: 168

  === ANOMALY DETECTION ===
    volume_anomaly_sigma (default 2.5)
      flags fired:           421
      per-ticker per-day:    0.231 (calibrated: 0.218 | bootstrap: 0.013)
      universe coverage:     58 of 65 tickers fired ≥ 1 flag
      saturation indicator:  HEALTHY (target ~1 per ticker per 3 invocations)

    price_move_atr_multiple (default 1.5)
      flags fired:           87
      per-ticker per-day:    0.048 (calibrated: 0.048 | bootstrap: 0.000)
      universe coverage:     31 of 65 tickers fired ≥ 1 flag
      saturation indicator:  HEALTHY

    options_low_oi_volume_multiple (default 5.0)
      flags fired:           4
      per-ticker per-day:    0.002 (calibrated: 0.002 | bootstrap: 0.000)
      universe coverage:     3 of 65 tickers fired ≥ 1 flag
      saturation indicator:  REVIEW — universe-wide silence; threshold may be too tight

  === REGIME CLASSIFICATION ===
    regime label transitions: 3 (low_vol_compression → vol_expansion ×2, vol_expansion → vol_normalization ×1)
    early-strong vs. confirmed ratio: 2 / 1 (HEALTHY)
    regime_skip_emergency triggers: 0

  === LEAD-LAG ===
    funding_to_credit overdue:        2 (HEALTHY)
    credit_to_equity overdue:         5 (HEALTHY)
    semis_to_tech overdue:            18 (REVIEW — saturated)
    ...

  === STRUCTURED TRIGGER STATUS ===
    [ ] saturation by single class                 — none detected
    [X] universe-wide silence on a class            — options_low_oi_volume_multiple
    [ ] regime transitions consistently mistuned   — none detected (see verify_regime_transition.py)
    [ ] emergency invocations without warrant      — none detected (see report_emergency_invocations.py)
  ```

- **Saturation indicator rules** (per threshold; thresholds the report applies — these are NOT new Class A thresholds, they are operator-side review heuristics that live in the script's source as named constants):
  - `HEALTHY`: per-ticker per-day rate within the design-doc target range (e.g., `volume_anomaly_sigma` targets ~1 flag per ticker per 3 invocations per the rationale in threshold-calibration.md).
  - `REVIEW — saturated`: per-ticker per-day rate ≥ 2× the documented target.
  - `REVIEW — universe-wide silence`: zero or near-zero flags across the universe over the window.
  - The rule is a deterministic function of (flag count, ticker count, window-days, expected rate). The expected-rate map lives in the script as a constant Python dict mirroring the rationale column from threshold-calibration.md.

- **JSON output** mirrors the text output's structure as a nested dict: top-level keys are threshold-class names; each value is a dict of threshold key → metrics record (flag count, per-ticker per-day rate, calibrated/bootstrap split, universe coverage, saturation indicator, expected rate).

- **Data sources**:
  - For per-flag counts: query the activity log for entries whose event_type identifies them as anomaly flags. The exact event-type names depend on stories 08* and 09 having registered them. The reporter codes against a stable list of event-type names defined in a constants module under `src/alphamind/distillation/flag_event_types.py`. If the per-category stories (08*) are not yet implemented, the reporter prints "no data — flag event types not yet registered" for the affected classes and exits 0.
  - For the `calibration_state` split: the activity-log entry's detail payload should include the calibration state of the source block, per the propagation contract in story 04. If absent, the reporter falls back to "uncategorized" and surfaces the gap in the report header.
  - For invocation counts: read the `invocations` table for the window.
  - For universe size: read `assets.yaml` (or `config/universe.yaml` if the universe-validation work has moved it).

- **Tests**:
  - End-to-end test against a fixture database with synthetic activity-log entries spanning 28 days, 5 tickers, 3 anomaly types, and 2 regime transitions. Verify the text output contains the expected counts, rates, and saturation indicators.
  - JSON-output test: same fixture, `--output json`, parse the JSON, assert the documented dict shape.
  - Filter test: `--threshold-class anomaly_detection` produces only the anomaly-detection section.
  - Filter test: `--ticker AAPL` restricts per-ticker rates to AAPL.
  - Empty-data test: a window with zero activity-log entries produces a report listing every threshold class with zero counts and saturation indicator `REVIEW — universe-wide silence`.
  - Calibration split test: a fixture with mixed `calibrated` and `bootstrap` entries reports the split correctly.

Out of scope:
- Automated tuning recommendations (the design doc names "no automated tuning" explicitly; the reporter informs, the operator decides).
- Trend detection across multiple windows (could be a follow-up; v1 is single-window).
- Slack/email/Discord delivery of the report (the operator runs it on demand or via the command-center activity-log explorer, which already has a "run report" surface — wiring is out of scope).
- Comparing reported rates against historical operator decisions (requires a calibration-log query surface that this story does not own).
- Detecting structured trigger #4 (regime transitions consistently mistuned) and #3 (emergency-invocation false positives). These are owned by story 13's `verify_regime_transition.py` and story 15. The flag-rate report's "STRUCTURED TRIGGER STATUS" section cross-references those reports rather than replicating them.
- Detecting structured trigger #5 (feedback-loop output shows poor predictive value). That trigger is owned by the feedback-loop work tree's deterministic-analytics-spine; the flag-rate report does not duplicate it.

## Notes

The flag-event-types constants module is the load-bearing coupling point. As stories 08* land per-category indicator implementations, each story registers its flag event-type names. The constants module imports from those stories OR each per-category story registers its names with a central registry. Recommendation: a central registry in `src/alphamind/distillation/flag_event_types.py` that maps `threshold_class → list[event_type_name]`. The per-category stories don't import from this registry; the registry imports from them (one-way) so this story's catalog stays the single source of truth.

The "expected rate" values in the saturation rules are operator-side heuristics, not authoritative thresholds. They live in the script's source so the operator (or a code review) can adjust them without ceremony. Per the user's "avoid numeric anchors" memory, treat these as soft targets — the indicator is `REVIEW`, not `BLOCKED`. The script does not gate any pipeline behavior on these rates.

The report's "REVIEW" verdicts are intentionally non-blocking. The script exits 0 even when verdicts say REVIEW. The operator runs the report periodically (monthly during paper, quarterly when live, plus on-demand when a structured trigger is suspected); the report's purpose is to inform, not enforce.

The `calibrated` vs. `bootstrap` split matters for interpretation: a high flag rate dominated by `bootstrap`-tagged flags is informational about the bootstrap-fallback being engaged, not necessarily about the threshold itself being miscalibrated. The report surfaces both numbers; the operator interprets.

The script integrates with the `report_emergency_invocations.py` tool from story 15 and the `verify_regime_transition.py` tool from story 13 by cross-referencing them in the "STRUCTURED TRIGGER STATUS" section header — the reporter does not invoke those scripts internally, just names them so the operator knows to also run them. This decoupling keeps each script focused on a single concern.

The design doc's monthly cadence during paper trading is operator discipline. The script doesn't enforce it. A future scheduled-routine can add periodic invocation, but that's a `/schedule`-level concern, not a script concern.

## Acceptance criteria

- [ ] `scripts/report_flag_rates.py` exists with the documented CLI surface.
- [ ] The script reads `config/distillation.yaml` (via the standard loader) and the activity log over `--window-days`.
- [ ] The text output contains sections for each Class A threshold group present (anomaly detection, regime classification, lead-lag, narrative-lag, prediction market).
- [ ] Per threshold the report includes: flag count, per-ticker per-day rate, calibrated vs. bootstrap split, universe coverage, saturation indicator (one of HEALTHY / REVIEW — saturated / REVIEW — universe-wide silence).
- [ ] The "STRUCTURED TRIGGER STATUS" section lists the five structured triggers from threshold-calibration.md and indicates which ones the current report addresses directly vs. delegates to a sibling tool.
- [ ] `--output json` produces a parseable JSON document mirroring the text-output structure.
- [ ] `--threshold-class` filters the report to the named class.
- [ ] `--ticker` filters per-ticker rates to the named ticker.
- [ ] An empty-data window produces a report with every threshold class listed and zero counts.
- [ ] A `flag_event_types.py` registry in `src/alphamind/distillation/` maps `threshold_class → list[event_type_name]`; the script reads from it.
- [ ] The expected-rate constants used by saturation indicators are named constants in the script, not magic literals; values are documented inline against the corresponding threshold-calibration.md rationale.
- [ ] The script exits 0 regardless of REVIEW verdicts; non-zero exit only on script-internal errors (DB unreachable, fixture corruption).
- [ ] End-to-end fixture test produces the expected text and JSON outputs.
- [ ] Filter tests cover `--threshold-class` and `--ticker`.
- [ ] Empty-data fixture test produces the expected zero-count report.
- [ ] Calibrated-vs-bootstrap split test verifies the breakdown is computed correctly.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
