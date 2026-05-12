# 04a — Adaptive MCP server adapter and tool registry wire-up

## Goal

Generalize `tools/_sdk_adapter.py` from a qualitative-only builder into a single shared primitive both analysis-layer agents use, and wire the five new tools (`sec_lending`, `short_interest`, `earnings_calendar`, `macro_data`, `ticker_deep_pull`) into the `TOOLS` registry. Two-part deliverable per parent issue resolutions (D) and (G):

* **(D)** Replace `build_qualitative_mcp_server(tool_names, session) -> (McpSdkServerConfig, list[str])` with `build_analysis_mcp_server(*, server_name, tool_names, session) -> (dict[str, McpSdkServerConfig], list[str])`. Drop the module-level `MCP_SERVER_NAME` constant and the `_allowed_tool_name` helper. Update the qualitative-research harness's `_resolve_tools` call site to call the new primitive with `server_name="alphamind_qualitative"`. The qualitative-research test suite must continue passing without changes.
* **(G)** Edit `tools/__init__.py` to register the five new tools in `_build_registry()` so the adaptive-research harness (story 05) can resolve them via the same `TOOLS` dict the qualitative harness already uses.

This is the single edit point for `tools/__init__.py` and `tools/_sdk_adapter.py` across the entire adaptive-research work tree — stories 03d and 03e produced their tool modules in isolation.

## Reading

* `src/alphamind/analysis/tools/_sdk_adapter.py` — current implementation. The `_build_sdk_tool` helper, `build_qualitative_mcp_server` builder, `MCP_SERVER_NAME` constant, `_allowed_tool_name` helper.
* `src/alphamind/analysis/tools/__init__.py` — current `_build_registry()` that constructs `TOOLS` with three entries; this story adds five.
* `src/alphamind/analysis/qualitative_research/harness.py` § `_resolve_tools`, `_build_sdk_options` — the call site this story updates. The qualitative harness imports `build_qualitative_mcp_server` from `_sdk_adapter`; that import becomes `build_analysis_mcp_server` after this story.
* `src/alphamind/analysis/tools/{sec_lending,short_interest,earnings_calendar,macro_data,ticker_deep_pull}.py` — the five tool modules whose `*Input`, `*Output`, `*_factory` symbols this story registers.
* `prompts/analysis/qualitative_researcher.md` § `<tool_policy>`, `prompts/analysis/adaptive_researcher.md` § `<tool_policy>` — the agent-side tool descriptions; mirror their phrasing in the registry's `description` field for each new tool.
* Parent issue § Pre-resolved configuration decisions (D) and (G) — the design intent this story implements.

## Depends on

* <issue id="4434d469-816d-4f9a-ac48-313209223abc">ALP-259</issue> (story 03d) — the four simpler tool modules.
* <issue id="2c6e9d78-5087-4978-8082-1894ddbceb4e">ALP-260</issue> (story 03e) — the `ticker_deep_pull` tool module.

## Scope

In scope:

* `src/alphamind/analysis/tools/_sdk_adapter.py` — refactor.
* `src/alphamind/analysis/tools/__init__.py` — add five new `TOOLS` entries.
* `src/alphamind/analysis/qualitative_research/harness.py` — update `_resolve_tools` to use the new primitive.
* `tests/analysis/tools/test_sdk_adapter.py` — extend or add tests for the new shape (server_name parameter, dict-keyed return).

Out of scope:

* Implementing the adaptive-research harness — story 05 (consumes the `build_analysis_mcp_server` primitive this story exposes).
* Touching any of the five new tool modules' source — those land in 03d/03e and do not change here.
* Refactoring the qualitative harness beyond the `_resolve_tools` call-site update.

### 1\. Generalize `_sdk_adapter.py`

Replace the existing module contents with:

```python
"""Adapt :class:`ToolDefinition` entries into Claude Agent SDK MCP tools.

The on-demand tool registry stores typed sync callables of shape
``Callable[[InputModel], OutputModel]``.  The Claude Agent SDK expects
async handlers of shape ``Callable[[dict], Awaitable[dict]]`` registered
via the ``@tool`` decorator and surfaced through an SDK MCP server.

This module bridges the two without leaking either concern back into the
registry or the harnesses.  Both the qualitative-research and adaptive-
research harnesses import :func:`build_analysis_mcp_server` to construct
their per-invocation server bound to its SQLAlchemy session, passing the
agent's chosen MCP server name (``alphamind_qualitative`` or
``alphamind_adaptive``) so the two agents' tool allowlists do not share
an MCP namespace.
"""

# ... existing _build_sdk_tool unchanged ...


def build_analysis_mcp_server(
    *,
    server_name: str,
    tool_names: Sequence[str],
    session: Session,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build the SDK MCP server bound to *session* under *server_name*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools`` respectively.

    *server_name* is the MCP namespace (``alphamind_qualitative`` or
    ``alphamind_adaptive``); the same string keys the returned dict and
    prefixes each entry in ``allowed_tool_names`` (``mcp__<name>__<tool>``).

    The caller is responsible for confirming each name in *tool_names*
    exists in :data:`alphamind.analysis.tools.TOOLS` — this function
    raises :class:`KeyError` otherwise.
    """
    sdk_tools = [_build_sdk_tool(TOOLS[name], session) for name in tool_names]
    server = create_sdk_mcp_server(name=server_name, tools=sdk_tools)
    allowed = [f"mcp__{server_name}__{name}" for name in tool_names]
    return {server_name: server}, allowed


__all__ = ["build_analysis_mcp_server"]
```

Drop:

* The `MCP_SERVER_NAME` constant.
* The `_allowed_tool_name` helper (inlined).
* The `build_qualitative_mcp_server` function (replaced by the new primitive).

### 2\. Update qualitative harness call site

In `src/alphamind/analysis/qualitative_research/harness.py`, replace the import:

```python
from alphamind.analysis.tools._sdk_adapter import build_qualitative_mcp_server
```

with:

```python
from alphamind.analysis.tools._sdk_adapter import build_analysis_mcp_server
```

And update `_resolve_tools` to call the new primitive:

```python
mcp_servers, allowed = build_analysis_mcp_server(
    server_name="alphamind_qualitative",
    tool_names=agent_config.tools,
    session=session,
)
return allowed, mcp_servers
```

(The function returns a dict directly now, so the harness no longer constructs `{"alphamind_qualitative": server}` itself.)

No other change to the qualitative harness. The qualitative-research test suite must pass unchanged after this edit.

### 3\. Register five new tools in `tools/__init__.py`

In `_build_registry()`, add five new entries after the existing three:

```python
from alphamind.analysis.tools.sec_lending import (
    SecLendingInput, SecLendingOutput, sec_lending_factory,
)
from alphamind.analysis.tools.short_interest import (
    ShortInterestInput, ShortInterestOutput, short_interest_factory,
)
from alphamind.analysis.tools.earnings_calendar import (
    EarningsCalendarInput, EarningsCalendarOutput, earnings_calendar_factory,
)
from alphamind.analysis.tools.macro_data import (
    MacroDataInput, MacroDataOutput, macro_data_factory,
)
from alphamind.analysis.tools.ticker_deep_pull import (
    TickerDeepPullInput, TickerDeepPullOutput, ticker_deep_pull_factory,
)

return {
    # ... existing 3 entries ...
    "sec_lending": ToolDefinition(
        name="sec_lending",
        description=(
            "Retrieve securities lending market data for specified tickers: "
            "borrow rates, availability, utilization, days-to-cover, and 5-day cost trend."
        ),
        input_model=SecLendingInput,
        output_model=SecLendingOutput,
        callable_factory=sec_lending_factory,
    ),
    "short_interest": ToolDefinition(
        name="short_interest",
        description=(
            "Retrieve short interest, short-volume ratio, days-to-cover, "
            "borrow cost, and a deterministic squeeze composite for specified tickers."
        ),
        input_model=ShortInterestInput,
        output_model=ShortInterestOutput,
        callable_factory=short_interest_factory,
    ),
    "earnings_calendar": ToolDefinition(
        name="earnings_calendar",
        description=(
            "Retrieve upcoming and most-recent earnings event metadata for specified tickers: "
            "next report date, consensus EPS, revision trend, last reported date."
        ),
        input_model=EarningsCalendarInput,
        output_model=EarningsCalendarOutput,
        callable_factory=earnings_calendar_factory,
    ),
    "macro_data": ToolDefinition(
        name="macro_data",
        description=(
            "Look up a specific macro indicator series (treasury yields, credit spreads, etc.) "
            "with values, day-over-day and 5-day changes, and trailing-1y percentile rank."
        ),
        input_model=MacroDataInput,
        output_model=MacroDataOutput,
        callable_factory=macro_data_factory,
    ),
    "ticker_deep_pull": ToolDefinition(
        name="ticker_deep_pull",
        description=(
            "Retrieve a multi-category data snapshot for a single ticker, selecting from "
            "price_volume, short_data, earnings, macro_context. Use when investigating "
            "a specific ticker anomaly that needs more granular data than routine ingestion delivers."
        ),
        input_model=TickerDeepPullInput,
        output_model=TickerDeepPullOutput,
        callable_factory=ticker_deep_pull_factory,
    ),
}
```

After this edit, `TOOLS` carries exactly 8 entries — the original 3 (`news_search`, `prediction_markets`, `earnings_commentary`) plus these 5. `social_sentiment` and `options_flow` are NOT registered (parent issue resolutions A and B).

### 4\. SDK adapter tests

`tests/analysis/tools/test_sdk_adapter.py` — extend or add:

* `build_analysis_mcp_server(server_name="alphamind_qualitative", tool_names=("news_search",), session=...)` returns a `(mcp_servers_dict, allowed_tool_names)` tuple where `mcp_servers_dict` has exactly one key (`"alphamind_qualitative"`) and `allowed_tool_names` is `["mcp__alphamind_qualitative__news_search"]`.
* `build_analysis_mcp_server(server_name="alphamind_adaptive", tool_names=("ticker_deep_pull", "sec_lending"), session=...)` returns the analogous shape with the two adaptive tools.
* `build_analysis_mcp_server` raises `KeyError` when a tool name is not in `TOOLS`.
* The two MCP server names produce non-overlapping `allowed_tool_names` lists (`mcp__alphamind_qualitative__*` vs. `mcp__alphamind_adaptive__*`).

### Out of scope

* Adaptive-research harness implementation — story 05.
* Refactoring the qualitative harness beyond the call-site update.
* Editing any of the five new tool source modules.
* Adding new `ToolQuality` members.

## Acceptance criteria

- [ ] `from alphamind.analysis.tools._sdk_adapter import build_analysis_mcp_server` resolves; `build_qualitative_mcp_server` no longer exists in the module.
- [ ] `_sdk_adapter.py` no longer defines `MCP_SERVER_NAME` or `_allowed_tool_name`.
- [ ] `from alphamind.analysis.tools import TOOLS` resolves with exactly 8 entries: `news_search`, `prediction_markets`, `earnings_commentary`, `sec_lending`, `short_interest`, `earnings_calendar`, `macro_data`, `ticker_deep_pull`.
- [ ] `TOOLS["sec_lending"].input_model` is `SecLendingInput`; analogous for the other four new tools.
- [ ] `build_analysis_mcp_server(server_name="alphamind_qualitative", tool_names=("news_search",), session=...)` returns a `(dict[str, McpSdkServerConfig], list[str])` tuple where the dict has key `"alphamind_qualitative"` and the list is `["mcp__alphamind_qualitative__news_search"]`.
- [ ] `build_analysis_mcp_server(server_name="alphamind_adaptive", tool_names=("ticker_deep_pull", "sec_lending"), session=...)` returns the analogous shape with `"alphamind_adaptive"` keys and `mcp__alphamind_adaptive__*` allowed names.
- [ ] `build_analysis_mcp_server` raises `KeyError` when a tool name is not in `TOOLS`.
- [ ] The qualitative-research harness import in `qualitative_research/harness.py` uses `build_analysis_mcp_server` (not the dropped `build_qualitative_mcp_server`).
- [ ] The qualitative-research test suite passes unchanged: `uv run pytest tests/analysis/qualitative_research/ -n auto`.
- [ ] `tests/analysis/tools/test_sdk_adapter.py` covers the four acceptance criteria above (server name, dict shape, KeyError, namespace separation).
- [ ] `uv run pytest tests/analysis/ -n auto` passes; `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/ -n auto` to confirm both qualitative-research (regression) and the new SDK adapter tests pass. Inspect `git diff main..HEAD -- src/alphamind/analysis/tools/_sdk_adapter.py` to confirm the rewrite produces the new primitive and drops the old symbols. Inspect `git diff main..HEAD -- src/alphamind/analysis/qualitative_research/harness.py` to confirm only the `_resolve_tools` call site changed. `python -c "from alphamind.analysis.tools import TOOLS; assert sorted(TOOLS) == ['earnings_calendar', 'earnings_commentary', 'macro_data', 'news_search', 'prediction_markets', 'sec_lending', 'short_interest', 'ticker_deep_pull']"` exits zero.