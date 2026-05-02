---
status: not_started
completed_date:
commit_id:
---

# 06b — Portfolio-state MCP tools

## Goal

Wrap the three `PortfolioStateReader` methods (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot` from story 05b) as Claude Agent SDK MCP tools the synthesizer's harness wires into its `ClaudeAgentOptions(allowed_tools=...)`. The tools render the typed value objects into compact text representations the synthesizer's LLM consumes as tool returns.

The synthesizer is the only consumer; per the design, decision-layer agents have their own deeper portfolio-state surfaces (strategist's full thesis records, PM's thesis-component retrieval).

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` § Portfolio state tools — the tool inventory, return shapes, and the explicit out-of-scope list (P/L, thesis components, capital efficiency, system health, activity log, derived metrics)
- `docs/design/03-analysis-layer/synthesizer.md` § Purpose — usage pattern: cross-referencing market signals against the current portfolio
- `docs/architecture/llm-integration.md` § Tool registration — MCP `@tool` registration pattern, return-shape convention
- `docs/implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — the `PortfolioStateReader` protocol and the three value objects
- `docs/implementation/03-analysis-layer/synthesizer/06a-retrieve-brief-mcp-tool.md` — the sibling MCP-tool factory pattern (mirror its structure for consistency)

## Depends on

- 05b (`PortfolioStateReader`, `PositionSummary`, `ThesisSummary`, `ExposureSnapshot`)

## Scope

In scope: under `src/alphamind/analysis/synthesizer/tools/` —

- `portfolio_state.py` defining a single factory:

  - `def make_portfolio_state_tools(reader: PortfolioStateReader) -> tuple[Callable, Callable, Callable]` returning the three SDK `@tool`-decorated async callables in declared order: `(get_positions_summary_tool, get_active_theses_summary_tool, get_exposure_snapshot_tool)`. Like the `retrieve_brief` factory (story 06a), the factory pattern injects the per-invocation `PortfolioStateReader` via closure; the runner (story 10) calls it once per invocation.

  - Each tool's name and description match the agent-config tool list (story 02):

    - `get_positions_summary` — `"Current open positions: ticker, direction, sector, size as percentage of portfolio, position age in hours."`. Argument schema: `{}` (no arguments).
    - `get_active_theses_summary` — `"Active thesis snapshots: ticker, thesis one-liner, key catalyst, time expectation."`. Argument schema: `{}`.
    - `get_exposure_snapshot` — `"Portfolio exposure profile: per-sector breakdown, net directional exposure, gross exposure."`. Argument schema: `{}`.

  - Return-shape conventions (deterministic compact text the LLM reads as a tool return):

    - **`get_positions_summary`** — Calls `reader.get_positions_summary()`. Renders into:
      ```
      OPEN POSITIONS (count: {N})
      {ticker} | {direction} | {sector} | size {size_pct:.1f}% | age {position_age_hours:.1f}h
      ...
      ```
      Empty case: `OPEN POSITIONS (count: 0)\n(no open positions)`. Sort: by `size_pct` descending, ties broken by `ticker` ascending.

    - **`get_active_theses_summary`** — Calls `reader.get_active_theses_summary()`. Renders into:
      ```
      ACTIVE THESES (count: {N})
      [{position_id}] {ticker}: {summary}
        Catalyst: {key_catalyst} | Time: {time_expectation_hours}
      ...
      ```
      Empty case: `ACTIVE THESES (count: 0)\n(no active theses)`. Sort: by `position_id` ascending (stable lookup against the strategist's referencing).

    - **`get_exposure_snapshot`** — Calls `reader.get_exposure_snapshot()`. Renders into:
      ```
      EXPOSURE SNAPSHOT
      Net directional: {net_directional_pct:+.1f}%
      Gross: {gross_exposure_pct:.1f}%
      Sector breakdown:
        {sector}: {pct:+.1f}%
        ...
      ```
      Sector entries sorted by absolute exposure descending; ties broken by sector name ascending. Empty `sector_exposure_pct` renders `Sector breakdown:\n  (no sector exposure)`. Always returns the snapshot — there is no "no portfolio" case (a zero-position portfolio still has a snapshot with zero exposures).

  - Each tool wraps its rendered text in `{"content": [{"type": "text", "text": rendered}]}` per the SDK convention.

  - Error semantics: if `reader` raises (e.g., the production reader hits a database error), the tool catches the exception and returns `{"content": [{"type": "text", "text": f"Portfolio state unavailable: {error_message}"}], "is_error": True}`. The synthesizer's prompt instructs the LLM that an `is_error: True` response means it should narrate without portfolio context for this synthesis cycle. This treatment is consistent with the `retrieve_brief` "missing reference" semantics — both are recoverable-by-the-LLM tool errors, not SDK-level abort triggers.

- Unit tests under `tests/analysis/synthesizer/`:
  - The factory returns three callables in the declared order with the documented tool names.
  - `get_positions_summary_tool()` against a `StubPortfolioStateReader` with three positions returns the expected formatted text; positions are sorted by `size_pct` descending; ties broken by ticker.
  - Empty-positions case renders `(no open positions)` with the count line.
  - `get_active_theses_summary_tool()` against three theses returns the expected text; theses sorted by `position_id` ascending.
  - Empty-theses case renders `(no active theses)`.
  - `get_exposure_snapshot_tool()` against a snapshot with three sectors returns the expected text; sectors sorted by absolute exposure descending.
  - Snapshot with empty `sector_exposure_pct` renders `(no sector exposure)`.
  - Reader exception → tool returns `is_error: True` payload with the exception message; tool does not re-raise.
  - Tools created from different `PortfolioStateReader` instances do not share state.
  - Determinism: identical reader state → identical rendered text across repeated calls.

Out of scope:
- Wiring the tools into the synthesizer's `ClaudeAgentOptions(allowed_tools=...)` — story 08 (harness) does this.
- The production `PortfolioStateReader` implementation — a future execution-layer or data-layer-internal work tree; this story tests against `StubPortfolioStateReader` from story 05b.
- Caching tool returns within an invocation — the synthesizer typically calls each tool 0–1 times per invocation per the design's "on quiet days, none of these tools may be called" framing; caching adds complexity for negligible gain.
- Decision-layer portfolio-state tools (the strategist's full thesis records via `get_thesis_components`, the PM's `get_thesis_components` retrieval) — those live in their own work trees with deeper read surfaces.

## Notes

The compact text rendering format is chosen for LLM-readability + token efficiency rather than machine-parseability. The synthesizer's LLM is the consumer; it reads the formatted text once per tool call and reasons about it in narrative form. There is no downstream parser; the structured value objects (story 05b) are not preserved past the tool boundary.

Sort orders are documented and tested so the synthesizer's prompt can reference them ("the largest position is at the top of `OPEN POSITIONS`"). Without a stable sort, the prompt would have to reason about ordering each time, and prompt-cache hit rate would degrade because identical reader state could produce different text across calls.

The `is_error: True` treatment of reader failures matches the `retrieve_brief` tool's missing-reference semantics. Per `llm-agent-failure-handling.md § Tool-use error`, raised tool exceptions are SDK-level errors that trigger one retry then abort. Treating "portfolio state momentarily unavailable" as a recoverable in-LLM signal preserves the synthesizer's fail-closed semantics where they belong (the LLM's structural validity) without making transient infrastructure issues escalate to invocation aborts.

The portfolio-state runtime does not yet exist on `main`; story 05b's `StubPortfolioStateReader` is the only concrete implementation at story-completion time. The runner (story 10) wires the stub for tests; production wiring lands when the runtime work tree lands. This story does NOT block on the runtime — the protocol-and-tools surface is fully testable against the stub.

Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md), do NOT introduce a `ToolRegistry` or `ToolBundle` class. The factory returns a tuple; the harness destructures it; the SDK consumes the three callables individually. No abstraction layer.

The `reader` argument is typed as `PortfolioStateReader`. Both the production reader (when it exists) and `StubPortfolioStateReader` satisfy the protocol per story 05b. Mypy enforces this; tests verify both shapes work.

## Acceptance criteria

- [ ] `make_portfolio_state_tools(reader)` returns three SDK `@tool`-decorated async callables in declared order with the documented names.
- [ ] `get_positions_summary` renders the documented format; sorts by `size_pct` desc then ticker asc; empty case renders `(no open positions)`.
- [ ] `get_active_theses_summary` renders the documented format; sorts by `position_id` asc; empty case renders `(no active theses)`.
- [ ] `get_exposure_snapshot` renders the documented format; sectors sorted by absolute exposure desc; empty `sector_exposure_pct` renders `(no sector exposure)`.
- [ ] Each tool wraps output in `{"content": [{"type": "text", "text": rendered}]}`.
- [ ] Reader exception → tool returns `is_error: True` with exception message; tool does not re-raise.
- [ ] Tools created from different `PortfolioStateReader` instances do not share state.
- [ ] Identical reader state produces identical rendered text across repeated calls (determinism).
- [ ] Tool names match the `tools` list in `config/agents.yaml` for the synthesizer (story 02).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
