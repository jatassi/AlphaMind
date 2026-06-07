"""Codec between ``AgentCallRecord`` and the ``agent_calls`` row dict (ALP-873).

Mirrors the counterfactual_replays_codec pattern:

* ``encode_agent_call(record) -> dict[str, Any]`` — projects a record to a
  column-keyed dict for INSERT.
* ``decode_agent_call(row) -> AgentCallRecord`` — rehydrates a column-keyed
  dict (or ORM row attribute-access) back into the typed record.

``error_class`` encodes to its ``.value`` and decodes back to the enum member.
All other fields are primitive types (str, int, bool) that pass through
without transformation.

Round-trip property: ``decode_agent_call(encode_agent_call(r)) == r`` for
every valid ``AgentCallRecord``.
"""

from __future__ import annotations

from typing import Any

from alphamind.state.tables.agent_calls import AgentCallErrorClass, AgentCallRecord


def encode_agent_call(record: AgentCallRecord) -> dict[str, Any]:
    """Project an ``AgentCallRecord`` to a column-keyed dict for INSERT."""
    return {
        "agent_call_id": record.agent_call_id,
        "invocation_id": record.invocation_id,
        "agent_name": record.agent_name,
        "attempt_number": record.attempt_number,
        "model_id": record.model_id,
        "prompt_path": record.prompt_path,
        "prompt_git_sha": record.prompt_git_sha,
        "prompt_content_hash": record.prompt_content_hash,
        "sampling_params_json": record.sampling_params_json,
        "output_schema_ref": record.output_schema_ref,
        "tools_definition_ref": record.tools_definition_ref,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "cache_read_tokens": record.cache_read_tokens,
        "cache_write_tokens": record.cache_write_tokens,
        "wall_clock_ms": record.wall_clock_ms,
        "stop_reason": record.stop_reason,
        "success": record.success,
        "error_class": record.error_class.value if record.error_class is not None else None,
        "error_message": record.error_message,
        "output_artifact_ref": record.output_artifact_ref,
    }


def decode_agent_call(row: Any) -> AgentCallRecord:
    """Rehydrate a column-keyed dict or ORM row into an ``AgentCallRecord``.

    Accepts both a plain ``dict`` (from ``encode_agent_call``) and an ORM row
    (attribute access) so the same function serves both round-trip tests and
    the live read path.
    """
    get = row.__getitem__ if isinstance(row, dict) else lambda key: getattr(row, key)

    error_class_raw = get("error_class")

    return AgentCallRecord(
        agent_call_id=get("agent_call_id"),
        invocation_id=get("invocation_id"),
        agent_name=get("agent_name"),
        attempt_number=get("attempt_number"),
        model_id=get("model_id"),
        prompt_path=get("prompt_path"),
        prompt_git_sha=get("prompt_git_sha"),
        prompt_content_hash=get("prompt_content_hash"),
        sampling_params_json=get("sampling_params_json"),
        output_schema_ref=get("output_schema_ref"),
        tools_definition_ref=get("tools_definition_ref"),
        input_tokens=get("input_tokens"),
        output_tokens=get("output_tokens"),
        cache_read_tokens=get("cache_read_tokens"),
        cache_write_tokens=get("cache_write_tokens"),
        wall_clock_ms=get("wall_clock_ms"),
        stop_reason=get("stop_reason"),
        success=get("success"),
        error_class=(AgentCallErrorClass(error_class_raw) if error_class_raw is not None else None),
        error_message=get("error_message"),
        output_artifact_ref=get("output_artifact_ref"),
    )


__all__ = ["decode_agent_call", "encode_agent_call"]
