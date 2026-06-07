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
import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    HarnessFailure,
    MalformedOutputFailure,
    TimeoutFailure,
)
from alphamind.state.repository.agent_calls_queries import insert_agent_call
from alphamind.state.tables.agent_calls import AgentCallErrorClass, AgentCallRecord

__all__ = [
    "AgentCallCapture",
    "error_class_for_failure",
    "persist_agent_call",
    "provenance_dir",
]

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
