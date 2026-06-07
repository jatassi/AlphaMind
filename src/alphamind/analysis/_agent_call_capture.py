"""agent_calls telemetry capture — extends the shared harness diag mechanism (ALP-880).

Every LLM agent invocation persists one ``agent_calls`` row plus its four raw
provenance artifacts. This module is the capture layer that the shared harness
core (:mod:`alphamind.analysis._harness_core`) drives off the same
``DiagState`` it already accumulates for the diagnostic archive — *extending*
that mechanism rather than introducing a second independent artifact writer.

Two halves, split functional-core / imperative-shell:

* :class:`AgentCallCapture` is an immutable value object holding every signal a
  completed call yields. :meth:`AgentCallCapture.to_record` is a pure
  projection to the persisted :class:`AgentCallRecord` (story 02a's shape — not
  redefined here). :meth:`AgentCallCapture.write_artifacts` writes the four
  provenance files (synchronous filesystem I/O, the same idiom
  ``DiagState.write`` already uses for ``prompt.md`` / ``metadata.json``).
* :func:`persist_agent_call` is the async shell: write the artifacts, then
  insert the row via story 02a's :func:`insert_agent_call`. The DB insert is
  the only async step; it runs in the subprocess worker that owns the session.

The provenance layout
``data/provenance/invocations/{invocation_id}/agent_calls/{agent_call_id}/``
is pinned by ``docs/design/05-execution-layer/state-persistence.md`` § Agent
calls — distinct from the date-partitioned diagnostic archive layout.

Multi-API-call agents (the adaptive researcher's tool loop, below-boundary
retries) aggregate into exactly one capture: the harness accumulates tokens
across constituent calls onto its ``DiagState`` and assembles a single
capture at the terminal path, so one row is written per agent call regardless
of how many API round-trips it took.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    HarnessFailure,
    MalformedOutputFailure,
    TimeoutFailure,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.state.repository.agent_calls_queries import insert_agent_call
from alphamind.state.tables.agent_calls import AgentCallErrorClass, AgentCallRecord

__all__ = [
    "AgentCallCapture",
    "CaptureSignals",
    "build_capture_from_diag",
    "error_class_for_failure",
    "new_agent_call_id",
    "persist_agent_call",
    "provenance_dir",
    "prompt_content_hash",
    "prompt_git_sha",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def new_agent_call_id() -> str:
    """Mint a fresh, stable per-agent-call identifier."""
    return f"ac-{uuid.uuid4().hex}"


def prompt_content_hash(text: str) -> str:
    """SHA-256 of the prompt text actually sent to the API (hex digest)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_git_sha(prompt_path: str) -> str:
    """Git blob SHA of the committed prompt file, or ``""`` if unavailable.

    Computes ``git hash-object`` against the working-tree file so the value
    confirms which committed prompt version was on disk for the call. A
    non-git environment (or a deleted prompt) yields ``""`` — the capture
    stays best-effort and never fails the call it observes.
    """
    try:
        completed = subprocess.run(
            ["git", "hash-object", prompt_path],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return completed.stdout.strip()

_SYSTEM_PROMPT_FILENAME = "system_prompt.md"
_OUTPUT_SCHEMA_FILENAME = "output_schema.json"
_TOOLS_DEFINITION_FILENAME = "tools_definition.json"
_OUTPUT_FILENAME = "output.json"


def provenance_dir(*, provenance_root: Path, invocation_id: str, agent_call_id: str) -> Path:
    """Return the per-call provenance directory under *provenance_root*.

    Pins ``invocations/{invocation_id}/agent_calls/{agent_call_id}/`` —
    *provenance_root* is the ``data/provenance`` directory; callers append
    nothing further. Distinct from the date-partitioned diagnostic archive
    layout (:func:`alphamind._kernel.archive_layout.invocation_archive_dir`).
    """
    return provenance_root / "invocations" / invocation_id / "agent_calls" / agent_call_id


def error_class_for_failure(exc: HarnessFailure) -> AgentCallErrorClass:
    """Map a :class:`HarnessFailure` subclass to its persisted ``error_class``.

    The harness taxonomy and the storage vocabulary differ in one place: an
    :class:`SDKFailure` (auth / non-recoverable SDK / CLI-error result) records
    as ``model_api_error``. ``TimeoutFailure`` / ``MalformedOutputFailure`` /
    ``ContextOverflowFailure`` map to their like-named members. Any future
    ``HarnessFailure`` subclass falls through to ``model_api_error`` — the
    fail-closed bucket for a non-recoverable call.
    """
    if isinstance(exc, TimeoutFailure):
        return AgentCallErrorClass.timeout
    if isinstance(exc, MalformedOutputFailure):
        return AgentCallErrorClass.malformed_output
    if isinstance(exc, ContextOverflowFailure):
        return AgentCallErrorClass.context_overflow
    return AgentCallErrorClass.model_api_error


@dataclasses.dataclass(frozen=True, slots=True)
class AgentCallCapture:
    """Every signal one completed agent call yields, ready to persist.

    Assembled by the harness at its terminal path from the accumulated
    ``DiagState`` plus the call-specific provenance signals the harness knows
    (the system prompt's git SHA + content hash, the sampling params, the
    output schema, the tool definitions, and the structured output payload).
    Immutable; projected to the persisted record by :meth:`to_record`.
    """

    agent_call_id: str
    invocation_id: str
    agent_name: str
    attempt_number: int
    model_id: str
    prompt_path: str
    prompt_git_sha: str
    prompt_content_hash: str
    system_prompt_text: str
    sampling_params: dict[str, Any]
    output_schema: dict[str, Any] | None
    tools_definition: list[str] | None
    output_payload: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    wall_clock_ms: int
    stop_reason: str
    success: bool
    error_class: AgentCallErrorClass | None
    error_message: str | None

    def to_record(self, *, provenance_root: Path) -> AgentCallRecord:
        """Project to the persisted :class:`AgentCallRecord` (pure).

        ``output_artifact_ref`` points at the per-call provenance directory the
        four artifacts are written under; ``output_schema_ref`` /
        ``tools_definition_ref`` point at the individual artifact files (null
        when the agent has no schema / no tools).
        """
        pdir = provenance_dir(
            provenance_root=provenance_root,
            invocation_id=self.invocation_id,
            agent_call_id=self.agent_call_id,
        )
        return AgentCallRecord(
            agent_call_id=self.agent_call_id,
            invocation_id=self.invocation_id,
            agent_name=self.agent_name,
            attempt_number=self.attempt_number,
            model_id=self.model_id,
            prompt_path=self.prompt_path,
            prompt_git_sha=self.prompt_git_sha,
            prompt_content_hash=self.prompt_content_hash,
            sampling_params_json=json.dumps(self.sampling_params, sort_keys=True),
            output_schema_ref=(
                str(pdir / _OUTPUT_SCHEMA_FILENAME) if self.output_schema is not None else None
            ),
            tools_definition_ref=(
                str(pdir / _TOOLS_DEFINITION_FILENAME)
                if self.tools_definition is not None
                else None
            ),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            wall_clock_ms=self.wall_clock_ms,
            stop_reason=self.stop_reason,
            success=self.success,
            error_class=self.error_class,
            error_message=self.error_message,
            output_artifact_ref=str(pdir),
        )

    def write_artifacts(self, *, provenance_root: Path) -> None:
        """Write the four provenance files for this call (synchronous fs I/O).

        All four files are always emitted so the layout is uniform across the
        9 agents; a narrative agent with no schema / no tools / no structured
        output gets ``null`` payloads rather than missing files.
        """
        pdir = provenance_dir(
            provenance_root=provenance_root,
            invocation_id=self.invocation_id,
            agent_call_id=self.agent_call_id,
        )
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / _SYSTEM_PROMPT_FILENAME).write_text(self.system_prompt_text, encoding="utf-8")
        (pdir / _OUTPUT_SCHEMA_FILENAME).write_text(
            json.dumps(self.output_schema, indent=2, sort_keys=True), encoding="utf-8"
        )
        (pdir / _TOOLS_DEFINITION_FILENAME).write_text(
            json.dumps(self.tools_definition, indent=2), encoding="utf-8"
        )
        (pdir / _OUTPUT_FILENAME).write_text(
            json.dumps(self.output_payload, indent=2, sort_keys=True), encoding="utf-8"
        )


class CaptureSignals(Protocol):
    """The capture-relevant surface every diag state exposes (ALP-880).

    Both the shared :class:`alphamind.analysis._harness_core.DiagState` and the
    decision harnesses' private ``_DiagState`` records satisfy this Protocol,
    so :func:`build_capture_from_diag` assembles the single aggregated capture
    off either without each diag class re-implementing the projection. The
    diag mints ``agent_call_id`` once and stamps the last terminal outcome
    onto ``last_success`` / ``last_wall_clock_seconds`` / ``last_stop_reason``
    when its ``write`` runs; ``None`` ``last_success`` means no terminal write
    happened and no capture is produced.
    """

    agent_name: str
    invocation_id: str
    model: str
    prompt_text: str
    prompt_path: str | None
    sampling_params: dict[str, Any]
    output_schema: dict[str, Any] | None
    tools_definition: list[str] | None
    output_payload: dict[str, Any] | None
    tokens_used: TokensUsed
    agent_call_id: str | None
    attempt_number: int
    last_success: bool | None
    last_wall_clock_seconds: float | None
    last_stop_reason: str | None


def build_capture_from_diag(
    diag: CaptureSignals, *, error: HarnessFailure | None = None
) -> AgentCallCapture | None:
    """Assemble the single aggregated capture for *diag*, or ``None``.

    Returns ``None`` when no terminal ``write`` has stamped an outcome
    (``agent_call_id`` unset or ``last_success`` ``None``). The accumulated
    ``tokens_used`` / ``attempt_number`` / outcome reflect every constituent
    API call (tool loop + below-boundary retries), so the result is exactly
    one aggregated record per agent call. *error* is the raised
    :class:`HarnessFailure` on a failure path; its subclass selects the
    persisted ``error_class``.
    """
    if diag.agent_call_id is None or diag.last_success is None:
        return None
    wall_clock_seconds = diag.last_wall_clock_seconds or 0.0
    prompt_path = diag.prompt_path or ""
    return AgentCallCapture(
        agent_call_id=diag.agent_call_id,
        invocation_id=diag.invocation_id,
        agent_name=diag.agent_name,
        attempt_number=diag.attempt_number,
        model_id=diag.model,
        prompt_path=prompt_path,
        prompt_git_sha=prompt_git_sha(prompt_path) if prompt_path else "",
        prompt_content_hash=prompt_content_hash(diag.prompt_text),
        system_prompt_text=diag.prompt_text,
        sampling_params=dict(diag.sampling_params),
        output_schema=diag.output_schema,
        tools_definition=diag.tools_definition,
        output_payload=diag.output_payload,
        input_tokens=diag.tokens_used.input_tokens,
        output_tokens=diag.tokens_used.output_tokens,
        cache_read_tokens=diag.tokens_used.cache_read_tokens,
        cache_write_tokens=diag.tokens_used.cache_write_tokens,
        wall_clock_ms=int(wall_clock_seconds * 1000),
        stop_reason=diag.last_stop_reason or "",
        success=diag.last_success,
        error_class=error_class_for_failure(error) if error is not None else None,
        error_message=str(error) if error is not None else None,
    )


async def persist_agent_call(
    session: AsyncSession,
    capture: AgentCallCapture,
    *,
    provenance_root: Path,
) -> None:
    """Write the provenance artifacts, then insert the ``agent_calls`` row.

    The artifacts land first so the row's ``output_artifact_ref`` always points
    at an existing directory. The row is queued on *session*; the caller
    controls the commit (the subprocess worker commits its telemetry session
    before returning).
    """
    capture.write_artifacts(provenance_root=provenance_root)
    await insert_agent_call(session, capture.to_record(provenance_root=provenance_root))
