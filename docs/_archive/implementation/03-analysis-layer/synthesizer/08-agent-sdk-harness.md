# 08 — Agent SDK harness with stop-reason classification

## Goal

Implement the LLM invocation harness for the synthesizer — wraps the Claude Agent SDK call, wires the per-invocation portfolio-state MCP server (story 06b), applies the documented Layer-4 stop-reason check on the response, preserves the diagnostic record, and propagates fail-closed errors to the caller.

The synthesizer harness is structurally **simpler** than the sibling agents' harnesses (qualitative-research, adaptive-research) because the synthesizer's output is unstructured prose with no producer-side schema — no parser, no validator, no corrective retry. It is structurally **more complex** in one dimension: per-invocation MCP-server construction with closures over the `SynthesizerPortfolioStateReader`. The global `alphamind.analysis.tools.TOOLS` registry pattern does not fit (see parent issue § Notes for the orchestrator).

## Reading

* `docs/architecture/llm-integration.md` § Orchestration pattern — Agent SDK invocation pattern.
* `docs/architecture/llm-integration.md` § Tool registration — `claude_agent_sdk.tool` plus `create_sdk_mcp_server` primitives.
* `docs/design/llm-agent-failure-handling.md` — fail-closed policy.
* `docs/design/testing/llm-output-validation.md` § Layer 4 — Stop-reason check.
* `docs/design/testing/llm-output-validation.md` § Diagnostic preservation.
* `src/alphamind/analysis/qualitative_research/harness.py` — sibling harness pattern reference (exception hierarchy, `HarnessSuccess`, `_DiagState`, prompt cache, `_collect_response`, `_invoke` timeout/SDK-error wrapping). **Reuse the structural patterns; drop the parser/validator/retry path.**
* `src/alphamind/portfolio_state/consumers/synthesizer.py` — `SynthesizerPortfolioStateReader` Protocol the harness accepts as a parameter.
* [ALP-200](https://linear.app/alphamind-jatassi/issue/ALP-200) (story 02) — `BaseAgentConfig` for the synthesizer.
* [ALP-206](https://linear.app/alphamind-jatassi/issue/ALP-206) (story 06b) — `build_portfolio_state_mcp_server(reader)` factory the harness calls.
* [ALP-209](https://linear.app/alphamind-jatassi/issue/ALP-209) (story 09) — system prompt path the harness loads.

## Depends on

* [ALP-200](https://linear.app/alphamind-jatassi/issue/ALP-200) (story 02) — `agents.yaml` synthesizer entry verified.
* [ALP-206](https://linear.app/alphamind-jatassi/issue/ALP-206) (story 06b) — `build_portfolio_state_mcp_server`.
* [ALP-209](https://linear.app/alphamind-jatassi/issue/ALP-209) (story 09) — system prompt verified.

## Scope

Module path is `src/alphamind/analysis/synthesizer/harness.py`. The module mirrors the qualitative-research harness's structural patterns but drops the parser/validator/retry path.

#### Exception hierarchy

Mirrors `qualitative_research.harness` with names adjusted for the no-parser stance.

* `HarnessFailure(Exception)` base — carries `agent_name`, `invocation_id`.
* `EmptyResponseFailure(HarnessFailure)` — non-error response with no text content. The qualitative harness's malformed-output failure is replaced by this since there is no parse step.
* `ContextOverflowFailure(HarnessFailure)` — empty response paired with `stop_reason="max_tokens"`.
* `SDKFailure(HarnessFailure)` — auth, model API error, network, tool-allowlist drift.
* `TimeoutFailure(HarnessFailure)` — exceeded `agent_config.latency_budget_seconds`.

#### HarnessSuccess

`class HarnessSuccess(BaseModel, frozen=True)` carries `response_text: str`, `tokens_used: TokensUsed` (imported from `alphamind.analysis._shared`), `tool_calls_used: int`, `wall_clock_seconds: float`, `stop_reason: str | None`.

#### Module-level state

Per-process system-prompt cache plus an asyncio lock — mirror the qualitative harness pattern so concurrent orchestrator coroutines don't redundantly re-read the same file.

#### invoke_synthesizer entry point

Async signature with keyword-only parameters.

```
async def invoke_synthesizer(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    portfolio_reader: SynthesizerPortfolioStateReader,
    archive_root: Path | None = None,
    sdk_query_fn: Callable | None = None,
) -> HarnessSuccess
```

The body proceeds as follows.

1. Load system prompt from `agent_config.prompt` (cached per process).
2. Build the per-invocation MCP server via `build_portfolio_state_mcp_server(portfolio_reader)`.
3. Construct `ClaudeAgentOptions` with `system_prompt`, `model`, `mcp_servers`, `allowed_tools`, `max_turns` (\~15 — synthesizer doesn't loop heavily), `setting_sources=[]`, and `env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)}`.
4. Drive the SDK with `asyncio.wait_for(timeout=agent_config.latency_budget_seconds)`. Collect text plus stop_reason plus tokens plus tool-call count.
5. Apply the Layer-4 stop-reason check. Empty response with `end_turn` raises `EmptyResponseFailure`. Empty response with `max_tokens` raises `ContextOverflowFailure`. Non-empty response (any stop_reason) returns `HarnessSuccess`.
6. Wrap CLI errors and timeouts per the sibling harness's pattern.
7. Write the diagnostic archive on both success and failure paths under `<archive_root>/invocations/<invocation_id>/analysis/synthesizer/` — `prompt.md`, `user_message.md`, `response.md`, `errors.json`, `metadata.json`. The `metadata.json` includes `tool_calls_used`, `tokens_used`, `stop_reason`, `wall_clock_seconds`.

The `SnapshotBackedSynthesizerReader` (already shipped at `portfolio_state/consumers/synthesizer.py`) is the production reader the runner (story 10) injects. Tests can implement the protocol inline as a stub.

#### Tests

Test module path is `tests/analysis/synthesizer/test_harness.py`. Test cases listed below.

* `test_happy_path_returns_success` — stubbed SDK returns text plus `end_turn`; success.
* `test_empty_end_turn_raises_empty_response` — stubbed empty plus `end_turn` raises `EmptyResponseFailure`.
* `test_empty_max_tokens_raises_context_overflow` — stubbed empty plus `max_tokens` raises `ContextOverflowFailure`.
* `test_nonempty_max_tokens_returns_success` — non-empty plus `max_tokens` returns success (synthesizer accepts truncated prose; Layer 4 only blocks empty).
* `test_auth_failure_raises_sdk_failure` — `CLIConnectionError` raises `SDKFailure` mentioning `CLAUDE_CODE_OAUTH_TOKEN`.
* `test_timeout_raises_timeout_failure`.
* `test_portfolio_tools_wired_into_options` — assert `allowed_tools` contains the three `mcp__alphamind_synthesizer_portfolio__*` names.
* `test_diagnostic_archive_written_on_success`.
* `test_diagnostic_archive_written_on_failure`.
* `test_no_corrective_retry_path` — failure raises immediately; no second SDK call.
* `test_system_prompt_cached_per_process` — second invocation reads from cache, not disk.

## Out of scope

* The runner that composes adapters then bundle assembler then harness (story 10).
* Live-SDK end-to-end verification (story 11).
* Output validation beyond Layer 4 (consumer-side Layer 3 covers reference-ID integrity).
* Any parse/validate/retry path.
* Wiring `retrieve_brief` (story 06a) — that tool is consumed by decision-layer agents, not the synthesizer.

## Acceptance criteria

- [ ] `invoke_synthesizer(...)` returns `HarnessSuccess` on a happy-path SDK response.
- [ ] Empty response plus `stop_reason=end_turn` raises `EmptyResponseFailure`.
- [ ] Empty response plus `stop_reason=max_tokens` raises `ContextOverflowFailure`.
- [ ] Non-empty response with any `stop_reason` returns `HarnessSuccess`.
- [ ] Auth failure raises `SDKFailure` with `CLAUDE_CODE_OAUTH_TOKEN` named in message.
- [ ] Timeout exceeded raises `TimeoutFailure`.
- [ ] All three portfolio-state MCP tools wired into `ClaudeAgentOptions`.
- [ ] Diagnostic archive written for both success and failure paths.
- [ ] `metadata.json` includes `tool_calls_used`, `tokens_used`, `stop_reason`, `wall_clock_seconds`.
- [ ] System prompt cached per process.
- [ ] No corrective retry path exists.
- [ ] SDK is stubbed via `sdk_query_fn` injection; no test calls the real Anthropic API.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.