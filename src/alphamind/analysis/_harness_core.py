"""Shared infrastructure for the four analysis-layer LLM harnesses (ALP-466).

The four agent harnesses — ``domain_researchers``, ``qualitative_research``,
``adaptive_research``, and ``synthesizer`` — duplicated ~3,250 LOC of
near-identical machinery: the exception hierarchy, prompt cache, SDK driver
loop, diagnostic-record writer, retry-message builder, token accountants,
and error-translation glue.

This module centralises that shared infrastructure. Each harness is
parameterised by:

* its agent-specific MCP server wiring;
* its parser / validator and corresponding retry-message strings;
* its ``HarnessSuccess`` shape (still Pydantic; converted to a frozen
  dataclass in story 10a);
* a small number of behaviour flags exposed on the driver
  (``init_stall_timeout_seconds``, ``tool_name_prefix``,
  ``on_cli_result_error``, ``resume_session_id``).

The SDK contracts preserved verbatim from the canonical
:mod:`alphamind.analysis.domain_researchers.harness` implementation:

* the ``aclose()``-from-this-task workaround (otherwise GC-time close
  prints "asynchronous generator is already running" to stderr);
* ``setting_sources=[]`` / ``tools=[]`` / ``strict-mcp-config`` go in the
  per-harness options builder, not here;
* the stall-retry on ``_StuckSDKCall``;
* JSON-schema output-format mode + diagnostic-archive paths.

Per ``feedback_prompt_output_format_compat.md``: harnesses using
``output_format={"type": "json_schema", ...}`` must NOT inject a
"begin with `{`" prefill directive. None of the four harnesses do, and
the surface here does not introduce one.
"""

from __future__ import annotations

# ruff: noqa: N818  # Exception class names are spec-mandated and mirror the
#                   #  per-harness names retained for API stability.
import asyncio
import json
import random
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._shared import TokensUsed

__all__ = [
    "_PROMPT_CACHE",
    "_PROMPT_CACHE_LOCK",
    "_REPO_ROOT",
    "CLIResultErrorMapping",
    "CollectOutcome",
    "ContextOverflowFailure",
    "DiagState",
    "DiagWriter",
    "HarnessFailure",
    "MalformedOutputFailure",
    "SDKFailure",
    "TimeoutFailure",
    "_CLIResultError",
    "_StuckSDKCall",
    "_absorb_metadata",
    "_add_tokens",
    "_build_retry_message",
    "_collect_response",
    "_load_prompt",
    "_render_raw_response",
    "_tokens_from_usage",
    "invoke_sdk",
]


# ---------------------------------------------------------------------------
# Global cap on concurrent SDK calls
# ---------------------------------------------------------------------------

# The Claude Agent SDK's local CLI subprocess + per-agent MCP-server
# proliferation produces a deterministic contention point at any
# concurrency above one. Empirical findings (2026-05-18 debug-e2e runs):
# - 4 concurrent (no cap): one agent deterministically stalls on the
#   180s between-message watchdog twice in a row (376-378s wall clock).
# - 4 concurrent with cap=3: still one of the three running agents
#   deterministically stalls — the failing agent rotates run-to-run
#   (energy in runs 1/2; tech_semis in run 3) but the count is always
#   exactly one.
# - 4 concurrent with cap=2: qualitative_researcher (no between-message
#   watchdog) stalls 360s with zero output tokens, surfaces as
#   "Invocation exceeded latency budget" rather than the watchdog path
#   but with the same zero-token signature.
# - 1 concurrent (isolation repro): same agent + same input succeeds
#   in 222s.
# Capping at one serializes the analysis-layer SDK fan-out, matching
# the only configuration that has been observed to complete cleanly.
# Wall-clock cost: the 4-way analysis fan-out runs as four sequential
# waves of one — roughly 4x the parallel baseline (~20 min for the
# analysis SDK calls against a ~5 min parallel baseline). Acceptable
# for a debug verify gate; revisit if the underlying SDK + OAuth +
# Windows + MCP stack tolerates higher concurrency in the future.
_MAX_CONCURRENT_SDK_CALLS = 1
_sdk_call_semaphore: asyncio.Semaphore | None = None


def _get_sdk_call_semaphore() -> asyncio.Semaphore:
    """Lazy-init the process-global SDK-call semaphore.

    Deferred construction avoids ``asyncio.Semaphore`` binding to a
    no-running-loop context at module import. The first call inside an
    event loop materialises the instance; subsequent calls return the
    same object. Single-threaded async guarantees the first-call check
    is atomic.
    """
    global _sdk_call_semaphore
    if _sdk_call_semaphore is None:
        _sdk_call_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SDK_CALLS)
    return _sdk_call_semaphore


# ---------------------------------------------------------------------------
# Diagnostic-state Protocol
# ---------------------------------------------------------------------------


class DiagWriter(Protocol):
    """Structural shape :func:`invoke_sdk` requires of any *diag* argument.

    Both :class:`DiagState` (the canonical analysis-side dataclass) and the
    decision-layer harnesses' private ``_DiagState`` records satisfy this
    Protocol. Defining the shape here lets ``invoke_sdk`` stay agnostic of
    the per-harness metadata shape while keeping the failure-path flush
    and the ``agent_response`` model-name emission consistent across the
    seven harnesses.
    """

    model: str

    def write(
        self,
        *,
        success: bool,
        wall_clock_seconds: float,
        stop_reason: str | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Per-process system-prompt cache
# ---------------------------------------------------------------------------

# Maps prompt file path → loaded prompt text.  No invalidation needed:
# the pipeline process restarts on agents.yaml edits per the deploy-time
# vs. invocation-time classification in configuration-management.md.
# The lock gates the read-then-write so concurrent orchestrator coroutines
# can't redundantly re-read the same file.
_PROMPT_CACHE: dict[str, str] = {}
_PROMPT_CACHE_LOCK = asyncio.Lock()

_REPO_ROOT = Path(__file__).resolve().parents[3]


async def _load_prompt(prompt_path: str) -> str:
    """Load system prompt from *prompt_path*, caching per process."""
    async with _PROMPT_CACHE_LOCK:
        if prompt_path not in _PROMPT_CACHE:
            _PROMPT_CACHE[prompt_path] = (_REPO_ROOT / prompt_path).read_text(encoding="utf-8")
        return _PROMPT_CACHE[prompt_path]


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class HarnessFailure(Exception):
    """Base class for all harness-level failures.

    Every subclass carries *agent_name* and *invocation_id* so the caller
    has full context for the failure log.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
    ) -> None:
        super().__init__(message)
        self.agent_name = agent_name
        self.invocation_id = invocation_id


class MalformedOutputFailure(HarnessFailure):
    """Exhausted retries on parse / Layer-2 / Layer-3 failure.

    Carries the raw response text from both attempts when a retry occurred,
    plus the underlying error trail.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        raw_response_initial: str | None = None,
        raw_response_retry: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.raw_response_initial = raw_response_initial
        self.raw_response_retry = raw_response_retry
        self.cause = cause


class ContextOverflowFailure(HarnessFailure):
    """stop_reason: max_tokens paired with any structural failure.

    The pipeline aborts immediately — no corrective retry is attempted.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        raw_response: str | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.raw_response = raw_response


class SDKFailure(HarnessFailure):
    """Non-recoverable SDK error (auth, model API error, network post-retry,
    or tool-allowlist drift)."""

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.cause = cause


class TimeoutFailure(HarnessFailure):
    """Invocation exceeded the per-call timeout (agent_config.latency_budget_seconds)."""


# ---------------------------------------------------------------------------
# Internal SDK signals
# ---------------------------------------------------------------------------


class _CLIResultError(Exception):
    """Internal signal: ResultMessage carried is_error=True.

    Raised from inside :func:`_collect_response` so the caller (``invoke_sdk``
    or a harness's local ``_invoke``) can convert into the appropriate
    :class:`HarnessFailure` subclass with full agent-name / invocation-id
    context. Not part of the public API.
    """

    def __init__(
        self,
        *,
        error_text: str,
        partial_response: str,
        stop_reason: str | None,
    ) -> None:
        super().__init__(error_text)
        self.error_text = error_text
        self.partial_response = partial_response
        self.stop_reason = stop_reason


class _StuckSDKCall(Exception):
    """Internal signal: SDK produced no message before the init-stall timeout.

    A healthy SDK call emits a ``SystemMessage`` within a few seconds of
    spawning the CLI subprocess. A multi-minute silence with zero messages
    indicates a stalled subprocess or backend admit-rate starvation when
    several researcher invocations race for the same OAuth token.
    ``invoke_sdk`` retries once on this signal before giving up.
    """


# ---------------------------------------------------------------------------
# Token-accounting helpers
# ---------------------------------------------------------------------------


def _tokens_from_usage(usage: dict[str, Any], previous: TokensUsed) -> TokensUsed:
    """Merge an SDK ``usage`` dict into *previous*, preserving fields the SDK omits."""
    return TokensUsed(
        input_tokens=usage.get("input_tokens", previous.input_tokens),
        output_tokens=usage.get("output_tokens", previous.output_tokens),
        cache_read_tokens=usage.get("cache_read_input_tokens", previous.cache_read_tokens),
        cache_write_tokens=usage.get("cache_creation_input_tokens", previous.cache_write_tokens),
    )


def _add_tokens(a: TokensUsed, b: TokensUsed) -> TokensUsed:
    """Sum two :class:`TokensUsed` instances field-wise."""
    return TokensUsed(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
    )


def _absorb_metadata(
    message: Any,
    *,
    stop_reason: str | None,
    tokens: TokensUsed,
) -> tuple[str | None, TokensUsed]:
    """Update accumulator state from any message that carries metadata — pure transformation."""
    if message.stop_reason:
        stop_reason = message.stop_reason
    if message.usage:
        tokens = _tokens_from_usage(message.usage, tokens)
    return stop_reason, tokens


# ---------------------------------------------------------------------------
# Retry-message builder
# ---------------------------------------------------------------------------


def _build_retry_message(
    *,
    framing: str,
    error_detail: str,
    contract_ref: str,
    directive: str,
    extra_directives: Sequence[str] = (),
) -> str:
    """Construct a corrective-retry message.

    Per ``llm-output-validation.md`` § Corrective-retry message construction:
    - Explicit framing line naming which contract failed.
    - First error only (caller extracts it).
    - Contract reference.
    - Directive with the relevant section header.
    - Optional trailing directives (e.g. the adaptive-research
      "content-preservation" nudge).

    Does NOT contain: full error list, analytical guidance, raw input data.
    """
    return "\n\n".join([framing, error_detail, contract_ref, directive, *extra_directives])


# ---------------------------------------------------------------------------
# Raw-response renderer
# ---------------------------------------------------------------------------


def _render_raw_response(payload: dict[str, Any] | None, response_text: str) -> str:
    """Format the SDK response for ``HarnessSuccess.raw_response`` and the diagnostic.

    The structured-output dict is the load-bearing artifact; any text the
    agent emitted alongside (rare in JSON mode but seen under provocation)
    is preserved as a leading section so forensic review is not lossy.
    """
    parts: list[str] = []
    if response_text:
        parts.append(response_text)
    if payload is not None:
        parts.append(json.dumps(payload, indent=2, sort_keys=True))
    else:
        parts.append("(structured_output not populated)")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# DiagState writer
# ---------------------------------------------------------------------------


@dataclass
class DiagState:
    """Mutable diagnostic state accumulated during an invocation.

    The default layout writes ``response_initial.md`` and conditionally
    ``response_retry.md`` (the canonical domain-researcher / qualitative
    / adaptive shape). Pass ``response_filename="response.md"`` to switch
    to the synthesizer's single-response layout.

    ``tool_calls_used`` is included in the metadata only when set to a
    positive value or when explicitly requested by passing
    ``record_tool_calls=True``. The domain-researcher harness does not
    track tool calls; the other three do.

    ``archive_layer`` selects the layer segment of the diagnostic path.
    Defaults to ``"analysis"`` (the four analysis harnesses' archive
    convention). The three decision harnesses pass ``"decision"`` so
    their diagnostics land under ``invocations/<id>/decision/<agent>/``.
    """

    agent_name: str
    invocation_id: str
    prompt_text: str
    user_message: str
    model: str
    archive_root: Path | None

    response_initial: str = ""
    response_retry: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: TokensUsed = field(
        default_factory=lambda: TokensUsed(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
        )
    )
    retry_count: int = 0
    tool_calls_used: int = 0
    record_tool_calls: bool = False
    # When set, the diagnostic writes a single response file under this name
    # instead of the response_initial.md / response_retry.md pair.
    response_filename: str | None = None
    # Layer segment of the diagnostic path; analysis harnesses use the
    # default, decision harnesses override to "decision".
    archive_layer: str = "analysis"

    def write(
        self,
        *,
        success: bool,
        wall_clock_seconds: float,
        stop_reason: str | None,
    ) -> None:
        """Flush the diagnostic record to disk, if archive_root is set."""
        if self.archive_root is None:
            return
        diag_dir = (
            self.archive_root
            / INVOCATIONS_DIRNAME
            / self.invocation_id
            / self.archive_layer
            / self.agent_name
        )
        diag_dir.mkdir(parents=True, exist_ok=True)

        (diag_dir / "prompt.md").write_text(self.prompt_text, encoding="utf-8")
        (diag_dir / "user_message.md").write_text(self.user_message, encoding="utf-8")
        if self.response_filename is not None:
            (diag_dir / self.response_filename).write_text(self.response_initial, encoding="utf-8")
        else:
            (diag_dir / "response_initial.md").write_text(self.response_initial, encoding="utf-8")
            if self.response_retry is not None:
                (diag_dir / "response_retry.md").write_text(self.response_retry, encoding="utf-8")
        (diag_dir / "errors.json").write_text(json.dumps(self.errors, indent=2), encoding="utf-8")
        metadata: dict[str, Any] = {
            "model": self.model,
            "tokens_used": self.tokens_used.model_dump(),
            "wall_clock_seconds": wall_clock_seconds,
            "stop_reason": stop_reason,
            "success": success,
        }
        # The DR harness omits retry_count from metadata only when it isn't a
        # parse/validate-retry harness; here both the retry and tool-call
        # fields are emitted whenever the corresponding feature is in use.
        if self.response_filename is None:
            metadata["retry_count"] = self.retry_count
        if self.record_tool_calls or self.tool_calls_used:
            metadata["tool_calls_used"] = self.tool_calls_used
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Collect-response driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectOutcome:
    """Result of one SDK call, parameterised by what the caller cares about."""

    structured_output: dict[str, Any] | None
    response_text: str
    stop_reason: str | None
    tokens_used: TokensUsed
    tool_calls: int
    session_id: str | None


async def _next_message(
    async_iter: AsyncIterator[Any],
    *,
    init_stall_timeout_seconds: float | None,
) -> Any:
    """Pull the next SDK message, applying the init-stall timeout if requested.

    Returns ``None`` on iterator exhaustion. Raises :class:`_StuckSDKCall`
    when ``init_stall_timeout_seconds`` is set and no message arrives in
    that window — the caller passes ``None`` after the first message to
    drop the watchdog.
    """
    if init_stall_timeout_seconds is None:
        try:
            return await anext(async_iter)
        except StopAsyncIteration:
            return None
    try:
        return await asyncio.wait_for(anext(async_iter), timeout=init_stall_timeout_seconds)
    except StopAsyncIteration:
        return None
    except TimeoutError as exc:
        raise _StuckSDKCall(
            f"No SDK message received within {init_stall_timeout_seconds}s"
        ) from exc


async def _collect_response(
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
    *,
    prompt: str,
    options: Any,
    init_stall_timeout_seconds: float | None = None,
    between_message_stall_seconds: float | None = None,
    tool_name_prefix: str | tuple[str, ...] | None = None,
) -> CollectOutcome:
    """Drive the SDK generator to completion.

    Returns a :class:`CollectOutcome` carrying every field any of the four
    harnesses might want — callers ignore those they don't track.

    ``structured_output`` is the dict the API delivers on ``ResultMessage``
    when ``output_format`` is set; ``None`` when the SDK did not populate it
    (the harness's parse path treats ``None`` as a parse failure). The
    concatenated ``response_text`` is preserved alongside for diagnostic-
    record forensics — JSON-mode runs typically have empty text but Sonnet
    occasionally narrates between tool calls.

    ``tool_calls`` counts ``ToolUseBlock`` instances in assistant messages.
    When ``tool_name_prefix`` is supplied (either a single ``str`` or a
    ``tuple[str, ...]``), only blocks whose ``name`` matches via
    ``str.startswith`` are counted; otherwise every ``ToolUseBlock``
    counts. The prefix lets per-harness MCP-wired runs filter out
    SDK-internal pseudo-events (e.g. ``StructuredOutput``); decision
    harnesses pass a tuple to merge multiple MCP-server allowlists.

    Two independent stall watchdogs guard the SDK driver loop:

    * ``init_stall_timeout_seconds`` bounds the wait for the *first* SDK
      message. A healthy call emits a ``SystemMessage`` within a few seconds
      of spawn; multi-minute silence with zero messages signals a stuck
      local CLI subprocess or backend admit-rate starvation.
    * ``between_message_stall_seconds`` bounds the wait *between* successive
      messages once the call is producing output. Covers the failure mode
      where the SDK emits the initial ``SystemMessage`` then hangs for the
      full outer budget without producing tokens (observed once on Windows
      under 4-way concurrent researcher launches). When ``None`` the
      between-message wait is unbounded, preserving the pre-existing
      "long thinking is not a stall" behaviour for harnesses that prefer
      to rely on the outer budget alone.

    Either watchdog firing raises :class:`_StuckSDKCall` so the caller's
    retry path can engage. Setting the between-message budget large enough
    to absorb genuine thinking gaps (~3 minutes) keeps long extended-
    thinking runs healthy while still catching the dead-loss subprocess
    case in a fraction of the outer budget.

    ``session_id`` is the SDK session identifier carried on the terminating
    ``ResultMessage``; threaded back so a corrective retry can pass it via
    ``options.resume`` to keep the agent's prior response in scope.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    text_parts: list[str] = []
    structured_output: dict[str, Any] | None = None
    stop_reason: str | None = None
    tokens = TokensUsed(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
    tool_calls = 0
    session_id: str | None = None

    query_iter = sdk_query_fn(prompt=prompt, options=options)
    async_iter = aiter(query_iter)
    pending_stall = init_stall_timeout_seconds
    try:
        while True:
            message = await _next_message(async_iter, init_stall_timeout_seconds=pending_stall)
            if message is None:
                break
            pending_stall = between_message_stall_seconds
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock) and (
                        tool_name_prefix is None or block.name.startswith(tool_name_prefix)
                    ):
                        tool_calls += 1
                stop_reason, tokens = _absorb_metadata(
                    message, stop_reason=stop_reason, tokens=tokens
                )
            elif isinstance(message, ResultMessage):
                stop_reason, tokens = _absorb_metadata(
                    message, stop_reason=stop_reason, tokens=tokens
                )
                session_id = message.session_id
                raw_so = getattr(message, "structured_output", None)
                if isinstance(raw_so, dict):
                    structured_output = raw_so
                if message.is_error:
                    raise _CLIResultError(
                        error_text=message.result or "(no result text)",
                        partial_response="".join(text_parts),
                        stop_reason=stop_reason,
                    )
                break
    finally:
        # Close from this task; GC-time aclose() races the SDK reader
        # and prints "asynchronous generator is already running" to stderr.
        await cast(AsyncGenerator[Any], query_iter).aclose()

    return CollectOutcome(
        structured_output=structured_output,
        response_text="".join(text_parts),
        stop_reason=stop_reason,
        tokens_used=tokens,
        tool_calls=tool_calls,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# SDK invocation wrapper (timeout + stall-retry + error translation)
# ---------------------------------------------------------------------------

CLIResultErrorMapping = Literal["context_overflow", "sdk_failure"]


async def invoke_sdk(  # noqa: C901,PLR0913 - all kw-only; each name documents one SDK behaviour the seven harnesses configure; the except-arms each translate one SDK signal into the matching HarnessFailure subclass (one branch per signal — extracting them would obscure the 1:1 translation table)
    *,
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
    prompt: str,
    options: Any,
    diag: DiagWriter,
    budget_seconds: float,
    init_stall_timeout_seconds: float | None,
    between_message_stall_seconds: float | None = None,
    concurrent_launch_jitter_seconds: float = 0.0,
    wall_start: float,
    agent_name: str,
    invocation_id: str,
    on_cli_result_error: CLIResultErrorMapping,
    tool_name_prefix: str | tuple[str, ...] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str,
) -> CollectOutcome:
    """Run one SDK call with stall-retry, timeout, and error translation.

    Behavioural toggles:

    * ``init_stall_timeout_seconds=None`` disables the first-message stall
      watchdog (QR, AR, synthesizer, analyst, strategist, PM behaviour).
      When set (the domain-researcher path), :class:`_StuckSDKCall` is
      retried once before surfacing :class:`TimeoutFailure`.
    * ``between_message_stall_seconds`` bounds the wait between successive
      SDK messages (default ``None`` = unbounded, preserving prior
      behaviour). Catches the failure mode where the SDK produces an
      initial message then hangs silently until the outer budget expires.
      A stuck between-message wait is treated identically to a stuck init
      wait: same :class:`_StuckSDKCall` raise + retry path.
    * ``concurrent_launch_jitter_seconds`` (default ``0.0``) introduces a
      random delay before opening the SDK call. Used at the parallel-
      researcher launch site to stagger concurrent subprocess spawns and
      reduce OAuth-token / admit-rate contention. The jitter is uniform on
      ``[0, value]``.
    * ``on_cli_result_error`` selects how a ``_CLIResultError`` (the SDK's
      ``is_error=True`` signal) is translated:
        - ``"context_overflow"`` → :class:`ContextOverflowFailure`
          (DR / QR / AR — the API surfaces context overflow this way).
        - ``"sdk_failure"`` → :class:`SDKFailure` (synthesizer + analyst +
          strategist + PM behaviour).

    Progress emission (single emit point per parent issue ALP-493 § B):

    * Emits ``progress.agent_request(phase, agent, model)`` immediately
      before opening the SDK call.
    * Emits ``progress.agent_response(phase, agent, model, duration_s,
      input_tokens, output_tokens, tool_calls, stop_reason)`` after the
      call settles — on the happy path with the outcome's fields, and on
      every terminal failure path with whatever cost the SDK accumulated
      before raising (zero tokens / no stop_reason for stalls and auth
      failures; partial tokens / stop_reason for ``_CLIResultError``).

    ``progress`` defaults to :class:`NoOpProgressEmitter` so production
    callers keep their original signature; ``phase`` is required and
    names the pipeline stage (``"domain_researchers"``, ``"analyst"``…).

    The function writes a diagnostic record on every terminal failure path
    and re-raises the matching :class:`HarnessFailure` subclass.
    """
    from claude_agent_sdk import ClaudeSDKError, CLIConnectionError

    if concurrent_launch_jitter_seconds > 0:
        await asyncio.sleep(random.uniform(0, concurrent_launch_jitter_seconds))

    progress.agent_request(phase=phase, agent=agent_name, model=diag.model)

    def _emit_response(
        *,
        stop_reason: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        tool_calls: int = 0,
    ) -> None:
        progress.agent_response(
            phase=phase,
            agent=agent_name,
            model=diag.model,
            duration_s=time.monotonic() - wall_start,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
        )

    def _record_failure(stop_reason: str | None) -> None:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=stop_reason,
        )

    stall_attempts = (
        2
        if (init_stall_timeout_seconds is not None or between_message_stall_seconds is not None)
        else 1
    )
    for stall_attempt in range(1, stall_attempts + 1):
        try:
            async with _get_sdk_call_semaphore():
                outcome = await asyncio.wait_for(
                    _collect_response(
                        sdk_query_fn,
                        prompt=prompt,
                        options=options,
                        init_stall_timeout_seconds=init_stall_timeout_seconds,
                        between_message_stall_seconds=between_message_stall_seconds,
                        tool_name_prefix=tool_name_prefix,
                    ),
                    timeout=budget_seconds,
                )
        except _StuckSDKCall as exc:
            if stall_attempt < stall_attempts:
                continue
            _record_failure(None)
            _emit_response(stop_reason=None)
            raise TimeoutFailure(
                f"SDK call stalled on two consecutive attempts "
                f"(init={init_stall_timeout_seconds}s, "
                f"between={between_message_stall_seconds}s). Likely "
                f"OAuth-token concurrency starvation or local CLI subprocess hang.",
                agent_name=agent_name,
                invocation_id=invocation_id,
            ) from exc
        except TimeoutError as exc:
            _record_failure(None)
            _emit_response(stop_reason=None)
            raise TimeoutFailure(
                f"Invocation exceeded latency budget of {budget_seconds}s",
                agent_name=agent_name,
                invocation_id=invocation_id,
            ) from exc
        except _CLIResultError as exc:
            _record_failure(exc.stop_reason)
            _emit_response(stop_reason=exc.stop_reason)
            if on_cli_result_error == "context_overflow":
                raise ContextOverflowFailure(
                    f"CLI returned is_error=True: {exc.error_text}",
                    agent_name=agent_name,
                    invocation_id=invocation_id,
                    raw_response=exc.partial_response,
                ) from exc
            raise SDKFailure(
                f"CLI returned is_error=True: {exc.error_text}",
                agent_name=agent_name,
                invocation_id=invocation_id,
            ) from exc
        except CLIConnectionError as exc:
            _record_failure(None)
            _emit_response(stop_reason=None)
            raise SDKFailure(
                f"Authentication or connection failure — ensure CLAUDE_CODE_OAUTH_TOKEN "
                f"is set and valid. Underlying error: {exc}",
                agent_name=agent_name,
                invocation_id=invocation_id,
                cause=exc,
            ) from exc
        except ClaudeSDKError as exc:
            _record_failure(None)
            _emit_response(stop_reason=None)
            raise SDKFailure(
                f"Non-recoverable SDK error: {exc}",
                agent_name=agent_name,
                invocation_id=invocation_id,
                cause=exc,
            ) from exc
        else:
            _emit_response(
                stop_reason=outcome.stop_reason,
                input_tokens=outcome.tokens_used.input_tokens,
                output_tokens=outcome.tokens_used.output_tokens,
                tool_calls=outcome.tool_calls,
            )
            return outcome
    raise AssertionError(  # pragma: no cover
        "unreachable: stall retry loop exhausted without returning or raising"
    )
