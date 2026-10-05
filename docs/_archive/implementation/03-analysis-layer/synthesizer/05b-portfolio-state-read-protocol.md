---
status: not_started
completed_date:
commit_id:
---

# 05b — Portfolio-state read protocol

## Goal

Declare the slim read-only protocol the synthesizer's three portfolio-state tools (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) read from. The protocol decouples the tool implementations (story 06b) from the underlying portfolio-state runtime (an execution-layer concern not yet implemented). The harness-level fixture wires a stub implementation; production wiring happens when the portfolio-state runtime work tree lands.

This story specifies the contract; story 06b implements the tools against it.

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` § Portfolio state tools — the three tools, their inputs/outputs, the "out-of-scope" list of fields the synthesizer deliberately does NOT see (P/L, capital efficiency, system health, activity log, derived metrics)
- `docs/design/01-data-layer/internal/portfolio-state.md` — the authoritative inventory of portfolio-state categories (positions, thesis registry, risk parameters, etc.); this story selects the slim read surface the synthesizer needs
- `docs/design/05-execution-layer/thesis-model.md` — the thesis structure (the `summary` one-liner is what `get_active_theses_summary` returns)
- `docs/design/05-execution-layer/position-model.md` — the position structure (ticker, direction, sector, size at quote-time)
- `docs/architecture/llm-integration.md` § Tool registration — MCP tool registration pattern the next story (06b) follows

## Depends on

- 02 (so the `synthesizer/` Python module path is established)

This story does NOT depend on the portfolio-state runtime existing — it declares the protocol and provides a stub implementation for tests. When the portfolio-state runtime work tree lands, it implements the protocol; no change to story 06b's tool code is required.

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `portfolio_state.py` defining:

  - Closed-set enum:
    - `Direction(StrEnum)`: `LONG = "long"`, `SHORT = "short"`. Re-defined here (rather than imported from a future execution-layer module) to keep this work tree's dependency graph closed; when the execution-layer module lands, both modules can converge on a shared definition.

  - Value-object dataclasses (Pydantic v2, `model_config = {"frozen": True}`):

    - `class PositionSummary(BaseModel)` — the per-position record returned by `get_positions_summary`:
      - `ticker: str`
      - `direction: Direction`
      - `sector: Sector` (re-uses `Sector` from `alphamind.analysis.domain_researchers.models`; this work tree's existing cross-tree dependency)
      - `size_pct: float` — position size as a percentage of total portfolio value (positive number, 0 ≤ x ≤ 100). The exact computation (current market value vs. cost basis vs. notional) is deferred to the runtime implementer, with the convention "current market value as percentage of current total portfolio value" documented in the field's docstring.
      - `position_age_hours: float` — hours since position open (non-negative).

    - `class ThesisSummary(BaseModel)` — the per-thesis record returned by `get_active_theses_summary`:
      - `position_id: str` — the linked position's ID, format `POS-{ticker}-{NNN}` per `position-model.md`.
      - `ticker: str`
      - `summary: str` — the one-liner `summary` field from the thesis model.
      - `key_catalyst: str` — concise catalyst description.
      - `time_expectation_hours: str` — preserved as the prose form (e.g., `"4-24h"`, `"24-72h"`) per the analyst's `time_expectation_hours` shape; not normalized to a number.

    - `class ExposureSnapshot(BaseModel)` — the single record returned by `get_exposure_snapshot`:
      - `sector_exposure_pct: dict[Sector, float]` — per-sector long-minus-short net exposure as a percentage of portfolio value. Sectors with zero exposure are omitted from the dict.
      - `net_directional_pct: float` — total net directional exposure (signed; positive means net long, negative means net short).
      - `gross_exposure_pct: float` — total gross exposure (always non-negative; sum of absolute values).

  - Read protocol via `typing.Protocol`:
    - `class PortfolioStateReader(Protocol)` — three async read methods (the tools are wired into the SDK as `async` functions per `llm-integration.md`'s registration pattern):
      - `async def get_positions_summary(self) -> tuple[PositionSummary, ...]` — current open positions; empty tuple if none.
      - `async def get_active_theses_summary(self) -> tuple[ThesisSummary, ...]` — active theses; empty tuple if none.
      - `async def get_exposure_snapshot(self) -> ExposureSnapshot` — always returns a snapshot (zero-position portfolio returns an `ExposureSnapshot` with empty `sector_exposure_pct` and zero exposures).

  - A test-and-fixture-only stub:
    - `class StubPortfolioStateReader(PortfolioStateReader)` — a concrete implementation backed by frozen tuples / dicts passed at construction. Used by harness tests (story 08) and runner tests (story 10) without depending on the real runtime. Constructor signature: `StubPortfolioStateReader(positions: tuple[PositionSummary, ...], theses: tuple[ThesisSummary, ...], exposure: ExposureSnapshot)`. The stub is plain data; no I/O, no SQL, no clock.

- Unit tests:
  - Each value-object validates field types; rejects negative `position_age_hours`, negative `gross_exposure_pct`, `size_pct` outside `[0, 100]`.
  - `Direction` enum has exactly the two documented members.
  - `ExposureSnapshot` accepts the empty `sector_exposure_pct` dict (zero-position case).
  - `StubPortfolioStateReader` returns the constructor-provided values via each of the three async methods.
  - `StubPortfolioStateReader` is a `PortfolioStateReader` (verified at type-check time via `mypy`; runtime via `isinstance` against the protocol with `runtime_checkable` if used).

Out of scope:
- The MCP tool registration that exposes the three reader methods to the SDK runtime (story 06b).
- The production `PortfolioStateReader` implementation reading from the actual portfolio-state tables (a future execution-layer or data-layer-internal work tree).
- Any field outside the design's "in scope" list — explicitly NOT here: P/L, thesis component-level detail, capital efficiency, system health, activity log, derived metrics (categories 7–11). Per the design, those are decision-layer concerns consumed by the strategist and PM via their own context packages.
- Caching / memoization of reads — the three methods are documented as fresh reads each call; the synthesizer's invocation is short-lived enough that staleness inside one invocation is not a concern, and the production runtime can add caching transparently behind the protocol if profiling reveals it matters.

## Notes

The protocol is `async` (not sync) to match the SDK's `@tool` registration pattern (`async def` callables). When the production reader is backed by SQLAlchemy + `aiosqlite` (the command-center stack per `command-center.md`), this is the natural shape; when backed by an in-memory test stub, the methods just `return` synchronous values from an async function with no overhead.

The deliberately narrow output shape is the design's anti-overspending lever. The synthesizer asks the portfolio "do my upstream signals matter to my book?" — answering that needs ticker, direction, sector, size, age, exposure totals. P/L, recent activity, thesis component status, all the rich detail the strategist and PM consume, would just bloat the synthesizer's context and invite the LLM to do strategist-style reasoning that is not its job.

Per [`feedback_no_inventing_component_names.md`](../../../../.claude/projects/-Users-jatassi-Developer-AlphaMind/memory/feedback_no_inventing_component_names.md), `PortfolioStateReader` is the named protocol; downstream stories cite it. `StubPortfolioStateReader` is the test fixture; the production implementation can name itself whatever fits its location (e.g., `SqlitePortfolioStateReader` under `alphamind.persistence`).

The `Sector` import: this story imports from `alphamind.analysis.domain_researchers.models`. Cross-package import within the analysis layer is fine — both packages live under `alphamind.analysis`. If the import order ever becomes circular (e.g., domain-researchers eventually wanting to import from synthesizer), refactor `Sector` into `alphamind.analysis._common` rather than duplicating it.

Per [`feedback_avoid_numeric_anchors.md`](../../../../.claude/projects/-Users-jatassi-Developer-AlphaMind/memory/feedback_avoid_numeric_anchors.md), the field docstrings should NOT impose numeric thresholds like "size_pct above 5% is a large position." Those are downstream interpretive concerns; the protocol carries the data, not the judgment.

## Acceptance criteria

- [ ] `Direction` enum has exactly `LONG`, `SHORT` members.
- [ ] `PositionSummary`, `ThesisSummary`, `ExposureSnapshot` defined as frozen Pydantic models with the documented fields.
- [ ] `PositionSummary` rejects `size_pct < 0`, `size_pct > 100`, and `position_age_hours < 0`.
- [ ] `ExposureSnapshot` rejects `gross_exposure_pct < 0`.
- [ ] `ExposureSnapshot` accepts an empty `sector_exposure_pct` dict (zero-position case).
- [ ] `PortfolioStateReader` declared as a `typing.Protocol` with three async methods returning the documented value objects.
- [ ] `StubPortfolioStateReader` implements `PortfolioStateReader` and returns constructor-provided values.
- [ ] `mypy` confirms `StubPortfolioStateReader` satisfies `PortfolioStateReader`.
- [ ] No P/L, thesis component, or system-health field appears in any of the three value objects.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
