# 02b — Extract `commands/` kernel + invert `submit_envelope_mcp`; break decision↔execution cycle

## Goal

Eliminate the 12-module import cycle between `decision.portfolio_manager.*` and `execution.{oms,state_persistence}.*` by hoisting the LLM↔engine wire-format types (`OMSCommand` family, `PMEnvelope`, engine envelope schema) into a new top-level `alphamind/commands/` package and inverting the direction of `submit_envelope_mcp` via a `BrokerDispatch(Protocol)` dependency injected at the composition root. After this story, both decision and execution depend *downward* on `commands.*`; neither imports the other.

The cycle is documented in code (`execution/oms/__init__.py:28-32` docstring; `__getattr__` lazy-loader at `:143-173`) as transitional debt. The audit recommends this fix in finding L3; the punch-list item is #6.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L3 — names the five decision→execution edges and three execution→decision edges
* `src/alphamind/execution/oms/__init__.py:28-32, 143-173` — docstring admits cycle workaround; `__getattr__` lazy-loader is removed by this story
* `src/alphamind/execution/oms/command_models.py` — `OMSCommand` discriminated union + `OpenCommand`/`CloseCommand`/`AdjustCommand`/`CancelCommand`/`AddCommand` variants; moved to `commands/`
* `src/alphamind/execution/oms/engine_envelope.py` — `EngineEnvelope` + `GuardrailTriggerRecord`/`BreachDetails`/`SecondaryBreachCheckResult`; moved to `commands/`
* `src/alphamind/execution/oms/submit_envelope_mcp.py:1-22` — docstring explicitly calls itself "transitional"; this story moves the file out of `execution.oms` into `decision.portfolio_manager.submit_envelope.py`
* `src/alphamind/decision/portfolio_manager/{models.py:34, validation.py:49, harness.py:52,57, runner.py:48}` — five decision-side imports of execution types
* `src/alphamind/execution/{oms/submit_envelope_mcp.py:49-53, oms/command_ids.py:23, state_persistence/write_paths/phase2.py:22-23}` — three execution-side imports of decision types
* `src/alphamind/execution/oms/command_ids.py` — `PMCommandIdComponents`/`EngineCommandIdComponents` parsers; remain in execution but consume `commands.*` types
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decisions (A), (H) — `_kernel/` and `commands/` placement
* `.claude/skills/python-architecture/references/foundations.md` § C1 (hexagonal where domain non-trivial), C4 (constructor injection beats DI containers)

## Depends on

* (none — this story is structurally independent of 01a/01b; can dispatch in parallel with 02a)

## Scope

In scope: new top-level `src/alphamind/commands/` package; relocation of OMS wire-format types; relocation of `submit_envelope_mcp` to `decision.portfolio_manager/`; introduction of `BrokerDispatch(Protocol)` at the kernel boundary; deletion of the `__getattr__` lazy-loader; update of every cross-package import. Tests at `tests/commands/` (new) plus updates to `tests/decision/`, `tests/execution/`.

### 1\. Create `alphamind/commands/` package

New files under `src/alphamind/commands/`:

* `commands/__init__.py` — curated re-exports of the public surface (`OMSCommand`, `PMEnvelope`, `EngineEnvelope`, `BrokerDispatch`, etc.); `__all__` populated
* `commands/command_models.py` — move from `execution/oms/command_models.py` (entire file); preserves the discriminated-union shape and the `Literal`/`Discriminator` boundary application
* `commands/engine_envelope.py` — move from `execution/oms/engine_envelope.py` (entire file)
* `commands/protocols.py` — new file defining `BrokerDispatch(Protocol)` (the callable interface that takes an `OMSCommand` + context and returns a dispatch result) and `ValidationCallable(Protocol)` (the callable interface `submit_envelope_mcp` uses to validate `PMEnvelope` — current concrete is `decision.portfolio_manager.validation.validate_pm_envelope`)

`commands/*` has zero first-party (`alphamind.*`) imports apart from `alphamind._kernel.*` (e.g., `_kernel.ids` if introduced by story 04 — but 02b doesn't strictly require story 04 since IDs remain bare `str` at this stage).

### 2\. Move `submit_envelope_mcp.py` to `decision.portfolio_manager/`

Move the file (1,599 LOC; story 06b will decompose it) from `execution/oms/submit_envelope_mcp.py` to `decision/portfolio_manager/submit_envelope.py`. The file imports `commands.*` for envelope types and `commands.protocols.BrokerDispatch` for the dispatcher. The concrete `BrokerDispatch` implementation stays in `execution` and is passed in at the composition root (`scheduler/orchestrator.py` or wherever PM harness is wired).

### 3\. Define `BrokerDispatch(Protocol)` and wire execution implementation

`commands/protocols.py`:

```python
from typing import Protocol
from alphamind.commands.command_models import OMSCommand

class BrokerDispatch(Protocol):
    async def __call__(
        self,
        command: OMSCommand,
        *,
        context: "DispatchContext",
    ) -> "DispatchResult": ...
```

`execution/oms/broker_dispatch.py` (existing or new) implements the Protocol. The composition root constructs the concrete dispatcher and passes it into the PM harness.

### 4\. Remove `__getattr__` lazy-loader

Delete `execution/oms/__init__.py:143-173` (the `__getattr__` block). Update the docstring (`:28-32`) to reflect that the cycle is gone and `submit_envelope_mcp` lives in decision. The `__init__.py` becomes a normal re-export module.

### 5\. Update all cross-package imports

* `decision/portfolio_manager/{models.py:34, validation.py:49, harness.py:52,57, runner.py:48}` retarget from `execution.oms.*` to `alphamind.commands.*`
* `execution/oms/command_ids.py:23` retarget from `decision.portfolio_manager.*` (was `TYPE_CHECKING`) to `alphamind.commands.command_models`
* `execution/state_persistence/write_paths/phase2.py:22-23` retarget from `decision.portfolio_manager.*` to `alphamind.commands.command_models` and to `decision.portfolio_manager.validation` (the validation callable injected via Protocol)

### Out of scope

* The decomposition of `submit_envelope_mcp.py` into 5 submodules is story 06b — this story moves the file as-is.
* The decomposition of `phase2.py` by OMS command kind is story 06c.
* `NewType` ID aliases (`OrderId`, `EnvelopeId`, etc.) are story 04; this story keeps bare `str` IDs.
* `Decimal` money primitives are story 04+05b; this story keeps bare `float` in `command_models.py`.

## Acceptance criteria

- [ ] `src/alphamind/commands/__init__.py`, `commands/command_models.py`, `commands/engine_envelope.py`, `commands/protocols.py` exist; `__all__` curated.
- [ ] `commands/*` modules import zero first-party `alphamind.*` modules except `alphamind._kernel.*`.
- [ ] `src/alphamind/execution/oms/command_models.py` and `oms/engine_envelope.py` are deleted (their contents now live in `commands/`).
- [ ] `src/alphamind/decision/portfolio_manager/submit_envelope.py` exists with the former contents of `execution/oms/submit_envelope_mcp.py`; the latter file is deleted.
- [ ] `commands/protocols.py` defines `BrokerDispatch(Protocol)` and `ValidationCallable(Protocol)`; `execution/oms/broker_dispatch.py` implements `BrokerDispatch`; the composition root passes the concrete dispatcher into the PM harness.
- [ ] `execution/oms/__init__.py:143-173` `__getattr__` lazy-loader is deleted; the `__init__.py` is a normal re-export module.
- [ ] Zero hits for `from alphamind.decision.portfolio_manager` across `src/alphamind/execution/` (per `grep -rn`); execution no longer imports decision.
- [ ] Zero hits for `from alphamind.execution.oms.command_models\|from alphamind.execution.oms.engine_envelope\|from alphamind.execution.oms.submit_envelope_mcp` across `src/` (per `grep -rn`); all consumers retargeted to `alphamind.commands.*` or `alphamind.decision.portfolio_manager.submit_envelope`.
- [ ] Running `analyze_imports.py` reports the 12-module decision↔execution cycle is no longer present.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`python .claude/skills/python-architecture/scripts/analyze_imports.py src/alphamind | jq '.cycles'` returns ≤2 cycles, none of them passing through `decision.portfolio_manager.*` and `execution.{oms,state_persistence}.*` together. `python -c "import alphamind.execution.oms; assert not hasattr(alphamind.execution.oms, '__getattr__')"` succeeds. End-to-end PM submission path test passes (e.g., the existing `tests/decision/portfolio_manager/test_harness.py` or `scripts/verify_pm.py` if it's executable in tests).