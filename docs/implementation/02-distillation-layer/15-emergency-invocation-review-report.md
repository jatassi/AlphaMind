---
status: blocked
completed_date:
commit_id:
---

# 15 — Emergency-invocation review report

## Goal

Implement the operator review tool for structured trigger #3 from the threshold-calibration design doc — "Continuous-monitor emergency invocations fire repeatedly without underlying market state warranting it (false positive on `regime_skip_emergency_trigger`)." Land a CLI script that lists every emergency invocation in a window, classifies each by downstream consequence (drove remediating action / produced no-op / was operator-overridden / closed without action), and surfaces the false-positive rate so the operator can decide whether to disable `regime_skip_emergency_trigger` or re-tune any other emergency-trigger logic.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — Structured trigger #3 (emergency invocations firing without warrant) is the trigger this report addresses
- `docs/design/02-distillation-layer/threshold-calibration.md` § Regime transition confidence — the `regime_skip_emergency_trigger` boolean threshold whose false-positive rate this report measures
- `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — the consumer that decides when to fire emergency invocations; lists every condition that can fire one, including but not limited to `regime_skip_emergency_trigger`
- `docs/design/05-execution-layer/state-persistence.md` § Tier 2 — the `invocations` table fields including `trigger_type` (`scheduled` / `emergency` / `manual`) and `trigger_reason` (which guardrail or regime-skip event prompted the emergency)
- `docs/design/05-execution-layer/state-persistence.md` § Activity log entries — the post-invocation events the report consults to classify downstream consequence (PM decisions, command-abandoned events, operator-console events)
- `scripts/verify_bootstrap.py`, `scripts/verify_ongoing_collection.py` — existing CLI script patterns to mirror
- Story `09-volatility-regime-classification.md` — the source of the `regime_skip_emergency` flag this report counts events for

## Depends on

- 09 (Volatility regime classification and universal broadcast) — the `regime_skip_emergency` flag is one of the triggers this report classifies; the report doesn't function until the flag exists.

## Scope

In scope:

- A new CLI script `scripts/report_emergency_invocations.py` with the surface:

  ```
  uv run python scripts/report_emergency_invocations.py \
    --window-days 90 \
    [--db-path PATH] \
    [--output FORMAT]
  ```

  - `--window-days`: required. Default 90 (one quarter, the operator-driven cadence for once-live calibration review per threshold-calibration.md).
  - `--db-path`: defaults to the configured `main.yaml` database path.
  - `--output`: `text` (default) or `json`.

- **Report content** (text format):

  ```
  Emergency invocation review
    Window: 2026-01-26 to 2026-04-26 (90 days)
    Scheduled invocations:    280
    Emergency invocations:    12
    Emergency rate:           4.3% of total

  === BY TRIGGER REASON ===
    regime_skip_emergency:                4
      drove remediating action:           1
      produced no-op (no commands):       3
      operator-overridden / cancelled:    0
      false-positive rate:                75% (REVIEW — high false-positive)

    guardrail_progressive_tier_breach:    6
      drove remediating action:           5
      produced no-op:                     1
      operator-overridden:                0
      false-positive rate:                17% (HEALTHY)

    manual:                               2
      drove remediating action:           2
      produced no-op:                     0
      operator-overridden:                0
      false-positive rate:                0% (HEALTHY — operator-driven)

  === PER-INVOCATION DETAIL ===
    2026-04-22 14:30 UTC | regime_skip_emergency | low_vol_compression → vol_expansion (skipped vol_normalization)
      duration:        87s
      pm_decisions:    3 (3 holds, 0 actions)
      classification:  produced no-op
      narrative:       regime label normalized within 2 invocations; no portfolio action warranted

    2026-04-15 11:12 UTC | regime_skip_emergency | normal → crisis_spike (VIX 22.4 → 38.1)
      duration:        112s
      pm_decisions:    5 (1 reduce, 1 hold, 3 close)
      classification:  drove remediating action
      narrative:       VIX shock confirmed; portfolio reduced exposure as designed

    ...

  === RECOMMENDATION ===
    regime_skip_emergency false-positive rate of 75% exceeds the 50% review threshold.
    Consider disabling `regime_skip_emergency_trigger` (currently `true`) or refining
    the regime-skip definition. Cross-reference with `verify_regime_transition.py` for
    transition-state-machine integrity in the same window.
  ```

- **Classification rules** (deterministic, encoded as named constants in the script):
  - `drove remediating action`: the emergency invocation's resulting `pm_decision` activity-log entries include at least one verdict of `approve` or `approve_with_modification` AND the resulting commands include at least one `reduce`, `close`, `cancel`, or `adjust-bracket` command (i.e., a defensive action). Reads the `pm_decision` envelope's commands list per `state-persistence.md`.
  - `produced no-op`: zero `pm_decision` entries with action commands (only holds, or zero PM decisions).
  - `operator-overridden / cancelled`: an `activity_log` entry with `source: operator_console` and an action that cancelled or paused the invocation flow within 5 minutes of the emergency trigger.
  - The classification is mutually exclusive; if multiple conditions match, precedence is `operator-overridden` > `drove remediating action` > `produced no-op`.

- **False-positive rate calculation**: per trigger reason, false-positive rate = (`produced no-op` + `operator-overridden`) / total. Saturation indicators:
  - `HEALTHY`: false-positive rate ≤ 30%.
  - `REVIEW — high false-positive`: false-positive rate > 30% AND total emergency count ≥ 4 (small-N events stay HEALTHY because one no-op out of one is not statistically meaningful).
  - The thresholds (30%, 4 events) are operator-side review heuristics in the script's source as named constants, not Class A thresholds. Per the user's "avoid numeric anchors" memory, treat them as soft targets — they drive REVIEW verdicts that the operator interprets, not pipeline gating.

- **JSON output** mirrors the text structure as a nested dict: top-level keys `summary`, `by_trigger_reason`, `per_invocation_detail`, `recommendations`. The `recommendations` array is empty when no REVIEW verdict triggers; otherwise it carries one entry per triggering reason with the false-positive rate and the cross-reference suggestion.

- **Data sources**:
  - `invocations` table: filter `trigger_type = 'emergency'`, parse `trigger_reason` to bucket by reason.
  - `activity_log`: per emergency invocation, fetch `pm_decision` entries scoped to the invocation ID; classify via the rules above.
  - `activity_log`: per emergency invocation, fetch `source: operator_console` entries within ±5 minutes of `started_at` to detect operator overrides.
  - `distillation_regime_state` (per story 09): for `regime_skip_emergency` invocations, fetch the prior_label and regime_label to populate the per-invocation detail's regime narrative.

- **Tests**:
  - End-to-end test against a fixture database with 12 emergency invocations spanning 90 days, each with synthetic `pm_decision` entries producing the four classification outcomes. Verify the text output's counts, false-positive rates, and saturation indicators.
  - JSON-output test: same fixture, parse the JSON, verify the documented dict shape.
  - Classification rule tests:
    - An emergency with one `approve` PM decision and one `reduce` command classifies as `drove remediating action`.
    - An emergency with three `pm_decision` entries all with `verdict: hold` classifies as `produced no-op`.
    - An emergency with an operator-console pause within 3 minutes classifies as `operator-overridden`.
    - Precedence test: an emergency with both an action command AND an operator-console pause classifies as `operator-overridden`.
  - REVIEW threshold test: a fixture where 4 of 4 `regime_skip_emergency` events are no-op produces the REVIEW verdict and a recommendation entry.
  - Empty-window test: a window with zero emergency invocations produces a report stating "no emergency invocations in window" and exits 0.
  - Small-N suppression test: a fixture with 1 of 1 `regime_skip_emergency` events being no-op produces HEALTHY (small-N suppression).

Out of scope:
- Automated mitigation actions (the script informs; the operator decides whether to edit `regime_skip_emergency_trigger` or any other Class A threshold).
- Cross-window trend analysis (v1 is single-window).
- Discord/email delivery of the report.
- Detecting whether the emergency invocation's regime classification was correct in hindsight (requires post-hoc regime-truth labeling that doesn't exist; the report's classification is downstream consequences only, not regime accuracy).
- Emergency-invocation latency measurement against the 30-second target from cost-and-rate-limit-modeling.md (operationally useful but a different concern; lives in the Throughput panel).
- Per-emergency-trigger-condition review for trigger conditions the design doc does not name in threshold-calibration.md (e.g., guardrail-breach-driven emergencies whose tuning lives in `06-risk-guardrails/`).

## Notes

The 30% / 4-event review thresholds match the user's "avoid numeric anchors" memory pattern: structural quality constraints, not hard pipeline gates. The 30% rate corresponds roughly to "more than 1 in 3 emergency invocations was unnecessary" — at that point, operator review is justified. The 4-event minimum prevents a 100%-no-op verdict against a single observation from triggering misleading REVIEW signals.

The classification rules deliberately treat "no PM decisions at all" as `produced no-op`. This handles the case where an emergency invocation aborts in distillation or analysis without reaching the PM. From the threshold-calibration tuning perspective, these are still false positives — the trigger fired but no signal materialized — so they belong in the same bucket.

The `regime_skip_emergency` per-invocation detail's regime narrative reads from `distillation_regime_state` to surface the prior label, new label, and the skipped tier. This tight coupling to story 09 is why the story depends on it. If the regime-state row is missing for an invocation (e.g., the emergency aborted before regime classification), the per-invocation detail line shows "regime narrative unavailable" rather than failing.

The report cross-references `verify_regime_transition.py` from story 13 because high false-positive rates on `regime_skip_emergency` may reflect transition-state-machine bugs (an `early-strong` transition mistakenly triggering the skip detection) rather than threshold mistuning. The operator runs both reports together when investigating.

The 5-minute window for detecting operator overrides is a deliberate heuristic — operator pauses much later than that are unlikely to be responses to the emergency itself. The window is a script constant, adjustable in code review.

The recommendation text in the report is templated, not LLM-generated. Per CLAUDE.md and the user's "avoid numeric anchors" memory, the script's role is to surface the data; the operator's role is to interpret and act. The template just paraphrases the false-positive rate against the review threshold and points at the relevant config key.

Per CLAUDE.md and the user's "LLM agents uniformly Critical" memory: this script is operator tooling, not a runtime decision-maker. There is no failure-mode where the script "automatically" disables the trigger — disabling is always an operator-driven YAML edit that flows through the audit-trail pipeline from story 14a.

## Acceptance criteria

- [ ] `scripts/report_emergency_invocations.py` exists with the documented CLI surface.
- [ ] The script reads the `invocations`, `activity_log`, and `distillation_regime_state` tables for the window.
- [ ] The text output includes summary counts (scheduled vs. emergency), per-trigger-reason breakdown with classification counts and false-positive rate, per-invocation detail block, and a recommendation section.
- [ ] Classification rules produce the four documented buckets with the documented precedence (`operator-overridden` > `drove remediating action` > `produced no-op`).
- [ ] False-positive rate is computed correctly per trigger reason.
- [ ] Saturation indicators apply the documented thresholds (30% rate, 4-event minimum).
- [ ] `regime_skip_emergency` per-invocation detail surfaces the prior label, new label, and skipped tier from `distillation_regime_state`.
- [ ] Missing regime-state rows produce "regime narrative unavailable" rather than failing.
- [ ] `--output json` produces a parseable JSON document mirroring the text structure with `summary`, `by_trigger_reason`, `per_invocation_detail`, and `recommendations` keys.
- [ ] An empty-window run reports "no emergency invocations in window" and exits 0.
- [ ] Recommendation entries cross-reference `verify_regime_transition.py` for `regime_skip_emergency` REVIEW verdicts.
- [ ] Classification rule tests cover the four buckets and the precedence rule.
- [ ] REVIEW threshold test produces the expected verdict and recommendation entry.
- [ ] Small-N suppression test produces HEALTHY for 1-of-1 no-op fixtures.
- [ ] The script exits 0 regardless of REVIEW verdicts; non-zero exit only on script-internal errors.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
