# Architecture refactoring audit summary (2026-05-12)

This doc summarizes the [2026-05-12 python-architecture audit](../audit-alphamind-2026-05-12.html) and the cross-cutting refactor that implemented its findings. The parent Linear issue is [ALP-454](https://linear.app/alphamind-jatassi/issue/ALP-454); the work landed across 27 stories ([ALP-455](https://linear.app/alphamind-jatassi/issue/ALP-455) through [ALP-481](https://linear.app/alphamind-jatassi/issue/ALP-481)) in 12 dependency-ordered waves between 2026-05-12 and 2026-05-15.

The audience for this doc is future contributors who want to understand the post-refactor architecture (where the boundary types live, which modules are leaves, how money/IDs flow, how the import-linter contracts are wired).

## Headline findings — before / after

| Audit ID | Finding | Before (2026-05-12) | After (2026-05-15) | Story refs |
| --- | --- | --- | --- | --- |
| **L1** | No `import-linter` configuration | 0 contracts; layer rules aspirational | **8 contracts** in `.importlinter`, wired into `CLAUDE.md` lint suite | ALP-455 (01a), ALP-459 (03), ALP-467 (07) |
| **L2** | 10-module cycle through `portfolio_state ↔ risk_guardrails ↔ distillation ↔ persistence` | Cycle present; 4 workarounds (`__getattr__`, `TYPE_CHECKING`, lazy imports, `# noqa: E402`) keeping it runnable | **Eliminated.** Regime/calibration enums hoisted to `_kernel/regime.py` + `_kernel/calibration.py` | ALP-457 (02a) |
| **L3** | 12-module cycle through `decision ↔ execution` | Cycle present; sustained by `submit_envelope_mcp.py` reaching into `execution.oms.*` from `decision.portfolio_manager.*` | **Eliminated.** Wire-format types hoisted to `alphamind/commands/`; `BrokerDispatch` Protocol inverted the direction; `submit_envelope.py` reclassified as composition root with `ignore_imports` exception | ALP-458 (02b), ALP-459 (03) |
| **L4** | Bare `except Exception` / `except BaseException` | 61 (audit count; scanner reports 60 today after preceding cleanup) | **29.** Every remaining handler is warranted (outer-supervisor, third-party SDK callback boundary) with inline comment | ALP-480 (12b) |
| **L5/L9** | Pydantic `BaseModel` for internal types | 363 total, ~180 warranted boundary + ~180 internal violations | **192 total.** Only warranted boundary types remain: LLM I/O, vendor JSON envelopes (Alpaca SDK), YAML config, persistence codec entry points. Internal types are now `@dataclass(frozen=True, slots=True)` with `__post_init__` invariants | ALP-463 (06a), ALP-474 (10a), ALP-475 (10b), ALP-476 (10c), ALP-477 (10d), ALP-478 (10e) |
| **L6** | Distillation P1 (functional core / imperative shell) violation — `Session` threaded into compute | All `q*/assemble.py` carry SQLAlchemy session calls | **Pilot landed.** Compute/load split applied to `q1` (gap) and `q3.flow_classification`; Protocol-shaped `DistillationRepository` is the seam. Remaining propagation tracked as [ALP-484](https://linear.app/alphamind-jatassi/issue/ALP-484) (q3 rest), [ALP-485](https://linear.app/alphamind-jatassi/issue/ALP-485) (q6), [ALP-486](https://linear.app/alphamind-jatassi/issue/ALP-486) (q7), [ALP-487](https://linear.app/alphamind-jatassi/issue/ALP-487) (qualitative) | ALP-467 (07) |
| **L7** | Pydantic config leaking through compute code | `DistillationConfig` Pydantic reached into 10+ distillation compute modules | **Boundary→domain pilot landed.** `config/models/distillation.py` keeps the Pydantic model at the YAML-load boundary; `distillation/_config_domain.py` is the frozen-dataclass mirror; consumers depend on the domain type | ALP-471 (09a) |
| **L8** | God modules at the OMS write boundary | `activity_log.py` (1033 LOC), `submit_envelope_mcp.py` (1599 LOC), `phase2.py` (1684 LOC), `scheduler/orchestrator.py` (1085 LOC), 4 LLM harnesses (~3250 LOC total, ~90% duplicated) | **Decomposed.** `events/` per-event-group submodules; `submit_envelope/` 5-submodule package; `write_paths/phase2/` per-OMS-command-kind split; orchestrator helpers lifted to feature packages; 4 LLM harnesses share `analysis/_harness_core.py` | ALP-463 (06a), ALP-464 (06b), ALP-465 (06c), ALP-466 (06d), ALP-472 (09b) |
| **L9** | Misplaced cross-cutting modules | `execution/state_persistence/` imported 125× from outside execution; `_atomic_write` duplicated across 3 packages; `data_sources/_common.py` 619 LOC / 8 concerns | **Extracted.** `state_persistence/` promoted to top-level `alphamind/state/`; `_atomic_write` hoisted to `_kernel/atomic_io.py`; `data_sources/_common.py` split into focused submodules; new top-level `commands/` and `_kernel/` packages introduced | ALP-470 (08c), ALP-473 (09c), ALP-458 (02b), ALP-460 (04) |
| **L10** | Async-over-sync (fake-async stub layer) | `portfolio_state/repository.py` + reader + pricing all `async def` over synchronous SQLite; `asyncio.gather` / `create_task` instead of `TaskGroup` | **Stripped.** `portfolio_state/` Repository / Reader / Provider are sync; `asyncio.gather` calls in `analysis/domain_researchers/orchestrator.py` + `pipeline/analysis.py` migrated to `TaskGroup`; `scheduler/supervisor.py` + `execution/continuous_monitor/` hand-rolled supervisors use structured concurrency | ALP-468 (08a), ALP-469 (08b) |
| **L11** | `float` for monetary fields; bare `str` for IDs | 115 `: float` annotations for money; 0 `Decimal`, 0 `NewType` aliases anywhere in `src/alphamind/` | **Decimal at boundary.** `_kernel/money.py` (`Money` / `Price` NewType-Decimal) + `_kernel/ids.py` (`OrderId`, `PositionId`, `BracketId`, `CommandId`, `Symbol`, `OccSymbol`, `InvocationId`, `ThesisId`, etc.) introduced. All `activity_log` events use `Money`. Decimal-as-text codec round-trips through SQLite TEXT columns. Residual `: float` in `commands/*` + `decision/*/models.py` are LLM/JSON wire-format boundary types where float is the correct codec (the `unit` discriminator disambiguates rule-value vs USD) | ALP-460 (04), ALP-461 (05a), ALP-462 (05b), ALP-463 (06a) |
| **L12** | Mocks of vendor SDKs in `tests/data_sources/` | 244 `MagicMock` / `Mock()` constructions across vendor adapters | **0.** Each vendor SDK gets a Protocol (`AlpacaTradingProtocol`, `PolygonRESTProtocol`, …) + an in-memory fake implementing the Protocol. The vendor SDK is constructor-injected; tests pass the fake | ALP-479 (12a) |

## Verification gates (run at story 13 close)

* **Cycle count:** `analyze_imports.py` reports 3 remaining cycles, all intra-package and none touching the audit-target modules (`portfolio_state.aggregates`, `risk_guardrails.*types`, `distillation.calibration`, `persistence.models`, `decision.portfolio_manager.*`, `execution.{oms,state_persistence}.*`). The 2 small intra-package cycles in `config.models` and `distillation.{calibration_snapshot, orchestrator}` predate the audit and are out of scope; the 12-node `portfolio_state.events` SCC is the deliberate lazy-dispatch validator in `events/types.py:_validate_entry_dispatch`. Regression-guarded by `tests/test_architecture_invariants.py`.
* **Money migration:** `grep -rn ": float" src/alphamind/{commands,decision/*/models.py,portfolio_state/snapshot.py,portfolio_state/events,execution/broker_adapter/queries.py}` returns only wire-format/quantity/ratio hits (no internal-computation USD). `portfolio_state/snapshot.py` and `portfolio_state/events/*` are fully `Money` / `Price`.
* **Import-linter:** `uv run lint-imports` — all 8 contracts kept, 0 broken. The synthetic-violation test in `tests/test_import_linter.py` confirms the framework catches new violations.
* **Test suite:** `uv run pytest -n auto` — 7810 passed, 2 skipped, 11 warnings, ~48s runtime (xdist with 10 workers). No mock-pinning regressions from ALP-479's fake substrate.
* **Antipattern scan:** L4 = 29 (down from 61), L9 = 192 (down from 363), L19 = 61 (down from 113), L12 = 86. All within the warranted-residue band asserted by `tests/test_architecture_invariants.py`.

## Architecture invariants going forward

`tests/test_architecture_invariants.py` (added by ALP-481 / 13) acts as the regression guard for the headline findings:

* **Cycles**: no audit-target module may appear in any cycle; every remaining cycle must stay inside a single top-level alphamind package.
* **Antipattern counts**: L4 ≤ 35, L9 ∈ [100, 250], L19 ≤ 90.
* **Contract set**: the 8 named contracts in `.importlinter` must all be present.

Drift from any of these baselines trips a test. The CLAUDE.md lint suite (`ruff`, `ruff format`, `mypy`, `lint-imports`) plus the architecture-invariants test should catch regressions before they reach review.

## Follow-up issues

Filed at audit close, parented to ALP-454:

* [ALP-482](https://linear.app/alphamind-jatassi/issue/ALP-482) — Audit `submit_envelope.py` for composition-root extraction (hoist to top-level `composition_roots/` vs keep). **Concluded keep**: the post-06b package is not a pure assembler — `process`, `dispatch`, and `server` carry PM request-routing logic alongside the wiring. The `submit_envelope.*` entries in the `decision-not-execution` `ignore_imports` block are therefore permanent architectural documentation, not a debt item.
* [ALP-483](https://linear.app/alphamind-jatassi/issue/ALP-483) — Relocate `portfolio_state/library_snapshot.py` to the consumer side (`risk_guardrails/`); retires the 2 `portfolio_state-not-risk_guardrails` `ignore_imports` entries.
* [ALP-484](https://linear.app/alphamind-jatassi/issue/ALP-484) — Propagate compute/load split to q3 (anomalies, atm_iv_baseline, etf_iv_divergence).
* [ALP-485](https://linear.app/alphamind-jatassi/issue/ALP-485) — Propagate compute/load split to q6 (macro / funding stress, 1380 LOC).
* [ALP-486](https://linear.app/alphamind-jatassi/issue/ALP-486) — Propagate compute/load split to q7 (cross-asset / correlation, 8 modules).
* [ALP-487](https://linear.app/alphamind-jatassi/issue/ALP-487) — Propagate compute/load split to qualitative_derived (3 public computations).
* [ALP-488](https://linear.app/alphamind-jatassi/issue/ALP-488) — Remove broad `mypy: disable-error-code` directives from 53 test/script files (ALP-477 follow-up). Wrap test fixtures with NewType constructors instead of suppressing.

Each of ALP-484–487 unlocks parallel execution in Phase 2 by lifting its q-package into its own `TaskGroup` task. ALP-483 retires the `portfolio_state-not-risk_guardrails` `ignore_imports` exceptions; ALP-488 removes the broad mypy directives.

## Story-by-story landings

Each row is a sub-issue of ALP-454, in landing order:

| Wave | Story | Description |
| --- | --- | --- |
| 1 | [ALP-455](https://linear.app/alphamind-jatassi/issue/ALP-455) (01a) | Import-linter scaffolding — initial 2 contracts |
| 1 | [ALP-456](https://linear.app/alphamind-jatassi/issue/ALP-456) (01b) | Trivial layer-violation cleanup bundle |
| 2 | [ALP-457](https://linear.app/alphamind-jatassi/issue/ALP-457) (02a) | `_kernel/regime` + `_kernel/calibration`; break 10-module cycle |
| 2 | [ALP-458](https://linear.app/alphamind-jatassi/issue/ALP-458) (02b) | `commands/` kernel + invert `submit_envelope_mcp`; break decision↔execution cycle |
| 3 | [ALP-459](https://linear.app/alphamind-jatassi/issue/ALP-459) (03) | Tighten import-linter (forbid cycle-prone edges); add 5 more contracts |
| 4 | [ALP-460](https://linear.app/alphamind-jatassi/issue/ALP-460) (04) | Create `_kernel/ids.py` + `_kernel/money.py` (type primitives) |
| 5 | [ALP-461](https://linear.app/alphamind-jatassi/issue/ALP-461) (05a) | Migrate command IDs to NewType aliases |
| 5 | [ALP-462](https://linear.app/alphamind-jatassi/issue/ALP-462) (05b) | Migrate boundary money to Money/Price (Decimal at boundary) |
| 6 | [ALP-463](https://linear.app/alphamind-jatassi/issue/ALP-463) (06a) | Refactor `portfolio_state/events/activity_log.py` (split + Pydantic→dataclass + Money) |
| 6 | [ALP-464](https://linear.app/alphamind-jatassi/issue/ALP-464) (06b) | Decompose `submit_envelope.py` into 5 submodules |
| 6 | [ALP-465](https://linear.app/alphamind-jatassi/issue/ALP-465) (06c) | Decompose `phase2.py` by OMS command kind |
| 6 | [ALP-466](https://linear.app/alphamind-jatassi/issue/ALP-466) (06d) | Extract `analysis/_harness_core.py` from 4 duplicated LLM harnesses |
| 7 | [ALP-467](https://linear.app/alphamind-jatassi/issue/ALP-467) (07) | Distillation compute/load boundary split + Protocol-shaped repository pilot; +1 contract |
| 8 | [ALP-468](https://linear.app/alphamind-jatassi/issue/ALP-468) (08a) | Strip async-over-sync in portfolio_state Repository + reader + pricing |
| 8 | [ALP-469](https://linear.app/alphamind-jatassi/issue/ALP-469) (08b) | Replace `asyncio.gather`/`create_task` with TaskGroup; fix async-without-await sites |
| 8 | [ALP-470](https://linear.app/alphamind-jatassi/issue/ALP-470) (08c) | Move `execution/state_persistence/` to top-level `alphamind/state/` |
| 9 | [ALP-471](https://linear.app/alphamind-jatassi/issue/ALP-471) (09a) | Frozen-dataclass mirror for `DistillationConfig` (boundary→domain converter) |
| 9 | [ALP-472](https://linear.app/alphamind-jatassi/issue/ALP-472) (09b) | Lift `scheduler/orchestrator.py` layer-spanning helpers; consolidate Mode enum |
| 9 | [ALP-473](https://linear.app/alphamind-jatassi/issue/ALP-473) (09c) | Split `data_sources/_common.py`; hoist `_atomic_write` to `_kernel` |
| 10 | [ALP-474](https://linear.app/alphamind-jatassi/issue/ALP-474) (10a) | Convert analysis internal Pydantic types to frozen dataclass; inject Clock |
| 10 | [ALP-475](https://linear.app/alphamind-jatassi/issue/ALP-475) (10b) | Convert decision internal Pydantic types to frozen dataclass |
| 10 | [ALP-476](https://linear.app/alphamind-jatassi/issue/ALP-476) (10c) | Convert execution Group C internal Pydantic types to frozen dataclass |
| 10 | [ALP-477](https://linear.app/alphamind-jatassi/issue/ALP-477) (10d) | Convert portfolio_state non-activity_log internal Pydantic types to frozen dataclass |
| 10 | [ALP-478](https://linear.app/alphamind-jatassi/issue/ALP-478) (10e) | Convert risk_guardrails state_delivery internal Pydantic types to frozen dataclass |
| 12 | [ALP-479](https://linear.app/alphamind-jatassi/issue/ALP-479) (12a) | Vendor Protocols + in-memory fakes for `tests/data_sources/` |
| 12 | [ALP-480](https://linear.app/alphamind-jatassi/issue/ALP-480) (12b) | Narrow bare `except Exception` handlers (~36 real sites) |
| 13 | [ALP-481](https://linear.app/alphamind-jatassi/issue/ALP-481) (13) | End-to-end verification + tracker update + memory hygiene (this story) |

Waves 11 and "01c–02c" placeholders in the dependency graph collapsed into adjacent waves during execution; the 27 sub-issues above are the complete set.
