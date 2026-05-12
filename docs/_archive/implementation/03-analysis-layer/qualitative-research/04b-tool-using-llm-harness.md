# 04b — Tool-using LLM harness

## Goal

Implement the LLM invocation harness for the qualitative researcher — wraps the Claude Agent SDK call with the agent's tool allowlist registered, runs the parser (story 03a) and validator (story 03b) on the response, executes a single corrective retry on parse-or-validation failure, and re-classifies failures paired with `stop_reason: max_tokens` as `ContextOverflowFailure`. Structurally similar to `alphamind.analysis.domain_researchers.harness.invoke_domain_researcher` but parameterized over the qualitative agent's tool registry (story 03e) and using the qualitative parser/validator. The harness is the integration point where the SDK, the system prompt, the input bundle, the tools, the parser, and the validator compose into a single call surface used by the runner (story 06).

## Reading

* `docs/design/llm-agent-failure-handling.md` § Single corrective retry, § Fail-closed, § Stop-reason classification — the runtime policy this harness implements.
* `docs/design/testing/llm-output-validation.md` § Corrective-retry message construction — the exact retry-message shape (framing, first error, contract reference, directive with section headers).
* `docs/architecture/llm-integration.md` § Authentication, § Pipeline execution flow — how `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` are wired into the SDK invocation.
* `docs/architecture/infrastructure.md` § Invocation archive — the diagnostic-archive layout this harness writes.
* `src/alphamind/analysis/domain_researchers/harness.py` — the canonical analog. Mirror exception hierarchy (`HarnessFailure`, `MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`), `HarnessSuccess`, the per-process system-prompt cache, the `_collect_response` SDK accumulation helper, the `_DiagState` archive writer, and the corrective-retry message builders. Diff systematically; do not deviate.
* `src/alphamind/analysis/qualitative_research/parser.py`, `validation.py`, `models.py` — the parser, validator, and brief shape this harness composes (stories 03a + 03b + 02).
* `src/alphamind/analysis/tools/__init__.py` — the `TOOLS` registry (story 03e); the harness reads this to build the SDK options' `allowed_tools` list and to register the per-tool callables.
* `src/alphamind/config/models/agents.py` — `BaseAgentConfig` shape carrying `cumulative_tool_call_limit`, `cumulative_tool_token_budget`, and `tool_caps`.

## Depends on

* ALP-243 — the parser the harness invokes on the response.
* ALP-244 — the validator the harness invokes on the parsed brief.
* ALP-247 — the tool registry the harness consumes when constructing the SDK options.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/harness.py` and `tests/analysis/qualitative_research/test_harness.py`.

### 1\. Exception hierarchy

Mirror the domain-researcher hierarchy: `HarnessFailure(Exception)` base; `MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure` subclasses. Each carries `agent_name` and `invocation_id`. `MalformedOutputFailure` carries `raw_response_initial`, `raw_response_retry`, and `cause`. `ContextOverflowFailure` carries `raw_response`. `SDKFailure` carries `cause`.

### 2\. `HarnessSuccess`

```python
class HarnessSuccess(BaseModel, frozen=True):
    brief: QualitativeBrief
    raw_response: str
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed
    tool_calls_used: int  # cumulative across both attempts
    wall_clock_seconds: float
```

The `tool_calls_used` field is new relative to `domain_researchers.harness.HarnessSuccess` because the qualitative agent uses tools and the runner needs the count for budget tracking against `cumulative_tool_call_limit`.

### 3\. `invoke_qualitative_researcher`

```python
async def invoke_qualitative_researcher(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    session: Session,                          # required for tool callable factories
    universe: frozenset[str],                  # for the validator's ticker check
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess: ...
```

Behavior:

* Load the system prompt from `agent_config.prompt` via the per-process cache.
* Build the SDK `allowed_tools` list from `agent_config.tools` cross-referenced against `TOOLS` (the registry from story 03e). Construct per-tool callables via `TOOLS[name].callable_factory(session)`. If `agent_config.tools` references a name not in `TOOLS` (e.g., the agent yaml says `"social_sentiment"` but story 03e omitted it), raise `SDKFailure` with a clear message at startup — fail loudly, not silently.
* Configure SDK options: `system_prompt=prompt_text`, `model=agent_config.model`, `allowed_tools=...`, `max_turns=N` where `N` is large enough for the multi-turn tool-use loop (default to 25 — the design doc says `cumulative_tool_call_limit=15` soft, with headroom for the agent's reasoning turns), `setting_sources=[]`, `env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)}`.
* Run the SDK invocation under `asyncio.wait_for(timeout=agent_config.latency_budget_seconds)`.
* Accumulate the response: drive the SDK generator, summing `tokens_used`, capturing the final `stop_reason`, counting tool calls (one per `ToolUseBlock` if the SDK exposes one; one per assistant message containing tool use otherwise — match `domain_researchers.harness._collect_response` style and extend for tool counting).
* Parse and validate per stories 03a + 03b. On `ParseError` or `ValidationResult.is_valid == False`:
  * If `stop_reason == "max_tokens"`, raise `ContextOverflowFailure` (no retry).
  * Otherwise, build a corrective-retry message per the `_build_retry_message` shape and run one more SDK call (the retry).
  * If the retry also fails, raise `MalformedOutputFailure` with both raw responses.
* On any `CLIConnectionError` → `SDKFailure`. On any `ClaudeSDKError` → `SDKFailure`. On any `TimeoutError` → `TimeoutFailure`.
* Diagnostic archive (when `archive_root` is supplied): write `prompt.md`, `user_message.md`, `response_initial.md`, `response_retry.md` (when retry happened), `errors.json`, `metadata.json` to `archive_root/invocations/{invocation_id}/analysis/qualitative_researcher/`.

### 4\. Corrective retry message

`_build_retry_message_for_parse_error(error: ParseError) -> str` and `_build_retry_message_for_validation_failure(result: ValidationResult) -> str`. Both produce messages with three parts: a framing line naming the contract that failed, the first error only (with `field_path` and `message`/`rule`), a contract reference (`See docs/design/03-analysis-layer/qualitative-research.md § Output § Output schema.`), and a directive line naming the exact section markers (`=== NARRATIVE THREADS ===`, `=== CATALYST WATCH ===`, `=== SENTIMENT SNAPSHOT ===`).

### 5\. Tests

Tests at `tests/analysis/qualitative_research/test_harness.py` use a stub `sdk_query_fn` to drive the harness through:

* Happy path — first response parses and validates cleanly. Returns `HarnessSuccess` with `retry_count=0`.
* One-retry recovery — first response is malformed, retry response is valid. Returns `HarnessSuccess` with `retry_count=1`.
* Both attempts malformed — raises `MalformedOutputFailure` with both `raw_response_initial` and `raw_response_retry`.
* `stop_reason: max_tokens` paired with parse error — raises `ContextOverflowFailure` immediately, no retry.
* Tool-allowlist drift — `agent_config.tools = ["social_sentiment"]` (not in `TOOLS`) raises `SDKFailure` at startup.
* Timeout — slow `sdk_query_fn` causes `TimeoutFailure`.
* Ticker validation — a brief whose catalyst-watch ticker is not in `universe` triggers a retry.

No test touches the real Anthropic API.

### Out of scope

* Modifying `alphamind.analysis.domain_researchers.harness` — the qualitative harness is a fork; lifting to a shared module is a separate refactor.
* Wiring the runner — story 06.
* Wiring the digest renderer or the input bundle into the harness call site — those happen in story 05 (bundle) and story 06 (runner). The harness consumes a pre-assembled `user_message` string.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.harness import invoke_qualitative_researcher, HarnessSuccess, HarnessFailure, MalformedOutputFailure, ContextOverflowFailure, SDKFailure, TimeoutFailure` resolves.
- [ ] Happy-path test returns `HarnessSuccess(retry_count=0)` with the parsed brief and populated `tokens_used`.
- [ ] One-retry-recovery test returns `HarnessSuccess(retry_count=1)`.
- [ ] Both-attempts-malformed test raises `MalformedOutputFailure` carrying both raw responses.
- [ ] `stop_reason: max_tokens` paired with parse error raises `ContextOverflowFailure` immediately and does not run the retry.
- [ ] An `agent_config.tools` value not in `TOOLS` raises `SDKFailure` at startup.
- [ ] Timeout test (slow stub) raises `TimeoutFailure`.
- [ ] When `archive_root` is supplied, the diagnostic files are written; when `None`, no I/O happens against the archive root.
- [ ] `tool_calls_used` on `HarnessSuccess` is correctly populated (zero for tests with no tool use; positive when the stub emits tool-use blocks).
- [ ] All tests pass under `uv run pytest tests/analysis/qualitative_research/test_harness.py -n auto`.
- [ ] `uv run mypy src/alphamind/analysis/qualitative_research/harness.py` is clean.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_harness.py -n auto`. Run `uv run ruff check . && uv run mypy` clean. Diff the harness module against `domain_researchers/harness.py` and confirm the differences are confined to: parser/validator imports, retry-message text, the `tools`-allowlist plumbing, and `tool_calls_used` accounting. Confirm no test reaches the real Anthropic API (every test injects `sdk_query_fn`).
