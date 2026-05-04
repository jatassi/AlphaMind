"""JSON-payload coercion for domain-researcher agents — ALP-288 (D).

Post-migration the SDK delivers each sector brief as a dict on
``ResultMessage.structured_output`` (``output_format = {"type": "json_schema",
"schema": ...}`` mode); this module replaces the legacy text-format parser.

The parser reduces to: inject the canonical ``invocation_id`` and ``sector``
(both override whatever the model emits), then
``SectorBrief.model_validate(payload)``. Any :class:`pydantic.ValidationError`
is wrapped as :class:`ParseError` so the harness's corrective-retry message
construction sees the same shape it does for the adaptive and qualitative
agents.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.models import SectorBrief

__all__ = ["ParseError", "parse_brief"]


class ParseError(Exception):
    """Raised when the structured payload cannot be coerced to a :class:`SectorBrief`.

    Mirrors the adaptive-researcher and qualitative-researcher ``ParseError``
    shape so the harness's corrective-retry message construction sees a
    uniform error shape across all three analysis-layer agents.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


def parse_brief(payload: Any, sector: Sector, *, invocation_id: str = "") -> SectorBrief:
    """Coerce *payload* (the SDK's ``structured_output``) to a :class:`SectorBrief`.

    Parameters
    ----------
    payload:
        The dict returned by ``ResultMessage.structured_output``. Shape is
        API-enforced against ``SectorBrief.model_json_schema()`` (with the
        conditional-field tightener applied); only Layer-2/3 invariants in
        Pydantic remain to fail.
    sector:
        Canonical sector for this invocation. Always wins over the value
        the model emits — the validator's reference-prefix check confirms
        downstream that the brief's reference IDs match this sector.
    invocation_id:
        Optional canonical invocation identifier. When non-empty, overrides
        the ``invocation_id`` the model emits. Defaults to ``""`` for
        backwards compatibility with callers that already trust the model's
        emitted id.

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
    normalized["sector"] = sector.value
    if invocation_id:
        normalized["invocation_id"] = invocation_id

    try:
        return SectorBrief.model_validate(normalized)
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
