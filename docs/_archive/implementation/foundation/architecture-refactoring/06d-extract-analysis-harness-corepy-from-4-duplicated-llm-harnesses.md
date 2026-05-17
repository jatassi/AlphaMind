# 06d — Extract `analysis/_harness_core.py` from 4 duplicated LLM harnesses

## Goal

Eliminate ~2,000 LOC of duplication across `analysis/domain_researchers/harness.py` (877), `analysis/qualitative_research/harness.py` (850), `analysis/adaptive_research/harness.py` (978), `analysis/synthesizer/harness.py` (541) — total ~3,250 LOC, ~90% structurally identical. Each harness explicitly documents "Structurally mirrors..." in its module docstring. The duplicated surface includes the entire exception hierarchy, prompt cache, SDK driver loop with stall-retry, `_DiagState` writer, and corrective-retry message builder.

Extract the shared machinery into `analysis/_harness_core.py`. Variants become small Protocols (`parse_response`, `build_options`) plus per-agent configuration. Net reduction estimated at 2,000+ LOC.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L8 — harness duplication; lists the four files and shared surface
* `src/alphamind/analysis/domain_researchers/harness.py` — canonical implementation; the other three mirror it (per their docstrings)
* `src/alphamind/analysis/qualitative_research/harness.py`
* `src/alphamind/analysis/adaptive_research/harness.py`
* `src/alphamind/analysis/synthesizer/harness.py`
* Memory `feedback_prompt_output_format_compat.md` — SDK contract gotchas to preserve through the refactor

## Depends on

* 01a (<issue id="93254003-faaf-4c77-a6b7-920879ed87e9">ALP-455</issue>) — `import-linter` scaffolding so the new `analysis/_harness_core.py` module is captured under the layer contracts

## Scope

In scope: new `src/alphamind/analysis/_harness_core.py`; refactor of all four harness modules to consume it. Tests update accordingly.

### 1\. Extract shared core

`analysis/_harness_core.py` contains:

* **Exception hierarchy** — `HarnessFailure`, `MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`
* **Prompt cache** — `_PROMPT_CACHE`, `_PROMPT_CACHE_LOCK`, `_REPO_ROOT`, `_load_prompt` (with the documented "pipeline restarts on agents.yaml edits" assumption preserved)
* **SDK driver loop** — `_collect_response`, `_StuckSDKCall`, `_CLIResultError`, `_invoke` with stall-retry
* **Diagnostic writer** — `_DiagState` (mutable accumulator; documented intent)
* **Corrective-retry builder** — `_compose_retry_prompt`
* **Anthropic-incompat key stripper** — `_strip_anthropic_incompat_keys` (and related output_format helpers)

### 2\. Define per-harness Protocols

```python
class ParseResponse[T](Protocol):
    def __call__(self, payload: dict, response_text: str) -> T | RetryRequest: ...

class BuildOptions(Protocol):
    def __call__(self, agent_config: AgentConfig, mcp_servers: list[McpServer], ...) -> ClaudeAgentOptions: ...
```

### 3\. Refactor each harness

Each of the four harness modules becomes ~200-300 LOC instead of 800-1000. Each contains only:

* The agent-specific MCP server factory imports / wiring
* The agent-specific parser/validator (implements `ParseResponse`)
* The agent-specific options builder (implements `BuildOptions`)
* The agent-specific `_MAX_TURNS` constant
* The agent-specific `HarnessSuccess` shape (frozen dataclass, per story 10a — leave as Pydantic for now and convert in 10a)
* The `invoke_*` orchestration function that calls into `_harness_core.run` passing the agent-specific Protocols

### 4\. Preserve SDK behaviors

* The `aclose()` race comment (`domain_researchers/harness.py:442-445`) — preserve the workaround in `_harness_core._invoke`
* `output_format` json_schema mode + per-agent schema generation — keep
* Stall-retry semantics — keep behavior identical
* Diagnostic archive writes (`_DiagState.write`) — keep paths and content exactly the same

### Out of scope

Pydantic→frozen-dataclass conversion of `HarnessSuccess` and runner result types is story 10a. The `asyncio.gather` → `TaskGroup` change in `pipeline/analysis.py` and `domain_researchers/orchestrator.py` is story 08b.

## Acceptance criteria

- [ ] `src/alphamind/analysis/_harness_core.py` exists with the shared exception hierarchy, prompt cache, SDK driver loop, `_DiagState` writer, and corrective-retry builder.
- [ ] Each of `domain_researchers/harness.py`, `qualitative_research/harness.py`, `adaptive_research/harness.py`, `synthesizer/harness.py` is reduced to ≤300 LOC.
- [ ] No duplicated definitions of `HarnessFailure`/`MalformedOutputFailure`/`ContextOverflowFailure`/`SDKFailure`/`TimeoutFailure` across the four files; all import from `_harness_core`.
- [ ] No duplicated `_PROMPT_CACHE`, `_collect_response`, `_invoke`, `_DiagState` definitions; all import from `_harness_core`.
- [ ] Diagnostic archive writes produce byte-identical files to pre-refactor (compare `tests/analysis/.../test_harness_diag.py` if present).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`wc -l src/alphamind/analysis/_harness_core.py src/alphamind/analysis/{domain_researchers,qualitative_research,adaptive_research,synthesizer}/harness.py` shows core ≤600 LOC and each individual harness ≤300 LOC. Existing harness integration tests (4 agents × verify scripts) pass unmodified. `RUN_LIVE_LLM_TESTS=1 uv run pytest tests/analysis/.../test_harness.py -n auto` if available.