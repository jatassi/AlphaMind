# 04 — Engine envelope submission path (monitor-facing write function)

## Goal

Land the monitor-facing write function `submit_engine_envelope(envelope, *, handle, ...)` at `src/alphamind/execution/oms/submit_engine_envelope.py`. The function validates an `EngineEnvelope` (Pydantic Layer-1 + per-design invariants), checks the embedded CLOSE's command_id matches the canonical engine-originated format, persists the protective CLOSE via the Phase 2 writeback machinery story 03 refreshed (engine_guardrail provenance), and returns a `SubmissionResult`. Broker routing remains deferred per parent decision (C). The continuous monitor work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>) consumes this contract when emitting protective CLOSEs between invocations.

## Reading

* `src/alphamind/execution/oms/engine_envelope.py` (story 02a) — the typed `EngineEnvelope`, `GuardrailTriggerRecord`, `BreachDetails`, `SecondaryBreachCheckResult`
* `src/alphamind/execution/oms/command_ids.py` (story 01b) — `derive_engine_command_id`, `parse_engine_command_id`, `is_engine_originated`
* `src/alphamind/execution/oms/submit_envelope_mcp.py` (story 03) — pattern for the PM-facing submission path; `submit_engine_envelope` mirrors its envelope-level validation + per-command writeback shape but operates on engine envelopes
* `src/alphamind/execution/state_persistence/write_paths/phase2.py` (story 03) — `persist_envelope_outcome` is the Phase 2 entry point. **Important:** the existing `persist_envelope_outcome` signature accepts a `PMEnvelope`; story 04 either extends it to accept either envelope type via union, or introduces a sibling `persist_engine_envelope_outcome` function. Choose the option that minimizes coupling between PM and engine submission paths.
* `docs/design/05-execution-layer/engine-envelope-schema.md` — the contract `submit_engine_envelope` validates against
* `docs/design/05-execution-layer/oms-commands.md` § Command origins — engine-originated CLOSEs bypass the normal validation path; secondary-breach checking is the monitor's responsibility (already encoded in `GuardrailTriggerRecord.secondary_breach_check_result`)
* `docs/design/06-risk-guardrails/breach-behavior.md` § Traceability for engine-originated actions — uniform activity log structure with `engine_guardrail` provenance
* `src/alphamind/portfolio_state/events/activity_log.py` — `PMDecisionDetail` is PM-specific; engine-originated CLOSEs need an engine-equivalent (or a generic envelope-decision detail). Check the existing detail types and either reuse `position_reduced` / `position_closed` with `mechanism="engine_guardrail"` or introduce a per-engine-envelope detail. **The existing** `position_closed` detail already has `exit_method` enum including `margin-liquidation` and similar engine-originated terminal states. Likely no new detail type is needed.
* Parent issue <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> § Pre-resolved configuration decisions (D) — engine envelope submission scope without continuous monitor

## Depends on

* <issue id="d59d4dd3-9fa5-4003-a9d5-db165d631d64">ALP-372</issue> (02a) — engine envelope Pydantic models
* <issue id="beb08c17-ddd8-4217-a2bd-611ade38f150">ALP-374</issue> (03) — Phase 2 writeback machinery refined to consume canonical models; `submit_engine_envelope` calls into the same `_writeback_close` helper

## Scope

In scope, all under `src/alphamind/execution/oms/`. Tests at `tests/execution/oms/`.

### 1\. `submit_engine_envelope.py`

Author the module exporting:

* `submit_engine_envelope(envelope: EngineEnvelope, *, handle: InvocationHandle, retrieval_store: RetrievalStore | None = None, library_config: LibraryConfig, library_market: MarketInputs, ...) -> SubmissionResult` — the monitor-facing write function. Per parent decision (D), this is a direct function call (not MCP), since the continuous monitor is not an LLM agent. Implementation:
  * Validate the envelope at Layer-1 via Pydantic (the function takes a typed `EngineEnvelope` so invariants per story 02a are already enforced).
  * Verify the embedded CLOSE's `command_id` (if present) matches `^MON\.{envelope.envelope_id_components.session}\\.{envelope.envelope_id_components.trigger}\\.0$` — i.e., consistent with the envelope's structural position. If `command_id` is absent (continuous monitor produced envelope without one), derive it via `derive_engine_command_id(monitor_session_id, trigger_id, command_ordinal=0)`.
  * Persist the CLOSE via the Phase 2 writeback machinery's `_writeback_close` helper (or via a `persist_engine_envelope_outcome` wrapper that calls into the same machinery). The activity-log entry's source is `bracket_manager` or a similar engine-originated source per `EventSource` enum (review existing `EventSource` values; if no fit exists, surface to the operator).
  * Return a `SubmissionResult` matching the existing PM-side shape (`command_ordinal=0`, `status="accepted"`, `command_id=<derived>`, `acknowledgment=Acknowledgment(...)`) — even though the consumer is the monitor, not an LLM, the result shape stays uniform per design's "uniform activity log regardless of command origin".
* Cascade handling: when `envelope.guardrail_trigger_record.cascade_id` is set, the activity-log entry includes the cascade_id so feedback-loop queries can correlate cascade members. The function does not orchestrate cascades — that's the monitor's responsibility (per design § Cascades produce multiple envelopes, not multiple commands).
* Secondary-breach handling: when `envelope.guardrail_trigger_record.secondary_breach_check_result.result == "deferred_to_pm"`, the function refuses submission with a `SubmissionResult(status="rejected", rejection_payload=...)` whose payload's `feature_disabled = None` and a recognizable rejection reason. The monitor's responsibility was to make this call before submission; receipt of `deferred_to_pm` here is a contract violation.

### 2\. `__init__.py`

Append re-exports for `submit_engine_envelope` and any new helper types.

### 3\. Activity log integration

Confirm an existing `EventSource` enum value covers engine-originated CLOSEs (likely `bracket_manager` or an engine-specific source). If no existing value fits, add a new enum value `engine_monitor` to `alphamind.portfolio_state.events.activity_log`'s `EventSource` and update its consumers — but only if no existing value applies.

### 4\. Tests at `tests/execution/oms/test_submit_engine_envelope.py`

* Happy path: construct a valid `EngineEnvelope`, call `submit_engine_envelope`, assert the position is closed, the bracket is dissolved, the cash ledger is credited, an activity-log entry with `engine_guardrail` provenance exists.
* Invalid envelope: `commands` empty (already raises in Pydantic per story 02a) — confirm the function reports the Layer-1 validation error rather than silently accepting.
* Cascade: envelope with `cascade_id` set produces an activity-log entry whose detail carries the same cascade_id; multiple envelopes sharing a cascade_id produce activity log entries that share it.
* Secondary breach `deferred_to_pm`: the function rejects with a recognizable payload.
* `command_id` mismatch: an envelope whose embedded CLOSE has a `command_id` inconsistent with the envelope's structural position raises (the contract is bijective — the monitor must derive consistent IDs).
* Idempotency / duplicate-detection: same as PM path — within a monitor session, a duplicate `(monitor_session_id, trigger_id)` combination is a structural error and raises. (Story 04 implements this check; per oms-command-ids.md § What happens if a duplicate command ID arrives.)

### Out of scope

* Real broker routing — deferred to broker adapter work tree per parent decision (C)
* Continuous monitor's envelope production — that's the monitor's work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>)
* End-to-end verify script — story 05
* Greek refresh, websocket reconnection, fill stream consumption — those are continuous monitor responsibilities

## Acceptance criteria

- [ ] `src/alphamind/execution/oms/submit_engine_envelope.py` exists and exports `submit_engine_envelope`.
- [ ] `submit_engine_envelope` accepts an `EngineEnvelope` (story 02a) and returns a `SubmissionResult` matching the existing PM-side shape (`command_ordinal=0`, `status="accepted"|"rejected"`, `command_id`, `acknowledgment` or `rejection_payload`).
- [ ] If the embedded CLOSE's `command_id` is absent, the function derives it via `derive_engine_command_id` and confirms the result's regex matches `^MON\.[^.]+\.[0-9]+\.[0-9]+$`.
- [ ] If the embedded CLOSE's `command_id` is present, the function validates it parses cleanly via `parse_engine_command_id` and matches the envelope's `envelope_id` components (same session_id, same trigger_id).
- [ ] On accepted submission, the protective CLOSE persists via `_writeback_close` (or a wrapper); position state, bracket dissolution, cash ledger credit, and activity log entry are emitted in one transaction.
- [ ] The activity log entry's detail carries `engine_guardrail` provenance, the `position_selection_rationale` from the trigger record, and `cascade_id` (when set on the envelope).
- [ ] Submitting an envelope with `secondary_breach_check_result.result == "deferred_to_pm"` returns a `SubmissionResult(status="rejected", ...)` rather than persisting the CLOSE.
- [ ] Submitting a duplicate `(monitor_session_id, trigger_id)` combination within a monitor session raises a structural error (per oms-command-ids.md duplicate handling).
- [ ] `tests/execution/oms/test_submit_engine_envelope.py` covers every acceptance criterion above and passes under `uv run pytest tests/execution/oms/test_submit_engine_envelope.py -n auto`.
- [ ] `uv run pytest tests/execution/ -n auto` passes (no regression in PM-side or phase2 tests).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/execution/oms/test_submit_engine_envelope.py -n auto`. Sanity-check that an accepted submission produces the expected SQL state by running one end-to-end test that constructs an `EngineEnvelope`, calls `submit_engine_envelope`, then queries the activity log for an entry with `engine_guardrail` provenance.
