# 01a — Broker-adapter Protocols + phase1_inputs refactor

## Goal

Extract structural Protocols (`AccountStateQueriesP`, `CorporateActionsQueriesP`) over the existing `AccountStateQueries` and `CorporateActionsQueries` classes. Refactor `gather_phase1_inputs` to depend on the Protocols via injected factories instead of the module-level `_build_*` seams. Retires the monkey-patch test seams in `tests/scheduler/test_phase1_inputs.py` in favor of direct factory injection. Foundation for the debug-e2e package's `LogOnlyAccountStateQueries` / `LogOnlyCorporateActionsQueries` (story 02b) to satisfy the same Protocols.

## Reading

* `docs/design/debug-e2e-mode.md` §§ 4 (Types), 5 (Testing seam), 6.1 (Formalize broker adapter Protocols) — Protocol signatures, factory injection shape
* `src/alphamind/execution/broker_adapter/queries.py` — existing `AccountStateQueries` class (sync `get_account` + `get_positions`, async `get_orders` + `get_account_activities`)
* `src/alphamind/execution/broker_adapter/corporate_actions_queries.py` — existing `CorporateActionsQueries` class (async `get_corporate_actions`)
* `src/alphamind/scheduler/phase1_inputs.py` — `_build_account_state_queries` / `_build_corporate_actions_queries` seams to retire; `gather_phase1_inputs` signature to extend
* `src/alphamind/execution/corporate_actions/fetcher.py` § `fetch_unprocessed_ca_activities` — consumes `CorporateActionsQueries`; signature change must remain compatible
* `tests/scheduler/test_phase1_inputs.py` — 4 monkey-patch call sites to migrate

## Depends on

* None — wave 1.

## Scope

In scope: `src/alphamind/execution/broker_adapter/protocols.py` (new) and `src/alphamind/scheduler/phase1_inputs.py` (refactor). Tests at `tests/execution/broker_adapter/` and `tests/scheduler/test_phase1_inputs.py`.

### 1\. New module — `execution/broker_adapter/protocols.py`

Two structural Protocols mirroring the as-built sync/async surface:

```python
from datetime import date
from typing import Protocol, Sequence

from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot, TradeAccountSnapshot,
)
from alphamind.execution.corporate_actions.types import CorporateActionActivity


class AccountStateQueriesP(Protocol):
    def get_account(self) -> TradeAccountSnapshot: ...
    def get_positions(self) -> tuple[PositionSnapshot, ...]: ...


class CorporateActionsQueriesP(Protocol):
    async def get_corporate_actions(
        self, *, start_date: date, end_date: date, symbols: Sequence[str],
    ) -> tuple[CorporateActionActivity, ...]: ...
```

Match the exact method shapes the existing concrete classes already expose — the Protocols are *extracted from* the classes, not invented alongside them. Only the methods `gather_phase1_inputs` consumes go into the Protocol surface (P9 — small Protocol surface). If the as-built `get_corporate_actions` signature differs from the shape above, mirror what's there.

### 2\. Refactor `phase1_inputs.gather_phase1_inputs`

Replace the `_build_account_state_queries` / `_build_corporate_actions_queries` module-level seams with optional factory kwargs:

```python
async def gather_phase1_inputs(
    *,
    handle: InvocationHandle,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    as_of: datetime,
    account_queries_factory: Callable[
        [VenueConfig, ExecutionMode], AccountStateQueriesP,
    ] | None = None,
    ca_queries_factory: Callable[
        [VenueConfig, ExecutionMode], CorporateActionsQueriesP,
    ] | None = None,
) -> Phase1Inputs:
```

When `account_queries_factory is None`, construct the Alpaca-backed default inline (same logic the seam used: `AlpacaClientFactory(venue_config, mode=...)` → `AccountStateQueries(factory.build_trading_client())`). Same for `ca_queries_factory`. The default-construction path is preserved so the production daemon keeps working untouched.

Delete `_build_account_state_queries` and `_build_corporate_actions_queries` — the inline defaults replace them.

### 3\. Migrate `tests/scheduler/test_phase1_inputs.py`

The four `monkeypatch.setattr(module, "_build_account_state_queries", ...)` / `_build_corporate_actions_queries` call sites become direct `account_queries_factory=lambda v, m: _stub_queries` parameter passing through `gather_phase1_inputs`. The test stubs (`_failing_factory`, etc.) keep working — they're just passed via the new kwarg instead of monkey-patched.

### Out of scope

* The orchestrator wiring that reads `RunInvocationContext.debug_e2e.account_queries` and threads it as a factory — story 04 (CLI integration) owns that.
* Changing the sync/async shape of any existing query method — the Protocols mirror what exists today.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/protocols.py` exists with `AccountStateQueriesP` and `CorporateActionsQueriesP` Protocols mirroring the as-built method surfaces (`get_account`, `get_positions` sync; `get_corporate_actions` async).
- [ ] `AccountStateQueries` satisfies `AccountStateQueriesP` and `CorporateActionsQueries` satisfies `CorporateActionsQueriesP` — verified by an `isinstance`-style check in tests (`assert isinstance(real_queries, AccountStateQueriesP)`), with `@runtime_checkable` on the Protocols if required for the check.
- [ ] `gather_phase1_inputs` accepts optional `account_queries_factory` / `ca_queries_factory` kwargs; defaults to inline Alpaca construction when both are None.
- [ ] `_build_account_state_queries` and `_build_corporate_actions_queries` are deleted; no references remain anywhere under `src/` or `tests/`.
- [ ] `tests/scheduler/test_phase1_inputs.py` exercises the failure-path scenarios via the factory parameter, not via monkey-patch.
- [ ] `uv run pytest tests/execution/broker_adapter/ tests/scheduler/ -n auto` passes.
- [ ] `uv run ruff check .` + `uv run ruff format .` + `uv run mypy` + `uv run lint-imports` all clean.

## Verification

`uv run pytest tests/execution/broker_adapter/ tests/scheduler/ -n auto` passes. Full linter chain (`ruff check . && ruff format . && mypy && lint-imports`) clean.