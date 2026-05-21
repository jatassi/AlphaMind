"""SDK subprocess worker — entry point for one isolated harness invocation.

Spawned by :mod:`alphamind.analysis._sdk_subprocess` via
``python -m alphamind.analysis._sdk_subprocess_worker``. Reads a JSON
payload from stdin, dispatches to the matching ``invoke_<agent>``,
and writes a single JSON line of result (or typed-error metadata) to
stdout before exiting.

The subprocess boundary is the workaround for the SDK's process-level
state leakage that causes intermittent ``stop_reason: null`` /
zero-token stalls on the second-or-later SDK call in a Python process.
Each invocation gets a fresh process and therefore a fresh SDK module /
CLI subprocess context.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.harness import invoke_domain_researcher
from alphamind.analysis.qualitative_research.harness import invoke_qualitative_researcher
from alphamind.config.models.agents import BaseAgentConfig
from alphamind.persistence.session import make_engine, make_session_factory


class _JsonlAppender:
    """Worker-local JSONL progress emitter.

    Implements :class:`ProgressEmitter` Protocol via append-only writes to
    the same ``progress.jsonl`` the parent's
    :class:`alphamind.scheduler.debug_e2e.jsonl_emitter.JsonlProgressEmitter`
    writes — matching its on-disk format so a single archive file
    interleaves parent ``phase_*`` events with worker ``agent_request`` /
    ``agent_response`` events.

    Inlined here rather than imported from ``scheduler.debug_e2e`` to
    keep the worker module within the ``analysis`` layer of the
    composition-root-layering import contract — the subprocess entry
    sits at the same architectural level as the harness it dispatches.
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def phase_start(self, phase: str) -> None:
        self._write({"event": "phase_start", "phase": phase})

    def phase_done(self, phase: str, **fields: Any) -> None:
        self._write({"event": "phase_done", "phase": phase, **fields})

    def agent_request(self, **fields: Any) -> None:
        self._write({"event": "agent_request", **fields})

    def agent_response(self, **fields: Any) -> None:
        self._write({"event": "agent_response", **fields})

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload["timestamp"] = datetime.now(UTC).isoformat()
        with self._path.open("a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _resolve_progress(progress_jsonl_path: str | None) -> ProgressEmitter:
    if progress_jsonl_path is None:
        return NOOP_PROGRESS_EMITTER
    return _JsonlAppender(path=Path(progress_jsonl_path))


async def _run_domain_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``invoke_domain_researcher`` in this fresh process and serialize the result."""
    # Parent marshals the resolved (per-trigger-override-applied)
    # ``agent_config`` as a dict; reconstruct here so the worker uses the
    # same Pydantic instance the parent would have.
    sector = Sector(payload["sector"])
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None

    try:
        result = await invoke_domain_researcher(
            agent_config=agent_config,
            sector=sector,
            user_message=payload["user_message"],
            invocation_id=payload["invocation_id"],
            archive_root=archive_root,
            progress=progress,
            phase=payload.get("phase", "domain_researchers"),
        )
    except MalformedOutputFailure as exc:
        return {
            "kind": "failure",
            "error_type": "MalformedOutputFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response_initial": exc.raw_response_initial,
            "raw_response_retry": exc.raw_response_retry,
        }
    except ContextOverflowFailure as exc:
        return {
            "kind": "failure",
            "error_type": "ContextOverflowFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response": exc.raw_response,
        }
    except TimeoutFailure as exc:
        return {
            "kind": "failure",
            "error_type": "TimeoutFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }
    except SDKFailure as exc:
        return {
            "kind": "failure",
            "error_type": "SDKFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }

    return {
        "kind": "success",
        "brief": result.brief.model_dump(mode="json"),
        "raw_response": result.raw_response,
        "retry_count": result.retry_count,
        "tokens_used": result.tokens_used.model_dump(mode="json"),
        "wall_clock_seconds": result.wall_clock_seconds,
    }


def _failure_payload(exc: Exception) -> dict[str, Any]:
    """Translate a known :class:`HarnessFailure` subclass into the wire dict."""
    if isinstance(exc, MalformedOutputFailure):
        return {
            "kind": "failure",
            "error_type": "MalformedOutputFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response_initial": exc.raw_response_initial,
            "raw_response_retry": exc.raw_response_retry,
        }
    if isinstance(exc, ContextOverflowFailure):
        return {
            "kind": "failure",
            "error_type": "ContextOverflowFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response": exc.raw_response,
        }
    if isinstance(exc, TimeoutFailure):
        return {
            "kind": "failure",
            "error_type": "TimeoutFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }
    if isinstance(exc, SDKFailure):
        return {
            "kind": "failure",
            "error_type": "SDKFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }
    # Unknown exception — surface as SDKFailure so the parent's fail-closed path engages.
    return {
        "kind": "failure",
        "error_type": "SDKFailure",
        "error_msg": f"{type(exc).__name__}: {exc}",
        "agent_name": getattr(exc, "agent_name", "unknown"),
        "invocation_id": getattr(exc, "invocation_id", "unknown"),
    }


async def _run_qualitative_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``invoke_qualitative_researcher`` in this fresh process and serialize the result.

    Opens a fresh SQLAlchemy session against ``DATABASE_PATH`` so the
    in-process MCP server (``alphamind_qualitative``) wires the
    ``news_search`` / ``prediction_markets`` / ``earnings_commentary``
    tools against the same DB the parent uses. Session is committed
    and closed before returning.
    """
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    universe = frozenset(payload["universe"])

    engine = make_engine()
    session_factory = make_session_factory(engine)
    session = session_factory()
    try:
        try:
            result = await invoke_qualitative_researcher(
                agent_config=agent_config,
                user_message=payload["user_message"],
                invocation_id=payload["invocation_id"],
                session=session,
                universe=universe,
                archive_root=archive_root,
                progress=progress,
                phase=payload.get("phase", "qualitative"),
            )
        except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
            return _failure_payload(exc)

        return {
            "kind": "success",
            "brief": result.brief.model_dump(mode="json"),
            "raw_response": result.raw_response,
            "retry_count": result.retry_count,
            "tokens_used": result.tokens_used.model_dump(mode="json"),
            "tool_calls_used": result.tool_calls_used,
            "wall_clock_seconds": result.wall_clock_seconds,
        }
    finally:
        session.close()
        engine.dispose()


async def main() -> int:
    payload = json.loads(sys.stdin.read())
    agent = payload["agent"]
    if agent == "domain_researcher":
        result = await _run_domain_researcher(payload)
    elif agent == "qualitative_researcher":
        result = await _run_qualitative_researcher(payload)
    else:
        result = {
            "kind": "failure",
            "error_type": "SDKFailure",
            "error_msg": f"Unsupported agent kind in worker: {agent!r}",
            "agent_name": payload.get("agent_name") or "unknown",
            "invocation_id": payload.get("invocation_id") or "unknown",
        }

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0 if result["kind"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
