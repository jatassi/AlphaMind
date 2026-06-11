# Analyst output fixtures

Canonical `AnalystOutput` JSON fixtures emitted by
`scripts/verify/verify_analyst.py --save-fixtures` against a recorded
synthesizer archive. Two scenarios:

- `normal.json` — parsed analyst output from the normal-mode scenario.
  The full `recommendations[]` shape with `validate_guardrail` results,
  invalidation legs, conviction levels, etc.
- `halt.json` — parsed analyst output from the halt (watchlist) scenario.
  The watchlist-entry shape: ticker, sector, thesis_summary, conviction.

Both files validate cleanly against
`alphamind.decision.analyst.models.AnalystOutput.model_json_schema()`.

## Provenance

Fixtures are produced by:

```bash
uv run python scripts/verify/verify_analyst.py \
    --archive-root <archive-root> \
    --synthesizer-invocation-id <synth-inv-id> \
    --save-fixtures
```

Each fixture's `invocation_id` field records the analyst-side invocation
ID from the verify run that produced it; the fixture file's mtime is the
production timestamp. The synthesizer-text input the analyst saw is
recorded under
`<archive-root>/invocations/<synth-inv-id>/analysis/synthesizer/response.md`
in the same archive.

## Intended consumers

These fixtures are the canonical analyst-output input for the downstream
feature trees' verifiers:

- `scripts/verify/verify_strategist.py` (future feature work tree).
- `scripts/verify/verify_proposal_pre_processor.py` (future feature work tree).
- `scripts/verify/verify_portfolio_manager.py` (future feature work tree).

Each downstream verifier reads `normal.json` and `halt.json` directly
rather than chaining live analyst calls (per ALP-115 parent-issue
decision C — fixture-based composition, not live).

Tests under `tests/decision/strategist/`, `tests/decision/proposal/`,
and `tests/decision/portfolio_manager/` may also load these fixtures
when exercising downstream parsers / validators against realistic
analyst output.

## Refresh procedure

Re-run the verifier whenever the analyst's contract changes:

- The system prompt at `prompts/decision/analyst.md`.
- The `AnalystOutput` schema at
  `docs/design/04-decision-layer/analyst-output-schema.md` or its
  Pydantic expression at `src/alphamind/decision/analyst/models.py`.
- The input-bundle assembler at
  `src/alphamind/decision/analyst/input_bundle.py`.
- The Layer-2/3 validator at
  `src/alphamind/decision/analyst/validation.py`.

Refresh procedure:

1. Run `scripts/verify/verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>`
   to produce a fresh synthesizer archive.
2. Run `scripts/verify/verify_analyst.py --archive-root <DIR>
   --synthesizer-invocation-id <INV> --save-fixtures` to overwrite the
   two fixture files.
3. Confirm both scenarios reported `PASS` (or, if `WARN`, that the
   warning is acceptable and the fixture is still usable for downstream
   consumers).
4. Run `uv run pytest -n auto` to confirm downstream consumers still
   parse the refreshed fixtures cleanly.

See `scripts/RUNBOOK_analyst.md` for the full operator runbook.
