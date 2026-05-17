# 10a — Convert analysis internal Pydantic types to frozen dataclass; inject Clock into tools

## Goal

Convert the 31 analysis-subdivision internal Pydantic types identified by the audit's L5 finding to `@dataclass(frozen=True, slots=True)` with `__post_init__` invariants. These are `HarnessSuccess`, every runner result (`DomainResearcherResult`, `QualitativeResearcherResult`, `AdaptiveResearcherResult`, `SynthesizerResult`), every `InputBundle`, every `ValidationError`/`ValidationResult`, `DomainResearchersOutput`, `RetrievalStore`, SQL-row mirror classes in `qualitative_research/loaders.py` (`SentimentAggregate`, `PredictionMarketSnapshot`, `CalendarEvent`, `ActiveThesis`, `QualitativeInputs`), and `HeadlineEntry`/`EventEntry`/`SectorQualitativeInput` in `domain_researchers/qualitative_input.py`.

Boundary types stay Pydantic: tool I/O (`analysis/tools/*` payload classes), brief schemas (`*/models.py` brief classes), `TokensUsed`, `BriefBundle`.

Also inject a `Clock` Protocol into `analysis/tools/*` so the 8 `datetime.now(UTC)` sites stop requiring monkey-patching for replay tests (folded in from former story 12c).

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L5 (Pydantic-for-internal) + Worth Knowing W6 (Clock injection)
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decision (D) — split-by-subdivision rationale
* `src/alphamind/analysis/domain_researchers/runner.py:58` `DomainResearcherResult` — exemplar conversion site
* `src/alphamind/analysis/_shared.py:51` `TokensUsed` — keep as Pydantic (boundary)
* `src/alphamind/analysis/tools/_sdk_adapter.py` — tool MCP wrapper (boundary, keep Pydantic)
* Story 06d (<issue id="a0ce0f10-cf3a-4d14-8ad5-50a6e9b0e8b6">ALP-466</issue>) — `_harness_core` extraction; `HarnessSuccess` lives in core or per-harness — coordinate

## Depends on

* 06d (<issue id="a0ce0f10-cf3a-4d14-8ad5-50a6e9b0e8b6">ALP-466</issue>) — harness core extraction must land first; the converted `HarnessSuccess` lives in or alongside it

## Scope

In scope: convert 31 named internal types to frozen dataclass; inject Clock into `analysis/tools/*`. Tests update accordingly.

### 1\. Pydantic→dataclass conversions

For each of the 31 sites (audit names them):

```python
# Before
class DomainResearcherResult(BaseModel, frozen=True):
    sector: Sector
    brief: SectorBrief
    ...

# After
@dataclass(frozen=True, slots=True)
class DomainResearcherResult:
    sector: Sector
    brief: SectorBrief
    ...
    def __post_init__(self) -> None: ...  # invariants
```

Leaf-first ordering:

1. SQL-row mirrors (`SentimentAggregate`, `PredictionMarketSnapshot`, `CalendarEvent`, `ActiveThesis`, `QualitativeInputs`)
2. `*Result` types (consumed only by [runner.py](http://runner.py) / [pipeline.py](http://pipeline.py))
3. `HarnessSuccess` per harness (coordinated with 06d)
4. `InputBundle` types
5. `ValidationError`/`ValidationResult`
6. `RetrievalStore`, `DomainResearchersOutput` (these are at the package boundary — verify no JSON round-trip before converting)

### 2\. Clock injection in tools

Each tool factory in `analysis/tools/*` takes a `clock: Clock` parameter (Protocol returning `datetime`); the 8 `datetime.now(UTC)` sites replace with `clock.now()`. Production composition root passes a `RealClock`; replay tests pass a frozen-time fake. The harness already has `now_utc` plumbing — wire through.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class RealClock:
    def now(self) -> datetime: return datetime.now(UTC)
```

Likely home for `Clock`: `_kernel/clock.py`.

### 3\. Keep boundary Pydantic

`analysis/tools/_sdk_adapter.py`, brief schemas in `*/models.py`, `TokensUsed`, `BriefBundle` — all stay Pydantic. The L9 scanner false-positives on these (already documented in audit).

### Out of scope

Other subdivisions' Pydantic conversions are stories 10b, 10c, 10d, 10e. Adaptive validation's `BriefRegistry` refactor (audit Worth Knowing) is a separate concern.

## Acceptance criteria

- [ ] 31 internal types are `@dataclass(frozen=True, slots=True)`; no `BaseModel` in analysis non-boundary modules.
- [ ] Boundary types (tool I/O, brief schemas, `TokensUsed`, `BriefBundle`) remain Pydantic.
- [ ] `src/alphamind/_kernel/clock.py` defines `Clock(Protocol)` and `RealClock`.
- [ ] Each tool factory in `analysis/tools/*` accepts a `clock: Clock`; `datetime.now(UTC)` calls inside tools are gone.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "BaseModel" src/alphamind/analysis/{domain_researchers,qualitative_research,adaptive_research,synthesizer}/{runner,harness,validation}.py` returns zero hits except in any preserved boundary types (manually verified). Replay test with frozen clock produces byte-identical diagnostic output. `grep -rn "datetime.now" src/alphamind/analysis/tools/` returns zero hits.