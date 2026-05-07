# Audit — `alphamind.distillation`

**Date:** 2026-05-06
**Scope:** Medium-depth audit (~30 min) of layout, data modelling, runtime concerns, and testing for the distillation subpackage at `src/alphamind/distillation`. Focus: how typed vendor data moves from the data layer through 54 modules to produce distilled features for the analysis layer.
**Constraints noted:** SQLite and APScheduler are fixed dependencies; not re-litigated. Replay harness and orchestrator are primary scope; detailed q1–q7 category implementations are sampled rather than exhaustive.

---

## 1. Calibration

The distillation layer is the middle of a three-layer pipeline: data collection → **distillation** → analysis. It takes OHLCV bars, news, prediction-market snapshots, macro observations, and event calendars from SQLAlchemy ORM rows and produces distilled feature blocks (price anomalies, gaps, options flow, regime labels, correlation briefs) keyed by ticker, pair, or contract.

The package is organised feature-first by quantile category: `q1/` (price-volume), `q3/` (options), `q6/` (macro), `q7/` (cross-asset), `q12/` (corporate actions), plus shared utilities (`calibration.py`, `baselines.py`, `normalization.py`, `regime.py`) and the orchestrator (`orchestrator.py`). There is also a replay harness (`replay_harness/`) for backtesting and a thin-wrapper module that each category exposes.

The code's self-description is explicit: distillation is a pipeline state-machine (seven phases per `orchestrator.py` docstring) driven by `run_external_distillation()`, an async entry point that refreshes rolling baselines, computes per-category indicators in parallel via `asyncio.gather`, classifies regimes, aggregates anomalies, assembles sector and correlation outputs, writes to archive, and buffers briefs. Every output travels in a frozen dataclass envelope (`OutputBlock`) with calibration tags. Data flows purely through parameters and return values; no global state.

The code claims to be "distillation orchestrator entry point — story 02-distillation-layer/12" and documents every story in comments. The gap between claim and reality is minimal — the architecture is as-documented. Python 3.13+, mypy --strict (targets the distillation layer per `pyproject.toml`), Ruff linter enforced, test suite organised per test type (baselines, external, replay_harness).

---

## 2. Scripts run

- **package_overview**: 54 modules total; 28 without `__all__` declarations (structural signal: the top-level `__init__.py` is empty, so boundary-sealing intent is unclear). 7 god-module candidates: `baselines.py` (1055 lines), `q1/assemble.py` (968), `q6_macro.py` (845), `q3/assemble.py` (718), `qualitative_derived.py` (640), `orchestrator.py` (565), `q12_corporate_actions.py` (547). Import summary: heavy use of internal first-party imports; clean separation between domain logic (pure functions over dataclasses) and I/O (SQLAlchemy ORM, file paths).

- **analyze_imports**: 165 edges, 54 modules. **One cycle detected**: `orchestrator` ↔ `calibration_snapshot`. No cross-boundary violations flagged (the script expects a named layer structure; this is feature-organised, so violations aren't detected). Import pattern is radial: `orchestrator` imports everything (16 first-party deps); category assemblies (`q1.assemble`, `q3.assemble`, `q7.assemble`) import their category internals plus shared utilities. No layer-linter config yet (aspirational rather than enforced).

- **antipattern_scan**: 13 findings. **L12 (float for money)**: 7 instances in price/currency fields (`qualitative_derived.py`, `normalization.py`, `q1/anomalies.py`, `q1/trend_state.py`, `q1/volume_profile.py`). **L4 (bare except)**: 2 instances (`baselines.py:87`, `replay_harness/cli.py:316`). **L25 (print in non-CLI)**: 3 instances in replay-harness CLI (`cli.py`). **L9 (Pydantic BaseModel outside boundary)**: 1 instance (`replay_harness/fixtures.py:46`).

---

## 3. Findings — load-bearing

### 3.1 Cycle between `orchestrator` and `calibration_snapshot` blocks state snapshot writes.

**Where.** `src/alphamind/distillation/orchestrator.py` imports `calibration_snapshot.py`; `calibration_snapshot.py` imports `orchestrator.py` (the `DistillationOutputs` type). Lines: orchestrator:64–65, calibration_snapshot (import).

**Why it matters.** Cycles in the import graph make refactoring and testing brittle (P2: the import graph IS the architecture). Here the cycle is lightweight (not a deep circular dependency), but it indicates orchestrator and snapshot-writer are not cleanly separated. The snapshot writer logically consumes the orchestrator's output; importing back to the orchestrator couples it. Under P4 (hexagonal), the orchestrator is the core; the snapshot writer should be an adapter that consumes its return type.

**Fix.** Move `DistillationOutputs` to a separate `types.py` or `output_models.py` module that both can import without coupling, OR place snapshot-writing logic directly in orchestrator's post-assembly phase so the snapshot writer is just a helper function taking the outputs as a parameter.

---

### 3.2 Seven god-modules (>500 lines each) are difficult to reason about and re-test in isolation.

**Where.** `baselines.py` (1055), `q1/assemble.py` (968), `q6_macro.py` (845), `q3/assemble.py` (718), `qualitative_derived.py` (640), `orchestrator.py` (565), `q12_corporate_actions.py` (547).

**Why it matters.** Modules over ~400 lines exceed the cognitive load threshold (P2: structure is readability). `baselines.py` alone bundles five Class B refresh primitives and supporting time arithmetic and transaction wrapper logic. `q1/assemble.py` stitches together all q1 sub-modules. Each is logically cohesive but their size limits local understanding and makes testing more complex.

**Fix.** Split largest modules by function: `baselines.py` → separate `ticker_baselines.py`, `pair_lag.py`, `contract_history.py` (or extract the time/transaction helpers into `_baseline_utils.py`). For assemble modules, extract the per-sub-indicator assembly steps into focused functions rather than one monolithic builder. Verify that tests still pass (tests are well-structured and should remain workable).

---

### 3.3 No import-linter configuration enforces the feature-organised boundary.

**Where.** Package-wide; no `.importlinter` file (the script checked for one).

**Why it matters.** The code is organised feature-first (q1, q3, q6, q7, q12, replay_harness), and it intentionally avoids a strict layer model (P2: the import graph is the architecture). But without import-linter contracts, the organization is aspirational. Five years from now, a new contributor can mistakenly import `q1.assemble` from `q3` or orchestrator internals from a domain function, and the CI won't catch it.

**Fix.** Add a minimal `.importlinter` config (at the project root, not just distillation) with one contract: `orchestrator` can import all q* and shared modules, but q* modules cannot import `orchestrator`. This enforces the radial dependency pattern without prescribing layers.

---

### 3.4 Pydantic `BaseModel` used in `replay_harness/fixtures.py` outside ingest/config boundary.

**Where.** `replay_harness/fixtures.py:46` — `SliceManifest(BaseModel)` is a frozen Pydantic model used to parse `manifest.json` from disk.

**Why it matters.** `SliceManifest` is at a trust boundary (file parsing), so Pydantic is correct (P5: Pydantic at boundaries). **However**, the fixtures module also exposes loaders that return `SliceManifest` instances flowing into the engine (`replay_harness/engine.py`). If the engine or downstream code treats the Pydantic model as a domain type and mutates it, the frozen-and-validated boundary is compromised. The intent is clear (frozen=True), but the type label is wrong.

**Fix.** After validation in `SliceManifest.model_validate_json()`, convert to a frozen dataclass `@dataclass(frozen=True) class LoadedSliceManifest:` and return that from the loader. The conversion is one-liner post-init. This clarifies that Pydantic is a boundary tool; internal code works with frozen dataclasses.

---

### 3.5 Float used for price and money fields throughout; should be Decimal.

**Where.** 7 antipattern findings in `qualitative_derived.py` (283, 303), `normalization.py` (163), `q1/anomalies.py` (71), `q1/trend_state.py` (140, 157), `q1/volume_profile.py` (74).

**Why it matters.** Float arithmetic silently loses precision on large prices and accumulates rounding error. Money/price should always be `Decimal` with explicit rounding (P3: illegal states unrepresentable; a price with 15 significant digits is an illegal state for finance). The distillation layer operates on vendor OHLCV data (floats from vendor APIs), so conversion at ingest time is clean. But internal functions should work with Decimal.

**Fix.** Import `Decimal` in each module. Change type annotations `price: float` → `price: Decimal` and `price_change: float` → `price_change: Decimal`. Ingest-layer functions that call vendor APIs should parse `float` → `Decimal` once at the adapter boundary (likely in data-layer models or distillation's input envelope). Pair with a test that verifies a known large price round-trips without loss.

---

### 3.6 Bare `except Exception:` in baselines and replay_harness without supervisor comment.

**Where.** `baselines.py:87` (in `_refresh_transaction`), `replay_harness/cli.py:316` (in event loop handler).

**Why it matters.** Bare `except Exception:` (not bare `except:`) is acceptable at the outermost supervisor per P7 (F1: catch narrow, re-raise with from). Both of these are supervisory: `_refresh_transaction` is a transaction wrapper at the data boundary; `cli.py` line 316 is an event-loop error handler. But the intent is not documented with a comment, so future maintainers may assume it's a code smell.

**Fix.** Add a one-line comment above each: `# Outermost supervisor: broad except to prevent data corruption (fail-closed on any error)` for baselines; `# CLI entry-point supervisor: catch Exception to exit gracefully` for cli.py.

---

## 4. Findings — high-yield

### L12: `float` for monetary/price fields (7 instances)

`qualitative_derived.py:283` (price_change), `qualitative_derived.py:303` (price_change), `normalization.py:163` (price_move), `q1/anomalies.py:71` (price_move), `q1/trend_state.py:140` (current_price), `q1/trend_state.py:157` (price), `q1/volume_profile.py:74` (price). Replace with `Decimal` import and type annotation. See fix in finding 3.5 above.

---

### L25: `print()` in replay harness (3 instances)

`replay_harness/cli.py:137`, `:221`, `:321`. The CLI is the appropriate place for print, so this is a false positive from the antipattern scanner (not an error). Confirm intent: if these are operator-facing messages, `print()` is correct; if they should be structured logs, migrate to `logging.info()` and remove the print statements.

---

### L4: Bare `except Exception:` (2 instances)

`baselines.py:87`, `replay_harness/cli.py:316`. Add clarifying comments (see finding 3.6). The exceptions are in correct supervisory contexts and roll back on error correctly; a comment removes ambiguity.

---

### L9: Pydantic `BaseModel` outside boundary (1 instance)

`replay_harness/fixtures.py:46` — see finding 3.4. Convert `SliceManifest` → `LoadedSliceManifest` frozen dataclass after parsing.

---

## 5. Findings — worth knowing

### Calibration state and bootstrap tagging framework is well-designed.

The `CalibratedValue` wrapper (frozen dataclass with state + reason) flows through every distillation output, and the three-state model (CALIBRATED, BOOTSTRAP, UNAVAILABLE) is enforced at the schema level (CHECK constraints mirror the enum in `calibration.py`). This is correct per P3 (illegal states unrepresentable) and well-documented in comments. No concerns here; serve as a model for similar patterns.

---

### Test coverage is comprehensive and well-organised.

The test structure (`tests/distillation/external/`, `tests/distillation/baselines/`, `tests/distillation/replay_harness/`, etc.) mirrors the source structure, tests use in-memory SQLite with proper fixtures, and the orchestrator end-to-end tests exercise the seven-phase contract. A sampling of test files confirms sociable tests (real collaborators) with minimal mocking (per P8). No missing test infrastructure.

---

### Module responsibilities are clear despite size.

Each module's docstring states the story it implements and links to the design docs. The radial pattern (everything → orchestrator; q* → shared utilities; no q* → q* imports) is clear when you read the adjacency list, and the data flows (pure functions returning dataclasses) are free of I/O leaks. This is a well-disciplined codebase; the god-module finding is about size, not responsibility confusion.

---

### Async/concurrency pattern is correct for this shape.

The orchestrator uses `asyncio.gather` to run the six per-category compute phases in parallel, with each phase wrapped in `asyncio.to_thread` to keep the DB-bound work off the event loop (P7: async only at I/O boundary, structured concurrency). The per-category functions are synchronous and DB-bound; `to_thread` is the right tool. No `create_task` without TaskGroup, no fire-and-forget.

---

## 6. What looked good

**Data modelling is consistent across the system.** `OutputBlock` is a frozen dataclass with immutable payloads; `CalibratedValue` is frozen; all internal domain types are frozen dataclasses. The ingest boundary uses Pydantic (config, replay fixtures); internal code uses frozen dataclasses. This is the gold standard per P5. Violations are rare and documented above.

**The orchestrator's seven-phase structure is clear and tested.** The docstring at line 1 enumerates the phases; tests verify each. The phase sequencing (refresh → compute → regime → anomaly aggregation → per-consumer assembly → archive → briefs) is documented and follows the spec. This is a well-governed pipeline.

**Error handling follows the fail-closed principle.** Every Class B refresh wraps in a transaction that rolls back on any exception. The orchestrator does not catch and continue; it propagates failures to the caller. This prevents partial state corruption per `docs/design/mid-pipeline-failure-handling.md`. Correct and documented.

---

## 7. Punch list

1. **[quick win] Add import-linter contract to enforce feature-organised boundaries.** 30 min. Create `.importlinter` at project root with one contract: `orchestrator` cannot be imported by q* modules. Verify no violations. This prevents accidental reverse-dependencies.

2. **[structural] Break the `orchestrator` ↔ `calibration_snapshot` cycle.** 1–2 hours. Extract `DistillationOutputs` to a separate types module, or move snapshot-writing into orchestrator's post-assembly phase as an internal step. Verify all imports resolve; re-run tests.

3. **[quick win] Add clarifying comments to bare `except Exception:` blocks.** 15 min. One-line comments above `baselines.py:87` and `replay_harness/cli.py:316` explaining supervisor intent.

4. **[structural] Split god-modules by responsibility.** 1–2 days. Start with `baselines.py` (split into per-primitive files) and one assemble module (`q1/assemble.py`). Extract helpers into `_utils` submodule. Verify tests pass unchanged; update import statements.

5. **[structural] Replace `float` with `Decimal` for all price/money fields.** 2–3 hours. Bulk replace in the 7 modules listed above; add `Decimal` import and type annotation. Identify ingest boundary (likely data-layer models) and confirm float → Decimal conversion happens once at entry. Add one test: large price → Decimal round-trip.

6. **[quick win] Convert `SliceManifest` (Pydantic) to a frozen dataclass after parsing.** 1 hour. Create `LoadedSliceManifest` frozen dataclass; loaders return that instead. Verifies the boundary pattern is clear.

7. **[forward-looking] Add `mypy --strict` to the full distillation package when modifying above.** Currently strict checking is not enforced project-wide for distillation (see pyproject.toml); once structural changes are made, lock in strict mode for the distillation tree to prevent future regressions.

---

## 8. Open questions for the user

**Q: Is the replay harness a first-class feature or a development/testing utility?** It is currently part of the distillation package (`distillation/replay_harness/`), but logically it's an orchestrator of orchestrator runs. If it's a long-term feature (backtesting, sensitivity analysis), clarify whether it should be a top-level package (e.g., `alphamind/backtester/`) to avoid confusion about what's pipeline vs. tooling. If it's temporary, the location is fine.

**Q: Will briefs ever be persisted?** The orchestrator docstring notes the briefs table is not yet implemented (line 24–26). `DistillationOutputs` carries `correlation_regime_brief` for in-process consumers. Confirm whether persistence is a planned story; if not, document the decision so future maintainers know it's intentional.

---
