# 06b — Decompose `decision/portfolio_manager/submit_envelope.py` into 5 submodules

## Goal

The 1,599-LOC `submit_envelope.py` (formerly `execution/oms/submit_envelope_mcp.py`, relocated in story 02b) hosts five subsystems in one file: boundary Pydantic types, state cell, per-command pipeline, broker dispatch routing, persistence path, MCP wiring. Decompose into `decision/portfolio_manager/submit_envelope/` package with five focused submodules. The 5 inline imports of `state_persistence/write_paths/phase2.py` (cycle workarounds prior to story 02b) become normal top-level imports once persistence has its own module.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L8 — submit_envelope_mcp decomposition plan
* Story 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — relocated the file to `decision/portfolio_manager/submit_envelope.py`; this story decomposes it
* `src/alphamind/decision/portfolio_manager/submit_envelope.py` (post-02b) — the file to decompose
* `src/alphamind/decision/portfolio_manager/submit_envelope.py:596,626,647,662,676,1155,1287` — inline imports of phase2 (cycle workarounds; can become normal imports after decomposition)

## Depends on

* 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — file must be relocated to `decision.portfolio_manager.submit_envelope` first

## Scope

In scope: convert single-file `submit_envelope.py` to `submit_envelope/` package with five submodules. Tests update accordingly.

### Decomposition

Create `decision/portfolio_manager/submit_envelope/__init__.py` (curated re-exports of public surface) and five submodules:

* `submit_envelope/types.py` — Boundary Pydantic types (`Acknowledgment`, `RejectionPayload`, `SubmissionResult`, `_PerRuleHeadroomEntry`, `_ValidationMetadata`, `_BreachedRule`) and the state cell (`SubmissionLogEntry`, `FailedSubmissionEntry`, `SubmitEnvelopeState`). Note: post-story 10b, the truly-internal Group C types (e.g., `_PerRuleHeadroomEntry`) become frozen dataclasses; this story leaves them as Pydantic per pre-decomposition shape.
* `submit_envelope/process.py` — Per-command pipeline: `_process_commands`, `_process_one_command`, `_command_to_validation_request`, `_build_acknowledgment`, `_build_envelope_level_rejection`. Imports `commands.command_models`, calls into `dispatch` and `persist`.
* `submit_envelope/dispatch.py` — Broker dispatch routing: `_route_through_broker`, `_dispatcher_context_for`, `_close_command_context`, `_adjust_command_context`, `_cancel_command_context`. Imports `commands.protocols.BrokerDispatch`; concrete dispatcher injected at composition root.
* `submit_envelope/persist.py` — Persistence path: `_persist_envelope_outcome_via_phase2`, `_emit_command_abandoned_via_phase2`, `_persist_envelope_parse_failure_via_phase2`, `_persist_envelope_rejection_via_phase2`. Imports `execution.state_persistence.write_paths.phase2` at the top of the file (the 5 inline imports become 1 top-level import). After 02b's cycle break, this is a normal downward import.
* `submit_envelope/server.py` — `build_submit_envelope_mcp_server` plus the `@tool`-decorated MCP entry points. Imports from `process`, `dispatch`, `persist`, `types`.

### Public surface

`submit_envelope/__init__.py` re-exports the public symbols the PM harness uses: `build_submit_envelope_mcp_server`, `SubmitEnvelopeState`, `build_initial_submit_envelope_state`, `SubmissionLogEntry`, `Acknowledgment`, `RejectionPayload`. Other modules' contents are package-private.

### Consumer migration

`decision/portfolio_manager/harness.py` and `decision/portfolio_manager/runner.py` currently import `from alphamind.decision.portfolio_manager.submit_envelope import ...`. These continue to work via the `__init__.py` re-export.

### Out of scope

Pydantic→frozen-dataclass conversion of Group C internal types (`SubmitEnvelopeState`, `SubmissionLogEntry`, etc.) is story 10c.

## Acceptance criteria

- [ ] `decision/portfolio_manager/submit_envelope/` is a package; the former single file is split into `types.py`, `process.py`, `dispatch.py`, `persist.py`, `server.py`, plus curated `__init__.py`.
- [ ] Each submodule is ≤500 LOC (single subsystem per file).
- [ ] `submit_envelope/persist.py` imports `from alphamind.execution.state_persistence.write_paths.phase2 import ...` at the top of the file; zero inline / function-local imports of `phase2` remain.
- [ ] Public surface of `submit_envelope` (via `__init__.py`) is unchanged from pre-decomposition; consumers continue to import without modification.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from alphamind.execution.state_persistence.write_paths.phase2" src/alphamind/decision/portfolio_manager/submit_envelope/` shows only `persist.py` importing it, at the top of the file. The existing PM harness integration tests pass without modification. Manual count: `wc -l src/alphamind/decision/portfolio_manager/submit_envelope/*.py` shows each file under 500 LOC.