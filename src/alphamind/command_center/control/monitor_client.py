"""``MonitorClient`` — loopback HTTP client for the monitor ``/control`` surface.

Story 04a wires three verb proxies (``cancel_order`` /
``force_close_position`` / ``set_halt_mode``) over the monitor control
surface shipped in ALP-665. The seam mirrors :mod:`pipeline_client`:

* :class:`MonitorClient` Protocol with the three verbs.
* :class:`RealMonitorClient` — :class:`httpx.AsyncClient`-backed.
* :class:`FakeMonitorClient` — in-memory canned-response store.

Per the ALP-128 architectural invariants:

* **No direct cross-process imports.** No edge into
  :mod:`alphamind.execution.continuous_monitor`.
* **Pydantic at boundaries only.** Verb methods return frozen
  dataclasses; the boundary Pydantic shapes live in
  :mod:`alphamind.command_center.control.models`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
)
from alphamind.command_center.control._envelope import (
    extract_str_field,
    failure_from_transport_error,
    parse_error_envelope,
    safe_json,
)

__all__ = [
    "FakeMonitorClient",
    "MonitorClient",
    "MonitorForceClosePositionResult",
    "RealMonitorClient",
]

log = logging.getLogger(__name__)

_SOURCE_TAG = "monitor"


# ---------------------------------------------------------------------------
# Verb-specific result records.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonitorForceClosePositionResult:
    """Result of ``force_close_position`` — envelope + synthesized envelope id."""

    result: ControlResult
    envelope_id: str | None = None


# ---------------------------------------------------------------------------
# Protocol seam.
# ---------------------------------------------------------------------------


class MonitorClient(Protocol):
    """Async interface to the monitor ``/control/*`` surface."""

    async def cancel_order(self, *, order_id: str) -> ControlResult: ...

    async def force_close_position(
        self, *, position_id: str, rationale: str
    ) -> MonitorForceClosePositionResult: ...

    async def set_halt_mode(self, *, enabled: bool, reason: str) -> ControlResult: ...


# ---------------------------------------------------------------------------
# RealMonitorClient — httpx-backed.
# ---------------------------------------------------------------------------


class RealMonitorClient:
    """httpx-backed :class:`MonitorClient` for production.

    Shares the lifecycle convention of :class:`RealPipelineClient`: the
    composition root owns the :class:`httpx.AsyncClient` so connection
    pools are shared across verbs.
    """

    def __init__(self, *, base_url: str, http_client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    async def cancel_order(self, *, order_id: str) -> ControlResult:
        return await self._call_envelope_only(
            path="/control/cancel_order", body={"order_id": order_id}
        )

    async def force_close_position(
        self, *, position_id: str, rationale: str
    ) -> MonitorForceClosePositionResult:
        try:
            response = await self._http_client.post(
                self._url("/control/force_close_position"),
                json={"position_id": position_id, "rationale": rationale},
            )
        except httpx.RequestError as exc:
            return MonitorForceClosePositionResult(
                result=failure_from_transport_error(exc, source=_SOURCE_TAG)
            )
        if response.is_success:
            payload = safe_json(response)
            applied_at = extract_str_field(payload, "applied_at")
            envelope_id = extract_str_field(payload, "envelope_id")
            if applied_at is None or envelope_id is None:
                return MonitorForceClosePositionResult(
                    result=ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail=(
                            "upstream force_close_position response missing "
                            "applied_at or envelope_id"
                        ),
                    )
                )
            return MonitorForceClosePositionResult(
                result=ControlResult.success(applied_at=applied_at),
                envelope_id=envelope_id,
            )
        return MonitorForceClosePositionResult(
            result=parse_error_envelope(response, source=_SOURCE_TAG)
        )

    async def set_halt_mode(self, *, enabled: bool, reason: str) -> ControlResult:
        return await self._call_envelope_only(
            path="/control/set_halt_mode",
            body={"enabled": enabled, "reason": reason},
        )

    async def _call_envelope_only(
        self, *, path: str, body: dict[str, object]
    ) -> ControlResult:
        try:
            response = await self._http_client.post(self._url(path), json=body)
        except httpx.RequestError as exc:
            return failure_from_transport_error(exc, source=_SOURCE_TAG)
        if response.is_success:
            payload = safe_json(response)
            applied_at = extract_str_field(payload, "applied_at")
            if applied_at is None:
                return ControlResult.failure(
                    error_code=ControlErrorCode.INTERNAL_ERROR,
                    error_detail=f"upstream {path} response missing applied_at",
                )
            return ControlResult.success(applied_at=applied_at)
        return parse_error_envelope(response, source=_SOURCE_TAG)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._base_url + path


# ---------------------------------------------------------------------------
# FakeMonitorClient — in-memory canned responses.
# ---------------------------------------------------------------------------


class FakeMonitorClient:
    """In-memory :class:`MonitorClient` for unit tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._cancel_order_response: ControlResult = ControlResult.success(
            applied_at="2026-05-26T12:00:00Z"
        )
        self._force_close_response: MonitorForceClosePositionResult = (
            MonitorForceClosePositionResult(
                result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                envelope_id="MON.s1.42",
            )
        )
        self._set_halt_mode_response: ControlResult = ControlResult.success(
            applied_at="2026-05-26T12:00:00Z"
        )

    # ---- response setters --------------------------------------------------

    def set_cancel_order_response(self, response: ControlResult) -> None:
        self._cancel_order_response = response

    def set_force_close_response(self, response: MonitorForceClosePositionResult) -> None:
        self._force_close_response = response

    def set_set_halt_mode_response(self, response: ControlResult) -> None:
        self._set_halt_mode_response = response

    # ---- Protocol surface --------------------------------------------------

    async def cancel_order(self, *, order_id: str) -> ControlResult:
        self.calls.append(("cancel_order", {"order_id": order_id}))
        return self._cancel_order_response

    async def force_close_position(
        self, *, position_id: str, rationale: str
    ) -> MonitorForceClosePositionResult:
        self.calls.append(
            (
                "force_close_position",
                {"position_id": position_id, "rationale": rationale},
            )
        )
        return self._force_close_response

    async def set_halt_mode(self, *, enabled: bool, reason: str) -> ControlResult:
        self.calls.append(("set_halt_mode", {"enabled": enabled, "reason": reason}))
        return self._set_halt_mode_response
