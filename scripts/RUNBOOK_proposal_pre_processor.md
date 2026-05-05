# Proposal Pre-Processor Verification Runbook

Operator guide for running `scripts/verify_proposal_pre_processor.py` — the
deterministic verify script for the proposal pre-processor (ALP-118 feature,
implemented across stories ALP-313 through ALP-319).

---

## When to run

- After `scripts/verify_analyst.py --save-fixtures` and
  `scripts/verify_strategist.py --save-fixtures` have produced their fixtures
  (`tests/fixtures/decision/{analyst,strategist}/*.json`).
- Whenever pre-processor source code changes
  (`src/alphamind/decision/proposal_pre_processor/`).
- Before merging any PR that touches the analyst or strategist output schemas
  (the pre-processor consumes both).

---

## Cost

**Zero.** No SDK calls. Sub-second total runtime across all four scenarios.

---

## Prerequisites

The analyst input fixtures must exist before running:

```
tests/fixtures/decision/analyst/normal.json
tests/fixtures/decision/analyst/halt.json
```

If either is missing, the script prints a clear error and exits 1. Produce them
with:

```bash
uv run python scripts/verify_analyst.py \
    --archive-root <archive-root> \
    --synthesizer-invocation-id <synth-inv-id> \
    --save-fixtures
```

The strategist output fixtures (`tests/fixtures/decision/strategist/*.json`) are
**not** required as inputs — this script constructs `StrategistOutput` in code
(per ALP-319 decision: strategist JSONs are not yet consistently emitted; they
are produced by the strategist's own verify script when it runs with
`--save-fixtures`).

---

## Invocation

```bash
# All four scenarios (default):
uv run python scripts/verify_proposal_pre_processor.py

# Single scenario:
uv run python scripts/verify_proposal_pre_processor.py --scenario normal
uv run python scripts/verify_proposal_pre_processor.py --scenario halt
uv run python scripts/verify_proposal_pre_processor.py --scenario emergency
uv run python scripts/verify_proposal_pre_processor.py --scenario normal_with_breach

# Override output directory (for testing):
uv run python scripts/verify_proposal_pre_processor.py --fixtures-dir /tmp/bundles

# Override analyst fixtures directory:
uv run python scripts/verify_proposal_pre_processor.py \
    --analyst-fixtures-dir /path/to/analyst/fixtures
```

---

## Expected output

```
=== Proposal pre-processor verification ===

  ✓ normal: PASS
  ✓ halt: PASS
  ✓ emergency: PASS
  ✓ normal_with_breach: PASS

Result: ALL PASS
```

On failure:

```
=== Proposal pre-processor verification ===

  ✓ normal: PASS
  ✗ halt: FAIL (halt: analyst_section.mode='normal' != 'watchlist')
  ...

Result: FAIL

Errors in 'halt':
  - halt: analyst_section.mode='normal' != 'watchlist'
```

Exit code 0 if all scenarios PASS; exit code 1 if any FAIL.

---

## What each scenario validates

| Scenario | Key assertions |
|----------|---------------|
| `normal` | No guardrail breaches; schema valid; all cross-field invariants hold |
| `halt` | `analyst_section.mode == "watchlist"`; `strategist_section.mode == "defensive_posture"`; `basis.analyst_proposal_ids == []` |
| `emergency` | At least one `WrappedPositionAssessment` has `remedy_flag` populated; passes through unchanged |
| `normal_with_breach` | `combined_set_impact.breaches` non-empty; `net_long_exposure` breached; each breach has non-empty `contributors[]` with at least one positive-contribution `REC-N` ID |

All scenarios also validate the produced bundle against `BUNDLE_OUTPUT_SCHEMA`
(Draft 2020-12 JSON Schema) and check cross-field invariants:
- `conviction_distribution.total == len(analyst recommendations)` (normal mode)
- `book_health_summary.total == len(strategist position assessments)`
- `basis.analyst_proposal_ids` members appear in `analyst_section.recommendations`
- `basis.strategist_action_ids` members come from non-hold position assessments

---

## Output artifacts

Each passing scenario writes a bundle JSON to:

```
tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json
```

These files are:
- Pretty-printed (`indent=2`) with sorted keys for diff stability.
- Checked into version control as the stable contract surface for downstream
  consumers (PM verify script, ALP-117).

---

## Failure-mode triage

| Failure mode | Most likely root cause | Next step |
|-------------|----------------------|-----------|
| `FileNotFoundError: Analyst fixture not found: tests/fixtures/decision/analyst/normal.json` | `verify_analyst.py --save-fixtures` not yet run | Run `verify_analyst.py` with `--save-fixtures`; confirm PASS before retrying |
| `schema validation FAIL: …` | A model or schema change broke the `ProposalPreProcessorBundle` JSON output | Check `src/alphamind/decision/proposal_pre_processor/models.py` for recent changes; re-run `uv run pytest tests/decision/proposal_pre_processor/ -n auto` |
| `halt: analyst_section.mode='normal' != 'watchlist'` | Halt fixture (`halt.json`) was not loaded correctly, or the halt strategist output was not set to `defensive_posture` | Confirm `tests/fixtures/decision/analyst/halt.json` has `"mode": "watchlist"`; check `build_fixture_halt_strategist_output` in the module |
| `emergency: no position assessment has remedy_flag populated` | `build_fixture_emergency_strategist_output` in the module was modified | Restore `remedy_flag` on at least one `PositionAssessment` in the emergency constructor |
| `normal_with_breach: expected a net_long_breach, got rules: []` | Snapshot `net_long_pct` not close enough to the 60% limit, or analyst recommendations too small | Check `build_fixture_portfolio_state_snapshot_near_limit()` — `net_long_pct` must be ≥ ~49% and the recommendations must add ≥ 11pp net-long delta |
| `run_proposal_pre_processor raised KeyError: 'POS-NVDA'` | `existing_positions` in the near-limit snapshot is missing an entry the conflict detector looks up | Ensure all positions referenced by the strategist output appear in `existing_positions` of the snapshot |
| `mirror-symmetry` invariant failures | The analyst or strategist fixture has an `invocation_id` mismatch | Verify that the analyst fixture's `invocation_id` is passed as the shared ID to the strategist output constructor |

---

## See also

- `tests/fixtures/decision/proposal_pre_processor/README.md` — fixture provenance
  and downstream consumers.
- `scripts/RUNBOOK_end_to_end_verification.md` — where the pre-processor fits in
  the full pipeline verification sequence.
- `src/alphamind/decision/proposal_pre_processor/` — source implementation.
- `docs/design/04-decision-layer/proposal-pre-processor.md` — design doc.
- `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` — schema.
