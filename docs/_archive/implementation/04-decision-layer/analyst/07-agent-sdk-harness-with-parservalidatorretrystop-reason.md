# 07 — Agent SDK harness with parser/validator/retry/stop-reason classification

## Goal

Implement the LLM invocation harness for the analyst agent. Wraps the Claude Agent SDK call, registers the two MCP servers (`validate_guardrail` from story 04, `retrieve_brief` from synthesizer's existing factory), runs the parser (05a) and validator (05b) on the response, executes a single corrective retry on parse-or-validation failure, re-classifies failures paired with `stop_reason: max_tokens` as `ContextOverflowFailure`, and propagates fail-closed errors to the runner. Mirrors `alphamind.analysis.qualitative_research.harness.invoke_qualitative_researcher` shape; the differences are confined to: parser/validator imports, two MCP servers instead of one, retry-message text, the analyst-specific tool-allowlist, and the JSON-Schema mode targeting `AnalystOutput.model_json_schema()`.

## Reading

* `src/alphamind/analysis/qualitative_research/harness.py` — direct sibling pattern; the analyst harness mirrors its shape almost completely. Read end-to-end before starting.
* `src/alphamind/analysis/synthesizer/harness.py` — second sibling; carries the per-invocation MCP construction pattern (analyst harness needs two such servers).
* `src/alphamind/decision/analyst/{models, parser, validation, input_bundle}.py` — the building blocks the harness composes.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` (story 04) — `build_validate_guardrail_mcp_server`.
* `src/alphamind/analysis/synthesizer/retrieval_tools.py` — `build_retrieve_brief_mcp_server`.
* `docs/design/testing/llm-output-validation.md` § Layer 4 stop-reason check, § Corrective-retry message construction.
* `docs/design/llm-agent-failure-handling.md` — fail-closed propagation (no degrade, no checkpoint).

## Depends on

* `05a — Parser` (ALP-295, this work tree) — for `parse_analyst_output` + `ParseError`.
* `05b — Validator` (ALP-296, this work tree) — for `validate_analyst_output` + `ValidationResult`.
* `06 — Input bundle assembler` (ALP-297, this work tree) — the harness consumes `user_message` already-assembled by the runner; no direct call. Listed here for the same-wave coordination.

(Story 04's `validate_guardrail` MCP wrapper and story 03's `AnalystOutput` model are transitively required via the wave-2 stories above.)

## Scope

In scope: `src/alphamind/decision/analyst/harness.py`. Tests at `tests/decision/analyst/test_harness.py`.

### 1\. Exception hierarchy

Mirror qualitative-research's:

* `HarnessFailure(Exception)` — base, carries `agent_name` and `invocation_id`.
* `MalformedOutputFailure(HarnessFailure)` — exhausted retry on parse or Layer-2/3 failure; carries `raw_response_initial`, `raw_response_retry`, `cause`.
* `ContextOverflowFailure(HarnessFailure)` — paired with `stop_reason: max_tokens`; no corrective retry.
* `SDKFailure(HarnessFailure)` — auth, model API error, network post-retry, tool-allowlist drift; carries `cause`.
* `TimeoutFailure(HarnessFailure)` — exceeded `agent_config.latency_budget_seconds`.
* `_CLIResultError(Exception)` — internal signal for `ResultMessage.is_error`.

### 2\. HarnessSuccess record

```python
class HarnessSuccess(BaseModel, frozen=True):
    output: AnalystOutput
    raw_response: str  # JSON dump of the structured_output payload (and any text)
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed
    tool_calls_used: int  # cumulative across both attempts; counts only mcp__... tool calls
    wall_clock_seconds: float
    stop_reason: str | None
```

### 3\. The MCP wiring helper

```python
def _build_mcp_wiring(
    *,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
) -> tuple[dict[str, Any], list[str]]:
    """Compose the two MCP servers and merge their allowed-tool lists."""
    validation_servers, validation_tools = build_validate_guardrail_mcp_server(initial_validation_state)
    retrieval_servers, retrieval_tools = build_retrieve_brief_mcp_server(retrieval_store)
    merged = {**validation_servers, **retrieval_servers}
    return merged, [*validation_tools, *retrieval_tools]
```

### 4\. SDK options builder

Mirror qualitative-research's `_build_sdk_options` with three changes:

* Two MCP servers (per step 3 above).
* `output_format={"type": "json_schema", "schema": AnalystOutput.model_json_schema()}`.
* `tools=[]`, `setting_sources=[]`, `extra_args={"strict-mcp-config": None}`, `env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)}`.

### 5\. Tool-call counting prefix

```python
_TOOL_NAME_PREFIXES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__",
    "mcp__alphamind_synthesizer_retrieval__",
)
```

`_collect_response` increments `tool_calls` only on `ToolUseBlock.name.startswith(prefix)` for any of the two prefixes — mirrors qualitative-research's single-prefix pattern, generalized to two.

### 6\. Corrective-retry message construction

Two retry-message builders:

* `_build_retry_message_for_parse_error(error: ParseError) -> str`
* `_build_retry_message_for_validation_failure(result: ValidationResult) -> str`

Mirror qualitative-research's text shape:

```
The prior response did not meet the {parse | structural} contract for the analyst output.

Field: {field_path}
Rule: {rule}    # validation only
Error: {message}

See docs/design/04-decision-layer/analyst-output-schema.md.

Re-emit the analyst output as a JSON payload conforming to the AnalystOutput schema attached to this invocation. The shape is API-enforced; fix the specific field named above and resubmit.
```

### 7\. The main entry point

```python
async def invoke_analyst(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the analyst agent and return :class:`HarnessSuccess`.

    Raises
    ------
    MalformedOutputFailure
        Parse or validation failed on both initial and retry attempts.
    ContextOverflowFailure
        Failure paired with stop_reason=max_tokens — context overflow, no retry.
    SDKFailure
        Authentication or non-recoverable SDK error.
    TimeoutFailure
        Invocation exceeded ``agent_config.latency_budget_seconds``.
    """
```

Behavior:

 1. Load prompt from `agent_config.prompt` (per-process cache mirroring qualitative-research's pattern).
 2. Build MCP wiring (step 3).
 3. Build SDK options (step 4).
 4. Set up `_DiagState` mirroring qualitative-research; archive directory `<archive_root>/invocations/{invocation_id}/decision/analyst/`.
 5. `_collect_response` initial attempt under `asyncio.wait_for(timeout=latency_budget_seconds)`; ContextOverflow / Timeout / CLIError / ClaudeSDKError / CLIConnectionError mapped to the right HarnessFailure subclass.
 6. `_parse_and_validate(payload, ...)` — parse via story 05a; if PASS, validate via story 05b. ContextOverflow short-circuits if paired with max_tokens. Validation results that have `is_valid == False` (errors) trigger retry; warnings do NOT trigger retry.
 7. On failure, build retry message; recurse to a single retry attempt under the same timeout (one shot, no further retry).
 8. On retry success, return `HarnessSuccess` with `retry_count=1`.
 9. On retry failure, raise `MalformedOutputFailure` carrying both raw responses.
10. Diagnostic archive flushed in both success and failure paths.

### 8\. Validator parameter plumbing

The validator (story 05b) takes `retrieval_store` and `active_sectors`. The harness accepts these as parameters and passes them through. This means `invoke_analyst` doesn't need to know how the runner builds the active sectors — the runner supplies.

### Out of scope

* The runner (story 08 owns calling `invoke_analyst`).
* E2E live-SDK testing (story 09 owns that).
* Cross-invocation state — the harness is single-invocation.

## Acceptance criteria

- [ ] `src/alphamind/decision/analyst/harness.py` exists with the full exception hierarchy, `HarnessSuccess`, `invoke_analyst`, and the SDK option / MCP wiring helpers.
- [ ] All exception classes carry `agent_name` (`"analyst"`) and `invocation_id` on instantiation.
- [ ] The harness builds two MCP servers (validate_guardrail + retrieve_brief), allowlists both tools, and counts both prefixes' ToolUseBlocks toward `tool_calls_used`.
- [ ] On parse failure, the harness builds a corrective-retry message naming the failing field and re-invokes the SDK once. Cumulative tokens across both attempts are reported.
- [ ] On validation failure with errors (`is_valid=False`), retry executes; on validation that yields warnings only (`is_valid=True`), the path is success (warnings don't trigger retry).
- [ ] On parse/validation failure paired with `stop_reason: max_tokens`, `ContextOverflowFailure` is raised — no retry.
- [ ] On exhausted retry, `MalformedOutputFailure` is raised with both raw responses.
- [ ] On timeout, `TimeoutFailure` is raised; on `CLIConnectionError`, `SDKFailure` with auth-context message; on `ClaudeSDKError`, `SDKFailure`; on `_CLIResultError`, `SDKFailure` with the CLI-error context.
- [ ] Diagnostic archive contains `prompt.md`, `user_message.md`, `response_initial.md`, `response_retry.md` (when retry occurred), `errors.json`, `metadata.json` — every artifact the qualitative-research harness writes.
- [ ] `tests/decision/analyst/test_harness.py` covers (using a stub `sdk_query_fn`): clean success, parse-failure-then-retry-success, parse-failure-then-retry-failure, validation-failure-then-retry-success, validation-warnings-only-pass-through, max-tokens classification, timeout, CLIConnectionError, ClaudeSDKError, CLIResultError, two-MCP-server tool-call counting.
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_harness.py -n auto` passes. The harness must be exercise-able without touching the real Anthropic API — the stub `sdk_query_fn` injection covers every test path.