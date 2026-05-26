"""Shared error-envelope + transport-error parsing helpers.

The pipeline + monitor schemas emit the same error-envelope shape
(``{"error": {"code", "detail", "details?"}}``) and the same wrapping
convention (FastAPI's ``HTTPException(detail=envelope)`` nests the
envelope under ``detail``). Both client implementations consume these
shared helpers so a future schema change updates one parser, not two.

Module-private to the ``command_center.control`` subpackage (single-
underscore prefix); not re-exported from the subpackage's ``__init__``.
"""

from __future__ import annotations

import httpx

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
)

__all__ = [
    "extract_str_field",
    "failure_from_transport_error",
    "parse_error_envelope",
    "safe_json",
]


_STATUS_TO_DEFAULT_CODE: dict[int, ControlErrorCode] = {
    400: ControlErrorCode.VALIDATION_FAILED,
    404: ControlErrorCode.NOT_FOUND,
    409: ControlErrorCode.PRECONDITION_FAILED,
    502: ControlErrorCode.BROKER_ERROR,
    500: ControlErrorCode.INTERNAL_ERROR,
}
"""Fallback HTTP status → ``ControlErrorCode`` when the envelope lacks a code."""


def parse_error_envelope(response: httpx.Response, *, source: str) -> ControlResult:
    """Parse an upstream error envelope into a typed :class:`ControlResult`.

    Both the pipeline and monitor schemas emit
    ``{"error": {"code": ..., "detail": ..., "details": ...}}`` on the
    4xx / 5xx paths. The pipeline routes wrap the envelope under
    ``detail`` (FastAPI's :class:`HTTPException` pathway); the monitor
    routes return the envelope directly. The function handles both
    shapes so the client doesn't have to know which upstream it's
    talking to.

    ``source`` is the caller-provided tag (``"pipeline"`` /
    ``"monitor"``) used only in the fallback error detail when the
    envelope is malformed.
    """
    detail_status = response.status_code
    try:
        body = response.json()
    except ValueError:
        code = _STATUS_TO_DEFAULT_CODE.get(detail_status, ControlErrorCode.INTERNAL_ERROR)
        return ControlResult.failure(
            error_code=code,
            error_detail=(f"upstream {source} returned HTTP {detail_status} with non-JSON body"),
        )
    error_obj = _extract_error_object(body)
    if error_obj is None:
        code = _STATUS_TO_DEFAULT_CODE.get(detail_status, ControlErrorCode.INTERNAL_ERROR)
        return ControlResult.failure(
            error_code=code,
            error_detail=(
                f"upstream {source} returned HTTP {detail_status} without an error envelope"
            ),
        )
    raw_code = error_obj.get("code")
    raw_detail = error_obj.get("detail") or f"HTTP {detail_status}"
    parsed_code: ControlErrorCode | None = None
    if isinstance(raw_code, str):
        try:
            parsed_code = ControlErrorCode(raw_code)
        except ValueError:
            parsed_code = None
    if parsed_code is None:
        parsed_code = _STATUS_TO_DEFAULT_CODE.get(detail_status, ControlErrorCode.INTERNAL_ERROR)
    return ControlResult.failure(error_code=parsed_code, error_detail=str(raw_detail))


def _extract_error_object(body: object) -> dict[str, object] | None:
    """Pull the ``{code, detail, details?}`` block out of either wire shape."""
    if not isinstance(body, dict):
        return None
    direct = body.get("error")
    if isinstance(direct, dict):
        return dict(direct)
    # FastAPI wraps HTTPException(detail=...) under "detail".
    inner = body.get("detail")
    if isinstance(inner, dict):
        nested = inner.get("error")
        if isinstance(nested, dict):
            return dict(nested)
    return None


def failure_from_transport_error(exc: Exception, *, source: str) -> ControlResult:
    """Map a transport-layer exception to ``ControlResult.failure``.

    Used for :class:`httpx.RequestError` subclasses (timeouts, connect
    errors, etc.). The browser sees this as ``internal_error`` because
    the upstream surface didn't get to render its own envelope.
    """
    return ControlResult.failure(
        error_code=ControlErrorCode.INTERNAL_ERROR,
        error_detail=f"upstream {source} transport error: {exc}",
    )


def safe_json(response: httpx.Response) -> object:
    """Return the JSON body or an empty dict if the body is non-JSON."""
    try:
        return response.json()
    except ValueError:
        return {}


def extract_str_field(payload: object, key: str) -> str | None:
    """Pull a string field off a JSON payload, returning ``None`` on absence."""
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) else None
