"""JSON-payload coercion for strategist agent — ALP-305.

Post-migration the SDK delivers the strategist output as a dict on
``ResultMessage.structured_output`` (``output_format = {"type": "json_schema",
"schema": ...}`` mode); this module coerces that dict to a typed
:class:`StrategistOutput`.

The parser reduces to: inject the canonical ``invocation_id``, then
``StrategistOutput.model_validate(payload)``. Any
:class:`pydantic.ValidationError` is wrapped as :class:`ParseError` so the
harness's corrective-retry message construction sees the same shape it does
for the qualitative and adaptive agents.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from alphamind.decision.strategist.models import StrategistOutput

__all__ = ["ParseError", "parse_strategist_output"]


class ParseError(Exception):
    """Raised when the structured payload cannot be coerced to a :class:`StrategistOutput`.

    Mirrors the qualitative-research and analyst ``ParseError`` shape so the
    harness's corrective-retry message construction sees a uniform error shape.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


def parse_strategist_output(payload: Any, *, invocation_id: str) -> StrategistOutput:
    """Coerce *payload* (the SDK's ``structured_output``) to a :class:`StrategistOutput`.

    Parameters
    ----------
    payload
        The dict returned by ``ResultMessage.structured_output``. Shape is
        API-enforced against ``StrategistOutput.model_json_schema()``; only
        Layer-2 invariants in Pydantic remain to fail.
    invocation_id
        Canonical invocation identifier supplied by the harness. Always wins
        over the value (if any) in *payload*.

    Raises
    ------
    ParseError
        If *payload* is ``None``, not a dict, fails Pydantic validation, or
        violates a model invariant.
    """
    if payload is None:
        raise ParseError(
            field_path="envelope",
            message="ResultMessage.structured_output was not populated",
        )
    if not isinstance(payload, dict):
        raise ParseError(
            field_path="envelope",
            message=f"expected dict payload, got {type(payload).__name__}",
        )

    normalized = dict(payload)
    normalized["invocation_id"] = invocation_id

    try:
        return StrategistOutput.model_validate(normalized)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = first["loc"]
            field_path = ".".join(str(p) for p in loc) or "payload"
            message = first["msg"]
        else:
            field_path = "payload"
            message = str(exc)
        raise ParseError(field_path=field_path, message=message) from exc
