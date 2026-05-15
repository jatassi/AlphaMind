# Guardrail Enforcement Verification Runbook

Operator workflow for the ALP-398 verification artifact that proves the Phase 1
guardrail-enforcement layer (ALP-125) is internally consistent: the composition
primitive translates regime-resolved parameters + cumulative-drawdown into the
canonical `(ActiveRiskParameterSet, DrawdownTier | None)` tuple, the
orchestrator bundles this into a `Phase1EnforcementResult`, the repository
provider exposes the bundle to `SqlPortfolioStateRepository`, and the assembler
surfaces the composed parameters at `snapshot.active_risk_parameters`.

Run after any change to `src/alphamind/execution/guardrail_enforcement/`,
`config/guardrails.yaml` (cumulative-drawdown progressive tiers), or the
assembler's risk-parameters seam.

## Purpose

`scripts/verify_guardrail_enforcement.py` is a pure in-process integration check
against a freshly-migrated SQLite DB. No SDK calls; no broker contact;
sub-second runtime. It exercises four phases plus a summary:

- **Phase 1 — composition primitive.** Drives `compose_active_risk_parameters`
  across the four documented tier cases (no-tier / `CONSTRAINED` /
  `HEAVILY_CONSTRAINED` / `FULL_HALT`); each case asserts the returned
  `(ActiveRiskParameterSet, DrawdownTier | None)` tuple matches expectations.
  Tier triggers are loaded from `config/guardrails.yaml` so the verify script
  consumes the same canonical tier sequence the production orchestrator reads
  at runtime.
- **Phase 2 — orchestrator.** Wraps the baseline parameter set in a synthetic
  `RegimeAdaptationOutput` and pairs it with a `DrawdownState` for each tier
  case; calls `compose_phase_1_enforcement` and asserts the bundled
  `Phase1EnforcementResult` carries the expected tier and parameters that
  match the underlying primitive's output (sanity check the orchestrator
  does not strip or duplicate primitive output).
- **Phase 3 — repository provider.** Builds a `SqlPortfolioStateRepository`
  against the freshly-migrated DB using `make_active_risk_parameters_provider`
  for the `active_risk_parameters_provider` slot; calls
  `repository.get_active_risk_parameters()` and asserts the returned set is
  identical (object identity) to the bundled result's
  `active_risk_parameters`.
- **Phase 4 — assembler integration.** Uses the same repository to build a
  portfolio-state snapshot via `assemble_snapshot`; asserts
  `snapshot.active_risk_parameters` equals the composed result. The
  assembler enriches `parameter_change_flag` from the prior provider — when
  no earlier invocation row exists (Phase 4's seeded single-invocation
  configuration), the prior context returns `None` and the enrichment is a
  no-op, so equality on the full set holds.
- **Summary** — one `[PASS]` / `[FAIL]` line per phase plus a
  `RESULT: PASS (N/N criteria)` or
  `RESULT: FAIL (M/N criteria, failing: <labels>)` summary.

## Prerequisites

1. **`uv sync` completed** — `uv run` is the entry point.
2. The work tree's integration branch (`jackson/alp-125-guardrail-enforcement-layer`)
   must be reachable from HEAD (predecessor stories ALP-394 / ALP-395 / ALP-396
   for the composition primitive, orchestrator, and repository provider).
3. A freshly-migrated SQLite DB. The script does NOT migrate; it relies on the
   durable substrate's tables existing. Two recipes:

   **Alembic chain (canonical for production-mirror runs):**

   ```bash
   mkdir -p /tmp/alphamind-verify-guardrail
   rm -f /tmp/alphamind-verify-guardrail/alphamind.db
   uv run alembic -c alembic.ini -x db=/tmp/alphamind-verify-guardrail/alphamind.db upgrade head
   ```

   The `-x db=<path>` arg is the env.py contract (see
   `src/alphamind/persistence/migrations/env.py`); plain `-x db_url=…` is
   ignored and the migration silently writes to the default path.

   **In-process create_all (canonical for ad-hoc runs):**

   ```python
   from pathlib import Path
   import alphamind.state.tables  # noqa: F401
   from alphamind.persistence.models import Base
   from alphamind.persistence.session import make_engine
   db_path = Path("/tmp/alphamind-verify-guardrail.db")
   engine = make_engine(str(db_path))
   Base.metadata.create_all(engine)
   engine.dispose()
   ```

   Both produce a DB whose state-persistence tables are at the migration head;
   the verify script's Phase 3 and Phase 4 each assert their required-table
   subset is present and FAIL with `missing required tables: <list>` on a
   partial schema.

   On macOS dev, point `--db-path` at the SMB-mounted production DB at
   `/Volumes/Users/jacks/AlphaMind/data/alphamind.db` only after taking a
   local snapshot per `RUNBOOK_end_to_end_verification.md` § Prerequisites
   (the production DB's WAL trio doesn't cooperate with concurrent SMB
   readers). On the Windows production server, point at
   `%USERPROFILE%\AlphaMind\data\alphamind.db` directly — same machine as
   the writer, no snapshot needed.
4. No live market data, broker connection, or Anthropic SDK token needed.

## Invocation

```bash
uv run python scripts/verify_guardrail_enforcement.py --db-path /tmp/alphamind-verify-guardrail.db
```

CLI flags:

- `--db-path PATH` — Path to the freshly-migrated SQLite DB. When omitted,
  the script falls back to the standard resolution chain (`DATABASE_PATH`
  env var, then `config/main.yaml`'s `paths.database` key) per
  `src/alphamind/persistence/session.py`.
- `--output {text,json}` — Output format. `text` (default) prints a
  human-readable per-phase block; `json` emits a structured payload
  suitable for downstream automation.

Exit code: `0` on full pass, `1` on any phase failure.

Expected runtime under 1 second (no live SDK calls, no network IO). On a
development laptop the typical run completes in well under a second.

## Expected output

A clean run prints (exit code 0):

```
======================================================================
AlphaMind Guardrail Enforcement Verification
======================================================================
  [PASS] Phase 1 — composition primitive
  [PASS] Phase 2 — orchestrator
  [PASS] Phase 3 — repository provider
  [PASS] Phase 4 — assembler integration
======================================================================
RESULT: PASS (4/4 criteria)
======================================================================
```

On any phase failure the script exits 1, the failed phase prints `[FAIL]`
followed by an indented diagnostic line, and the summary reads
`RESULT: FAIL (M/N criteria, failing: <labels>)`.

The `--output json` mode emits:

```json
{
  "all_pass": true,
  "phases": [
    {"label": "Phase 1 — composition primitive", "ok": true, "detail": null},
    {"label": "Phase 2 — orchestrator", "ok": true, "detail": null},
    {"label": "Phase 3 — repository provider", "ok": true, "detail": null},
    {"label": "Phase 4 — assembler integration", "ok": true, "detail": null}
  ]
}
```

## Failure-mode triage

| Phase | Typical failure | Diagnostic | Likely fix |
|---|---|---|---|
| 1 — composition primitive | `tier 1 / CONSTRAINED: tier=<X>, expected DrawdownTier.CONSTRAINED` | The cumulative-drawdown classifier no longer maps the first non-halt tier's `trigger_pct` to `CONSTRAINED`, or `config/guardrails.yaml`'s `progressive_tiers` lost a non-halt entry. | Re-read `src/alphamind/risk_guardrails/breach_behavior/drawdown_tiers.py § classify_cumulative_drawdown_tier` against `docs/design/06-risk-guardrails/breach-behavior.md § Cumulative drawdown response`; cross-check the shipped tiers in `config/guardrails.yaml`. |
| 1 — composition primitive | `no-tier (zero drawdown): parameters not pass-through` | `compose_active_risk_parameters` no longer returns the input parameters by identity when `current_drawdown_pct == 0`. | Re-read `src/alphamind/risk_guardrails/breach_behavior/drawdown_tiers.py § apply_progressive_tier_overrides` — when `tier is None`, the function returns `active_risk_parameters` unchanged. |
| 1 — composition primitive | `tier 3 / FULL_HALT: missing overlay tag 'cumulative_drawdown_tier_3' in (...)` | The `FULL_HALT` branch no longer appends the canonical overlay tag. | Re-read `apply_progressive_tier_overrides`'s `_extend_overlays` helper. |
| 2 — orchestrator | `<case>: orchestrator returned 'X', expected Phase1EnforcementResult` | The orchestrator's return type regressed. | Re-read `src/alphamind/execution/guardrail_enforcement/orchestrator.py § compose_phase_1_enforcement`. |
| 2 — orchestrator | `<case>: drawdown_tier=<X>, expected <Y>` | The orchestrator's tier extraction disagrees with the underlying primitive (most likely the orchestrator started discarding `tier` from the primitive's return tuple). | Re-read `compose_phase_1_enforcement` — both fields of the primitive's return tuple must thread into the bundled `Phase1EnforcementResult`. |
| 2 — orchestrator | `<case>: orchestrator parameters disagree with composition primitive output` | The orchestrator started post-processing the primitive's parameters (stripping overlays, re-applying overrides, …). | Re-read `compose_phase_1_enforcement` — the orchestrator must be a thin wrapper. |
| 3 — repository provider | `missing required tables: invocations` | The DB's schema is partial — most often the operator pointed `--db-path` at a stale DB or skipped the migration step. | Re-run the migration recipe from § Prerequisites and retry. |
| 3 — repository provider | `repository.get_active_risk_parameters() did not return the result's active_risk_parameters by identity` | `make_active_risk_parameters_provider`'s closure stopped returning `result.active_risk_parameters` (most likely a defensive `model_copy`), or `SqlPortfolioStateRepository.get_active_risk_parameters` started post-processing the provider's output. | Re-read `src/alphamind/execution/guardrail_enforcement/repository_provider.py` and `src/alphamind/execution/state_persistence/repository/sql_repository.py § get_active_risk_parameters`. |
| 4 — assembler integration | `missing required tables: <list>` | A schema drop affected one of the tables `assemble_snapshot` reads (positions, theses, orders, brackets, cash_ledger, drawdown_state, fill_records, corporate_action_integration_ledger). | Re-run the migration recipe from § Prerequisites and retry. |
| 4 — assembler integration | `assemble_snapshot raised RepositoryConsistencyError: …` | The seeded invocation's `phase1_completed_at` is null relative to `now` (clock skew on the dev box) or the cash-ledger / drawdown singleton seed failed. | Confirm the dev box's clock is consistent with UTC; re-run after dropping the DB. |
| 4 — assembler integration | `snapshot.active_risk_parameters does not match the composed Phase1EnforcementResult.active_risk_parameters` | The assembler's `parameter_change_flag` enrichment started flipping `False → True` even with no prior, OR a regression elsewhere in the assembler started post-processing the provider's output (overlays stripped, regime label rewritten, …). | Re-read `src/alphamind/portfolio_state/assembler.py` § Step 14 and `src/alphamind/portfolio_state/computations/risk_budget.py § compute_parameter_change_flag` — the prior-None branch must return False. |

## Operational caveats

**The verify script does NOT auto-migrate.** Mirroring the
`verify_state_persistence` convention, this script asserts the schema is at
head and fails Phases 3 + 4 with `missing required tables: <list>` if any
required table is absent. The runbook's two migration recipes (Alembic chain
and in-process `Base.metadata.create_all`) produce equivalent fresh-on-disk
DBs; the in-process recipe is faster for ad-hoc runs.

**Phase 4 prior-context branch is degenerate by design.** With a single seeded
invocation, the SQL repository's prior-invocation lookup returns `None` and
the assembler's `parameter_change_flag` enrichment is a no-op (flag stays
`False`). Equality on the full parameter set holds in this configuration. If
a future story adds richer fixtures (multi-invocation parity), the equality
check may need to compare on the flag-normalized set instead — surface the
ambiguity to the orchestrator before relaxing it.

**Tier triggers come from `config/guardrails.yaml`, not from
`config/breach_behavior.yaml`.** The composition primitive consumes the
`progressive_tiers` field on the `cumulative_drawdown_pct` rule entry under
`GuardrailsConfig`. A common source of confusion: the ALP-398 story body's
reading-list reference to `load_breach_behavior_config(...).progressive_tiers`
is mistaken — the canonical loader is `GuardrailsConfig.model_validate(...)`.

**Idempotent on re-runs against the same DB.** The Phase 4 invocation,
process-lifetime, and singleton seeders are all idempotent — operators can
re-run the script against the same DB without dropping the schema first.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook; this
  script runs as the guardrail-enforcement phase between distillation
  (which produces the regime label) and the decision-layer agents (which
  consume `snapshot.active_risk_parameters`).
- `scripts/verify_state_persistence.py` + `RUNBOOK_state_persistence.md`
  — sibling pattern; Phase F there exercises the same SQL repository's
  read parity, and Phase 4 here mirrors its assembler-invocation shape.
- `scripts/verify_oms_commands.py` + `RUNBOOK_oms_commands.md`
  — sibling shim/implementation/test layout; same shape, swap names.
- `docs/design/05-execution-layer/architecture.md` § 3. Guardrail enforcement
  layer — design reference for the composition primitive + orchestrator.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown
  response — design reference for the progressive-tier semantics.
- `config/guardrails.yaml` — the shipped tier triggers Phase 1 and Phase 2 read.
- ALP-125 parent issue — work-tree overview, dependency graph, pre-resolved
  decisions.
- ALP-394 / ALP-395 / ALP-396 — predecessor stories (composition primitive,
  orchestrator, repository provider) whose deliverables this script
  integrates.
- `src/alphamind/execution/guardrail_enforcement/` — the package this script
  exercises end-to-end.
- `tests/scripts/test_verify_guardrail_enforcement.py` — unit tests for this
  script's phase functions; run with
  `uv run pytest tests/scripts/test_verify_guardrail_enforcement.py -n auto`.
