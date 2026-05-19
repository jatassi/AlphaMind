# 06c — submit_envelope MCP tool wrapper (engine-stub)

## Goal

Author the engine-stub `submit_envelope` MCP wrapper. The wrapper accepts one PM envelope per tool call, runs Layer-2/3 validation (story 06b), then re-runs `validate_guardrail` per embedded command against cumulative state. Returns `accepted` (with synthetic `command_id`, `validation_metadata` for OPEN/ADD) or `rejected` (with realistic `rejection_payload` matching the breach-behavior hard-rejection shape). Holds a mutable cumulative-state cell mirroring `validate_guardrail`'s pattern; appends every call to a submission log accessible via `get_submission_log()`. Per parent decision (F), lives at `src/alphamind/execution/oms/submit_envelope_mcp.py` (creating the package skeleton).

This is the most architecturally-load-bearing story in the work tree — it is the engine-stub that the entire PM verification chain depends on. The eventual real-engine implementation ([ALP-120](<https://linear.app/alphamind-jatassi/issue/ALP-120>), [ALP-119](<https://linear.app/alphamind-jatassi/issue/ALP-119>)) replaces this file in a coordinated edit when those work trees ship.

## Reading

* `docs/design/04-decision-layer/submit-envelope-tool-schema.md` — tool input/response contract; this story implements the producer side.
* `docs/design/04-decision-layer/pm-envelope-schema.md` — envelope shape the tool's input schema mirrors.
* `docs/design/06-risk-guardrails/breach-behavior.md` § Hard rejection semantics — `rejection_payload` shape (rules_breached, current/limit/overage, suggested_modification, headroom_after_suggestion).
* `docs/design/oms-command-ids.md` — `command_id` derivation rule (`{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}`); the engine-stub assigns synthetic IDs following this pattern.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — sibling pattern: `build_validate_guardrail_mcp_server(state_cell, ...)` factory closing over a mutable state cell. Mirror this exactly for state-cell architecture, MCP server construction, and tool registration.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` — `validate_guardrail` Python function this wrapper re-uses. The library is shared; the engine-stub re-runs it per command.
* `src/alphamind/decision/portfolio_manager/validation.py` (story 06b / [ALP-327](<https://linear.app/alphamind-jatassi/issue/ALP-327>)) — `validate_pm_envelope` the wrapper calls before per-command guardrail validation.
* `src/alphamind/decision/portfolio_manager/models.py` (story 03 / [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>)) — `PMEnvelope`, `OMSCommand`. The wrapper coerces the input dict to `PMEnvelope` (Layer-1 schema validation) before validator dispatch.
* `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` — `validate_guardrail`, `ValidationResult`, `ValidationToolState`, `ProjectedDelta`, `RuleProjection`. The wrapper accumulates a `ValidationToolState` cell across submit calls.
* `claude_agent_sdk` — `tool` decorator, `create_sdk_mcp_server`. Standard MCP wrapper toolkit.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decisions (A), (D), (F), (I), (J) for the wrapper's role, location, state cell, and sector resolver plumbing.

## Depends on

* [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>) (this work tree, story 03) — provides `PMEnvelope` and `OMSCommand` types.
* [ALP-327](<https://linear.app/alphamind-jatassi/issue/ALP-327>) (this work tree, story 06b) — provides `validate_pm_envelope` Layer-2/3 validator.

## Scope

Code at:

* `src/alphamind/execution/__init__.py` — empty package skeleton.
* `src/alphamind/execution/oms/__init__.py` — package skeleton; exports `build_submit_envelope_mcp_server`, `build_initial_submit_envelope_state`, `SubmitEnvelopeState`, `SubmissionLogEntry`.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` — the wrapper.

Tests at `tests/execution/oms/test_submit_envelope_mcp.py`.

### 1\. State cell + log entry types

Author at the top of `submit_envelope_mcp.py` (or in a sibling `state.py` if cleaner):

```python
@dataclass
class SubmissionLogEntry:
    """One submit_envelope call's record — envelope + per-command results."""
    envelope: PMEnvelope
    submission_results: tuple[SubmissionResult, ...]


@dataclass
class SubmitEnvelopeState:
    """Mutable per-invocation cumulative state for the submit_envelope tool."""
    validation_state: ValidationToolState
    submission_log: tuple[SubmissionLogEntry, ...]
    command_id_counter: int  # synthetic command_id assignment
    invocation_id: str  # for synthetic command_id formatting
```

`SubmissionResult` is the per-command result shape mirroring `submit-envelope-tool-schema.md`'s `submission_result` `$def`:

```python
class SubmissionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    command_ordinal: int
    status: Literal["accepted", "rejected"]
    command_id: str
    acknowledgment: Acknowledgment | None = None
    rejection_payload: RejectionPayload | None = None
```

`Acknowledgment` and `RejectionPayload` are inline minimal Pydantic models mirroring the design doc's shape (position_id / order_id / validation_metadata / released_capital_usd for `Acknowledgment`; rules_breached / suggested_modification / headroom_after_suggestion / greeks / delta_adjusted_exposure / feature_disabled for `RejectionPayload`).

### 2\. `build_initial_submit_envelope_state(...)`

Mirror `build_initial_validation_state(...)` from analyst story 04. Construct a fresh `SubmitEnvelopeState` from the per-invocation inputs:

```python
def build_initial_submit_envelope_state(
    *,
    invocation_id: str,
    starting_validation_state: ValidationToolState,
) -> SubmitEnvelopeState: ...
```

The `starting_validation_state` is the same cell that backs `validate_guardrail` — the engine-stub re-uses it to keep cumulative-impact tracking unified across both tools (`validate_guardrail` and `submit_envelope`).

### 3\. `build_submit_envelope_mcp_server(...)` factory

Author the factory:

```python
def build_submit_envelope_mcp_server(
    state: SubmitEnvelopeState,
    *,
    retrieval_store: RetrievalStore,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> tuple[Mapping[str, McpServer], tuple[str, ...]]: ...
```

The factory constructs an in-process MCP server registering `submit_envelope`. Wire-form name: `mcp__alphamind_execution_oms_submit__submit_envelope`.

The tool's input schema is the PM envelope JSON Schema — set via `input_schema=PMEnvelope.model_json_schema()` (Pydantic-derived), or via a hand-written schema mirroring `pm-envelope-schema.md`. Match analyst's `validate_guardrail` MCP wrapper's stance for input-schema authoring.

### 4\. Tool implementation

The async tool callable closes over the `state` cell and the per-invocation parameters. On invocation:

1. **Coerce input to** `PMEnvelope`. `envelope = PMEnvelope.model_validate(payload)`. On `ValidationError`, return a synthetic rejection envelope with `submission_results: [{status: rejected, command_ordinal: 0, command_id: ..., rejection_payload: {rules_breached: [{rule: "schema_invariant", current: 0, limit: 0, overage: 0, unit: "ok"}], suggested_modification: <error message>}}]`. The synthetic `command_ordinal: 0` is acceptable when the envelope is structurally invalid (no commands to ordinalize).
2. **Run Layer-2/3 validator.** `result = validate_pm_envelope(envelope, retrieval_store=..., pre_processor_bundle=..., pm_view=..., active_sectors=..., halt_mode=...)`. If `not result.is_valid`, return the same shape as step 1 with the validator's first error message in `suggested_modification`. **Do not** include any per-command results — the envelope did not reach command processing.
3. **Process each embedded command.** For each `command in envelope.commands`, in order, with `command_ordinal = i`:
   * Construct a `ValidationRequest` from the command (close commands and cancel commands have no exposure delta — trivial PASS path; OPEN / ADD / ADJUST commands compute the `ProposedDelta` from `command.position_size` + `command.instrument` per the analyst's translator pattern).
   * Call `validate_guardrail(request, state=state.validation_state, ...)` — re-uses the shared library function.
   * On `result.overall == "PASS"`:
     * Update `state.validation_state` to `state.validation_state.with_accepted_proposal(delta)`.
     * Increment `state.command_id_counter`.
     * Format synthetic `command_id` as `f"inv-{state.invocation_id}.{envelope.envelope_id}.{command_ordinal}.0"` (or attempt_seq from envelope's modification count if there are post_rejection modifications — see oms-command-ids.md `attempt_seq` rule).
     * Construct `Acknowledgment`: position_id (synthetic for OPEN, copied for CLOSE/ADJUST/ADD/CANCEL), order_id (synthetic), validation_metadata (greeks + iv + delta_adjusted_exposure + per_rule_headroom for OPEN/ADD), released_capital_usd (for CANCEL).
     * Append `SubmissionResult(command_ordinal=i, status="accepted", command_id=..., acknowledgment=...)`.
   * On `result.overall == "FAIL"`:
     * Do NOT update `state.validation_state` (the proposal didn't accept).
     * Construct `RejectionPayload` from the validation result: `rules_breached` (one entry per FAIL rule with rule name, current, limit, overage), `suggested_modification` (from `result.failure_guidance`), `headroom_after_suggestion` (post-suggestion projected per-rule headroom — match the validate_guardrail tool's `per_rule[]` shape).
     * Append `SubmissionResult(command_ordinal=i, status="rejected", command_id=..., rejection_payload=...)`.
4. **Append to submission log.** `state.submission_log = state.submission_log + (SubmissionLogEntry(envelope=envelope, submission_results=results),)`.
5. **Return** `Response(envelope_id=envelope.envelope_id, submission_results=results)`. Serialize as JSON in the MCP `text` content.

**Important:** rejection of one command does NOT abort processing — every command in `envelope.commands` produces a `SubmissionResult`. State updates are skipped only for the rejected command; subsequent commands see the cumulative impact of accepted-prior-commands but not the rejected one.

### 5\. Submission log accessor

Export a top-level `get_submission_log(state) -> tuple[SubmissionLogEntry, ...]` helper that simply returns `state.submission_log`. The runner (story 08) and verify script (story 09) use this to capture the submission log for fixture-export.

### 6\. Tests

Tests at `tests/execution/oms/test_submit_envelope_mcp.py`:

* `test_factory_returns_mcp_server_and_allowed_tools` — factory returns a `(mcp_servers, allowed_tools)` tuple of expected shape.
* `test_accepts_well-formed_envelope` — submit a well-formed `pm_analyst` envelope with one valid OPEN command; assert response is `accepted` with synthetic command_id of correct format and the cumulative state cell is updated.
* `test_rejects_envelope_with_layer_2_violation` — submit a malformed envelope (e.g., `verdict: reject` with non-empty `commands`); assert response is `rejected` with `rules_breached: [{rule: "schema_invariant"}]` and the state cell is NOT updated.
* `test_rejects_command_breaching_sector_concentration` — submit an envelope whose OPEN command would breach sector concentration; assert that command's result is `rejected` with rules_breached naming `sector_concentration`, current/limit/overage populated, and the state cell is NOT updated for that command.
* `test_processes_multiple_commands_with_partial_rejection` — submit an envelope with 3 commands where command 1 passes, command 2 rejects, command 3 sees the cumulative state-after-1; assert all three results are present in correct ordinal order.
* `test_close_commands_skip_validation_pass_through` — close + cancel commands have zero exposure delta; pass through to `accepted` without invoking `validate_guardrail`'s rule projection (validate_guardrail still sees them but PASS is trivial).
* `test_halt_mode_rejects_open_command` — with `halt_mode=True`, an envelope carrying an OPEN command results in `rejected` (Layer-2 invariant violation from the validator).
* `test_submission_log_captures_every_call` — call submit_envelope twice with two envelopes; assert `state.submission_log` has 2 entries in call order.
* `test_state_cell_isolation` — construct two distinct `SubmitEnvelopeState` instances; submit to one; assert the other's state cell is unchanged (no cross-cell leakage).
* `test_synthetic_command_id_format` — accepted command's `command_id` matches the regex `^inv-{invocation_id}\\\\.ENV-(REC|SA|SA-ORD)-[0-9]+\\\\.[0-9]+\\\\.[0-9]+$`.

Use the same in-line fixture builders as story 06b's tests for `RetrievalStore`, `PortfolioManagerView`, `ProposalPreProcessorBundle`.

### Out of scope

* The harness (story 07) — wires the factory into the SDK's MCP server registration.
* The runner (story 08) — constructs the initial state via `build_initial_submit_envelope_state` and threads the per-invocation parameters through.
* Production engine submission — that lives in [ALP-120](<https://linear.app/alphamind-jatassi/issue/ALP-120>). This story's stub does not touch a broker.
* Persistence — that lives in [ALP-119](<https://linear.app/alphamind-jatassi/issue/ALP-119>). This story's stub holds state in memory only.
* Engine-originated envelopes — those use `engine-envelope-schema.md` and are owned by [ALP-123](<https://linear.app/alphamind-jatassi/issue/ALP-123>). This stub handles only PM-originated envelopes.

## Acceptance criteria

- [ ] `src/alphamind/execution/__init__.py` and `src/alphamind/execution/oms/__init__.py` exist as package skeletons.
- [ ] `src/alphamind/execution/oms/submit_envelope_mcp.py` exports `build_submit_envelope_mcp_server`, `build_initial_submit_envelope_state`, `SubmitEnvelopeState`, `SubmissionLogEntry`, `SubmissionResult`, `Acknowledgment`, `RejectionPayload`, `get_submission_log`.
- [ ] The wrapper coerces input to `PMEnvelope` and runs Layer-2/3 validation before command processing.
- [ ] On Layer-2/3 failure, returns rejection with `rule: "schema_invariant"`; no state updates; no command-level processing.
- [ ] On per-command FAIL, returns rejection with realistic `rules_breached` payload; cumulative state NOT updated for rejected command; subsequent commands see cumulative state of prior accepts.
- [ ] On per-command PASS, updates cumulative state via `state.with_accepted_proposal(delta)`.
- [ ] Synthetic `command_id` values match the OMS command-IDs regex.
- [ ] Submission log captures every call (even rejections).
- [ ] All 10 tests pass.
- [ ] `uv run pytest tests/execution/oms/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.
- [ ] Module docstring on `submit_envelope_mcp.py` names this as a transitional engine-stub and references [ALP-120](<https://linear.app/alphamind-jatassi/issue/ALP-120>) / [ALP-119](<https://linear.app/alphamind-jatassi/issue/ALP-119>).

## Verification

Run `uv run pytest tests/execution/oms/test_submit_envelope_mcp.py -n auto` — all tests pass. Construct a sample `SubmitEnvelopeState` + envelope in REPL; call the tool's underlying callable directly; inspect the resulting submission log to confirm shape matches the design.
