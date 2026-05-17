# 06b — Portfolio-state MCP tools

## Goal

Wrap the three `SynthesizerPortfolioStateReader` methods (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) as Claude Agent SDK MCP tools the synthesizer's harness wires into its `ClaudeAgentOptions`. The tools render the typed value objects into compact text representations the synthesizer's LLM consumes as tool returns.

The reader Protocol and value objects are already implemented at `src/alphamind/portfolio_state/consumers/synthesizer.py` (shipped under the Portfolio state work tree, ALP-53). This story adds the MCP-tool wrapping only.

## Reading

* `docs/architecture/llm-integration.md` § Tool registration — MCP registration pattern using `claude_agent_sdk.tool` plus `create_sdk_mcp_server`.
* `docs/architecture/llm-integration.md` § Tool protocol — return-type discipline (tools return `{"content": [{"type": "text", "text": ...}]}`).
* `docs/design/03-analysis-layer/synthesizer.md` § Portfolio state tools — the three tools' purpose and return contract.
* `src/alphamind/portfolio_state/consumers/synthesizer.py` — `SynthesizerPortfolioStateReader` Protocol plus value objects (`SynthesizerPositionSummary`, `SynthesizerThesisSummary`, `SynthesizerExposureSnapshot`, `SynthesizerView`) plus concrete adapter (`SnapshotBackedSynthesizerReader`). **Import these directly; do not redefine.**
* `src/alphamind/analysis/qualitative_research/harness.py` — pattern reference for MCP-server construction. Note: the synthesizer's wiring uses **per-invocation closures** rather than the `TOOLS` registry — see parent issue § Notes for the orchestrator.
* [ALP-208](https://linear.app/alphamind-jatassi/issue/ALP-208) (story 08) — the harness that calls this story's factory and wires the result into `ClaudeAgentOptions`.

## Depends on

Nothing in this work tree — the consumer module is already shipped.

## Scope

Module path is `src/alphamind/analysis/synthesizer/portfolio_tools.py`. The module defines a factory that builds the per-invocation MCP server bound to a specific reader, plus three private renderer helpers.

#### Factory signature

`build_portfolio_state_mcp_server(reader: SynthesizerPortfolioStateReader, *, server_name: str = "alphamind_synthesizer_portfolio") -> tuple[dict[str, McpSdkServerConfig], list[str]]`. Returns `(mcp_servers_dict, allowed_tool_names)` ready for direct assignment to `ClaudeAgentOptions.mcp_servers` and `ClaudeAgentOptions.allowed_tools`. The allowed-tools list contains three names of the form `mcp__alphamind_synthesizer_portfolio__<tool>`.

#### Implementation pattern

Define three async handlers inside the factory. Each handler closes over the `reader` parameter and follows the same shape — call the reader's async method, render the result to text, and return the standard MCP content envelope `{"content": [{"type": "text", "text": <rendered>}]}`.

The three handler names are `get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`. Each `@tool` decorator declares an empty input schema (`{"type": "object", "properties": {}}`) since the tools take no arguments. After defining all three, call `create_sdk_mcp_server(name=server_name, tools=[h1, h2, h3])` and build the allowed-tools list.

#### Renderer helpers

Three private functions render the typed value objects into compact, LLM-readable text. Each renderer covers an empty-input branch that returns a graceful sentence rather than an empty string or exception.

* `_render_positions(positions: tuple[SynthesizerPositionSummary, ...]) -> str` — one line per position summarising ticker, direction, sector, size %, position age. Empty input renders to `"No open positions."`.
* `_render_theses(theses: tuple[SynthesizerThesisSummary, ...]) -> str` — one line per thesis summarising ticker, summary, key catalyst, time expectation. Empty input renders to `"No active theses."`.
* `_render_exposure(snapshot: SynthesizerExposureSnapshot) -> str` — sector breakdown plus net directional plus gross exposure. All-zero exposure renders to `"No exposure (all positions flat or empty book)."`.

#### Tests

Test module path is `tests/analysis/synthesizer/test_portfolio_tools.py`. Test cases listed below.

* `test_factory_returns_server_and_allowed_tools` — shape of return value with three tool names.
* `test_positions_handler_returns_text` — fixture reader returning two positions; handler returns text containing both tickers.
* `test_theses_handler_returns_text` — fixture reader returning a thesis; handler returns text containing the thesis summary.
* `test_exposure_handler_returns_text` — text contains sector and net exposure values.
* `test_empty_state_handlers_return_graceful_text` — reader returning empty tuples or zero exposure produces non-empty graceful messages, not exceptions.
* `test_handlers_use_async_reader_methods` — each handler awaits the reader's async method.

## Out of scope

* Implementing or modifying `SynthesizerPortfolioStateReader` or its value objects (already shipped under ALP-53).
* Implementing the production wiring of `SnapshotBackedSynthesizerReader` (the harness story 08 supplies the reader instance per-invocation).
* The harness-level wiring of these tools into `ClaudeAgentOptions` (story 08).

## Acceptance criteria

- [ ] `build_portfolio_state_mcp_server(reader)` returns `(mcp_servers_dict, allowed_tools)` with three tool names.
- [ ] Each handler renders its value object to LLM-readable text.
- [ ] Empty inputs produce graceful text, not exceptions.
- [ ] All three handlers `await` the reader's async methods.
- [ ] No redefinition of `SynthesizerPortfolioStateReader` or any of its value objects.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.