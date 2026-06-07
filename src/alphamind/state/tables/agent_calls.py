"""SQLAlchemy mapping for the ``agent_calls`` table (ALP-873).

One row per LLM agent invocation — analyst, strategist, portfolio manager,
synthesizer, qualitative researcher, adaptive researcher, the three domain
researchers, and the proposal pre-processor. Append-only; immutable once
written.

FK to ``invocations.invocation_id`` with ``ON DELETE RESTRICT`` — invocation
rows are the provenance root; deleting an invocation that has agent call rows
is prevented at the DB level.

``error_class`` is a closed ``StrEnum`` with seven members (see
``AgentCallErrorClass``). A CHECK constraint enforces the same vocabulary at
the storage layer so a future direct-SQL writer faces the same fail-closed
guarantee.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

if TYPE_CHECKING:
    pass


class AgentCallErrorClass(StrEnum):
    """Closed set of error classes for a failed agent call.

    Null on success — ``error_class`` is only populated when ``success`` is False.

    ``tool_use_error`` is reserved: the harness taxonomy has no tool-use-specific
    failure today, so no producer routes to it. It is retained in the vocabulary
    (and the frozen CHECK) so a future tool-use failure class can map to it
    without a migration. ``empty_response`` is produced by an empty-response
    transient that exhausted its retry budget (synthesizer); ``internal_error``
    is produced when a non-:class:`HarnessFailure` exception escapes the capture
    body after the call was already stamped (see ``_agent_call_capture`` /
    ``_harness_core``).
    """

    timeout = "timeout"
    malformed_output = "malformed_output"
    context_overflow = "context_overflow"
    model_api_error = "model_api_error"
    tool_use_error = "tool_use_error"
    empty_response = "empty_response"
    internal_error = "internal_error"


_ERROR_CLASS_VALUES: tuple[str, ...] = tuple(m.value for m in AgentCallErrorClass)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


@dataclasses.dataclass(frozen=True, slots=True)
class AgentCallRecord:
    """Immutable value object representing one LLM agent call.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Agent calls.
    Round-trips losslessly through the codec in ``agent_calls_codec.py``.
    """

    agent_call_id: str
    invocation_id: str
    agent_name: str
    attempt_number: int
    model_id: str
    prompt_path: str
    prompt_git_sha: str
    prompt_content_hash: str
    sampling_params_json: str
    output_schema_ref: str | None
    tools_definition_ref: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    wall_clock_ms: int
    stop_reason: str
    success: bool
    error_class: AgentCallErrorClass | None
    error_message: str | None
    output_artifact_ref: str | None


class AgentCallsRow(Base):
    """Append-only per-agent-call row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Agent calls.
    """

    __tablename__ = "agent_calls"

    agent_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    invocation_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("invocations.invocation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_path: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_git_sha: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    sampling_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_definition_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    wall_clock_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_reason: Mapped[str] = mapped_column(Text, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "error_class IS NULL OR " + _check_in("error_class", _ERROR_CLASS_VALUES),
            name="ck_agent_calls_error_class",
        ),
        Index("ix_agent_calls_invocation_id", "invocation_id"),
        Index("ix_agent_calls_agent_name_attempt", "agent_name", "attempt_number"),
    )


__all__ = ["AgentCallErrorClass", "AgentCallRecord", "AgentCallsRow"]
