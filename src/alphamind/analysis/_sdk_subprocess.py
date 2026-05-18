"""Subprocess-isolated SDK call wrapper.

The Claude Agent SDK exhibits process-level state leakage on Windows: the
first ``claude_agent_sdk.query()`` call in a Python process generally
succeeds, but subsequent calls in the same process can stall after
admission — emitting only a ``SystemMessage`` and never streaming
``AssistantMessage`` content. The stall surfaces as ``stop_reason: null``
with zero output tokens. Even the same-process stall-retry mechanism
sometimes fails (both attempts stall) because the process state itself
has degraded.

See ``docs/_investigation/debug-e2e-sdk-stall.md`` for the full
reproduction and analysis. The "back-to-back" diagnostic
(``scripts/_debug_two_back_to_back.py``) shows call 1 sometimes stalls
once and retries to success, call 2 sometimes has both attempts stall.

The workaround in this module: run each harness invocation in a fresh
Python subprocess. Each subprocess imports the harness module, runs one
``invoke_<agent>`` call, serializes the result to stdout, and exits.
The parent reads stdout, reconstructs the typed result, and returns it
— so the caller's interface is unchanged. Failures are reconstructed
from a typed error payload so the orchestrator's per-failure-class
handling continues to work.

Per-harness subprocess wrappers live next to this module
(``invoke_domain_researcher_in_subprocess`` here for now; siblings
added incrementally as the e2e gate reveals which harnesses need
isolation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    _get_sdk_call_semaphore,
)
from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.harness import HarnessSuccess as DRHarnessSuccess
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.harness import HarnessSuccess as QRHarnessSuccess
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.config.models.agents import BaseAgentConfig

__all__ = [
    "invoke_domain_researcher_in_subprocess",
    "invoke_qualitative_researcher_in_subprocess",
]

logger = logging.getLogger(__name__)

# The worker module path used by ``-m`` when spawning. Lives next to this
# module so the subprocess can import all of alphamind's harness machinery
# the same way the parent does.
_WORKER_MODULE = "alphamind.analysis._sdk_subprocess_worker"


def _extract_progress_jsonl_path(progress: ProgressEmitter) -> str | None:
    """Pull the JSONL file path off a ``JsonlProgressEmitter``.

    The worker re-instantiates a fresh emitter against this path so the
    ``agent_request`` / ``agent_response`` events land in the same
    archive ``progress.jsonl`` as if the call had run in-parent.

    Returns ``None`` for the no-op emitter (production / tests).
    """
    path = getattr(progress, "_path", None)
    if isinstance(path, Path):
        return str(path)
    return None


def _raise_failure(error_payload: dict[str, Any]) -> None:
    """Reconstruct and raise the typed harness failure on the parent side."""
    err_type = error_payload["error_type"]
    err_msg = error_payload["error_msg"]
    agent_name = error_payload.get("agent_name") or "unknown"
    invocation_id = error_payload.get("invocation_id") or "unknown"

    if err_type == "MalformedOutputFailure":
        raise MalformedOutputFailure(
            err_msg,
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response_initial=error_payload.get("raw_response_initial"),
            raw_response_retry=error_payload.get("raw_response_retry"),
        )
    if err_type == "ContextOverflowFailure":
        raise ContextOverflowFailure(
            err_msg,
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response=error_payload.get("raw_response"),
        )
    if err_type == "TimeoutFailure":
        raise TimeoutFailure(
            err_msg,
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
    # SDKFailure or anything unrecognized — surface as SDKFailure so the
    # orchestrator's fail-closed path engages.
    raise SDKFailure(
        f"{err_type}: {err_msg}",
        agent_name=agent_name,
        invocation_id=invocation_id,
    )


async def _run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Spawn the worker subprocess under the parent's SDK-call semaphore.

    The semaphore is acquired before spawning ``claude.exe`` and released
    after the subprocess exits. Without this gate, parallel callers (e.g.
    the three domain-researcher tasks plus the in-parent
    ``qualitative_researcher`` call running concurrently under the
    analysis-pipeline ``TaskGroup``) would each spawn their own
    ``claude.exe`` in parallel — the 4-way concurrency the original
    in-process pipeline confirmed deterministically stalls.
    """
    async with _get_sdk_call_semaphore():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},  # propagate CLAUDE_CODE_OAUTH_TOKEN, DATABASE_PATH, etc.
        )
        return await _drive_worker(proc, payload)


async def _drive_worker(
    proc: asyncio.subprocess.Process, payload: dict[str, Any]
) -> dict[str, Any]:
    """Stream the payload into *proc*, collect the result, raise on protocol violation."""
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_bytes, stderr_bytes = await proc.communicate(json.dumps(payload).encode("utf-8"))
    returncode = proc.returncode

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if stderr_text:
        # Forward worker stderr to the parent's logger so a crash leaves
        # a trail — the worker's stderr is normally the only signal of an
        # import-time crash or unhandled exception.
        logger.warning("[sdk-subprocess stderr] %s", stderr_text.rstrip())

    if returncode != 0 and not stdout_bytes.strip():
        raise SDKFailure(
            f"SDK subprocess exited {returncode} with no result payload. "
            f"stderr: {stderr_text[:1000]}",
            agent_name=payload.get("agent_name", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        )

    # The worker writes a single JSON line as the LAST stdout line; earlier
    # lines (if any) are noise. Read from the end.
    output_lines = [line for line in stdout_bytes.decode("utf-8").splitlines() if line.strip()]
    if not output_lines:
        raise SDKFailure(
            f"SDK subprocess produced no stdout. stderr: {stderr_text[:1000]}",
            agent_name=payload.get("agent_name", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        )

    try:
        parsed: dict[str, Any] = json.loads(output_lines[-1])
    except json.JSONDecodeError as exc:
        raise SDKFailure(
            f"SDK subprocess emitted malformed JSON: {exc}. Last line: {output_lines[-1][:500]!r}",
            agent_name=payload.get("agent_name", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        ) from exc
    return parsed


async def invoke_domain_researcher_in_subprocess(
    *,
    agent_config: BaseAgentConfig,
    sector: Sector,
    user_message: str,
    invocation_id: str,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,  # noqa: ARG001 — signature parity; subprocess uses its own SDK call
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "domain_researchers",
) -> DRHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_domain_researcher``.

    The SDK call runs in a fresh Python subprocess (see module docstring
    for why); the parent reconstructs the typed result on completion.
    """
    payload = {
        "agent": "domain_researcher",
        "agent_config": agent_config.model_dump(mode="json"),
        "sector": sector.value,
        "user_message": user_message,
        "invocation_id": invocation_id,
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)

    return DRHarnessSuccess(
        brief=SectorBrief.model_validate(result["brief"]),
        raw_response=result["raw_response"],
        retry_count=result["retry_count"],
        tokens_used=TokensUsed.model_validate(result["tokens_used"]),
        wall_clock_seconds=result["wall_clock_seconds"],
    )


async def invoke_qualitative_researcher_in_subprocess(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    universe: frozenset[str],
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,  # noqa: ARG001 — signature parity; subprocess uses its own SDK call
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "qualitative",
) -> QRHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_qualitative_researcher``.

    Note: the in-parent ``invoke_qualitative_researcher`` takes a
    ``session`` argument so it can wire the analysis MCP server's
    ``news_search`` / ``prediction_markets`` / ``earnings_commentary``
    tools against the live DB connection. The subprocess opens its own
    session from ``DATABASE_PATH`` and rebuilds the MCP server there
    — the in-parent ``session`` argument is intentionally not exposed
    here because closures over an open session can't cross the
    subprocess boundary.
    """
    payload = {
        "agent": "qualitative_researcher",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "invocation_id": invocation_id,
        "universe": sorted(universe),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)

    return QRHarnessSuccess(
        brief=QualitativeBrief.model_validate(result["brief"]),
        raw_response=result["raw_response"],
        retry_count=result["retry_count"],
        tokens_used=TokensUsed.model_validate(result["tokens_used"]),
        tool_calls_used=result["tool_calls_used"],
        wall_clock_seconds=result["wall_clock_seconds"],
    )
