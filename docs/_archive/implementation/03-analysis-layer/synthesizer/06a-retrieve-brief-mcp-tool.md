# 06a — `retrieve_brief` MCP tool

## Goal

Wrap the `RetrievalStore.lookup` Python-level interface in a Claude Agent SDK MCP tool the **decision-layer agents (analyst, strategist, PM) — not the synthesizer itself —** call at runtime. The tool accepts a single `ref_id` argument and returns the corresponding section text from the per-invocation retrieval store this synthesizer assembled.

This story owns the MCP wrapper construction. The actual wiring of the tool into each decision-layer agent's `ClaudeAgentOptions` lives in the decision-layer work trees (analyst ALP-115, strategist ALP-116, PM ALP-117); this story provides the construction primitive they import.

## Reading

* `docs/architecture/llm-integration.md` § Tool registration — MCP `@tool` registration pattern using `claude_agent_sdk.tool` plus `create_sdk_mcp_server`.
* `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources and their reference-ID prefixes (`SA-TECH`, `SA-FIN`, `SA-ENERGY`, `CR`, `QR`, `QR-CW`, `AR`, plus sub-typed variants). **No** `DC` prefix exists.
* `docs/architecture/llm-integration.md` § Tool protocol — return-type discipline.
* `src/alphamind/analysis/qualitative_research/harness.py` — sibling harness's MCP wiring as a pattern reference. Note that the qualitative harness uses `_sdk_adapter.build_analysis_mcp_server` over the `TOOLS` registry; **the synthesizer/decision tools cannot use that pattern** because they need per-invocation closures. This story builds the MCP server directly.
* [ALP-203](https://linear.app/alphamind-jatassi/issue/ALP-203) (story 05a) — the `RetrievalStore` whose `.lookup(ref_id) -> str | None` this tool wraps.
* [ALP-208](https://linear.app/alphamind-jatassi/issue/ALP-208) (story 08) — the synthesizer's harness that produces the `RetrievalStore` instance flowing into this tool's closure.

## Depends on

[ALP-203](https://linear.app/alphamind-jatassi/issue/ALP-203) (story 05a) — `RetrievalStore`.

## Scope

Module path is `src/alphamind/analysis/synthesizer/retrieval_tools.py`. The module defines a factory that builds the per-invocation MCP server bound to a specific `RetrievalStore`.

#### Factory signature

`build_retrieve_brief_mcp_server(store: RetrievalStore, *, server_name: str = "alphamind_synthesizer_retrieval") -> tuple[dict[str, McpSdkServerConfig], list[str]]`. Returns `(mcp_servers_dict, allowed_tool_names)` ready for direct assignment to `ClaudeAgentOptions.mcp_servers` and `ClaudeAgentOptions.allowed_tools`.

#### Implementation pattern

Construct one inner `@tool`-decorated async handler that closes over `store`. The decorator declares the tool name `retrieve_brief` and the input schema `{"type": "object", "properties": {"ref_id": {"type": "string"}}, "required": ["ref_id"]}`. The handler reads `args["ref_id"]`, calls `store.lookup(ref_id)`, and returns either the section text or a graceful "not found" message wrapped as `{"content": [{"type": "text", "text": ...}]}`. The handler does not raise on missing IDs; raising would break the SDK loop.

After defining the handler, call `create_sdk_mcp_server(name=server_name, tools=[handler])` and build the allowed-tools list as `[f"mcp__{server_name}__retrieve_brief"]`. Return both.

The closure pattern is what makes this per-invocation. Each call to `build_retrieve_brief_mcp_server` produces a fresh server bound to a fresh `RetrievalStore`.

#### Tests

Test module path is `tests/analysis/synthesizer/test_retrieval_tools.py`. Test cases listed below.

* `test_factory_returns_server_and_allowed_tools` — shape of return value.
* `test_handler_returns_section_for_known_id` — wire a `RetrievalStore` with a known entry; invoke the handler directly; assert text content matches.
* `test_handler_returns_text_for_unknown_id` — handler returns a graceful text message (not exception) for unknown IDs.
* `test_handler_handles_subtype_prefixes` — sample includes `SA-TECH-ANOM-3` and `QR-CW-2`; handler resolves both.
* `test_each_call_produces_isolated_server` — two calls with different stores produce handlers that resolve to different sections.

## Out of scope

* Wiring the tool into a decision-layer agent's `ClaudeAgentOptions` — that lives in each decision-layer agent's work tree.
* The synthesizer's own harness (story 08) — the synthesizer does not call `retrieve_brief`; it produces the store the decision agents read.
* Cross-invocation persistence of the retrieval store.

## Acceptance criteria

- [ ] `build_retrieve_brief_mcp_server(store)` returns `(mcp_servers_dict, allowed_tools)`.
- [ ] Handler resolves known IDs to section text.
- [ ] Handler returns a graceful text message (not exception) for unknown IDs.
- [ ] Sub-typed prefixes (`SA-TECH-ANOM-3`, `QR-CW-2`) resolve correctly.
- [ ] Each factory call produces an isolated server (closure over its own store).
- [ ] No `DC` prefix references anywhere in tests, code, or docstrings.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.