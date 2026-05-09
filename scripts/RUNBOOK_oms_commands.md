# OMS Commands Verification Runbook

Operator workflow for the ALP-376 verification artifact that proves the OMS
commands work tree (ALP-120) is internally consistent: the canonical Pydantic
command shapes round-trip, the command-ID derivation utility produces the
documented PM-originated and engine-originated patterns, the engine-stub
`submit_envelope` MCP wrapper writes through canonical commands to Phase 2
with **real** dollar values (no stub constants), and the monitor-facing
`submit_engine_envelope` write function persists protective CLOSEs with
engine-guardrail provenance threaded through to the activity log.

Run after any change to `src/alphamind/execution/oms/`,
`src/alphamind/execution/state_persistence/write_paths/phase2.py`, or the
PM envelope schema at `src/alphamind/decision/portfolio_manager/models.py`.

## Purpose

`scripts/verify_oms_commands.py` is a pure in-process integration check
against a freshly-migrated SQLite DB. No SDK calls; no broker contact;
sub-minute runtime. It exercises four phases plus a summary:

- **Phase 1** — canonical model round-trip. Constructs one of every OMS
  command variant (OPEN / CLOSE / ADJUST / CANCEL / ADD) plus an
  `EngineEnvelope`, round-trips each via the canonical
  `TypeAdapter[OMSCommand]` / `TypeAdapter[EngineEnvelope]`. CLOSE covers
  every `close_rationale_type`; ADJUST covers every change-field variant.
- **Phase 2** — command-ID utility. Calls `derive_pm_command_id` against
  the worked example from `oms-command-ids.md`; calls
  `derive_engine_command_id` for the `MON.{session}.{trigger}.{ord}` shape;
  parse-round-trips both via `parse_pm_command_id` / `parse_engine_command_id`;
  computes `attempt_seq` from a synthesized `PMEnvelope` carrying one
  `pre_submission` plus two `post_rejection` modifications and asserts
  `compute_attempt_seq` returns `2`.
- **Phase 3** — PM envelope path against a fresh DB. Constructs a canonical
  `PMAnalystEnvelope` with one OPEN command (real `dollar_value=$5,000`,
  `quantity=5`), opens an `InvocationContext`, builds an MCP server via
  `build_submit_envelope_mcp_server` bound to that handle, and calls the
  `submit_envelope` tool. Asserts: per-command result is `accepted` with a
  PM-pattern `command_id`; the persisted `capital_reserved` activity-log
  entry's `amount_usd` equals the real $5,000 (proves the retired `$1k`
  stub from story 03 / ALP-374 is gone); the `cash_ledger` row's
  `reserved_capital_usd` equals the same $5,000; the bracket carries one
  TAKE_PROFIT leg + one leg per `command.invalidation_legs`; the thesis
  carries at least one component per wire-side `command.thesis.components`;
  the activity log carries `order_submitted` + `thesis_created` +
  `capital_reserved` + `pm_decision` entries.
- **Phase 4** — engine envelope path against the same DB. Constructs an
  `EngineEnvelope` carrying one CLOSE on the position Phase 3 opened,
  drives it through `submit_engine_envelope`. Asserts: result is
  `accepted` with an engine-pattern `command_id`
  (`MON.{session}.{trigger}.{ord}`); one `order_submitted` activity-log
  entry lands whose `order_parameters_json` detail carries
  `risk_management_subtype="engine_guardrail"`,
  `position_selection_rationale=<trigger record's rationale>`, and
  `rule_breached=<trigger record's rule>`.
- **Phase 5** (Summary) — one `[PASS]` / `[FAIL]` line per phase plus a
  `RESULT: PASS (N/N criteria)` or
  `RESULT: FAIL (M/N criteria, failing: <labels>)` summary.

## Prerequisites

1. **`uv sync` completed** — `uv run` is the entry point.
2. The work tree's integration branch (`jackson/alp-120-oms-commands`) must
   be reachable from HEAD (all five predecessor stories landed: ALP-370
   through ALP-375).
3. A freshly-migrated SQLite DB. The script does NOT migrate; it relies
   on the durable substrate's tables existing. The canonical setup runs
   the Alembic migration chain against a temp DB:

   ```bash
   mkdir -p /tmp/alphamind-verify-oms
   rm -f /tmp/alphamind-verify-oms/alphamind.db
   uv run alembic -c alembic.ini -x db=/tmp/alphamind-verify-oms/alphamind.db upgrade head
   ```

   The `-x db=<path>` arg is the env.py contract (see
   `src/alphamind/persistence/migrations/env.py`); plain `-x db_url=…` is
   ignored and the migration silently writes to the default path.

   On macOS dev, point `--db-path` at the SMB-mounted production DB at
   `/Volumes/Users/jacks/AlphaMind/data/alphamind.db` only after taking
   a local snapshot per `RUNBOOK_end_to_end_verification.md` § Prerequisites
   (the production DB's WAL trio doesn't cooperate with concurrent SMB
   readers). On the Windows production server, point at
   `%USERPROFILE%\AlphaMind\data\alphamind.db` directly — same machine
   as the writer, no snapshot needed.
4. No live market data, broker connection, or Anthropic SDK token needed.

## Invocation

```bash
uv run python scripts/verify_oms_commands.py --db-path /tmp/alphamind-verify-oms/alphamind.db
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

Expected runtime under 1 minute (no live SDK calls). On a development
laptop the typical run completes in well under 5 seconds; the budget is
deliberately loose to absorb cold-start sqlite migration overhead in CI.

## Expected output

A clean run prints (exit code 0):

```
======================================================================
AlphaMind OMS Commands Verification
======================================================================
  [PASS] Phase 1 — canonical model round-trip
  [PASS] Phase 2 — command-ID utility
  [PASS] Phase 3 — PM envelope path
  [PASS] Phase 4 — engine envelope path
======================================================================
RESULT: PASS (4/4 criteria)
======================================================================
```

On any phase failure the script exits 1, the failed phase prints `[FAIL]`
followed by an indented diagnostic line (e.g.,
`capital_reserved.amount_usd=1000.0, expected 5000.0 (regression to retired $1k stub?)`),
and the summary reads
`RESULT: FAIL (M/N criteria, failing: <labels>)`.

The `--output json` mode emits:

```json
{
  "all_pass": true,
  "phases": [
    {"label": "Phase 1 — canonical model round-trip", "ok": true, "detail": null},
    {"label": "Phase 2 — command-ID utility", "ok": true, "detail": null},
    {"label": "Phase 3 — PM envelope path", "ok": true, "detail": null},
    {"label": "Phase 4 — engine envelope path", "ok": true, "detail": null}
  ]
}
```

## Failure-mode triage

| Phase | Typical failure | Diagnostic | Likely fix |
|---|---|---|---|
| 1 — canonical round-trip | `OMSCommand round-trip failed: <pydantic error>` | A `model_validator` in `command_models.py` started rejecting a previously-valid construction (e.g., the OPEN `_validate_hard_backstop` invariant tightened, or a `CloseCommand` rationale-pairing rule changed). | Compare the failing variant's construction in `verify_oms_commands.py` against the latest `oms-command-schema.md`; either the schema or the verify fixture is out of step. |
| 1 — canonical round-trip | `EngineEnvelope round-trip failed: <pydantic error>` | The `EngineEnvelope` cross-field invariants (`trigger_timestamp` denormalization, `invocation_id is None`, embedded CLOSE rationale/subtype) regressed. | Re-read `engine_envelope.py § _validate_invariants` against `engine-envelope-schema.md`. |
| 2 — command-ID utility | `derive_pm_command_id worked example mismatch: got 'X', expected 'Y'` | The PM-originated derivation format changed in a way the design doc's worked example no longer reflects. | Re-read `derive_pm_command_id` against `oms-command-ids.md § Worked example`; either the doc or the function regressed. |
| 2 — command-ID utility | `derive_engine_command_id shape mismatch: got 'X'` | The engine-originated format (`MON.{session}.{trigger}.{ord}`) changed. | Cross-check `derive_engine_command_id` against `oms-command-ids.md § Engine-originated commands`. |
| 2 — command-ID utility | `compute_attempt_seq returned N, expected 2` | The `attempt_seq` computation no longer counts only `post_rejection` modifications. | Re-read `command_ids.py § compute_attempt_seq`; the documented contract is "count modifications where `phase == 'post_rejection'`". |
| 3 — PM envelope path | `submit_envelope tool returned error: <text>` | The MCP server rejected the well-formed envelope. Check `response_text` for the rejection reason; likely a Layer-2/3 invariant broke. | Re-read `validate_pm_envelope`; commonly a guardrail-side reshape broke the recommendation-resolution check. |
| 3 — PM envelope path | `command_id 'X' does not match PM-originated regex` | `derive_pm_command_id` produced a malformed ID (empty `invocation_id`?). | Re-read `submit_envelope_mcp.py § _process_one_command` and confirm `state.invocation_id` is non-empty at construction. |
| 3 — PM envelope path | `capital_reserved.amount_usd=1000.0, expected 5000.0 (regression to retired $1k stub?)` | Story 03 / ALP-374 retired the `$1k` stub constant in `_writeback_open`; this failure means the writeback regressed and is again hard-coding a stub dollar value. | Re-read `phase2.py § _writeback_open` and confirm `_reserve_capital(handle, amount_usd=command.position_size.dollar_value)` reads from the canonical command, not a constant. |
| 3 — PM envelope path | `cash_ledger.reserved_capital_usd=<X>, expected <Y>` | The cash-ledger reservation amount disagrees with the activity-log entry. Either `_reserve_capital` regressed or the `capital_reserved` activity-log emission disagrees with the cash-ledger update. | Re-read `phase2.py § _reserve_capital` — both surfaces should derive from the same `amount_usd` argument. |
| 3 — PM envelope path | `expected N bracket legs (one TAKE_PROFIT + one per invalidation_leg), got M` | `_writeback_open` no longer emits a TAKE_PROFIT leg per `command.target` plus one leg per `command.invalidation_legs`. | Re-read `phase2.py § _writeback_open`; the TAKE_PROFIT leg construction lives alongside the invalidation-leg loop. |
| 3 — PM envelope path | `activity log missing required events: <list>` | One of `ORDER_SUBMITTED` / `THESIS_CREATED` / `CAPITAL_RESERVED` / `PM_DECISION` failed to emit. | Re-trace the emission helpers in `phase2.py` § `_writeback_open` and § `_emit_pm_decision`; usually a missing `await _emit(...)`. |
| 4 — engine envelope path | `no position found to close (run Phase 3 first)` | Phase 3 must populate the positions table before Phase 4 can close one. | Confirm Phase 3 is passing; the verify orchestrator sequences 3 then 4. |
| 4 — engine envelope path | `secondary_breach_check_result` rejection | The trigger record carried `result == "deferred_to_pm"`, which `submit_engine_envelope` refuses per `oms-commands.md § Command origins`. | The verify fixture's trigger record sets `secondary_breach_check_result=None`; if this fires, the function regressed and is gating on something other than `result == "deferred_to_pm"`. |
| 4 — engine envelope path | `command_id 'X' does not match engine-originated regex` | `derive_engine_command_id` produced a malformed ID. | Re-read `command_ids.py § derive_engine_command_id` — the format is `MON.{session}.{trigger_id}.{command_ordinal}`. |
| 4 — engine envelope path | `order_submitted detail missing risk_management_subtype='engine_guardrail'` | The engine-guardrail provenance regressed in the writeback's `extra_metadata` plumbing. | Re-read `submit_engine_envelope.py` — `extra_metadata["source_provenance"]` should be `"engine_guardrail"` and the embedded CLOSE's `risk_management_subtype` should be `"engine_guardrail"`; both surface on the activity-log detail. |
| 4 — engine envelope path | `order_submitted detail position_selection_rationale='X', expected 'Y'` | The trigger record's `position_selection_rationale` did not thread through to the activity-log entry. | Re-read `submit_engine_envelope.py` — `extra_metadata["position_selection_rationale"]` is the threading site; `phase2.py § _writeback_close` folds `extra_metadata` into the `OrderSubmittedDetail.order_parameters_json`. |

## Operational caveats

**Engine-stub now routes to real Alpaca paper-mode submission.** As of
ALP-390 (engine-stub coordinated swap, work tree ALP-121), when the runner
supplies a `TradingClient` + `AccountStateQueries` + `ExecutionConfig` to
`build_submit_envelope_mcp_server` (PM envelopes) or `submit_engine_envelope`
(engine envelopes), accepted commands route through
`alphamind.execution.oms.broker_dispatch.dispatch_command_to_broker` before
the Phase 2 writeback. The persisted entry / close / add / adjust order
carries Alpaca's real `alpaca_order_id`; protective leg orders keep the
synthetic `alp-{order_id}` placeholder until `trade_updates` (story 02f)
acks each child leg.

Run `verify_oms_commands.py` against a fresh DB to exercise the legacy
synthetic-ack path (no broker context supplied — preserves the pre-ALP-390
behavior the verify covers); operator dry-runs against live Alpaca paper
mode are the responsibility of the e2e verify (`RUNBOOK_end_to_end_verification.md`).
On gateway-submission failure the OMS writes a `command_abandoned`
activity-log entry and the per-command result is rejected; on permanent
rejection (4xx with documented reason) the synchronous OMS rejection
carries the broker's `PermanentRejection.code` in
`rejection_payload.gateway_reason`.

**The verify script does not exercise live broker calls.** Phases 3 + 4
exercise the canonical command → Phase 2 writeback → activity log surface
without supplying the optional `client` / `queries` / `execution_config`
to the engine-stub factory; the Alpaca submission wire is out of scope.
End-to-end coverage of the broker-routed path lives in
`tests/execution/oms/test_engine_stub_broker_routing.py` and
`scripts/verify_e2e_runbook.py`.

**The PM envelope path uses a degenerate Layer-2/3 fixture.** The
pre-processor bundle, retrieval store, and PM view are minimal — just
enough to satisfy the engine-stub's validators. Real production envelopes
flow through a richer bundle from the proposal-pre-processor and
synthesizer; verify_oms_commands does not exercise that integration. PM
end-to-end correctness lives in `verify_pm.py`.

**Phase 3's $5,000 dollar value is deliberate.** A regression to the
retired `$1k` stub constant from story 03 / ALP-374 would surface as
`capital_reserved.amount_usd=1000.0, expected 5000.0`. Don't shrink the
fixture's dollar value; the canonical command's value is the assertion.

**Phase 4 closes the Phase-3-opened position.** Phase 4 is sequenced after
Phase 3 because the engine-envelope CLOSE references `position_id`. Running
Phase 4 in isolation against a DB without a Phase-3-equivalent position
will FAIL with `no position found to close`.

**Engine-stub re-exports are lazy.** Per the OMS package's `__getattr__`,
`build_submit_envelope_mcp_server` and friends are loaded lazily to break
the latent cycle with `alphamind.decision.portfolio_manager.models`. The
verify script does a side-effect import of `portfolio_manager.models` at
module load to pre-resolve the cycle; this is the documented pattern for
any script that combines the engine-stub MCP path with PMEnvelope
construction.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook;
  this script runs as the OMS commands phase between State persistence
  and Decision-layer composition.
- `scripts/verify_state_persistence.py` + `RUNBOOK_state_persistence.md`
  — sibling pattern; the Phase 2 surface this verify exercises is the
  same surface state-persistence verify covers from the persistence-side.
- `docs/design/05-execution-layer/oms-command-schema.md` — canonical
  command shapes Phase 1 round-trips.
- `docs/design/05-execution-layer/engine-envelope-schema.md` — engine
  envelope shape Phase 1 round-trips.
- `docs/design/oms-command-ids.md` — command-ID derivation contract Phase
  2 verifies.
- `docs/design/05-execution-layer/oms-commands.md` — OMS commands
  architecture; § Command origins covers the engine-envelope path.
- ALP-120 parent issue — work-tree overview, dependency graph,
  pre-resolved decisions.
- ALP-370 through ALP-375 — predecessor stories whose deliverables this
  script integrates.
- `src/alphamind/execution/oms/` — canonical command package.
- `src/alphamind/execution/state_persistence/write_paths/phase2.py`
  — Phase 2 writeback consuming canonical commands; verified end-to-end
  by Phases 3 + 4.
- `tests/scripts/test_verify_oms_commands.py` — unit tests for this
  script's phase functions; run with
  `uv run pytest tests/scripts/test_verify_oms_commands.py -n auto`.
