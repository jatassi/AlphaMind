# Proposal pre-processor output fixtures

Canonical `ProposalPreProcessorBundle` JSON fixtures emitted by
`scripts/verify/verify_proposal_pre_processor.py`. Four scenarios:

- `normal.json` — normal analyst mode (empty recommendations, all-hold
  strategist assessments) against a low-utilisation portfolio snapshot. No
  guardrail breaches expected.
- `halt.json` — watchlist analyst mode (six watchlist entries) paired with a
  defensive-posture strategist output. `analyst_section.mode == "watchlist"`,
  `strategist_section.mode == "defensive_posture"`,
  `basis.analyst_proposal_ids == []`.
- `emergency.json` — normal analyst mode paired with an emergency-invocation
  strategist output that carries a `remedy_flag` on the NVDA position
  (regime-transition breach). The flag passes through unchanged into §2's
  wrapped records.
- `normal_with_breach.json` — normal analyst mode with three LONG equity
  recommendations against a portfolio snapshot at `net_long_pct = 49.5`
  (near the 60% limit). The combined set pushes net long past the limit;
  `combined_set_impact.breaches` is non-empty, each breach has non-empty
  `contributors[]` including positive-contribution `REC-N` IDs.

All four files validate cleanly against
`alphamind.decision.proposal_pre_processor.models.BUNDLE_OUTPUT_SCHEMA`.

## Provenance

Fixtures are produced by:

```bash
uv run python scripts/verify/verify_proposal_pre_processor.py
```

Or for a single scenario:

```bash
uv run python scripts/verify/verify_proposal_pre_processor.py --scenario normal
```

The analyst input fixtures (`tests/fixtures/decision/analyst/normal.json` and
`halt.json`) must exist before running — produce them with
`scripts/verify/verify_analyst.py --save-fixtures` if missing. Strategist outputs are
constructed in-code by
`alphamind.scripts.verify_proposal_pre_processor.build_fixture_{normal,halt,emergency}_strategist_output`.

## Intended consumers

These fixtures are the canonical proposal pre-processor output for the
downstream feature trees' verifiers:

- `scripts/verify/verify_portfolio_manager.py` (PM work tree, ALP-117) — the PM
  agent's verify script will read the bundle JSONs here as its primary inputs.

The files are checked into version control to provide a stable contract surface
for downstream consumers: the PM verify script can read them without re-running
the pre-processor itself.

## Schema

Each bundle JSON validates against
`alphamind.decision.proposal_pre_processor.models.ProposalPreProcessorBundle.model_json_schema()`.
The schema is exported at `BUNDLE_OUTPUT_SCHEMA` in the same module.

Top-level shape:
```json
{
  "invocation_id": "...",
  "timestamp": "...",
  "aggregate_observations": {
    "combined_set_impact": { "basis": {...}, "per_rule": [...], "breaches": [...] },
    "conviction_distribution": { "by_level": {...}, "total": 0 },
    "book_health_summary": { ... }
  },
  "strategist_section": { "mode": "normal|defensive_posture", "position_assessments": [...], ... },
  "analyst_section": { "mode": "normal|watchlist", "recommendations": [...] | null, "watchlist": [...] | null }
}
```

## Refresh procedure

Re-run the verifier whenever the pre-processor's contract changes:

1. Confirm analyst fixtures exist:
   `ls tests/fixtures/decision/analyst/normal.json tests/fixtures/decision/analyst/halt.json`
2. Run `uv run python scripts/verify/verify_proposal_pre_processor.py` to overwrite
   all four fixture files.
3. Confirm all four scenarios reported PASS.
4. Run `uv run pytest -n auto` to confirm downstream consumers still parse the
   refreshed fixtures cleanly.

See `scripts/verify/RUNBOOK_proposal_pre_processor.md` for the full operator runbook.
