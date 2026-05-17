# 08a — Strip async-over-sync in `portfolio_state` Repository + reader + pricing

## Goal

Remove the `async` colouring from `portfolio_state/repository.py` (Protocol + Stub), `consumers/synthesizer.py` (Reader Protocol + `SnapshotBackedSynthesizerReader` adapter), and `pricing.py` (Provider Protocol + Stub) — none of these have any actual `await` in their implementations, and SQLite is synchronous. Per parent issue's Pre-resolved decision (C): SQLite is the persistence engine; Postgres is future-not-immediate; strip now and revisit if/when async is genuinely needed.

After this story: 18 fake-`async def` definitions become sync; `assemble_snapshot` is sync; the 14-concurrent `asyncio.gather` in `assembler.py:449-468` collapses to a sequential 14 reads (which is what's actually happening over SQLite); every consumer of these APIs drops `await`.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L10 part 1
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decision (C)
* `src/alphamind/portfolio_state/repository.py:133-190, 232-305` — 19 async-without-await methods
* `src/alphamind/portfolio_state/consumers/synthesizer.py:73-81, 193-210` — same pattern
* `src/alphamind/portfolio_state/pricing.py:43-113` — same pattern
* `src/alphamind/portfolio_state/assembler.py:449-468` — the decorative `asyncio.gather`
* `.claude/skills/python-architecture/references/runtime.md` § G — async at I/O boundary thesis
* Story 06a (<issue id="b72bd206-1c70-42e6-860c-b7f05d24b7f6">ALP-463</issue>) — `portfolio_state.events` refactor; this story coordinates with any async-await in event-emission paths

## Depends on

* 06a (<issue id="b72bd206-1c70-42e6-860c-b7f05d24b7f6">ALP-463</issue>) — must land first; activity_log refactor changes some consumer signatures and this story coordinates

## Scope

In scope: strip `async` from `portfolio_state/repository.py`, `consumers/synthesizer.py`, `pricing.py`. Update every consumer to drop `await`. Update test fixtures from `pytest.mark.anyio`/`asyncio` to sync.

### 1\. Strip Protocols

`portfolio_state/repository.py:133-190`:

```python
class PortfolioStateRepository(Protocol):
    def load_positions(self, ...) -> Sequence[PositionRecord]: ...  # was async
    # ... 19 methods
```

`consumers/synthesizer.py:73-81`:

```python
class SynthesizerPortfolioStateReader(Protocol):
    def positions(self) -> Sequence[Position]: ...  # was async
    def theses(self) -> Sequence[Thesis]: ...
    def aggregates(self) -> SynthesizerAggregates: ...
```

`pricing.py:43-113` — same pattern.

### 2\. Strip Stubs

`StubPortfolioStateRepository` at `repository.py:232-305` — 19 method bodies become sync (no async wrappers). `SnapshotBackedSynthesizerReader:193-210` — 3 method bodies become sync.

### 3\. Update `assembler.py`

`assemble_snapshot` becomes sync. The `asyncio.gather` at `assembler.py:449-468` collapses to a sequential loop. The function's signature drops `async`.

### 4\. Update consumers

Every consumer of these APIs (most in analysis/, decision/, risk_guardrails/state_delivery/, scheduler/orchestrator.py) drops `await`. The consumers' functions remain `async def` if they have other awaitables; if `repository.load_*()` was the only await, the consumer can become sync.

### 5\. Update tests

`tests/portfolio_state/` — remove `pytest.mark.anyio` / `asyncio` from any test that only awaits these methods. Sync test fixtures replace async ones.

### Out of scope

Async surface elsewhere (broker_adapter, continuous_monitor, analysis SDK calls) stays — these have real I/O. Story 08b handles the `TaskGroup` upgrade for those legitimate async sites.

## Acceptance criteria

- [ ] `portfolio_state/repository.py` Protocol + Stub: zero `async def` definitions.
- [ ] `portfolio_state/consumers/synthesizer.py` Protocol + adapter: zero `async def`.
- [ ] `portfolio_state/pricing.py` Protocol + Stub: zero `async def`.
- [ ] `portfolio_state/assembler.py::assemble_snapshot` is sync.
- [ ] Every consumer of these APIs drops `await`; if any consumer's only await was on these methods, it becomes sync (with `async def` removed where appropriate).
- [ ] Tests use sync fixtures where appropriate; `pytest.mark.anyio`/`asyncio` markers removed from tests that no longer await.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "async def" src/alphamind/portfolio_state/repository.py src/alphamind/portfolio_state/consumers/synthesizer.py src/alphamind/portfolio_state/pricing.py` returns zero hits. The full test suite under `uv run pytest -n auto` passes and the runtime measurably drops (the spin-up of the asyncio event loop per test is gone for these subdivisions).