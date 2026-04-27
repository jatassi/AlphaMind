---
status: not_started
completed_date:
commit_id:
---

# 07 — Agent SDK harness with retry & stop-reason classification

## Goal

Implement the LLM invocation harness for domain researchers — wraps the Claude Agent SDK call, runs the parser (story 04) and validator (story 05) on the response, executes a single corrective retry on parse-or-validation failure per the documented runtime policy, and re-classifies failures paired with `stop_reason: max_tokens` as `context_overflow`. The harness is the integration point where the SDK, the system prompt, the input bundle, the parser, and the validator compose into a single call surface used by the per-sector runner (story 10).

## Reading

- `docs/architecture/llm-integration.md` § Orchestration pattern — Agent SDK invocation pattern (`ClaudeAgentOptions`, `query()`, fresh-context-windows behavior)
- `docs/design/llm-agent-failure-handling.md` — fail-closed policy; runtime response to validator failures
- `docs/design/llm-agent-failure-handling.md` § Recovery semantics — single corrective retry on schema/referential failure; immediate abort on context overflow
- `docs/design/testing/llm-output-validation.md` § Layer 4 — Stop-reason check — re-classification of structural failures with `max_tokens`
- `docs/design/testing/llm-output-validation.md` § Corrective-retry message construction — what the retry message contains and omits
- `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup

## Depends on

- 02 (configuration — `AgentConfig` with `model`, `system_prompt_path`, `output_token_budget`)
- 04 (parser — `parse_brief`, `ParseError`)
- 05 (validator — `validate_brief`, `ValidationError`, `ValidationResult`)

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `harness.py` defining:
  - `class HarnessFailure(Exception)` with subclasses:
    - `MalformedOutputFailure(HarnessFailure)` — exhausted retries on parse / Layer 2 / Layer 3 failure.
    - `ContextOverflowFailure(HarnessFailure)` — `stop_reason: max_tokens` paired with any structural failure.
    - `SDKFailure(HarnessFailure)` — non-recoverable SDK error (auth failure, model API error, network failure post-retry).
    - `TimeoutFailure(HarnessFailure)` — invocation exceeded the per-call timeout.
    Each subclass carries the agent name, invocation id, raw response text (when available), and the underlying error trail (parse-error / validation-errors / SDK exception).
  - `class HarnessSuccess(BaseModel, frozen=True)` carrying:
    - `brief: SectorBrief`
    - `raw_response: str`
    - `retry_count: int` (0 or 1)
    - `tokens_used: TokensUsed` (input, output, cache-read, cache-write — per the SDK metadata)
    - `wall_clock_seconds: float`
  - `async def invoke_domain_researcher(agent_config: AgentConfig, sector: Sector, sector_membership: Mapping[Sector, frozenset[str]], user_message: str, invocation_id: str) -> HarnessSuccess` — the main entry point. Composes:
    1. Load the system prompt from `agent_config.system_prompt_path` (cached per process).
    2. Build `ClaudeAgentOptions` with `model=agent_config.model`, `system_prompt=<loaded>`, `max_tokens=agent_config.output_token_budget`, `allowed_tools=[]`.
    3. Call `query(prompt=user_message, options=...)` and accumulate the full response.
    4. Run `parse_brief(response_text, sector)` → on `ParseError`, attempt corrective retry (see below). On success → `SectorBrief`.
    5. Run `validate_brief(brief, sector_membership=sector_membership)` → on `ValidationResult.is_valid is False`, attempt corrective retry. On success → return `HarnessSuccess`.
    6. On exhausted retries, raise the appropriate `HarnessFailure` subclass.
- Corrective retry construction per [llm-output-validation.md § Corrective-retry message construction](../../../design/testing/llm-output-validation.md#corrective-retry-message-construction):
  - Same-context retry: continue the SDK session rather than starting fresh. The Agent SDK supports session continuity via the `session_id` parameter on `ClaudeAgentOptions` (verify against the SDK at implementation time; if not, manually carry the prior turn into the new query as conversation history).
  - The retry message contains:
    1. An explicit framing line: `"The prior response did not meet the {parse | structural | referential} contract for the domain researcher output."` — naming which layer failed.
    2. The first error only — `field_path` and `message`. Multi-error messages produce fix-one-break-another loops.
    3. A reference to the contract: `"See docs/design/03-analysis-layer/domain-researchers/tech-semis.md § Domain researcher output contract."` — the agent reads its own design doc, not an inlined excerpt.
    4. A directive: `"Emit a single corrected sector brief. No prose preceding or following the structured content. Use exactly the section headers === KEY FINDINGS ===, === FLAGGED ANOMALIES ===, === THESIS CANDIDATES ==="`.
  - The retry message does NOT contain: the full error list; analytical guidance about what content should be in the field; the raw input data (preserved in the session history).
  - Bounded to one retry. On second failure, raise `MalformedOutputFailure` per the runtime policy.
- Stop-reason re-classification per [llm-output-validation.md § Layer 4](../../../design/testing/llm-output-validation.md#layer-4--stop-reason-check):
  - The SDK exposes `stop_reason` on the response metadata. When `parse_brief` raises `ParseError` OR `validate_brief` returns `is_valid=False`, check the `stop_reason`:
    - `end_turn` (or absent): proceed to corrective retry.
    - `max_tokens`: skip the corrective retry entirely; raise `ContextOverflowFailure` immediately.
- SDK error handling:
  - Authentication failures (`CLAUDE_CODE_OAUTH_TOKEN` invalid or unset): raise `SDKFailure` with a clear message naming the env var.
  - Network or transient SDK failures: the SDK's own retry behavior applies; on persistent failure, raise `SDKFailure`.
  - Per-invocation timeout: configurable via `agent_config.timeout_seconds` (default 60 seconds for domain researchers per the small input bundle and 800-token output cap). Exceeded → raise `TimeoutFailure`.
- Diagnostic preservation per [llm-output-validation.md § Diagnostic preservation](../../../design/testing/llm-output-validation.md#diagnostic-preservation):
  - Every invocation writes a diagnostic record to `%USERPROFILE%\AlphaMind\archive\<date>\<invocation>\analysis\<agent_name>\` (cross-platform: same path resolution helper as the distillation orchestrator):
    - `prompt.md` — the system prompt (verbatim)
    - `user_message.md` — the input bundle
    - `response_initial.md` — the first SDK response, raw text
    - `response_retry.md` — the second SDK response, raw text (only when retry occurred)
    - `errors.json` — the parse / validation error trail
    - `metadata.json` — model, retry_count, tokens_used, wall_clock_seconds, stop_reason, success/failure classification
  - The diagnostic record is written regardless of whether the call ultimately succeeded or aborted (load-bearing for the no-checkpoint-but-diagnostic-persistence invariant in `mid-pipeline-failure-handling.md`).
- Unit tests:
  - Happy path: SDK returns valid brief text → harness returns `HarnessSuccess` with `retry_count=0`.
  - Parse failure → corrective retry message constructed correctly → second SDK response is valid → harness returns `HarnessSuccess` with `retry_count=1`.
  - Two consecutive parse failures → `MalformedOutputFailure` raised; both responses preserved in the diagnostic record.
  - Validation failure (e.g., findings indexing gap) → corrective retry → success.
  - Validation failure with `stop_reason: max_tokens` → `ContextOverflowFailure` raised immediately, no retry attempted.
  - Auth failure → `SDKFailure` with env-var name in message.
  - Timeout → `TimeoutFailure`.
  - The retry message contains the framing line, the first error, the contract reference, and the directive — and does not contain the raw input or the full error list.
  - Diagnostic record is written even when the call aborts.
  - The SDK is stubbed via dependency injection; tests do not call the real Anthropic API.

Out of scope:
- The input bundle assembly (story 08).
- The per-sector runner that calls this harness (story 10).
- The parallel orchestrator (story 11).
- Cross-agent retry orchestration (e.g., re-invoking the entire pipeline) — fail-closed at this layer means the caller aborts the invocation per `mid-pipeline-failure-handling.md`.
- Tool-use loops — domain researchers have `allowed_tools=[]` per the agent inventory.

## Notes

The Claude Agent SDK supports system prompts via `ClaudeAgentOptions(system_prompt=...)` and structured response handling via the streaming `query()` API. Read the SDK source or examples before implementation; the published examples in `docs/architecture/llm-integration.md § Tool registration` cover the registration pattern and the harness implementation here uses the same one with no tools.

The same-context retry mechanism on the Agent SDK: per the SDK's documented behavior, each `query()` call is a new session by default. To preserve the prior turn, either pass `session_id` (if exposed by the SDK), or accumulate conversation history manually via `ClaudeSDKClient`'s message-list interface. Implement whichever the SDK supports cleanly; document the choice in source comments.

The diagnostic record path uses the existing path resolution helper from the collector / distillation work trees (`%USERPROFILE%\AlphaMind\` on Windows; `~/AlphaMind/` on macOS). Reuse, do not reinvent.

The system-prompt loader caches per-process: the prompt file is read once at first call, then served from memory for subsequent calls. Test by mocking the file read and asserting one call across multiple harness invocations. (The pipeline process restarts on `agents.yaml` edits per the deploy-time vs. invocation-time classification in `configuration-management.md`, so cache invalidation is not a concern.)

The `tokens_used: TokensUsed` shape comes from the SDK's response metadata. Per `llm-integration.md § What the Agent SDK doesn't provide`: "No raw token counting per call. Token usage is in response metadata but less fine-grained than the raw API's usage object." Use what the SDK exposes; supplement with rough estimates if needed for the `cost-and-rate-limit-modeling.md` accounting.

The fail-closed semantics propagate up: any `HarnessFailure` raised here aborts the entire pipeline invocation. The caller (the per-sector runner, story 10) does not catch and continue; the parallel orchestrator (story 11) does not run subsequent sectors after a failure.

## Acceptance criteria

- [ ] `invoke_domain_researcher(...)` returns `HarnessSuccess` on a happy-path SDK response.
- [ ] Parse failure → corrective retry message is constructed with framing line, first error, contract reference, and directive.
- [ ] Two consecutive parse failures raise `MalformedOutputFailure` with both raw responses preserved.
- [ ] Validation failure → corrective retry → success returns `retry_count=1`.
- [ ] Validation failure with `stop_reason: max_tokens` raises `ContextOverflowFailure` immediately (no retry attempted).
- [ ] Authentication failure raises `SDKFailure` with `CLAUDE_CODE_OAUTH_TOKEN` named in the message.
- [ ] Per-invocation timeout exceeds → `TimeoutFailure` raised.
- [ ] Corrective retry message does not contain the full error list, analytical content guidance, or the raw input data.
- [ ] Diagnostic record is written under `<archive>/<date>/<invocation>/analysis/<agent_name>/` for both success and failure paths, including `prompt.md`, `user_message.md`, `response_initial.md`, optional `response_retry.md`, `errors.json`, `metadata.json`.
- [ ] System prompt is loaded once per process and served from cache on subsequent calls.
- [ ] SDK is stubbed in unit tests via dependency injection; no test calls the real Anthropic API.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
