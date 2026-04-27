---
status: not_started
completed_date:
commit_id:
---

# 08 — Agent SDK harness with stop-reason classification

## Goal

Implement the LLM invocation harness for the synthesizer — wraps the Claude Agent SDK call, wires the three portfolio-state MCP tools, applies the documented Layer-4 stop-reason check on the response (the synthesizer's *only* validation surface per [`llm-output-validation.md § Per-agent surface mapping`](../../../design/testing/llm-output-validation.md#per-agent-surface-mapping)), preserves the diagnostic record, and propagates fail-closed errors to the caller. Mirrors the domain-researcher harness ([story 07](../domain-researchers/07-agent-sdk-harness.md)) where applicable, with three deliberate divergences:
1. **No corrective retry** — the synthesizer produces prose with no schema, so there is nothing to corrective-retry.
2. **Stop-reason check is the only post-call validation** — `max_tokens` reclassifies an "empty / suspect" response as `ContextOverflowFailure`; otherwise the response text is accepted as-is.
3. **Tools are wired** — three portfolio-state MCP tools per the agent config (story 02), unlike the domain researchers which run with `allowed_tools=[]`.

## Reading

- `docs/architecture/llm-integration.md` § Orchestration pattern — Agent SDK invocation pattern (`ClaudeAgentOptions`, `query()`, fresh-context-windows behavior, tool wiring)
- `docs/architecture/llm-integration.md` § Tool registration — the SDK tool registration shape
- `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup
- `docs/design/llm-agent-failure-handling.md` — fail-closed policy; runtime response to failures; the synthesizer is Critical
- `docs/design/llm-agent-failure-handling.md` § Recovery semantics — "Context overflow — no retry; abort. Inputs are too large — a structural problem to investigate."
- `docs/design/testing/llm-output-validation.md` § Layer 4 — Stop-reason check — re-classification policy
- `docs/design/testing/llm-output-validation.md` § Per-agent surface mapping — synthesizer is "Stop-reason check only (Layer 4)"
- `docs/design/testing/llm-output-validation.md` § Diagnostic preservation — what to write to the invocation archive regardless of outcome
- `docs/implementation/03-analysis-layer/domain-researchers/07-agent-sdk-harness.md` — sibling harness; mirror its structure where the contract aligns

## Depends on

- 02 (`AgentConfig` for the synthesizer)
- 06b (`make_portfolio_state_tools` factory — produces the three tools wired into `ClaudeAgentOptions`)
- 07 (`SynthesizerInputBundle`, `assemble_synthesizer_input` — the bundle text the harness sends as the user message)

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `harness.py` defining:

  - `class HarnessFailure(Exception)` with subclasses (mirrors the domain-researcher tree's failure taxonomy):
    - `EmptyResponseFailure(HarnessFailure)` — SDK returned no text (or only whitespace) and `stop_reason` is not `max_tokens`. Treated as malformed; no retry per the synthesizer's no-corrective-retry stance.
    - `ContextOverflowFailure(HarnessFailure)` — `stop_reason: max_tokens` AND the response is empty / truncated mid-stream / fails the empty-check. Per `llm-agent-failure-handling.md`, immediate abort.
    - `SDKFailure(HarnessFailure)` — non-recoverable SDK error (auth failure, model API error after SDK's own retries, network failure).
    - `TimeoutFailure(HarnessFailure)` — invocation exceeded the per-call timeout.
    - `ToolUseFailure(HarnessFailure)` — an MCP tool raised an exception (as opposed to returning an `is_error: True` payload, which is recoverable per story 06a/06b). One same-call retry attempt at the SDK layer; persistent failure raises this.

    Each subclass carries the agent name (`"synthesizer"`), the `invocation_id`, the raw response text (when available), and the underlying error trail.

  - `class HarnessSuccess(BaseModel, frozen=True)` carrying:
    - `invocation_id: str`
    - `response_text: str` — the synthesizer's full prose output. Per the design, no producer-side IDs to extract; the text is the entire output.
    - `tokens_used: TokensUsed` (input, output, cache-read, cache-write — per the SDK metadata; mirrors the domain-researcher harness's same-named struct)
    - `wall_clock_seconds: float`
    - `tool_call_count: int` — number of times the synthesizer invoked any of its three portfolio tools during the session. Surfaces in the diagnostic record and the feedback-loop telemetry.
    - `stop_reason: str` — the SDK's `stop_reason` for the final response. Always `end_turn` on success (the harness raises `ContextOverflowFailure` if `max_tokens`).

  - `async def invoke_synthesizer(agent_config: AgentConfig, input_bundle: SynthesizerInputBundle, portfolio_reader: PortfolioStateReader) -> HarnessSuccess` — the main entry point. Composes:
    1. Load the system prompt from `agent_config.prompt` (cached per process, per the domain-researcher harness convention).
    2. Build the three portfolio MCP tools via `make_portfolio_state_tools(portfolio_reader)`.
    3. Build `ClaudeAgentOptions` with `model=agent_config.model`, `system_prompt=<loaded>`, `max_tokens=agent_config.output_token_budget`, `allowed_tools=<the three portfolio tool names>` and the in-process MCP server registration (see SDK docs for the exact wiring shape).
    4. Pre-call token-counting check per `llm-agent-failure-handling.md § Detection`: count tokens of `system_prompt + input_bundle.bundle_text` against `agent_config.context_token_budget`; refuse to dispatch if over (raise `ContextOverflowFailure` with a message naming the observed-vs-budget delta — this catches input-side overflow before the SDK call).
    5. Call `query(prompt=input_bundle.bundle_text, options=...)` under `asyncio.wait_for(timeout=agent_config.latency_budget_seconds)`.
    6. Accumulate the full streamed response into `response_text`. Capture stop-reason and token-usage metadata from the SDK response.
    7. Apply the Layer-4 stop-reason check:
       - If `response_text.strip() == ""` AND `stop_reason == "max_tokens"`: raise `ContextOverflowFailure`.
       - If `response_text.strip() == ""` AND `stop_reason != "max_tokens"`: raise `EmptyResponseFailure`.
       - Otherwise (`response_text.strip() != ""`): treat as success. (Even if `stop_reason == "max_tokens"` here — a non-empty response that hit the output cap is still usable; the consumer-side Layer 3 will catch invented references if the cap caused mid-thought truncation that named a reference incorrectly. Per `llm-output-validation.md § Layer 4`, "a syntactically valid JSON response that hit `max_tokens` is rare but possible; such outputs pass Layers 1–3 and don't need re-classification" — the analogue for synthesizer prose is: a non-empty response with `max_tokens` is accepted, and the diagnostic record preserves the stop-reason for post-hoc analysis.)
    8. Return `HarnessSuccess(...)`.

- SDK error handling (mirrors domain-researcher harness):
  - Authentication failures (`CLAUDE_CODE_OAUTH_TOKEN` invalid or unset) → `SDKFailure` with the env var named in the message.
  - Network or transient SDK failures → SDK's own retry behavior applies; persistent failure → `SDKFailure`.
  - `asyncio.TimeoutError` → `TimeoutFailure`.
  - MCP tool exception during a tool call (as opposed to an `is_error: True` return) → `ToolUseFailure` after one in-session retry attempt at the SDK layer.

- Diagnostic preservation per [`llm-output-validation.md § Diagnostic preservation`](../../../design/testing/llm-output-validation.md#diagnostic-preservation):
  - Every invocation writes a diagnostic record to `<archive>/<date>/<invocation>/analysis/synthesizer/`:
    - `prompt.md` — the system prompt (verbatim)
    - `user_message.md` — `input_bundle.bundle_text`
    - `response.md` — the SDK response, raw text
    - `metadata.json` — model, retry_count (always 0 for the synthesizer; field present for cross-agent uniformity), tokens_used, wall_clock_seconds, stop_reason, success/failure classification, `tool_call_count`, `unknown_markers_by_source` (read from the retrieval store the runner builds from the same input bundles — passed in via the harness signature; see Notes)
  - The diagnostic record is written regardless of outcome (load-bearing for the no-checkpoint-but-diagnostic-persistence invariant in `mid-pipeline-failure-handling.md`).

- Unit tests:
  - **Happy path**: SDK returns non-empty prose → harness returns `HarnessSuccess` with `response_text` populated and `stop_reason: "end_turn"`.
  - **Pre-call input-overflow**: `system_prompt + bundle_text` exceeds `context_token_budget` → harness raises `ContextOverflowFailure` *before* the SDK call.
  - **Empty response with `end_turn`** → `EmptyResponseFailure`.
  - **Empty response with `max_tokens`** → `ContextOverflowFailure`.
  - **Non-empty response with `max_tokens`** → `HarnessSuccess` (accepted; stop-reason recorded in diagnostic).
  - **Auth failure** → `SDKFailure` with `CLAUDE_CODE_OAUTH_TOKEN` named in the message.
  - **Per-invocation timeout exceeds** → `TimeoutFailure` (configurable via `agent_config.latency_budget_seconds`).
  - **Tool-use exception during a tool call** → `ToolUseFailure` after one SDK retry; tool returning `is_error: True` payload does NOT trigger this (recoverable in-LLM signal).
  - **Tool wiring**: the three portfolio-tool names (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) are registered with the SDK and discoverable via the SDK's tool-introspection surface.
  - **Diagnostic record**: `<archive>/<date>/<invocation>/analysis/synthesizer/` contains `prompt.md`, `user_message.md`, `response.md`, `metadata.json` for both success and failure paths. `metadata.json` includes `tool_call_count` and `unknown_markers_by_source`.
  - **System prompt cache**: prompt file read once per process across multiple harness invocations (verified by mocking the file-read and asserting one call).
  - **SDK is stubbed** via dependency injection; no test calls the real Anthropic API.

Out of scope:
- The runner that composes adapters → bundle assembler → harness (story 10).
- The end-to-end live-SDK verification (story 11).
- Cross-agent retry orchestration — fail-closed semantics propagate up; the runner does not catch and continue.
- Output validation beyond the Layer-4 stop-reason check — per [`llm-output-validation.md § Per-agent surface mapping`](../../../design/testing/llm-output-validation.md#per-agent-surface-mapping), the synthesizer has no Layer 1-3 validation, and inventing one here would over-tighten the contract.

## Notes

The "non-empty response with `max_tokens` is accepted" decision deserves explicit statement. The synthesizer's output is prose, not JSON. A truncation at the output-token cap may produce a response that ends mid-sentence — but it is still useful synthesis text. The downstream consumers (analyst, strategist, PM) read the prose and cite references; if the truncation orphaned a reference (e.g., the synthesizer started writing "[SA-TECH-3] is corroborated by [QR" and stopped), the orphaned reference is invalid syntax and would be ignored by Layer 3 at the consumer. The harness preserves stop-reason in the diagnostic so the operator can see truncation patterns and increase `output_token_budget` empirically.

Why this differs from the domain-researcher harness's behavior on `max_tokens`: domain researchers produce structured `SectorBrief` text. A truncation mid-section produces malformed structure (e.g., a `[SA-TECH-3]` opener with no body, or an unclosed indented detail). The validator catches this at Layer 2; reclassification to `ContextOverflowFailure` is the correct response per the design. The synthesizer has no such structure — there is nothing to "fail validation on" except emptiness.

The `unknown_markers_by_source` field in `metadata.json` is sourced from the retrieval store the runner builds in parallel — the harness signature does NOT take a `RetrievalStore` (the harness's job is the SDK call, not retrieval), so the runner is responsible for adding the `unknown_markers_by_source` value when it writes the metadata file. Document this clearly in the metadata writer; the harness writes the rest of the metadata it owns.

The pre-call token-counting check is critical for the synthesizer because its inputs are the union of six upstream briefs — historically the agent most likely to hit context overflow. Per `llm-agent-failure-handling.md § Context overflow is a hard failure`: "the synthesizer gets the union of bounded upstream briefs". The check enforces the boundedness assumption at runtime.

The `tool_call_count` metric in `HarnessSuccess` and `metadata.json` feeds the Phase 4 feedback loop's telemetry — the design notes "On quiet days with no portfolio-relevant signals, none of these tools may be called" and the dashboard tracks the call rate over time.

Per [`feedback_llm_agents_uniformly_critical.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_llm_agents_uniformly_critical.md), every harness failure is Critical. The harness raises; the runner does not catch and continue; the pipeline aborts the invocation per `mid-pipeline-failure-handling.md`.

Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md), this story does NOT introduce a generic `LLMHarness` base class shared with the domain-researcher harness. The two harnesses share concepts (`HarnessFailure` taxonomy, diagnostic preservation, system-prompt caching) but differ in specifics (corrective-retry, validator integration, tool wiring). Premature abstraction would entangle them; let them evolve independently and refactor when a third agent class arrives.

The system-prompt loader cache uses the same convention as the domain-researcher harness: read once at first call, then served from memory. The pipeline process restarts on `agents.yaml` edits per the deploy-time vs. invocation-time classification in `configuration-management.md`, so cache invalidation is not a concern.

Per CLAUDE.md, alert the orchestrator before disabling the linter or any rule. The harness MAY have a few `# type: ignore` candidates around the SDK's loose type stubs — surface those choices to the orchestrator rather than silently suppressing.

## Acceptance criteria

- [ ] `invoke_synthesizer(...)` returns `HarnessSuccess` on a happy-path SDK response.
- [ ] Pre-call input-overflow (system prompt + bundle exceeds `context_token_budget`) raises `ContextOverflowFailure` before the SDK call.
- [ ] Empty response with `stop_reason: end_turn` raises `EmptyResponseFailure`.
- [ ] Empty response with `stop_reason: max_tokens` raises `ContextOverflowFailure`.
- [ ] Non-empty response with `stop_reason: max_tokens` returns `HarnessSuccess` (accepted; stop-reason recorded in `metadata.json`).
- [ ] Authentication failure raises `SDKFailure` with `CLAUDE_CODE_OAUTH_TOKEN` named in the message.
- [ ] Per-invocation timeout exceeds → `TimeoutFailure` raised.
- [ ] Tool-use exception during a tool call → `ToolUseFailure` after one SDK retry; tool returning `is_error: True` does not trigger this.
- [ ] All three portfolio MCP tools (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) are wired into `ClaudeAgentOptions` and discoverable.
- [ ] Diagnostic record written to `<archive>/<date>/<invocation>/analysis/synthesizer/` for both success and failure paths, including `prompt.md`, `user_message.md`, `response.md`, `metadata.json`.
- [ ] `metadata.json` includes `tool_call_count` and supports an externally-supplied `unknown_markers_by_source` field (the runner provides it).
- [ ] System prompt is loaded once per process and served from cache on subsequent calls.
- [ ] SDK is stubbed via dependency injection; no test calls the real Anthropic API.
- [ ] No corrective retry path exists in the harness (regression check: a parse-or-validation failure for the synthesizer is not a thing — there is no parser/validator). The synthesizer-specific failure surface is the four `*Failure` subclasses above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
