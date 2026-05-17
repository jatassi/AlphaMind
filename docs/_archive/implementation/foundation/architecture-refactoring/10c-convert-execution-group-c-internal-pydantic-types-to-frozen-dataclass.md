# 10c — Convert execution Group C internal Pydantic types to frozen dataclass

## Goal

Convert the ~15 execution-subdivision "Group C" types (audit's triage classified ~40 of 61 L9 as correct boundary, ~6 as boundary-out also OK, ~15 as truly-internal P5 violations) from Pydantic to `@dataclass(frozen=True, slots=True)`. Also convert the 3 mutable `@dataclass` (L8) cases in `submit_envelope` to `frozen=True, slots=True` with `dataclasses.replace()` mutation.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L5 + LB4 from the execution-subdivision audit (the Group A/B/C triage)
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decision (D)
* `src/alphamind/execution/continuous_monitor/session.py:27` `MonitorSession` — Group C
* `src/alphamind/execution/oms/command_ids.py:57,73` `PMCommandIdComponents`, `EngineCommandIdComponents` — Group C
* `src/alphamind/execution/state_persistence/invocation_context/records.py:28,54` `ProcessLifetimeRecord`, `InvocationRecord` — verify no JSON round-trip before classifying as Group C
* `src/alphamind/decision/portfolio_manager/submit_envelope/types.py` (post-06b): `SubmissionLogEntry`, `FailedSubmissionEntry`, `SubmitEnvelopeState` — L8 mutable @dataclass
* `src/alphamind/execution/oms/submit_engine_envelope.py:73` `SubmitEngineEnvelopeState` — L8

## Depends on

* 06b (<issue id="8fded466-a77b-4bda-aa7f-9a3e91a65609">ALP-464</issue>) — `submit_envelope` decomposition must complete first; Group C types live in the `submit_envelope/types.py` submodule after that story

## Scope

In scope: convert ~15 Group C types from Pydantic to frozen dataclass; convert 4 mutable `@dataclass` cases to `frozen=True, slots=True` with `dataclasses.replace()` mutation pattern. Tests update.

### Group C conversions (Pydantic → frozen dataclass)

* `continuous_monitor/session.py:27` `MonitorSession`
* `oms/command_ids.py:57` `PMCommandIdComponents`
* `oms/command_ids.py:73` `EngineCommandIdComponents`
* `state_persistence/invocation_context/records.py:28` `ProcessLifetimeRecord` (verify no JSON round-trip; if it does serialize, leave Pydantic)
* `state_persistence/invocation_context/records.py:54` `InvocationRecord` (same caveat)
* Any other Group C types the audit named

### L8 mutable @dataclass conversions

* `submit_envelope/types.py` (post-06b) `SubmissionLogEntry`, `FailedSubmissionEntry`, `SubmitEnvelopeState`:

  ```python
  @dataclass(frozen=True, slots=True)
  class SubmitEnvelopeState:
      submission_log: tuple[SubmissionLogEntry, ...] = ()
      failed_submission_log: tuple[FailedSubmissionEntry, ...] = ()
  ```

  Mutation sites use `state = dataclasses.replace(state, submission_log=state.submission_log + (entry,))`.
* `submit_engine_envelope.py:73` `SubmitEngineEnvelopeState`
* `continuous_monitor/supervisor.py:51` `_RegisteredTask` — likely deleted entirely by story 08b's TaskGroup replacement; verify whether still needed

### Keep Pydantic

* `oms/command_models.py` (after 02b → `commands/command_models.py`) — boundary
* `oms/engine_envelope.py` (after 02b → `commands/engine_envelope.py`) — boundary
* `broker_adapter/queries.py` `TradeAccountSnapshot`/`PositionSnapshot`/`OrderSnapshot`/`OptionContractSnapshot` — Alpaca API boundary
* `state_persistence/config.py`, `regt_margin_attribution/config.py`, `corporate_actions/config.py` — config boundary
* `tables/orders_codec.py:_OrderMetadata` and similar sidecar types — JSON-row boundary
* `submit_envelope/types.py` `Acknowledgment`, `RejectionPayload`, `SubmissionResult` — MCP response boundary

## Acceptance criteria

- [ ] Group C types are `@dataclass(frozen=True, slots=True)`.
- [ ] 4 mutable `@dataclass` cases are `frozen=True, slots=True`; mutation sites use `dataclasses.replace()`.
- [ ] Boundary Pydantic preserved (verify each Keep-Pydantic item above).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "BaseModel" src/alphamind/execution/continuous_monitor/session.py src/alphamind/execution/oms/command_ids.py` returns zero hits. `grep -rn "@dataclass" src/alphamind/decision/portfolio_manager/submit_envelope/types.py` shows `frozen=True, slots=True` on every class. End-to-end execution pipeline test passes.