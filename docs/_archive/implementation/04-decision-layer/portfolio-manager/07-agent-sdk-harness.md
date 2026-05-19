# 07 — Agent SDK harness

## Goal

Author the LLM invocation harness for the PM agent. Wraps the Claude Agent SDK call, registers FOUR MCP servers (`validate_guardrail`, `retrieve_brief`, `get_thesis_components`, `submit_envelope`), runs the completion-sentinel parser (story 06a) on the response, executes a single corrective retry on parse failure, and re-classifies failures paired with `stop_reason: max_tokens` as `ContextOverflowFailure`. Diagnostic archive includes a new `submission_log.json` capturing the engine-stub's per-envelope submission log. Mirrors strategist's harness (`src/alphamind/decision/strategist/harness.py`) extended with two additional MCP servers and the submission-log archive write.

## Reading

* `src/alphamind/decision/strategist/harness.py` — sibling pattern (post-2026-05-05); the harness this story's structure mirrors most closely. Read end-to-end before authoring.
* `src/alphamind/decision/analyst/harness.py` — sibling pattern (older but more complete); the per-process system-prompt cache, multi-turn budget enforcement, and tool-name-prefix gating live here. Mirror those primitives.
* `src/alphamind/decision/portfolio_manager/parser.py` (story 06a / [ALP-326](<https://linear.app/alphamind-jatassi/issue/ALP-326>)) — `parse_pm_completion_record` and `ParseError` the harness invokes.
* `src/alphamind/decision/portfolio_manager/models.py` (story 03 / [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>)) — `PMCompletionRecord` is the harness's `output_format` JSON-Schema target.
* `src/alphamind/decision/portfolio_manager/input_bundle.py` (story 04 / [ALP-324](<https://linear.app/alphamind-jatassi/issue/ALP-324>)) — `assemble_input_bundle_normal` and `assemble_input_bundle_halt` produce the user message.
* `src/alphamind/portfolio_state/consumers/portfolio_manager_thesis_mcp.py` (story 05 / [ALP-325](<https://linear.app/alphamind-jatassi/issue/ALP-325>)) — `build_get_thesis_components_mcp_server`.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` (story 06c / [ALP-328](<https://linear.app/alphamind-jatassi/issue/ALP-328>)) — `build_submit_envelope_mcp_server` and submission-log accessor.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — `build_validate_guardrail_mcp_server` (shared, from analyst).
* `src/alphamind/analysis/synthesizer/retrieval_tools.py` — `build_retrieve_brief_mcp_server` (shared, from synthesizer).
* `docs/architecture/llm-integration.md` — Agent SDK orchestration, ClaudeAgentOptions, output_format JSON-Schema mode, allowed-tools list semantics.
* `docs/design/llm-agent-failure-handling.md` — fail-closed propagation contract.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decision (D) (sentinel-only output stance, validator runs inside submit_envelope wrapper not parser pass) and the diagnostic-archive parity bullet.

## Depends on

* [ALP-324](<https://linear.app/alphamind-jatassi/issue/ALP-324>) (this work tree, story 04) — provides the input-bundle assemblers.
* [ALP-325](<https://linear.app/alphamind-jatassi/issue/ALP-325>) (this work tree, story 05) — provides `build_get_thesis_components_mcp_server`.
* [ALP-326](<https://linear.app/alphamind-jatassi/issue/ALP-326>) (this work tree, story 06a) — provides `parse_pm_completion_record` and `ParseError`.
* [ALP-327](<https://linear.app/alphamind-jatassi/issue/ALP-327>) (this work tree, story 06b) — provides `validate_pm_envelope` (used inside the submit_envelope wrapper, not directly by the harness; the harness threads the validator's per-invocation context into the wrapper factory call).
* [ALP-328](<https://linear.app/alphamind-jatassi/issue/ALP-328>) (this work tree, story 06c) — provides `build_submit_envelope_mcp_server`.

## Scope

Code at `src/alphamind/decision/portfolio_manager/harness.py`. Tests at `tests/decision/portfolio_manager/test_harness.py`.

### 1\. Exception hierarchy

Mirror analyst's exception hierarchy exactly:

* `HarnessFailure(Exception)` — base class, carries `agent_name` and `invocation_id`.
* `MalformedOutputFailure(HarnessFailure)` — parse failure or missing structured output post-retry.
* `ContextOverflowFailure(HarnessFailure)` — parse-or-validation failure paired with `stop_reason: max_tokens`.
* `SDKFailure(HarnessFailure)` — SDK-level error (auth, network, transport).
* `TimeoutFailure(HarnessFailure)` — wall-clock deadline exceeded.

### 2\. `HarnessSuccess` result type

```python
@dataclass(frozen=True)
class HarnessSuccess:
    output: PMCompletionRecord
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None
    submission_log: tuple[SubmissionLogEntry, ...]
```

### 3\. `invoke_pm(...)` async function

Author the function with this signature:

```python
async def invoke_pm(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    initial_submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess: ...
```

Behavior (mirror strategist's harness exactly except for the four MCP servers):

* Load system prompt from `agent_config.prompt` via `_load_prompt` (per-process cache, mirrors analyst).
* Construct four MCP servers via the four factories:
  * `validate_mcp_servers, validate_allowed_tools = build_validate_guardrail_mcp_server(initial_validation_state, ...)` — note the analyst's wrapper takes a state cell, not a state value; check the analyst's signature and adapt.
  * `retrieve_mcp_servers, retrieve_allowed_tools = build_retrieve_brief_mcp_server(retrieval_store)`.
  * `thesis_mcp_servers, thesis_allowed_tools = build_get_thesis_components_mcp_server(thesis_component_reader)`.
  * `submit_mcp_servers, submit_allowed_tools = build_submit_envelope_mcp_server(initial_submit_envelope_state, retrieval_store=..., pre_processor_bundle=..., pm_view=..., active_sectors=..., halt_mode=..., sector_resolver=..., library_config=..., library_market=...)`.
* Compose `ClaudeAgentOptions` with all four MCP servers merged into `mcp_servers={**validate_mcp_servers, **retrieve_mcp_servers, **thesis_mcp_servers, **submit_mcp_servers}` and `allowed_tools=tuple(validate_allowed_tools + retrieve_allowed_tools + thesis_allowed_tools + submit_allowed_tools)`.
* `output_format = {"type": "json_schema", "schema": PMCompletionRecord.model_json_schema()}`.
* `model = agent_config.model`, `system_prompt = system_prompt_text`, `max_turns = _MAX_TURNS` (mirror analyst's value, currently 25 — bump to 40 since PM has more tool-calls per envelope).
* Multi-turn-budget enforcement via `_TOOL_NAME_PREFIXES` matching the four MCP servers' wire-form names (mirror analyst's `_TOOL_NAME_PREFIXES` extension).
* Wall-clock deadline = `agent_config.latency_budget_seconds` (raised to 600s in story 01).
* Run the SDK query loop, accumulating: `tokens_used`, `tool_calls_used`, `wall_clock_seconds`, `stop_reason`, the final `ResultMessage.structured_output` payload, and any narration.
* Call `parse_pm_completion_record(payload, invocation_id=invocation_id)`. On `ParseError`:
  * If retry count is 0 and `stop_reason != "max_tokens"`: format a corrective-retry user message naming the parse error; re-invoke the SDK with the same MCP servers (preserving cumulative state cells). Mirror analyst's retry-message construction.
  * If `stop_reason == "max_tokens"`: raise `ContextOverflowFailure`.
  * If retry count is 1 and parse fails again: raise `MalformedOutputFailure`.
* On wall-clock timeout: raise `TimeoutFailure`.
* On SDK error: raise `SDKFailure`.
* Diagnostic archive (when `archive_root` is set): write `prompt.md`, `user_message.md`, `response_initial.md`, `response_retry.md` (when retry occurred), `errors.json`, `metadata.json` under `<archive_root>/invocations/<invocation_id>/decision/portfolio_manager/`. PLUS `submission_log.json` capturing `state.submission_log` from the engine-stub's state cell.
* Return `HarnessSuccess` with the parsed `PMCompletionRecord`, retry count, tokens, tool-call count, wall-clock, stop reason, and `submission_log` tuple.

### 4\. Tests

Tests at `tests/decision/portfolio_manager/test_harness.py`:

* `test_happy_path_with_stub_sdk` — provide a stub `sdk_query_fn` returning a well-formed sentinel + simulated tool calls; assert `HarnessSuccess` returned, retry_count=0.
* `test_parse_failure_retries_then_succeeds` — stub sdk_query_fn returns malformed payload first time, well-formed second; assert retry_count=1 and the corrective-retry user message included the original parse error.
* `test_parse_failure_with_max_tokens_raises_context_overflow` — stub returns malformed + `stop_reason: max_tokens`; assert `ContextOverflowFailure`.
* `test_parse_failure_after_retry_raises_malformed` — stub returns malformed both times; assert `MalformedOutputFailure`.
* `test_timeout_raises_timeout_failure` — stub never returns within `latency_budget_seconds`; assert `TimeoutFailure`.
* `test_sdk_exception_raises_sdk_failure` — stub raises a synthetic error; assert `SDKFailure`.
* `test_diagnostic_archive_written` — with `archive_root` set, call the harness; assert all six files (prompt, user_message, response_initial, errors, metadata, submission_log) exist with non-empty content.
* `test_four_mcp_servers_registered` — inspect the SDK options dict; assert the merged `mcp_servers` map contains all four MCP server names.
* `test_state_cells_are_per_invocation` — call the harness twice with two distinct initial states; assert no state leakage.

The test stubs the SDK via the `sdk_query_fn` injection — no real SDK calls in unit tests.

### Out of scope

* The runner (story 08) — composes initial states + reads agent_config + dispatches input bundle by mode.
* The verify script (story 09) — runs the live SDK against real Anthropic API.
* The validator and submit_envelope wrapper — wired in but not modified here.

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/harness.py` exists exporting `invoke_pm`, `HarnessSuccess`, all four exception types.
- [ ] The harness composes four MCP servers and passes them to `ClaudeAgentOptions`.
- [ ] `output_format = {"type": "json_schema", "schema": PMCompletionRecord.model_json_schema()}`.
- [ ] Single corrective retry on parse failure; `max_tokens` paired with parse failure → `ContextOverflowFailure`.
- [ ] Wall-clock deadline from `agent_config.latency_budget_seconds`.
- [ ] Diagnostic archive includes `submission_log.json` alongside the standard six files.
- [ ] `HarnessSuccess.submission_log` is the tuple from the engine-stub's state cell after the SDK loop completes.
- [ ] All nine tests pass.
- [ ] `uv run pytest tests/decision/portfolio_manager/test_harness.py -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/test_harness.py -n auto` — all tests pass. The harness is invoked from story 09's verify script (live SDK).
