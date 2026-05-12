# 05 — Tool-using LLM harness

## Goal

Implement the LLM invocation harness for the adaptive researcher — wraps the Claude Agent SDK call with the agent's tool allowlist registered (via the `build_analysis_mcp_server` primitive landed in story 04a with `server_name="alphamind_adaptive"`), runs the parser (story 03b) and validator (story 03c) on the response, executes a single corrective retry on parse-or-validation failure, and re-classifies failures paired with `stop_reason: max_tokens` as `ContextOverflowFailure`. Structurally mirrors `alphamind.analysis.qualitative_research.harness` exactly; the differences are confined to: parser/validator imports, retry-message text, the validator's three additional upstream-brief arguments, and the MCP server name.

## Reading

* `src/alphamind/analysis/qualitative_research/harness.py` — sibling harness to mirror end-to-end. Mirror: per-process system-prompt cache (`_PROMPT_CACHE`, `_PROMPT_CACHE_LOCK`, `_load_prompt`), `_MAX_TURNS` constant, exception hierarchy (`HarnessFailure`, `MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`), `HarnessSuccess` shape, `_collect_response` SDK-driver helper, `_build_retry_message_*` corrective-retry constructors, `_DiagState` dataclass for diagnostic accumulation, `_parse_and_validate` central helper, the two-attempt orchestration in the public entry function.
* `src/alphamind/analysis/adaptive_research/parser.py` § `parse_adaptive_brief`, `ParseError` — the parser this harness drives.
* `src/alphamind/analysis/adaptive_research/validation.py` § `validate_adaptive_brief`, `ValidationResult` — the validator this harness drives. Note: the validator signature requires three upstream-brief arguments (`sector_briefs`, `qualitative_brief`, `correlation_regime_brief`); the harness threads them through.
* `src/alphamind/analysis/adaptive_research/models.py` § `AdaptiveBrief` — the typed brief returned in `HarnessSuccess`.
* `src/alphamind/analysis/tools/_sdk_adapter.py` § `build_analysis_mcp_server` — the primitive this harness calls with `server_name="alphamind_adaptive"`.
* `src/alphamind/config/models/agents.py` § `BaseAgentConfig`, `AgentName`, `AdaptiveAgentConfig` — the config shape; `AgentName.adaptive_researcher` is the agent identifier.
* `docs/design/testing/llm-output-validation.md` § Layer 1, § Layer 4, § Corrective-retry message construction — the failure taxonomy and corrective-retry contract this harness implements.

## Depends on

* <issue id="14ae4d5a-4a9e-452e-939b-6c53c25f3b6b">ALP-257</issue> (story 03b) — `parse_adaptive_brief` + `ParseError`.
* <issue id="29d232cc-a978-454d-9d07-51e126571c0d">ALP-258</issue> (story 03c) — `validate_adaptive_brief` + `ValidationResult`.
* <issue id="2c4dd539-20d7-418d-975d-cc172f44810f">ALP-261</issue> (story 04a) — `build_analysis_mcp_server` and the registered tool set.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/harness.py` and `tests/analysis/adaptive_research/test_harness.py`.

### 1\. Module shape

Mirror `qualitative_research/harness.py` line-for-line, substituting:

* `parse_qualitative_brief` → `parse_adaptive_brief`
* `validate_qualitative_brief` → `validate_adaptive_brief`
* `QualitativeBrief` → `AdaptiveBrief`
* `AgentName.qualitative_researcher` → `AgentName.adaptive_researcher`
* MCP server name `"alphamind_qualitative"` → `"alphamind_adaptive"`
* Section-marker references in the corrective-retry message construction:
  * Use the markers `=== INVESTIGATION THREADS ===` only (single-section schema, unlike qualitative's three-section schema).
  * Contract reference: `docs/design/03-analysis-layer/adaptive-research.md § Output § Output schema`.
  * Reuse the `_SECTION_DIRECTIVE` / `_CONTRACT_REF` / `_build_retry_message` pattern.

### 2\. Public entry point signature

Per the validator's required upstream-brief arguments, the harness signature is:

```python
async def invoke_adaptive_researcher(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the adaptive-researcher agent and return a validated HarnessSuccess.

    Raises MalformedOutputFailure, ContextOverflowFailure, SDKFailure, TimeoutFailure
    per the same exception hierarchy as the qualitative researcher.
    """
```

Threading the three upstream briefs through: `_parse_and_validate` calls `validate_adaptive_brief(brief, sector_briefs=sector_briefs, qualitative_brief=qualitative_brief, correlation_regime_brief=correlation_regime_brief, universe=universe)`. The harness does NOT mutate or inspect the upstream briefs beyond passing them to the validator.

### 3\. Corrective-retry message text

Two retry-message constructors mirroring the qualitative-research pattern:

```python
_SECTION_DIRECTIVE = (
    "Emit a single corrected adaptive research findings brief. "
    "No prose preceding or following the structured content. "
    "Use exactly the section header === INVESTIGATION THREADS ==="
)

_CONTRACT_REF = (
    "See docs/design/03-analysis-layer/adaptive-research.md § Output § Output schema."
)


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    framing = (
        "The prior response did not meet the parse contract for the adaptive researcher output."
    )
    error_detail = f"Field: {error.field_path}\nError: {error.message}"
    return _build_retry_message(framing, error_detail)


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = (
        "The prior response did not meet the structural contract for "
        "the adaptive researcher output."
    )
    first_error = result.errors[0]
    error_detail = (
        f"Field: {first_error.field_path}\nRule: {first_error.rule}\nError: {first_error.message}"
    )
    return _build_retry_message(framing, error_detail)
```

### 4\. Tests

`tests/analysis/adaptive_research/test_harness.py` covers the same shape as `tests/analysis/qualitative_research/test_harness.py` adapted for adaptive — happy path with stub SDK, parse-failure single retry then success, validation-failure single retry then success, exhausted retries → `MalformedOutputFailure`, `stop_reason=max_tokens` paired with parse failure → `ContextOverflowFailure` (no retry), `CLIConnectionError` → `SDKFailure`, timeout → `TimeoutFailure`. Use a stub `sdk_query_fn` matching the SDK's async generator shape; tests must NOT touch the real Anthropic API.

Mirror the qualitative-research test fixtures for `agent_config`, `BaseAgentConfig` construction, and `_DiagState` archive verification.

### Out of scope

* Adaptive-researcher runner — story 06 (composes loaders + bundle + harness).
* Editing the qualitative-research harness — story 04a does that update.
* Editing the validator or parser — stories 03b and 03c.
* End-to-end integration with live SDK — story 07.

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.harness import invoke_adaptive_researcher, HarnessSuccess, MalformedOutputFailure, ContextOverflowFailure, SDKFailure, TimeoutFailure, HarnessFailure` all resolve cleanly.
- [ ] `invoke_adaptive_researcher` is `async def` and accepts the eight named-only arguments documented in scope (plus the optional `sdk_query_fn` and `archive_root`).
- [ ] Happy path: a stub `sdk_query_fn` returning a well-formed adaptive brief produces a `HarnessSuccess` with `retry_count=0`, populated `tokens_used`, and `tool_calls_used >= 0`.
- [ ] Parse failure on first attempt followed by valid response on retry produces a `HarnessSuccess` with `retry_count=1`.
- [ ] Validation failure (e.g., a Strengthens reference that does not resolve) on first attempt followed by valid response on retry produces a `HarnessSuccess` with `retry_count=1`.
- [ ] Two successive parse failures raise `MalformedOutputFailure` carrying both `raw_response_initial` and `raw_response_retry`.
- [ ] Parse failure paired with `stop_reason="max_tokens"` raises `ContextOverflowFailure` immediately, with NO retry attempted.
- [ ] `CLIConnectionError` from the SDK raises `SDKFailure`.
- [ ] Invocation exceeding `agent_config.latency_budget_seconds` raises `TimeoutFailure`.
- [ ] The harness uses `build_analysis_mcp_server(server_name="alphamind_adaptive", ...)` to register tools (verifiable by inspecting `_resolve_tools` source).
- [ ] The diagnostic archive (when `archive_root` is supplied) writes `prompt.md`, `user_message.md`, `response_initial.md`, `errors.json`, `metadata.json` under `<archive_root>/invocations/<invocation_id>/analysis/adaptive_researcher/`.
- [ ] No test in `tests/analysis/adaptive_research/test_harness.py` touches the real Anthropic API; all SDK calls go through the injected stub.
- [ ] `tests/analysis/adaptive_research/test_harness.py` covers each scenario above and passes under `uv run pytest tests/analysis/adaptive_research/test_harness.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/adaptive_research/test_harness.py -n auto`. Inspect `git diff main..HEAD -- src/alphamind/analysis/qualitative_research/harness.py` and confirm zero lines changed (this story does NOT touch the qualitative harness — story 04a owns its single one-line update). Run `uv run pytest tests/analysis/qualitative_research/ -n auto` to confirm the qualitative harness still passes after this story lands.