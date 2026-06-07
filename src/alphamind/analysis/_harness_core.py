"""Shared infrastructure for the four analysis-layer LLM harnesses.

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
import contextlib
import json
import logging
import os
import random
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._shared import TokensUsed

if TYPE_CHECKING:
    from alphamind.analysis._agent_call_capture import CaptureSignals

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
    "_SdkCallTracer",
    "_StuckSDKCall",
    "_absorb_metadata",
    "_add_tokens",
    "_build_retry_message",
    "_collect_response",
    "_describe_sdk_message",
    "_load_prompt",
    "_render_raw_response",
    "_tokens_from_usage",
    "capture_agent_call",
    "invoke_sdk",
]


# ---------------------------------------------------------------------------
# Global cap on concurrent SDK calls
# ---------------------------------------------------------------------------

# Sized to the widest single-phase fan-out today: ``domain_researchers``
# (3 sectors) + ``qualitative`` overlap under one ``asyncio.TaskGroup``
# for 4 concurrent SDK calls. ``analyst`` + ``strategist`` overlap for 2;
# ``synthesizer`` / ``adaptive_researcher`` / ``portfolio_manager`` each
# run solo.
#
# History (ALP-702): this was 1 on 2026-05-18 while diagnosing a
# back-to-back-stall failure mode in which the SDK's second in-process
# ``query()`` call would admit and then never stream
# ``AssistantMessage`` content. The actual root cause was the
# between-message stall watchdog firing falsely on Sonnet 4.6's silent
# extended-thinking gaps; cap=1 was a defensive belt added during the
# investigation. ALP-650 then moved every LLM harness onto subprocess
# isolation — each SDK call now runs in a fresh
# ``python -m alphamind.analysis._sdk_subprocess_worker``, so the
# in-process state degradation that motivated cap=1 cannot recur:
# corrective-retry harnesses may call ``invoke_sdk`` twice within one
# worker, but those calls are strictly sequential, and no worker
# overlaps SDK calls with itself. Restoring 4 unblocks the analysis-
# layer fan-out the runbook documents.
_MAX_CONCURRENT_SDK_CALLS = 4
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

    The agent_calls capture (ALP-880) rides the same ``write`` funnel: each
    ``write`` stamps the terminal outcome onto the diag, and the harness body
    wrapped in :func:`capture_agent_call` drains the one aggregated capture on
    exit. The richer ``CaptureSignals`` Protocol in
    :mod:`alphamind.analysis._agent_call_capture` describes that surface.
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
    as_of: datetime | None = None

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

    # --- agent_calls capture (ALP-880) -----------------------------------
    # Provenance signals the harness sets once at construction so the same
    # DiagState that backs the diagnostic archive also backs the agent_calls
    # row + provenance artifacts. When these stay at their defaults the call
    # carries no schema / no tools (a narrative agent) and the capture emits
    # null payloads for those artifacts. ``output_payload`` is the structured
    # output dict the harness assigns alongside ``response_initial``. These
    # plus the outcome stamps below satisfy the ``CaptureSignals`` Protocol in
    # ``_agent_call_capture`` so ``build_capture_from_diag`` projects this diag
    # without a per-class capture builder.
    prompt_path: str | None = None
    output_schema: dict[str, Any] | None = None
    tools_definition: list[str] | None = None
    sampling_params: dict[str, Any] = field(default_factory=dict)
    output_payload: dict[str, Any] | None = None
    # Stable per-agent-call id, generated once on first ``write`` so retries
    # aggregate into one record/provenance dir rather than one per attempt.
    agent_call_id: str | None = None
    # Last terminal outcome stamped by ``write`` — drives the capture build.
    last_success: bool | None = None
    last_wall_clock_seconds: float | None = None
    last_stop_reason: str | None = None

    @property
    def attempt_number(self) -> int:
        """1-indexed attempt count of the aggregated call (``retry_count`` + 1)."""
        return self.retry_count + 1

    @property
    def diag_dir(self) -> Path | None:
        """Per-agent diagnostic directory, or ``None`` when unarchived.

        ``invoke_sdk`` reads this off the *diag* it is handed so the
        per-call ``sdk_trace.jsonl`` lands next to ``metadata.json``.
        """
        if self.archive_root is None:
            return None
        if self.as_of is None:
            msg = "DiagState.as_of must be set when archive_root is provided"
            raise ValueError(msg)
        return (
            invocation_archive_dir(
                archive_root=self.archive_root,
                as_of=self.as_of,
                invocation_id=self.invocation_id,
            )
            / self.archive_layer
            / self.agent_name
        )

    def write(
        self,
        *,
        success: bool,
        wall_clock_seconds: float,
        stop_reason: str | None,
    ) -> None:
        """Flush the diagnostic record to disk, if archive_root is set.

        Also records this terminal outcome (success / wall-clock / stop-reason)
        and mints the stable ``agent_call_id`` on first call, so a subsequent
        :meth:`build_capture` can assemble the single aggregated agent_calls
        record regardless of whether the archive is enabled. Capture is
        independent of ``archive_root`` — the diag archive and the agent_calls
        provenance are separate layouts.
        """
        if self.agent_call_id is None:
            self.agent_call_id = f"ac-{uuid.uuid4().hex}"
        self.last_success = success
        self.last_wall_clock_seconds = wall_clock_seconds
        self.last_stop_reason = stop_reason

        diag_dir = self.diag_dir
        if diag_dir is None:
            return
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
        # The dual-response-file harnesses (response_initial/response_retry)
        # always emit retry_count. A single-response harness (response_filename
        # set, e.g. the synthesizer) emits it only once it has actually retried
        # — so the field's presence is itself the retry signal, and a
        # first-attempt success stays free of it (ALP-756).
        if self.response_filename is None or self.retry_count:
            metadata["retry_count"] = self.retry_count
        if self.record_tool_calls or self.tool_calls_used:
            metadata["tool_calls_used"] = self.tool_calls_used
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


@contextlib.asynccontextmanager
async def capture_agent_call(
    diag: CaptureSignals,
    *,
    telemetry_session: AsyncSession | None,
    provenance_root: Path | None,
) -> AsyncGenerator[None]:
    """Drain *diag*'s agent_calls capture once, on exit of the harness body.

    Wraps a harness's single attempt-loop scope. On exit — clean OR a raised
    :class:`HarnessFailure` — assembles the one aggregated capture from *diag*
    and persists the row + four provenance artifacts. A no-op when telemetry
    is not wired (``telemetry_session`` or ``provenance_root`` is ``None``), so
    the production-without-telemetry and unit-test paths are unchanged.

    Accepts any *diag* satisfying the ``CaptureSignals`` Protocol — the shared
    :class:`DiagState` and the decision harnesses' private ``_DiagState`` both
    qualify — so the single drain serves all nine agents.

    Capture must never break the call it observes: a persistence error is
    swallowed (the harness result / failure propagates regardless), mirroring
    the diagnostic writer's "diagnostics never break the call" contract.
    """
    if telemetry_session is None or provenance_root is None:
        yield
        return
    error: HarnessFailure | None = None
    try:
        yield
    except HarnessFailure as exc:
        error = exc
        raise
    finally:
        await _drain_capture(
            diag, telemetry_session=telemetry_session, provenance_root=provenance_root, error=error
        )


async def _drain_capture(
    diag: CaptureSignals,
    *,
    telemetry_session: AsyncSession,
    provenance_root: Path,
    error: HarnessFailure | None,
) -> None:
    """Build + persist *diag*'s capture; swallow persistence errors."""
    from alphamind.analysis._agent_call_capture import (
        build_capture_from_diag,
        persist_agent_call,
    )

    capture = build_capture_from_diag(diag, error=error)
    if capture is None:
        return
    try:
        await persist_agent_call(telemetry_session, capture, provenance_root=provenance_root)
    except Exception:
        logging.getLogger(__name__).exception(
            "agent_calls capture failed for %s/%s", diag.invocation_id, diag.agent_name
        )


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


def _describe_block(block: Any) -> dict[str, Any]:
    """One content-block summary for :func:`_describe_sdk_message`.

    For ``ToolUseBlock`` the SDK exposes ``id`` and ``name``; both are
    captured so the matching ``ToolResultBlock`` (which carries
    ``tool_use_id`` linking back to ``id``) can be paired by the
    ``verify_debug_e2e`` tool-layer health check (ALP-703).

    For ``ToolResultBlock`` the envelope JSON content is parsed and its
    ``quality`` field extracted — that is what the operator needs to see
    to know whether the analysis tool layer was healthy on this run.
    Unparseable content lands the row without ``quality``; the check
    classifies such rows as ``"other"``.
    """
    summary: dict[str, Any] = {"block": type(block).__name__}
    name = getattr(block, "name", None)
    if name is not None:
        summary["tool"] = name
    block_id = getattr(block, "id", None)
    if isinstance(block_id, str):
        summary["id"] = block_id
    tool_use_id = getattr(block, "tool_use_id", None)
    if isinstance(tool_use_id, str):
        summary["tool_use_id"] = tool_use_id
        is_error = getattr(block, "is_error", None)
        if isinstance(is_error, bool):
            summary["is_error"] = is_error
        quality = _extract_tool_result_quality(getattr(block, "content", None))
        if quality is not None:
            summary["quality"] = quality
    text = getattr(block, "text", None)
    if isinstance(text, str):
        summary["text_len"] = len(text)
    return summary


def _extract_tool_result_quality(content: Any) -> str | None:
    """Pull the envelope ``quality`` field out of a ToolResultBlock's content.

    The MCP adapter wraps each tool result in a single ``text`` content
    block whose body is the JSON-encoded envelope (see
    :mod:`alphamind.analysis.tools._sdk_adapter`). The SDK surfaces that
    content as either a plain string or a list of
    ``{"type": "text", "text": <json>}`` dicts depending on transport;
    handle both shapes and ignore anything else so a non-envelope tool
    (or a malformed payload) doesn't crash the trace summary.
    """
    if isinstance(content, str):
        return _parse_quality_from_json_text(content)
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            quality = _parse_quality_from_json_text(text)
            if quality is not None:
                return quality
    return None


def _parse_quality_from_json_text(text: str) -> str | None:
    """Decode *text* as JSON and return its ``quality`` field if present."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    quality = payload.get("quality")
    return quality if isinstance(quality, str) else None


def _describe_sdk_message(message: Any) -> dict[str, Any]:
    """Compact, JSON-safe description of one SDK message — never raises.

    Captures the message class plus whichever diagnostic attributes are
    present: a ``RateLimitEvent`` carries the full rate-limit posture
    (status / utilization / reset epoch), an ``AssistantMessage`` a
    per-block summary, a ``ResultMessage`` the terminal status + usage.
    Attribute-probed rather than ``isinstance``-typed so an unrecognised
    or future message type still lands a useful row in the trace.
    """
    detail: dict[str, Any] = {"type": type(message).__name__}
    try:
        rate_limit = getattr(message, "rate_limit_info", None)
        if rate_limit is not None:
            detail["rate_limit_info"] = (
                asdict(rate_limit)
                if is_dataclass(rate_limit) and not isinstance(rate_limit, type)
                else str(rate_limit)
            )
        subtype = getattr(message, "subtype", None)
        if subtype is not None:
            detail["subtype"] = subtype
        content = getattr(message, "content", None)
        if isinstance(content, list):
            detail["blocks"] = [_describe_block(block) for block in content]
        for attr in ("stop_reason", "is_error", "num_turns", "duration_ms", "duration_api_ms"):
            value = getattr(message, attr, None)
            if value is not None:
                detail[attr] = value
        usage = getattr(message, "usage", None)
        if isinstance(usage, dict):
            detail["usage"] = {
                key: usage[key]
                for key in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "output_tokens",
                )
                if key in usage
            }
    except (TypeError, AttributeError, ValueError) as exc:
        detail["describe_error"] = repr(exc)
    return detail


class _SdkCallTracer:
    """Append-only forensic trace of one SDK call.

    Writes ``sdk_trace.jsonl`` into the agent's diagnostic directory: one
    JSON line per SDK message arrival — carrying the inter-message
    ``gap_s`` — plus one per ``stderr`` line the CLI subprocess emits and
    harness-side lifecycle markers. Every record is flushed + ``fsync``'d,
    so when a stalled call is hard-killed at the latency budget the file
    still pinpoints *where* the stream went silent and what (if anything)
    the CLI logged across the gap.

    A no-op when the harness runs without an archive (``diag_dir=None``):
    production-without-archive and unit-test callers see no file writes
    and no behaviour change.
    """

    _FILENAME = "sdk_trace.jsonl"

    def __init__(self, diag_dir: Path | None) -> None:
        self._path: Path | None = diag_dir / self._FILENAME if diag_dir is not None else None
        self._start = time.monotonic()

    @property
    def enabled(self) -> bool:
        """Whether records are persisted (an archive directory was supplied)."""
        return self._path is not None

    def event(self, name: str, **fields: Any) -> None:
        """Record a harness-side lifecycle marker (call start, timeout, …)."""
        self._emit({"event": name, **fields})

    def message(self, message: Any, *, gap_s: float) -> None:
        """Record one SDK message arrival and the silence that preceded it."""
        if self._path is None:
            return
        self._emit(
            {"event": "sdk_message", "gap_s": round(gap_s, 3), **_describe_sdk_message(message)}
        )

    def cli_stderr(self, line: str) -> None:
        """Record one CLI ``stderr`` line — wired as ``ClaudeAgentOptions.stderr``."""
        self._emit({"event": "cli_stderr", "line": line.rstrip("\n")})

    def _emit(self, record: dict[str, Any]) -> None:
        if self._path is None:
            return
        framed = {
            "ts": datetime.now(UTC).isoformat(),
            "elapsed_s": round(time.monotonic() - self._start, 3),
            **record,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(framed, default=str, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Diagnostics must never break the call they observe.
            pass


def _attach_stderr_hook(options: Any, tracer: _SdkCallTracer) -> Any:
    """Rebuild SDK *options* with the tracer's CLI-stderr callback wired in.

    Returns *options* unchanged when tracing is disabled or the options
    shape cannot carry a ``stderr`` field (e.g. a unit-test stub) — so a
    stall that logs retry/backoff chatter is captured without perturbing
    a production or test call that has no archive.
    """
    if not tracer.enabled or not is_dataclass(options) or isinstance(options, type):
        return options
    with contextlib.suppress(TypeError):
        options = replace(options, stderr=tracer.cli_stderr)
    return options


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
    tracer: _SdkCallTracer | None = None,
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

    # A disabled (no-archive) tracer keeps every call site unconditional —
    # its writes early-return, so production and unit-test paths are unchanged.
    tracer = tracer if tracer is not None else _SdkCallTracer(None)
    options = _attach_stderr_hook(options, tracer)
    tracer.event("call_start")

    query_iter = sdk_query_fn(prompt=prompt, options=options)
    async_iter = aiter(query_iter)
    pending_stall = init_stall_timeout_seconds
    last_message_at = time.monotonic()
    try:
        while True:
            message = await _next_message(async_iter, init_stall_timeout_seconds=pending_stall)
            arrived_at = time.monotonic()
            if message is None:
                tracer.event("iterator_exhausted", gap_s=round(arrived_at - last_message_at, 3))
                break
            tracer.message(message, gap_s=arrived_at - last_message_at)
            last_message_at = arrived_at
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
        tracer.event("collect_exit")
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
      input_tokens, cache_read_tokens, cache_write_tokens, output_tokens,
      tool_calls, stop_reason)`` after the call settles. Happy path
      forwards the outcome's accumulated token counts; every terminal
      failure path emits zero tokens because ``_collect_response``'s
      local accumulator is not threaded through the exception types
      that the failure arms catch (``_CLIResultError`` /
      ``_StuckSDKCall`` / ``CLIConnectionError`` / ``ClaudeSDKError``
      carry only error/stop-reason context, not partial usage).
      ``stop_reason`` is forwarded on ``_CLIResultError`` (the SDK
      surfaces it on the error result) and ``None`` elsewhere. The
      three input-side counts mirror the SDK ``usage`` split so the
      operator can tell apart a cache-hit prompt (bulk in
      ``cache_read_tokens``) from a broken context-assembly path
      (ALP-701).

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

    tracer = _SdkCallTracer(getattr(diag, "diag_dir", None))

    def _emit_response(
        *,
        stop_reason: str | None,
        input_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        output_tokens: int = 0,
        tool_calls: int = 0,
    ) -> None:
        progress.agent_response(
            phase=phase,
            agent=agent_name,
            model=diag.model,
            duration_s=time.monotonic() - wall_start,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
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
        tracer.event("attempt_start", attempt=stall_attempt)
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
                        tracer=tracer,
                    ),
                    timeout=budget_seconds,
                )
        except _StuckSDKCall as exc:
            tracer.event("stuck", attempt=stall_attempt, retrying=stall_attempt < stall_attempts)
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
            tracer.event("budget_timeout", attempt=stall_attempt, budget_s=budget_seconds)
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
            tracer.event(
                "success",
                stop_reason=outcome.stop_reason,
                output_tokens=outcome.tokens_used.output_tokens,
                tool_calls=outcome.tool_calls,
            )
            _emit_response(
                stop_reason=outcome.stop_reason,
                input_tokens=outcome.tokens_used.input_tokens,
                cache_read_tokens=outcome.tokens_used.cache_read_tokens,
                cache_write_tokens=outcome.tokens_used.cache_write_tokens,
                output_tokens=outcome.tokens_used.output_tokens,
                tool_calls=outcome.tool_calls,
            )
            return outcome
    raise AssertionError(  # pragma: no cover
        "unreachable: stall retry loop exhausted without returning or raising"
    )
