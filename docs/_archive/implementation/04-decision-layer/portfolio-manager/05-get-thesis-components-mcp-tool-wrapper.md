# 05 — get_thesis_components MCP tool wrapper

## Goal

Author the in-process MCP server that exposes the PM's `get_thesis_components(position_id)` tool — a closure over `PortfolioManagerThesisComponentReader` (already shipped in portfolio_state) that returns the full component-level thesis record for one held position. Mirrors the synthesizer's `build_retrieve_brief_mcp_server` and the analyst's `build_validate_guardrail_mcp_server` patterns. Per parent decision (G), the wrapper lives at `src/alphamind/portfolio_state/consumers/portfolio_manager_thesis_mcp.py` (co-located with the producer Protocol it wraps).

## Reading

* `docs/design/04-decision-layer/portfolio-manager.md` § Retrieval tools § Thesis-component retrieval — names the tool's purpose and signature.
* `src/alphamind/portfolio_state/consumers/portfolio_manager.py` — defines `PortfolioManagerThesisComponentReader` (Protocol with `async get_thesis_components(position_id) -> tuple[ThesisComponent, ...]`) and `SnapshotBackedThesisComponentReader` (concrete adapter). The MCP wrapper closes over an instance of the Protocol.
* `src/alphamind/portfolio_state/records/theses.py` — `ThesisComponent` and the thesis record types. The MCP tool's output schema mirrors `ThesisComponent.model_json_schema()`.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — sibling pattern: `build_validate_guardrail_mcp_server` factory closes over a state cell, registers a `@tool` decorated callable, returns `(mcp_servers, allowed_tools)`. Mirror this shape exactly.
* `src/alphamind/analysis/synthesizer/retrieval_tools.py` — sibling pattern (an even-simpler wrapper closing over a `RetrievalStore`). Read for naming and docstring patterns.
* `claude_agent_sdk` — `tool` decorator and `create_sdk_mcp_server` constructor. The MCP wrapper uses these directly.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decision (G) for the wrapper location.

## Depends on

None — Wave 1 dispatch.

## Scope

Code at `src/alphamind/portfolio_state/consumers/portfolio_manager_thesis_mcp.py`. Tests at `tests/portfolio_state/consumers/test_portfolio_manager_thesis_mcp.py`.

### 1\. Factory function

Author `build_get_thesis_components_mcp_server(reader: PortfolioManagerThesisComponentReader) -> tuple[Mapping[str, McpServer], tuple[str, ...]]` (or whatever return-type shape the analyst's wrapper uses — match exactly).

The function constructs an in-process MCP server (via `create_sdk_mcp_server`) registering one `@tool` decorated async callable named `get_thesis_components`. The tool:

* Accepts `position_id: str` as input.
* Calls `await reader.get_thesis_components(position_id)`.
* Returns the resulting `tuple[ThesisComponent, ...]` serialized to a JSON string in the MCP `text` content (mirroring how `validate_guardrail` returns `ValidationResult` JSON).
* On `position_id` not matching any held position, the reader returns `()` — the tool returns an empty list. Do NOT raise; the model handles empty results.

The MCP server name is `alphamind_portfolio_state_thesis_components` (or whatever fits the existing convention — match the analyst wrapper's naming).

The wire-form tool name is `mcp__alphamind_portfolio_state_thesis_components__get_thesis_components`.

The factory returns `(mcp_servers_dict, allowed_tools_tuple)` — the same shape the analyst harness expects.

### 2\. Tool input/output schemas

The `@tool` decorator takes:

* `name="get_thesis_components"`.
* A short description matching the design doc.
* `input_schema={"type": "object", "properties": {"position_id": {"type": "string"}}, "required": ["position_id"]}` — minimal, hand-authored.

The output is text content. The model is documented (in the prompt + the design doc) to expect a JSON-serialized list of `ThesisComponent` records; this story does not export a typed Pydantic schema for the output (the model parses the JSON; the validator does not check tool-output structure).

### 3\. Module-level docstring

The module docstring names:

* The MCP wrapper's purpose (PM-only tool surfacing component-level thesis structure on demand).
* The Protocol it closes over (`PortfolioManagerThesisComponentReader`).
* The wire-form tool name.
* The cross-link to the PM's `<tool_policy>` in `prompts/decision/pm.md` and the design doc § Thesis-component retrieval.

### 4\. Tests

Tests at `tests/portfolio_state/consumers/test_portfolio_manager_thesis_mcp.py`:

* `test_factory_returns_mcp_server_and_allowed_tools` — the factory returns a `(mcp_servers, allowed_tools)` tuple of the expected shape.
* `test_tool_returns_components_for_held_position` — construct a fixture `SnapshotBackedThesisComponentReader` with a known thesis; call the tool's underlying callable directly (without going through SDK); assert the returned text is a JSON-serialized list with the right component count.
* `test_tool_returns_empty_for_unknown_position` — call the tool with a `position_id` not in the snapshot; assert the returned text is `"[]"`.
* `test_factory_does_not_share_state_across_calls` — call the factory twice with the same reader; assert the two `mcp_servers` instances are distinct (closure-isolation property).

The factory is exercised directly via the `@tool` callable's underlying function — do NOT spin up the SDK in unit tests (mirror analyst's test pattern).

### Out of scope

* The harness (story 07) — wires this factory into the SDK.
* The runner (story 08) — constructs the reader from the snapshot.
* The validator (story 06b) — does not interact with this tool.

## Acceptance criteria

- [ ] `src/alphamind/portfolio_state/consumers/portfolio_manager_thesis_mcp.py` exists exporting `build_get_thesis_components_mcp_server`.
- [ ] The factory's return shape matches the analyst's `build_validate_guardrail_mcp_server` factory return shape.
- [ ] The tool's wire-form name is `mcp__alphamind_portfolio_state_thesis_components__get_thesis_components` (or matches the convention; the harness's tool-allowlist references this name).
- [ ] The tool returns a JSON-serialized list of `ThesisComponent` records as text content.
- [ ] The tool returns `"[]"` for a `position_id` not matching any held position (no exception).
- [ ] The factory is closure-isolated — two separate calls produce two independent MCP servers.
- [ ] `tests/portfolio_state/consumers/test_portfolio_manager_thesis_mcp.py` covers each acceptance bullet.
- [ ] `uv run pytest tests/portfolio_state/consumers/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/portfolio_state/consumers/test_portfolio_manager_thesis_mcp.py -n auto` — all tests pass. Confirm the wire-form name is consistent with the harness's tool-allowlist plumbing in story 07.
