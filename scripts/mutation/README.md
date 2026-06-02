# Scoped Mutation Testing — cosmic-ray

Per-module mutation testing for AlphaMind's pure-logic cores using
[cosmic-ray](https://cosmic-ray.readthedocs.io/).  Mutation testing
systematically injects small code changes (mutants) and checks whether the
test suite detects them.  It measures *test constraint strength*, not just
coverage: a line can be 100% covered but never actually pin the logic.

## Why cosmic-ray (not mutmut)

mutmut copies the whole source tree before mutating, which dies on this
repo's macOS/SMB artifact `/.VolumeIcon.icns`.  cosmic-ray mutates files
**in-place**, restoring them after each test run — no copy, no crash.

## Running

From the repo root:

```bash
./scripts/mutation/run_mutation.sh <module_key>
```

Supported module keys:

| Key | Source package | Test scope |
|---|---|---|
| `portfolio_state/computations` | `src/alphamind/portfolio_state/computations` | `tests/portfolio_state/computations/` |
| `execution/regt_margin_attribution` | `src/alphamind/execution/regt_margin_attribution` | `tests/execution/regt_margin_attribution/` |
| `execution/corporate_actions` | `src/alphamind/execution/corporate_actions` | `tests/execution/corporate_actions/` |
| `distillation/q7` | `src/alphamind/distillation/q7` | `tests/distillation/q7/test_compute.py` (pure-compute only) |
| `risk_guardrails/guardrail_evaluation` | `src/alphamind/risk_guardrails/guardrail_evaluation` | `tests/risk_guardrails/guardrail_evaluation/` |
| `risk_guardrails/breach_behavior` | `src/alphamind/risk_guardrails/breach_behavior` | `tests/risk_guardrails/breach_behavior/` |

The script:

1. Runs a baseline (unmutated) test pass to confirm the suite is green.
2. Initialises a cosmic-ray session database (`.sqlite`, git-ignored).
3. Executes all mutations **single-threaded** (`local` distributor — no
   parallel workers, no concurrency issues).
4. Writes a human-readable report to `scripts/mutation/reports/<module_key>.txt`
   (with `__` substituted for `/`).

cosmic-ray is invoked ephemerally via `uv run --with cosmic-ray ...` — it is
**not** added to `pyproject.toml` or `uv.lock`.

## Interpreting the report

```
[job-id] abc123
src/alphamind/.../foo.py  operator_name  2
worker outcome: survived, test outcome: survived
```

**Survived** — the mutant was not caught.  The tests do **not** constrain
that line's logic.  This is a signal that the test is weak or redundant for
that specific behaviour.

**Killed** — the mutant was caught (tests failed).  The tests *do* constrain
that line.

### Key statistics

At the end of each report:

```
total jobs: N
complete: N (100.00%)
surviving mutants: K (S%)
```

**Survival rate (S%)** — fraction of mutants not caught.  Lower is better.
A high survival rate on a pure-compute module means the tests exercise the
code paths but don't tightly pin the arithmetic or logic.

**Mutation score** = 1 − survival_rate = killed / total.  A score of 100%
means every mutant was caught; 0% means no mutants were caught despite
coverage.

### Operators

cosmic-ray applies standard Python operators:

- `NumberReplacer` — changes integer/float literals
- `BooleanReplacer` — flips `True`/`False`
- `ComparisonOperatorReplacement` — swaps `<` → `≤`, `==` → `!=`, etc.
- `ArithmeticOperatorReplacement` — swaps `+` → `-`, `*` → `/`, etc.
- `BinaryOperatorReplacement` — swaps `and`/`or` etc.
- `ExpressionDeletion` — removes statements

See `uv run --with cosmic-ray cosmic-ray operators` for the full list.

## Files committed

- `cr-template.toml` — TOML config template; per-run configs are generated
  from it and deleted after the run.
- `run_mutation.sh` — runner script.
- `README.md` — this file.
- `reports/*.txt` — baseline mutation reports (one per module).

## Files NOT committed (git-ignored)

- `*.sqlite` — cosmic-ray session databases generated at runtime.
- `*__*.toml` — per-run generated configs.

## Re-running

Re-runs overwrite `reports/<module>.txt`.  The `--force` flag is passed to
`cosmic-ray init` so existing session DBs are replaced.  Session DBs are
ephemeral and disposable.
