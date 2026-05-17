# 07 — Distillation compute/load boundary split + Protocol-shaped repository pilot

## Goal

Restore P1 (functional core / imperative shell) in `distillation/` by splitting each `q*/assemble.py` into a thin `q*/loaders.py` shell that does all DB reads in one pass and a pure `q*/compute.py` core that operates on frozen dataclass inputs. Introduce a Protocol-shaped `DistillationRepository` as the seam between the shell and core; this is also the pilot for the broader Repository Protocol propagation pattern (audit finding L9). After this story, distillation's orchestrator Phase 2 can run `asyncio.gather(*[asyncio.to_thread(compute_*)...])` over pure functions instead of serializing due to "Session is already flushing" issues with a shared mutable session.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L6 (P1 violation) + L9 (Repository Protocol pattern)
* `src/alphamind/distillation/q1/assemble.py:1103,1156,1176,1247` — pattern of `Session`-threaded compute
* `src/alphamind/distillation/normalization.py` (341 LOC pure, exemplar) — already P1-clean; mirror this style
* `src/alphamind/distillation/q1/indicators.py` (636 LOC pure) — already P1-clean
* `src/alphamind/distillation/orchestrator.py:706-769` — the serialization comment ("Session is already flushing") that this story unlocks
* `src/alphamind/portfolio_state/repository.py` — Protocol-shaped repository template
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decisions (E) — pilot-only scope

## Depends on

* 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/` calibration/regime extraction; the compute/load split's frozen-dataclass inputs may reference `_kernel` types

## Scope

In scope: pilot the compute/load split on `q1/` (gap + anomalies first, then propagate to indicators, volume_profile, trend_state, divergence, relative_performance); introduce `DistillationRepository(Protocol)`; orchestrator Phase 2 parallelization via TaskGroup over `asyncio.to_thread(compute_q1_*)`. Tests at `tests/distillation/q1/test_compute.py` (new — pure compute tests, no SQLite).

### 1\. Define `DistillationRepository(Protocol)`

`src/alphamind/distillation/_repository.py`:

```python
class DistillationRepository(Protocol):
    def load_daily_bars(self, ticker: Symbol, lookback_days: int, as_of: datetime) -> Sequence[Bar]: ...
    def load_latest_baseline(self, ticker: Symbol) -> Baseline | None: ...
    def load_window_returns(self, ticker: Symbol, window: int, as_of: datetime) -> Sequence[Return]: ...
    # ... other read methods exposed to compute
```

Concrete impl in `distillation/_repository_sql.py` closes over `Session`.

### 2\. Split q1 (pilot)

For each q1 module (gap, anomalies, indicators, volume_profile, trend_state, divergence, relative_performance):

* `q1/<sub>_loaders.py` — DB reads only; returns `FrozenInputs` (a frozen dataclass per sub-module)
* `q1/<sub>_compute.py` — pure function `compute(inputs: FrozenInputs, config: DistillationConfig) -> Sequence[OutputBlock]`

`q1/assemble.py` becomes a thin orchestration: `for sub: inputs = loaders.load(...); blocks = compute(inputs, config)`.

### 3\. Orchestrator Phase 2 parallelization

`distillation/orchestrator.py:706-769` — replace the serialized loop with:

```python
async with asyncio.TaskGroup() as tg:
    q1_task = tg.create_task(asyncio.to_thread(compute_q1_blocks, q1_inputs, config))
    q3_task = tg.create_task(asyncio.to_thread(compute_q3_blocks, q3_inputs, config))
    # etc.
```

Loading happens sequentially under one Session before the gather; computation happens in parallel over frozen inputs (no shared mutable state).

### 4\. Propagate the pattern (post-pilot)

After q1 validates the seam, propagate to q3, q6, q7, qualitative — but per audit's multi-quarter framing, the propagation can split into follow-up issues if too large. This story includes q1 + at least one of (q3 OR q6) as proof the pattern propagates beyond pilot.

### 5\. Add import-linter contract

Add a `forbidden` contract: `alphamind.distillation.q*_compute → sqlalchemy`. The pure compute modules must never import the ORM directly.

### Out of scope

Full propagation to q6, q7, qualitative is tracked as follow-up issues filed during this story's completion. Money/Price migration in distillation is permitted but optional (ratios stay `float`).

## Acceptance criteria

- [ ] `src/alphamind/distillation/_repository.py` defines `DistillationRepository(Protocol)` with the read methods.
- [ ] `q1/gap_loaders.py` + `q1/gap_compute.py` exist; `q1/gap.py` is thin orchestration.
- [ ] Same split for q1/anomalies, q1/indicators, q1/volume_profile, q1/trend_state, q1/divergence, q1/relative_performance.
- [ ] Pure compute modules import zero ORM types; `lint-imports` enforces via the new `distillation.q*_compute → sqlalchemy` forbidden contract.
- [ ] `distillation/orchestrator.py:706-769` uses `asyncio.TaskGroup` + `asyncio.to_thread(compute_*)` for Phase 2; no comment about "Session is already flushing".
- [ ] At least one of (q3, q6) follows the same split pattern (proof of propagation).
- [ ] `tests/distillation/q1/test_compute.py` exists with pure-compute tests using lists of bars; zero SQLite usage in these tests.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from sqlalchemy\|session\." src/alphamind/distillation/q1/*_compute.py src/alphamind/distillation/q3/*_compute.py` returns zero hits. Orchestrator timing: a synthetic test with 6 categories × 10 tickers shows Phase 2 wall-clock is roughly 1× the slowest category (parallelism working) rather than the sum (was serialized). Pure-compute test `tests/distillation/q1/test_compute.py::test_gap_anomaly_detection` runs without spinning up `sqlite:///:memory:`.