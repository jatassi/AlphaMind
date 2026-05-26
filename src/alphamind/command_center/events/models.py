"""Browser-facing event envelope for the SSE multiplexer (ALP-669, story 04b).

This is the typed boundary between the multiplexer's internal frozen-
dataclass events (:mod:`alphamind.command_center._kernel.events`) and
the JSON the browser's ``EventSource`` consumes. Per ALP-128's
"Pydantic at boundaries only" invariant, the envelope is Pydantic;
internal multiplexer state stays on the dataclasses.

The envelope carries three fields:

* ``source`` — ``"pipeline"`` or ``"monitor"``, identifying which
  upstream produced the record. The SSE ``event:`` field on the wire
  is the dual form ``<source>:<event>`` (e.g. ``pipeline:invocation_started``)
  so the browser can disambiguate pipeline ``heartbeat`` from monitor
  ``heartbeat`` without inspecting the data payload.
* ``event`` — the upstream event-type name (e.g. ``"invocation_started"``,
  ``"fill_received"``). Free-string at this boundary because the
  consumer task has already parsed and validated the name against the
  matching :class:`alphamind.command_center._kernel.events.PipelineEventType`
  / :class:`MonitorEventType` enum before constructing the envelope —
  by the time the envelope serializes, the name is known-valid.
* ``data`` — the raw upstream payload, round-tripped unmodified per
  ALP-669 AC. Untyped at this boundary because the per-event-payload
  schemas live upstream of the multiplexer (in the schema docs) and
  every value already validated as JSON when the upstream consumer
  parsed the SSE frame.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["BrowserEventEnvelope"]


class BrowserEventEnvelope(BaseModel):
    """Pydantic envelope serialized into the SSE ``data:`` line.

    The browser's ``EventSource`` callback receives this as the
    parsed ``data`` payload after stripping the SSE framing. The
    ``extra="forbid"`` config prevents a multiplexer bug from silently
    adding a field the browser ignores — the wire shape is locked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["pipeline", "monitor"]
    event: str
    data: dict[str, Any]
