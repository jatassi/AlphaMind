# 04a — /api/control proxy + audit

## Goal

Ship the browser-facing `/api/control/*` surface that authenticates, audits, and proxies each of the eight operator-callable verbs (5 pipeline + 3 monitor) to the corresponding loopback HTTP endpoints from stories 01b and 01c. Each verb opens an `OperatorInvocationHandle` (from story 02), writes an `activity_log` entry with `source=operator_console` carrying the verb name + parameters + result, then closes the handle. The `switch_profile` verb additionally calls `emit_profile_switch_entry` (from story 01a) inside the handle. Errors from the upstream surface are mapped to user-friendly response envelopes that the frontend can render.

## Reading

* `docs/design/command-center.md` § Control surface and § Operator actions — what each verb does + which activity_log event type / source attribution each writes.
* `docs/design/pipeline-control-and-events-schema.md` and `monitor-control-and-events-schema.md` — wire format the proxy must speak upstream + error code → HTTP status mapping.
* `src/alphamind/command_center/_kernel/control.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `ControlVerb`, `ControlErrorCode`, `ControlResult` types.
* `src/alphamind/command_center/_kernel/operator_invocation.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `OperatorInvocationHandle` primitive.
* `src/alphamind/command_center/auth/dependencies.py` (from [ALP-667](https://linear.app/alphamind-jatassi/issue/ALP-667/03-webauthn-sessions-csrf)) — `current_session`, `csrf_required` dependencies that gate each route.
* `src/alphamind/state/invocation_context/config_change.py` (from [ALP-663](https://linear.app/alphamind-jatassi/issue/ALP-663/01a-profile-switched-event-substrate)) — `emit_profile_switch_entry` the `switch_profile` verb calls.
* [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (D) — operator-action InvocationHandle binding shape.

## Depends on

* [ALP-663](<https://linear.app/alphamind-jatassi/issue/ALP-663>) (01a) — `emit_profile_switch_entry` consumed by the `switch_profile` proxy.
* [ALP-664](<https://linear.app/alphamind-jatassi/issue/ALP-664>) (01b) — pipeline `/control/*` endpoints the 5 pipeline verbs proxy to.
* [ALP-665](<https://linear.app/alphamind-jatassi/issue/ALP-665>) (01c) — monitor `/control/*` endpoints the 3 monitor verbs proxy to.
* [ALP-667](<https://linear.app/alphamind-jatassi/issue/ALP-667>) (03) — `current_session` + `csrf_required` dependencies.

## Scope

In scope, under `src/alphamind/command_center/control/`. Tests at `tests/command_center/control/`.

### 1\. New `command_center/control/` subpackage

```
src/alphamind/command_center/control/
├── __init__.py
├── pipeline_client.py      # PipelineClient Protocol + RealPipelineClient (httpx.AsyncClient)
├── monitor_client.py       # MonitorClient Protocol + RealMonitorClient (httpx.AsyncClient)
├── proxy.py                # 8 verb implementations: open handle, call upstream, write audit row, close handle
├── audit.py                # write_operator_action_entry: typed activity_log row for the verb invocation
├── models.py               # Pydantic request/response/error models for the /api/control/* surface
└── routes.py               # 8 POST /api/control/* handlers
```

### 2\. Client Protocols (`pipeline_client.py`, `monitor_client.py`)

```python
class PipelineClient(Protocol):
    async def pause(self, *, reason: str) -> ControlResult: ...
    async def resume(self) -> ControlResult: ...
    async def trigger_emergency_invocation(self, *, reason: str) -> TriggerEmergencyResult: ...
    async def switch_profile(self, *, profile_name: ProfileName) -> ControlResult: ...
    async def run_universe_validation(self) -> RunUniverseValidationResult: ...

class MonitorClient(Protocol):
    async def cancel_order(self, *, order_id: str) -> ControlResult: ...
    async def force_close_position(self, *, position_id: str, rationale: str) -> ForceClosePositionResult: ...
    async def set_halt_mode(self, *, enabled: bool, reason: str) -> ControlResult: ...
```

`RealPipelineClient` and `RealMonitorClient` are `httpx.AsyncClient`-backed implementations targeting the loopback URLs from `command-center.yaml` (add fields: `pipeline.control_url`, `monitor.control_url`). Error-envelope parsing maps upstream `ControlErrorCode` → typed exception → `ControlResult(status="error", ...)`.

In-memory fakes (`tests/command_center/control/fakes.py`) seed canned responses + error envelopes for each verb; tests use them to exercise the proxy without booting real loopback surfaces.

### 3\. Audit helper (`audit.py`)

```python
async def write_operator_action_entry(
    *,
    handle: InvocationHandle,
    verb: ControlVerb,
    parameters: Mapping[str, Any],
    result: ControlResult,
) -> None:
    """Append a typed ActivityLogEntry inside the open handle's transaction.
  
    event_type carries the verb-specific activity-log event type:
      - pause/resume/set_halt_mode -> RISK_PARAMETER_CHANGED
      - trigger_emergency_invocation -> EMERGENCY_INVOCATION_REQUESTED (existing event type)
      - switch_profile -> handled separately via emit_profile_switch_entry; this helper writes no row for switch_profile
      - cancel_order -> ORDER_CANCELLED (with reason=operator_cancel)
      - force_close_position -> a row referencing the synthesized envelope_id; the envelope itself writes its own POSITION_CLOSED later
      - run_universe_validation -> no activity_log row (read-only)
  
    source=OPERATOR_CONSOLE; details capture verb name + parameters + result envelope.
    """
```

### 4\. Proxy implementations (`proxy.py`)

One async function per verb. Common shape:

```python
async def proxy_pause(
    *, session_id: OperatorSessionId, reason: str,
    invocation_writer: InvocationContextWriter, pipeline: PipelineClient,
) -> ControlResult:
    async with operator_invocation(
        invocation_context_writer=invocation_writer,
        operator_session_id=session_id, verb=ControlVerb.PAUSE,
    ) as handle:
        result = await pipeline.pause(reason=reason)
        await write_operator_action_entry(
            handle=handle, verb=ControlVerb.PAUSE,
            parameters={"reason": reason}, result=result,
        )
        return result
```

`switch_profile` is special: after `pipeline.switch_profile(...)`, it constructs a `ProfileSwitchOutcome` from the upstream response and calls `emit_profile_switch_entry(handle=handle, outcome=outcome)` instead of `write_operator_action_entry`.

### 5\. Routes (`routes.py`)

Eight POST handlers under `/api/control/*` mirroring the verb table from `command-center.md`. Each handler:

* Includes `Depends(current_session)` and `Depends(csrf_required)`.
* Validates the request body against the Pydantic model.
* Dispatches to the matching `proxy_*` function.
* Returns the result envelope; maps `ControlResult.status="error"` to the documented HTTP status code per the upstream schema's per-verb error table.

### Out of scope

* SSE event multiplexer — story 04b.
* Frontend buttons that call these endpoints — story 04c sets up the API client; per-view stories wire the buttons.

## Acceptance criteria

- [ ] `command_center/control/` subpackage exists with all six modules.
- [ ] `PipelineClient` and `MonitorClient` Protocols exist; `RealPipelineClient` / `RealMonitorClient` httpx-backed; in-memory fakes under `tests/command_center/control/fakes.py`.
- [ ] All eight verbs have a `proxy_*` implementation wrapping `operator_invocation()` correctly.
- [ ] `switch_profile` proxy invokes `emit_profile_switch_entry` (from [ALP-663](https://linear.app/alphamind-jatassi/issue/ALP-663/01a-profile-switched-event-substrate)) inside the open handle when the upstream response indicates a non-no-op switch.
- [ ] Each route includes both `Depends(current_session)` and `Depends(csrf_required)`.
- [ ] Each route's response envelope matches the upstream schema's response shape; upstream error codes map to the documented HTTP status codes.
- [ ] `pipeline.control_url` and `monitor.control_url` fields exist on `CommandCenterConfig` (extending the model from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)).
- [ ] Tests under `tests/command_center/control/` cover: each verb's happy path via fake, each documented error envelope, audit row presence + content, CSRF + session enforcement.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/control/ -n auto`. Real loopback integration deferred to verify story 07.