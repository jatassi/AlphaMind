# 06 — Agent SDK harness with parser/validator/retry/stop-reason classification

## Goal

Implement the LLM invocation harness for the strategist agent. Wraps the Claude Agent SDK call, registers the two MCP servers (`validate_guardrail` from analyst story 04, `retrieve_brief` from synthesizer's existing factory), runs the parser (05a) and validator (05b) on the response, executes a single corrective retry on parse-or-validation failure, re-classifies failures paired with `stop_reason: max_tokens` as `ContextOverflowFailure`, and writes the diagnostic archive. Mirrors `src/alphamind/decision/analyst/harness.py` exactly — same exception hierarchy, same retry shape, same diagnostic-archive layout, same MCP-wiring pattern.

## Reading

* `src/alphamind/decision/analyst/harness.py` — sibling pattern (918 lines). The strategist harness mirrors this file's structure point-for-point. Read it end-to-end before starting.
* `src/alphamind/decision/strategist/input_bundle.py` — the user-message assembler the harness invokes (story 04).
* `src/alphamind/decision/strategist/parser.py` — `parse_strategist_output` and `ParseError` (story 05a).
* `src/alphamind/decision/strategist/validation.py` — `validate_strategist_output` and `ValidationResult` (story 05b).
* `src/alphamind/decision/strategist/models.py` — `StrategistOutput.model_json_schema()` is fed to `output_format` (story 03).
* `src/alphamind/risk_guardrails/state_delivery/__init__.py` — `build_validate_guardrail_mcp_server`, `build_initial_validation_state` (analyst story 04 deliverable, <issue id="c0562d99-f38f-49fb-b4db-bed449fe1217">ALP-294</issue>, *done*).
* `src/alphamind/analysis/synthesizer/retrieval_tools.py` — `build_retrieve_brief_mcp_server` factory the harness registers.
* `src/alphamind/analysis/qualitative_research/harness.py` — the upstream sibling harness pattern the analyst harness was modeled on; useful when the analyst's approach is unclear.
* `src/alphamind/config/models/agents.py` — `BaseAgentConfig` field shape; harness reads `model`, `latency_budget_seconds`, `context_token_budget`, `output_token_budget` from the strategist entry.
* `docs/architecture/llm-integration.md` — Agent SDK orchestration, ClaudeAgentOptions, JSON-Schema output mode contract.
* `docs/design/llm-agent-failure-handling.md` — fail-closed propagation contract.
* `tests/decision/analyst/test_harness.py` — sibling test pattern.
* Parent Issue <issue id="213b1ce9-8210-4b63-8d1b-7957cc9466f2">ALP-116</issue> § Notes for the orchestrator (Type-reuse hard rule, Per-invocation MCP wiring, Output stance, Diagnostic archive parity).

## Depends on

* <issue id="31d73ea7-6bd0-49aa-a2ea-babb2e5084bf">ALP-304</issue> (Story 04 — Input bundle assembler). Harness invokes `assemble_input_bundle_normal` / `assemble_input_bundle_defensive_posture`.
* <issue id="92ed9fe3-a893-4206-8012-d4dd6e90cfe8">ALP-305</issue> (Story 05a — Parser). Harness invokes `parse_strategist_output` and pattern-matches on `ParseError`.
* <issue id="21422dd3-fae4-4f90-a3ae-51c575ad16fb">ALP-306</issue> (Story 05b — Validator). Harness invokes `validate_strategist_output` and pattern-matches on FAIL.

## Scope

In scope: `src/alphamind/decision/strategist/harness.py`. Tests at `tests/decision/strategist/test_harness.py`.

### 1\. Public entry point

```python
async def run_strategist_harness(
    *,
    user_message: str,
    system_prompt: str,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    archive_root: Path | None,
    sdk_query_fn: Callable[..., AsyncIterable[Any]] = ...,
) -> HarnessSuccess:
    """Invoke the strategist agent and return a HarnessSuccess record.

    Wires the two MCP servers (validate_guardrail closing over validation_state,
    retrieve_brief closing over retrieval_store), submits the user_message,
    parses + validates the structured_output payload, runs one corrective
    retry on parse-or-validation FAIL, writes the diagnostic archive, and
    returns a HarnessSuccess with the parsed StrategistOutput, tokens used,
    SDK metadata, and validation report.

    On unrecoverable failure, raises one of MalformedOutputFailure /
    ContextOverflowFailure / SDKFailure / TimeoutFailure with full
    agent_name='strategist' and invocation_id context.
    """
```

### 2\. MCP wiring

* Build `validate_guardrail` MCP server via `build_validate_guardrail_mcp_server(initial_state=validation_state)`. Yields `mcp_servers_dict_validate, allowed_tools_validate`.
* Build `retrieve_brief` MCP server via `build_retrieve_brief_mcp_server(retrieval_store)`. Yields `mcp_servers_dict_retrieve, allowed_tools_retrieve`.
* Compose: `mcp_servers = {**mcp_servers_dict_validate, **mcp_servers_dict_retrieve}`; `allowed_tools = [*allowed_tools_validate, *allowed_tools_retrieve]`.
* Pass to `ClaudeAgentOptions(mcp_servers=..., allowed_tools=..., system_prompt=system_prompt, output_format={"type": "json_schema", "schema": StrategistOutput.model_json_schema()}, model=agent_config.model, max_thinking_tokens=..., max_tokens=agent_config.output_token_budget, ...)`.

### 3\. Parse + validate + retry loop

Mirror analyst's exactly:

1. Submit initial query via `sdk_query_fn(...)`.
2. Collect `ResultMessage` and surface `structured_output` dict + `stop_reason` + `usage`.
3. Parse via `parse_strategist_output(payload, invocation_id=invocation_id)`. On `ParseError`, build a corrective-retry user message (per analyst harness's `_build_retry_message_for_parse_error`) and re-submit.
4. On parse success, validate via `validate_strategist_output(output, retrieval_store=retrieval_store, active_sectors=active_sectors)`. On FAIL (non-empty `failures`), build a corrective-retry user message (per analyst's `_build_retry_message_for_validation_failure`) and re-submit.
5. On retry parse-or-validate FAIL: raise `MalformedOutputFailure` with both attempts' diagnostic info.
6. If at any failure point `stop_reason == "max_tokens"`: re-classify as `ContextOverflowFailure`.
7. SDK errors (`CLIResultError` etc.): wrap as `SDKFailure` or `TimeoutFailure` per analyst harness pattern.

### 4\. Diagnostic archive

When `archive_root is not None`, write to `<archive_root>/invocations/<invocation_id>/decision/strategist/`:

* `prompt.md` — the system prompt verbatim.
* `user_message.md` — the initial user message verbatim.
* `response_initial.md` — the LLM's initial response (the raw JSON or the textual fallback if parse failed).
* `response_retry.md` — present only if a retry occurred; the retried response.
* `errors.json` — list of `{"attempt": 1|2, "kind": "parse|validate|sdk|timeout", "message": ...}` records. Empty list if happy path.
* `metadata.json` — `{invocation_id, mode, model, agent_config_snapshot, total_tokens, attempts}`.

When `archive_root is None`, skip archiving (used by unit tests with no SDK).

### 5\. Exception hierarchy

Mirror analyst's exactly. Define harness-local subclasses (or import from a shared module if `alphamind.decision._shared` exists):

* `HarnessFailure(Exception)` — base, carries `agent_name="strategist"` and `invocation_id`.
* `MalformedOutputFailure(HarnessFailure)` — parse or validate FAIL after retry.
* `ContextOverflowFailure(HarnessFailure)` — failure paired with `stop_reason: max_tokens`.
* `SDKFailure(HarnessFailure)` — SDK CLI error.
* `TimeoutFailure(HarnessFailure)` — latency budget exceeded.

### 6\. `HarnessSuccess` record

```python
class HarnessSuccess(BaseModel, frozen=True):
    output: StrategistOutput
    validation_result: ValidationResult
    tokens_used: TokensUsed
    metadata: dict[str, Any]
```

### 7\. The aclose-bug workaround

After the parser/validator successful path, `break` out of the SDK message loop on first ResultMessage to avoid the SDK's post-result `Command failed exit 1` bug (per the 2026-05-03 fix in the analysis-layer harnesses). The benign `aclose()` warning that surfaces is acceptable for now (tracked separately in the project to-do list).

### Out of scope

* Runner-level orchestration (loading agent_config from yaml, building validation_state, choosing mode) — story 07.
* End-to-end live-SDK verification + fixture emission — story 08.

## Acceptance criteria

- [ ] `src/alphamind/decision/strategist/harness.py` exports `run_strategist_harness`, `HarnessSuccess`, and the four `HarnessFailure` subclasses.
- [ ] Both MCP servers (`validate_guardrail`, `retrieve_brief`) are wired and surfaced in `allowed_tools`.
- [ ] `output_format` is set to JSON-Schema mode with `StrategistOutput.model_json_schema()`.
- [ ] Single corrective retry on parse OR validate FAIL; second-attempt FAIL → `MalformedOutputFailure`.
- [ ] `stop_reason == "max_tokens"` paired with parse/validate FAIL → `ContextOverflowFailure`.
- [ ] SDK CLI errors → `SDKFailure`; latency-budget exceedance → `TimeoutFailure`.
- [ ] Diagnostic archive layout matches the bullet list in § 4 above; archive write is skipped cleanly when `archive_root is None`.
- [ ] `HarnessSuccess` carries `output`, `validation_result`, `tokens_used`, `metadata`.
- [ ] Tests under `tests/decision/strategist/test_harness.py` cover: happy path with both modes; parse-fail → retry → parse-success; validate-fail → retry → validate-success; parse-fail → retry → parse-fail → MalformedOutputFailure; max_tokens → ContextOverflowFailure; SDK error → SDKFailure; archive layout when archive_root is set; archive skipped when archive_root is None. Use a fake `sdk_query_fn` async iterator that yields canned ResultMessage payloads — no real SDK calls in unit tests.
- [ ] `uv run pytest tests/decision/strategist/test_harness.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.
- [ ] No magic numbers — all numeric values come from `agent_config` or model constants imported from `agent_config`.
- [ ] Diagnostic archive written under `<archive_root>/invocations/<invocation_id>/decision/strategist/`.

## Verification

```bash
uv run pytest tests/decision/strategist/test_harness.py -n auto
```

Inspect the harness file structure for parity with `src/alphamind/decision/analyst/harness.py` — same exception names, same retry shape, same diagnostic file names. Live-SDK verification happens in story 08.
