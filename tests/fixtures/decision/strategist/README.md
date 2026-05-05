# Strategist output fixtures

Canonical `StrategistOutput` JSON fixtures emitted by
`scripts/verify_strategist.py --save-fixtures` against a recorded
synthesizer archive. Three scenarios:

- `normal.json` — parsed strategist output from the normal-mode
  scenario. Four positions across three sectors (NVDA-semis-long,
  JPM-financials-long, XOM-energy-long, AAPL-tech-long) plus one
  pending order.
- `defensive_posture.json` — parsed strategist output from the
  defensive-posture scenario. Six positions across more sectors with
  daily drawdown at the halt threshold; activity log carries a recent
  engine-originated CLOSE on a sector-correlated position so the
  strategist's `engine_originated_closure_signal` discipline is
  exercised.
- `emergency.json` — parsed strategist output from the
  emergency-invocation scenario. Four positions in normal mode (no
  halt) with a regime-transition breach on one position (sized 4.5%,
  new regime limit 3.5%, overage 1.0 percentage points). The rendered
  guardrail header surfaces the EMERGENCY INVOCATION line.

All three files validate cleanly against
`alphamind.decision.strategist.models.StrategistOutput.model_json_schema()`.

### Current state (PR #22 land, 2026-05-05)

None of the three fixture JSONs have been emitted yet. The 2026-05-05 verification produced a real PASS on the normal scenario but the run halted at `defensive_posture` (fail-fast loop, since fixed) before reaching `--save-fixtures` for any scenario. The defensive_posture and emergency scenarios are blocked on follow-up work tracked at [ALP-311](https://linear.app/alphamind-jatassi/issue/ALP-311) (harness retry must preserve original user_message) and [ALP-312](https://linear.app/alphamind-jatassi/issue/ALP-312) (prompt clarity on `remedy_flag` scope). Once those land, re-run the verifier with `--save-fixtures` to emit all three.

## Provenance

Fixtures are produced by:

```bash
uv run python scripts/verify_strategist.py \
    --archive-root <archive-root> \
    --synthesizer-invocation-id <synth-inv-id> \
    --save-fixtures
```

Each fixture's `invocation_id` field records the strategist-side
invocation ID from the verify run that produced it; the fixture
file's mtime is the production timestamp. The synthesizer-text input
the strategist saw is recorded under
`<archive-root>/invocations/<synth-inv-id>/analysis/synthesizer/response.md`
in the same archive.

The three `StrategistView` fixtures are constructed in code by
`alphamind.scripts.verify_strategist.build_fixture_{normal,defensive_posture,emergency}_view`
per parent-issue ALP-116 decision (C). Per parent decision (B), the
three scenarios cover normal, defensive_posture, and
emergency-invocation paths.

## Intended consumers

These fixtures are the canonical strategist-output input for the
downstream feature trees' verifiers:

- `scripts/verify_proposal_pre_processor.py` (future feature work tree).
- `scripts/verify_portfolio_manager.py` (future feature work tree).

Each downstream verifier reads the appropriate scenario JSON directly
rather than chaining live strategist calls (per ALP-116 parent-issue
decision C — fixture-based composition, not live).

Tests under `tests/decision/proposal/` and
`tests/decision/portfolio_manager/` may also load these fixtures when
exercising downstream parsers / validators against realistic
strategist output.

## Refresh procedure

Re-run the verifier whenever the strategist's contract changes:

- The system prompt at `prompts/decision/strategist.md`.
- The `StrategistOutput` schema at
  `docs/design/04-decision-layer/strategist-output-schema.md` or its
  Pydantic expression at
  `src/alphamind/decision/strategist/models.py`.
- The input-bundle assembler at
  `src/alphamind/decision/strategist/input_bundle.py`.
- The Layer-2/3 validator at
  `src/alphamind/decision/strategist/validation.py`.
- The `StrategistView` shape at
  `src/alphamind/portfolio_state/consumers/strategist.py`.

Refresh procedure:

1. Run `scripts/verify_synthesizer.py --archive-root <DIR>
   --invocation-id <INV>` to produce a fresh synthesizer archive (or
   reuse an existing one if the synthesizer's contract is unchanged).
2. Run `scripts/verify_strategist.py --archive-root <DIR>
   --synthesizer-invocation-id <INV> --save-fixtures` to overwrite
   the three fixture files.
3. Confirm all three scenarios reported `PASS` (or, if `WARN`, that
   the warning is acceptable and the fixture is still usable for
   downstream consumers).
4. Run `uv run pytest -n auto` to confirm downstream consumers still
   parse the refreshed fixtures cleanly.

See `scripts/RUNBOOK_strategist.md` for the full operator runbook.
