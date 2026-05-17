# 10d — Convert portfolio_state non-activity_log internal Pydantic types to frozen dataclass

## Goal

Convert the ~71 portfolio_state internal Pydantic types outside `events/activity_log.py` (which is owned by story 06a) to `@dataclass(frozen=True, slots=True)`. Hot files: `records/orders.py` (12), `aggregates/thesis_quality.py` (10), `records/positions.py` (8), `records/theses.py` (5), plus the rest. Migration is leaf-first: convert types with no downstream Pydantic consumers first, let mypy --strict verify the call sites, proceed upward.

This is the largest single Pydantic→dataclass story by volume (~71 types). Coordination with downstream is critical — `portfolio_state.records.positions` is imported by 30+ modules outside portfolio_state.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L5 + portfolio_state-subdivision LB-2 (activity_log) is owned by 06a; this story is the rest
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decision (D), (F)
* `src/alphamind/portfolio_state/records/{positions,orders,theses,capital}.py` — hot files for L9 (per audit per-subdivision count)
* `src/alphamind/portfolio_state/aggregates/{thesis_quality,drawdown,risk_parameters,risk_budget,exposure}.py`
* `src/alphamind/portfolio_state/consumers/{analyst,strategist,portfolio_manager,synthesizer}.py` — view types
* `src/alphamind/portfolio_state/snapshot.py:122-269` — 9 `@model_validator(mode="after")` chain (high-yield HY-4: coalesce into one `_validate_all`)
* Story 05a (<issue id="8626cb73-9ffb-493a-a6b0-f66c21172e8f">ALP-461</issue>) — IDs are NewType after this story migrates ID fields
* Story 05b (<issue id="2852122e-7fa0-4d6f-8f52-9bf285a44fb8">ALP-462</issue>) — Money/Price types are available for monetary fields outside activity_log
* Story 06a (<issue id="b72bd206-1c70-42e6-860c-b7f05d24b7f6">ALP-463</issue>) — activity_log is converted separately; coordinates on codec layer

## Depends on

* 06a (<issue id="b72bd206-1c70-42e6-860c-b7f05d24b7f6">ALP-463</issue>) — activity_log refactor must land first (it's the densest conversion site; this story does the rest using the same patterns)

## Scope

In scope: ~71 Pydantic types in `portfolio_state/` outside `events/activity_log.py`. Tests update; codec layer for each record type updates to encode/decode frozen-dataclass instances.

### Leaf-first order

1. `records/positions.py` (8 types) — `PositionRecord` and components; consumed by 30+ outside modules but no Pydantic-internal-only fields
2. `records/orders.py` (12 types) — `OrderRecord`, `BracketRecord`, etc.
3. `records/theses.py` (5 types) — `ThesisRecord`, `ThesisComponent`
4. `aggregates/thesis_quality.py` (10 types) — `ThesisQualityScore` and components
5. `aggregates/{drawdown,risk_parameters,risk_budget,exposure}.py` — `DrawdownState`, `RiskParameterSet`, etc.
6. `consumers/{analyst,strategist,portfolio_manager,synthesizer}.py` view types — depends on aggregates above
7. `snapshot.py` — `PortfolioStateSnapshot` plus the 9 validators (high-yield HY-4)
8. `assembler.py` helpers — `_PriceFields` (`__slots__`-bearing manual class → `@dataclass(frozen=True, slots=True)`)

For each conversion: `class X(BaseModel)` → `@dataclass(frozen=True, slots=True) class X` with `__post_init__` invariants; codec for the table updates to encode/decode dataclass.

### Coalesce snapshot validators (high-yield HY-4)

`snapshot.py:122-269` has 9 separate `@model_validator(mode="after")` methods. Coalesce into one `_validate_all` calling private `_check_*` methods. Pattern in `records/positions.py:259-265` is the template.

### Keep boundary Pydantic

* `repository.py` doesn't have its own Pydantic types — Repository Protocol; uses records
* Anything that round-trips via JSON (verify per type before converting)

## Acceptance criteria

- [ ] ~71 internal Pydantic types under `portfolio_state/` (excluding `events/activity_log.py`) are `@dataclass(frozen=True, slots=True)`.
- [ ] Each table codec (`state/tables/*_codec.py` after 08c) encodes/decodes frozen-dataclass instances.
- [ ] `snapshot.py:122-269` 9 validators are coalesced into one `_validate_all`.
- [ ] `assembler.py` `_PriceFields` is a frozen dataclass.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "BaseModel" src/alphamind/portfolio_state/{records,aggregates,consumers}/` returns zero hits except in any documented boundary type. Round-trip test per record type: instantiate via Hypothesis or fixture, encode via codec, decode, assert exact equality. Snapshot validator timing improves (one pass over positions instead of 9).