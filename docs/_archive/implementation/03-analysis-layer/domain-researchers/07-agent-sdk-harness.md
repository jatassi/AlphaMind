## Goal

Implement the LLM invocation harness for domain researchers — wraps the Claude Agent SDK call, runs the parser (story 04) and validator (story 05) on the response, executes a single corrective retry on parse-or-validation failure per the documented runtime policy, and re-classifies failures paired with `stop_reason: max_tokens` as `context_overflow`. The harness is the integration point where the SDK, the system prompt, the input bundle, the parser, and the validator compose into a single call surface used by the per-sector runner (story 10).

## Reading

* `docs/architecture/llm-integration.md` § Orchestration pattern — Agent SDK invocation pattern (`ClaudeAgentOptions`, `query()`, fresh-context-windows behavior).
* `docs/design/llm-agent-failure-handling.md` — fail-closed policy; runtime response to validator failures.
* `docs/design/llm-agent-failure-handling.md` § Recovery semantics — single corrective retry on schema/referential failure; immediate abort on context overflow.
* `docs/design/testing/llm-output-validation.md` § Layer-4 — Stop-reason check — re-classification of structural failures with `max_tokens`.
* `docs/design/testing/llm-output-validation.md` § Corrective-retry message construction — what the retry message contains and omits.
* `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup.
* `src/alphamind/analysis/_shared.py` (story 02) — `TokensUsed` reused here.
* `src/alphamind/config/snapshot.py` — existing `archive_root / "invocations" / invocation_id` path-resolution pattern; reuse this convention for the diagnostic archive (do not invent a new path layout).

## Depends on

* **02** (<issue id="d47bb088-b447-4f25-b67c-67d73739dad4">ALP-133</issue>) — `TokensUsed` and the verified `AgentConfig` shape.
* **04** (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief`, `ParseError`.
* **05** (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief`, `ValidationError`, `ValidationResult`.

## Scope — [harness.py](<http://harness.py>) exception hierarchy

Under `src/alphamind/analysis/domain_researchers/harness.py`:

```python
class HarnessFailure(Exception): ...

class MalformedOutputFailure(HarnessFailure):
    """Exhausted retries on parse / Layer-2 / Layer-3 failure."""

class ContextOverflowFailure(HarnessFailure):
    """stop_reason: max_tokens paired with any structural failure."""

class SDKFailure(HarnessFailure):
    """Non-recoverable SDK error (auth, model API error, network post-retry)."""

class TimeoutFailure(HarnessFailure):
    """Invocation exceeded the per-call timeout."""
```

Each subclass carries the agent name, invocation id, raw response text (when available), and the underlying error trail (parse-error / validation-errors / SDK exception).

## Scope — HarnessSuccess record

```python
from alphamind.analysis._shared import TokensUsed   # imported, NOT redefined

class HarnessSuccess(BaseModel, frozen=True):
    brief: SectorBrief
    raw_response: str
    retry_count: int           # 0 or 1
    tokens_used: TokensUsed    # from _shared
    wall_clock_seconds: float
```

## Scope — main entry point

```python
async def invoke_domain_researcher(
    *,
    agent_config: AgentConfig,
    sector: Sector,
    user_message: str,
    invocation_id: str,
    archive_root: Path | None = None,
) -> HarnessSuccess:
    ...
```

Composition:

1. Load the system prompt from `agent_config.prompt` (cached per process).
2. Build `ClaudeAgentOptions` with `model=agent_config.model`, `system_prompt=<loaded>`, `max_tokens=agent_config.output_token_budget`, `allowed_tools=[]`.
3. Call `query(prompt=user_message, options=...)` and accumulate the full response.
4. Run `parse_brief(response_text, sector)` → on `ParseError`, attempt corrective retry. On success → `SectorBrief`.
5. Run `validate_brief(brief)` → on `ValidationResult.is_valid is False`, attempt corrective retry. On success → return `HarnessSuccess`.
6. On exhausted retries, raise the appropriate `HarnessFailure` subclass.

## Scope — corrective-retry message construction

Per `llm-output-validation.md § Corrective-retry message construction`:

* Same-context retry: continue the SDK session rather than starting fresh. The Agent SDK supports session continuity via the `session_id` parameter on `ClaudeAgentOptions` (verify against the SDK at implementation time; if not, manually carry the prior turn into the new query as conversation history).
* The retry message contains: an explicit framing line naming which layer failed (`"The prior response did not meet the {parse | structural | referential} contract for the domain researcher output."`); the first error only — `field_path` and `message`; a reference to the contract (`"See docs/design/03-analysis-layer/domain-researchers/tech-semis.md § Domain researcher output contract."`); a directive (`"Emit a single corrected sector brief. No prose preceding or following the structured content. Use exactly the section headers === KEY FINDINGS ===, === FLAGGED ANOMALIES ===, === THESIS CANDIDATES ==="`).
* The retry message does NOT contain: the full error list; analytical guidance about what content should be in the field; the raw input data (preserved in the session history).
* Bounded to one retry. On second failure, raise `MalformedOutputFailure` per the runtime policy.

## Scope — stop-reason re-classification

Per `llm-output-validation.md § Layer-4`: when `parse_brief` raises `ParseError` OR `validate_brief` returns `is_valid=False`, check the SDK response's `stop_reason`. `end_turn` (or absent): proceed to corrective retry. `max_tokens`: skip the corrective retry entirely; raise `ContextOverflowFailure` immediately.

## Scope — SDK error handling

Authentication failures (`CLAUDE_CODE_OAUTH_TOKEN` invalid or unset) raise `SDKFailure` with a clear message naming the env var. Network or transient SDK failures: the SDK's own retry behavior applies; on persistent failure, raise `SDKFailure`. Per-invocation timeout configurable via `agent_config.latency_budget_seconds` (the existing field on `AgentConfig`); exceeded → raise `TimeoutFailure`.

## Scope — diagnostic preservation

Every invocation writes a diagnostic record under `archive_root / "invocations" / invocation_id / "analysis" / agent_name /` (the `archive_root / "invocations" / invocation_id` prefix matches `alphamind.config.snapshot`'s convention; do not invent a new layout). Files:

* `prompt.md` — the system prompt (verbatim)
* `user_message.md` — the input bundle
* `response_initial.md` — the first SDK response, raw text
* `response_retry.md` — the second SDK response, raw text (only when retry occurred)
* `errors.json` — the parse / validation error trail
* `metadata.json` — model, retry_count, tokens_used, wall_clock_seconds, stop_reason, success/failure classification

## Notes

The Claude Agent SDK supports system prompts via `ClaudeAgentOptions(system_prompt=...)` and structured response handling via the streaming `query()` API. Read the SDK source or examples before implementation; the harness here uses the same registration pattern as `docs/architecture/llm-integration.md § Tool registration` with no tools.

The same-context retry mechanism on the Agent SDK: per the SDK's documented behavior, each `query()` call is a new session by default. To preserve the prior turn, either pass `session_id` (if exposed by the SDK), or accumulate conversation history manually via `ClaudeSDKClient`'s message-list interface. Implement whichever the SDK supports cleanly; document the choice in source comments.

The system-prompt loader caches per-process: the prompt file is read once at first call, then served from memory for subsequent calls. Test by mocking the file read and asserting one call across multiple harness invocations. (The pipeline process restarts on `agents.yaml` edits per the deploy-time vs. invocation-time classification in `configuration-management.md`, so cache invalidation is not a concern.)

The fail-closed semantics propagate up: any `HarnessFailure` raised here aborts the entire pipeline invocation. The caller (the per-sector runner, story 10) does not catch and continue; the parallel orchestrator (story 11) does not run subsequent sectors after a failure.

## Acceptance criteria

- [ ] `invoke_domain_researcher(...)` returns `HarnessSuccess` on a happy-path SDK response.
- [ ] `HarnessSuccess.tokens_used` is typed as `alphamind.analysis._shared.TokensUsed` (no local redefinition).
- [ ] Parse failure → corrective retry message is constructed with framing line, first error, contract reference, and directive.
- [ ] Two consecutive parse failures raise `MalformedOutputFailure` with both raw responses preserved.
- [ ] Validation failure → corrective retry → success returns `retry_count=1`.
- [ ] Validation failure with `stop_reason: max_tokens` raises `ContextOverflowFailure` immediately (no retry attempted).
- [ ] Authentication failure raises `SDKFailure` with `CLAUDE_CODE_OAUTH_TOKEN` named in the message.
- [ ] Per-invocation timeout exceeded → `TimeoutFailure` raised.
- [ ] Corrective retry message does not contain the full error list, analytical content guidance, or the raw input data.
- [ ] Diagnostic record is written under `<archive_root>/invocations/<invocation_id>/analysis/<agent_name>/` for both success and failure paths, including `prompt.md`, `user_message.md`, `response_initial.md`, optional `response_retry.md`, `errors.json`, `metadata.json`.
- [ ] System prompt is loaded once per process and served from cache on subsequent calls.
- [ ] SDK is stubbed in unit tests via dependency injection; no test calls the real Anthropic API.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
