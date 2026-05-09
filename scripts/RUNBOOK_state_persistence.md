# State-Persistence Verification Runbook

Operator workflow for the ALP-367 verification artifact that proves the
state-persistence work tree (ALP-119) is internally consistent: the durable
substrate's tables exist, the transactional `InvocationContext` commits and
rolls back atomically, the Phase 1 fill-integration path drains an unprocessed
fill end-to-end, the Phase 2 command-execution path persists an accepted
envelope, the Phase 2 Layer-1 parse-failure path populates both the in-memory
diagnostic log and the SQL `envelope_parse_failed` activity log entry, and the
`SqlPortfolioStateRepository` reads back what was written via `assemble_snapshot`.

Run after any change to `src/alphamind/execution/state_persistence/`,
`src/alphamind/execution/oms/submit_envelope_mcp.py`, or any of the table
schemas the substrate depends on.

## Purpose

`scripts/verify_state_persistence.py` is a pure in-process integration check
against a freshly-migrated SQLite DB. No SDK calls; no broker contact;
sub-second runtime. It exercises six phases:

- **Phase A** — schema. Inspects `sqlite_master` and confirms every
  state-persistence table exists.
- **Phase B** — invocation context. Opens an `InvocationContext` cleanly and
  asserts the row persists; opens another with an injected exception inside
  and asserts the row rolls back.
- **Phase C** — Phase 1 write path. Seeds an order, position, bracket,
  thesis, cash-ledger singleton, and drawdown-state singleton; appends an
  unprocessed entry fill via `append_fill_record`; runs
  `process_unprocessed_fills(handle)`; asserts the fill transitioned to
  `processed`, the position transitioned PENDING → OPEN, and the activity
  log carries `order_filled`, `position_opened`, `bracket_activated`, and
  `cash_debited` entries.
- **Phase D** — Phase 2 envelope writeback. Constructs a synthetic
  `PMAnalystEnvelope` carrying one OPEN command, runs
  `persist_envelope_outcome`, asserts the new position + thesis + bracket +
  PENDING orders persist and the activity log carries `order_submitted`,
  `thesis_created`, `capital_reserved`, and `pm_decision`.
- **Phase E** — Phase 2 Layer-1 parse failure. Submits a malformed
  payload, mirrors the engine-stub's two side effects (in-memory
  `failed_submission_log` append plus `persist_envelope_parse_failure`
  SQL writeback), and asserts both surfaces carry the failure with
  `raw_args_json` and `validation_error_repr` populated.
- **Phase F** — Repository read parity. Builds a
  `SqlPortfolioStateRepository` against a Phase-1-committed invocation,
  runs `assemble_snapshot(repo, ...)` against it, asserts the snapshot
  surfaces the open position Phase C wrote. Then points the repo at an
  invocation whose `phase1_completed_at` is NULL and asserts
  `RepositoryConsistencyError` fires.

## Prerequisites

1. **`uv sync` completed** — `uv run` is the entry point.
2. The work tree's integration branch
   (`jackson/alp-119-state-persistence`) must be reachable from HEAD (all
   eight predecessor stories landed: ALP-354 through ALP-366).
3. A freshly-migrated SQLite DB. The script does NOT migrate; it asserts the
   schema is at head and fails Phase A if a state-persistence table is
   missing. To produce one for ad-hoc verification:

   ```python
   from pathlib import Path
   import alphamind.execution.state_persistence.tables  # noqa: F401
   from alphamind.persistence.models import Base
   from alphamind.persistence.session import make_engine

   db_path = Path("/tmp/alphamind-verify.db")
   engine = make_engine(str(db_path))
   Base.metadata.create_all(engine)
   engine.dispose()
   ```
4. No live market data, broker connection, or Anthropic SDK token needed.

## Verification command

```bash
uv run python scripts/verify_state_persistence.py --db-path /tmp/alphamind-verify.db
```

CLI flags:

- `--db-path PATH` — Path to the freshly-migrated SQLite DB. When omitted,
  the script falls back to the standard resolution chain (`DATABASE_PATH`
  env var, then `config/main.yaml`'s `paths.database` key) per
  `src/alphamind/persistence/session.py`.
- `--output {text,json}` — Output format. `text` (default) prints a
  human-readable per-phase block; `json` emits a structured payload suitable
  for downstream automation.

Exit code: `0` on full pass, `1` on any phase failure.

## Expected output

A clean run prints (exit code 0):

```
======================================================================
AlphaMind State-Persistence Verification
======================================================================
  Phase A — schema                                        PASS
  Phase B — invocation context                            PASS
  Phase C — Phase 1 write path                            PASS
  Phase D — Phase 2 envelope                              PASS
  Phase E — Layer-1 parse failure                         PASS
  Phase F — repository read parity                        PASS
======================================================================
ALL PHASES PASS
======================================================================
```

On any phase failure, the script exits 1, the failed phase prints `FAIL`
followed by an indented diagnostic line (e.g., `missing tables: positions`),
and the trailer reads `FAIL: N phase(s) failed: <labels>`.

The `--output json` mode emits:

```json
{
  "all_pass": true,
  "phases": [
    {"label": "Phase A — schema", "ok": true, "detail": null},
    {"label": "Phase B — invocation context", "ok": true, "detail": null},
    {"label": "Phase C — Phase 1 write path", "ok": true, "detail": null},
    {"label": "Phase D — Phase 2 envelope", "ok": true, "detail": null},
    {"label": "Phase E — Layer-1 parse failure", "ok": true, "detail": null},
    {"label": "Phase F — repository read parity", "ok": true, "detail": null}
  ]
}
```

## Failure-mode triage

| Phase | Typical failure | Diagnostic | Likely fix |
|---|---|---|---|
| A — schema | `missing tables: <name>` | `sqlite3 <db> ".tables"` to confirm; check that `alphamind.execution.state_persistence.tables` was imported before `Base.metadata.create_all`. | Ensure the side-effect import lands; if a single table is missing, re-run the schema setup. |
| B — invocation context | `clean-exit invocation row did not persist` or `rollback probe persisted the invocation row` | `InvocationContext` regression in `src/alphamind/execution/state_persistence/invocation_context/context.py` (commit/rollback path). | Re-read `__aenter__` / `__aexit__` for missing `await session.commit()` or missing `await session.rollback()`. |
| C — Phase 1 write path | `expected 1 fill processed, got 0` (FK violation under the hood) | Inspect the `fill_records` table to confirm the fill landed; check the `_quarantine_invalid` branch isn't filtering it. | Confirm the seeded order/position/bracket FK chain; usually a missing `originating_thesis_id` on the seed order. |
| C — Phase 1 write path | `position status is 'PENDING'` | The Phase 1 entry-fill apply path failed to transition; check `_apply_entry_fill` and the position codec round-trip. | Look for a regression in `src/alphamind/execution/state_persistence/write_paths/phase1.py` § `_apply_entry_fill`. |
| C — Phase 1 write path | `activity log missing required events: <list>` | One of the lifecycle emitters (`_emit_fill_activity_log_entries`) didn't fire. | Re-read `_emit_fill_activity_log_entries`; usually a guard clause skipped the event. |
| D — Phase 2 envelope | `expected 1 PENDING_ENTRY bracket for <position>, got 0` | The `_writeback_open` path silently swallowed an INSERT failure. | Inspect `src/alphamind/execution/state_persistence/write_paths/phase2.py` § `_writeback_open`; confirm the bracket codec round-trip succeeds. |
| D — Phase 2 envelope | `activity log missing required events: <list>` | One of `ORDER_SUBMITTED`/`THESIS_CREATED`/`CAPITAL_RESERVED`/`PM_DECISION` failed to emit. | Re-trace the emission helpers in phase2.py; usually a missing `await _emit(...)`. |
| E — Layer-1 parse failure | `expected exactly 1 envelope_parse_failed row, got 0` | `persist_envelope_parse_failure` did not write through; OR the activity_log INSERT was filtered by a CHECK constraint. | Check the `envelope_parse_failed` migration (`tests/execution/state_persistence/test_activity_log_envelope_parse_failed_migration.py`); inspect the CHECK extension. |
| E — Layer-1 parse failure | `envelope_parse_failed detail missing raw_args_json` | The `EnvelopeParseFailedDetail` schema dropped a field. | Re-read `src/alphamind/portfolio_state/events/activity_log.py` § `EnvelopeParseFailedDetail`. |
| F — repository read parity | `assemble_snapshot raised RepositoryConsistencyError` | The committed invocation row's `phase1_completed_at` was not stamped, or the FK chain has a gap. | Confirm Phase C ran first against the same DB, and that Phase F's seeded invocation record carries `phase1_completed_at`. |
| F — repository read parity | `snapshot.open_positions is empty` | Phase C didn't run, OR the SQL repo's `get_open_positions` returns nothing despite a seeded OPEN row. | Inspect `SqlPortfolioStateRepository.get_open_positions`; confirm `PositionStatus.OPEN.value` matches the row's stored status. |
| F — repository read parity | `expected RepositoryConsistencyError when reading current_invocation_metadata` | The snapshot-isolation guard in `SqlPortfolioStateRepository.get_current_invocation_metadata` regressed (no longer raises on `phase1_completed_at IS NULL`). | Re-read that method; the guard is the snapshot isolation contract. |

## Operational caveats

**Fill-integration path scope.** Per ALP-365's narrowing, only the
long-equity + stock-split paths are wired in Phase 1. Options-strategy fills,
SHORT-side entry fills, and other corporate-action types raise
`NotImplementedError` from `process_unprocessed_fills` if an unprocessed fill
of the unsupported kind is seen. This is by design at the v1 substrate;
production callers must filter the fill stream upstream of Phase 1 until the
follow-up stories land.

**`persist_command_abandoned` is engine-side only.** The function exists,
is exported, and is unit-tested for its post-rollback emission contract,
but the engine-stub `_handle_submit_envelope` does not call it — the stub
assumes broker success. The live broker-failure wire belongs to ALP-120
(the real OMS submission engine). Phase D / Phase E exercise the
already-wired persistence paths; do not extend them to assert against
`COMMAND_ABANDONED` until the engine wire lands.

**`get_recent_thesis_resolutions` lookback parameter.** Per ALP-364's note,
`SqlPortfolioStateRepository.get_recent_thesis_resolutions(lookback_trading_days=...)`
accepts the parameter for Protocol parity but does NOT filter on it — the
trading-calendar primitive that would enable a date-windowed SQL filter
lives in the data layer and is not yet wired through. The resolved-thesis
registry is bounded by Phase 1 retention so returning all resolved theses
is correct for the v1 snapshot. Relying callers should not assume the
lookback caps the result set.

**Deferred entities.** Per the ALP-119 parent issue's Pre-resolved decision
(B), the substrate intentionally omits `agent_calls`, `saved_queries`, and
the feedback-loop entities. Phase A asserts only the in-scope tables exist;
do not extend `_STATE_PERSISTENCE_TABLES` in `src/alphamind/scripts/verify_state_persistence.py`
until those entities ship in their own follow-up work tree.

**`command_type` on `CommandAbandonedDetail` is load-bearing.** Per ALP-355,
the `CommandAbandonedDetail` extension carries the originating command type
through to the strategist projection. A regression here surfaces as a
strategist-side parse error, not as a Phase E failure. Cross-check
`tests/portfolio_state/consumers/test_strategist*.py` if Phase E passes but
strategist tests fail.

**Submission-log file artifacts coexist with SQL persistence.** The PM
diagnostic archive at `<archive-root>/invocations/<inv-id>/decision/portfolio_manager/{submission_log,failed_submission_log}.json`
(produced by ALP-353) is operator-facing forensics — the SQL `activity_log`
entries Phase E + Phase D verify are the queryable analytics surface. Both
must populate consistently for the same invocation; a divergence (one
populated, the other not) is a regression in the engine-stub's dual-write
path. The verify script asserts the SQL surface; cross-check the file
artifacts manually if the two ever drift.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook; this
  script runs as the State-persistence phase before any decision-layer
  pipeline-composition phase that consumes durable state.
- `scripts/verify_bootstrap.py` — extended in this story to include the
  state-persistence tables in `ALL_TABLES` so a bootstrap pass covers both
  the data-layer schema and the durable substrate.
- `docs/design/05-execution-layer/state-persistence.md` — design doc the
  verification asserts conformance to.
- ALP-119 parent issue — work-tree overview, dependency graph,
  pre-resolved decisions.
- ALP-354 through ALP-366 — predecessor stories whose deliverables this
  script integrates.
- `src/alphamind/execution/state_persistence/` — substrate package.
- `src/alphamind/execution/oms/submit_envelope_mcp.py` — the engine-stub
  whose Layer-1 parse-failure dual-write Phase E asserts.
- `tests/scripts/test_verify_state_persistence.py` — unit tests for this
  script's phase functions; run with `uv run pytest
  tests/scripts/test_verify_state_persistence.py -n auto`.
