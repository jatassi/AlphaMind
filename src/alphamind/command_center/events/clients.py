"""SSE event-stream clients for upstream pipeline + monitor (ALP-669, story 04b).

Two Protocols + httpx-backed impls + in-memory fakes:

* :class:`PipelineEventsClient` — Protocol the pipeline consumer task talks to.
* :class:`MonitorEventsClient` — Protocol the monitor consumer task talks to.
* :class:`HttpxPipelineEventsClient` / :class:`HttpxMonitorEventsClient` —
  ``httpx.AsyncClient``-backed production implementations targeting the
  loopback ``/events`` SSE surface on the corresponding upstream port.
* :class:`FakePipelineEventsClient` / :class:`FakeMonitorEventsClient` —
  in-memory implementations for tests; yield canned ``(event_name, payload)``
  tuples from a frozen list.

The Protocols are *nominally* distinct even though their method
signatures are identical. This is deliberate: nominal typing lets mypy
catch "plumbed the monitor client into the pipeline consumer" wiring
bugs at type-check time, which a single shared Protocol would miss.

Coordination with story 04a (`/api/control` proxy): 04a ships
``PipelineClient`` / ``MonitorClient`` Protocols for the *verb-proxy*
surface (RPC-style call/return). These are separate concerns from the
SSE-stream surface here — different I/O lifecycles, different timeout
regimes, different httpx idioms (``request`` vs ``stream``). The
orchestrator may decide at merge time to surface both under a single
"upstream services" Protocol-of-Protocols; that's strictly additive
and easier than splitting a combined Protocol after the fact.

Per ALP-128:
- **Protocols + in-memory fakes** for cross-process I/O.
- **No direct cross-process imports** — we never reach into
  ``scheduler/`` or ``execution/continuous_monitor/``. The upstream
  schema lives in the doc tree; the wire vocabulary lives in
  :mod:`alphamind.command_center._kernel.events`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

__all__ = [
    "FakeMonitorEventsClient",
    "FakePipelineEventsClient",
    "HttpxMonitorEventsClient",
    "HttpxPipelineEventsClient",
    "MonitorEventsClient",
    "PipelineEventsClient",
]

log = logging.getLogger(__name__)

# SSE frame separator: each event is one or more ``event: <name>`` /
# ``data: <json>`` lines terminated by a blank line. Per
# pipeline-control-and-events-schema.md § SSE framing, the producer
# guarantees one ``data:`` line per frame.
_FRAME_SEPARATOR = "\n\n"


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class PipelineEventsClient(Protocol):
    """Subscribes to the pipeline ``/events`` SSE stream.

    The consumer task opens ``stream()`` as an async-context manager and
    iterates the yielded ``AsyncIterator`` for ``(event_name, payload)``
    tuples. Connection drop / parse failure surfaces as an exception
    from the iterator; the consumer's reconnect-with-backoff loop
    catches it.
    """

    def stream(
        self,
    ) -> AbstractAsyncContextManager[AsyncIterator[tuple[str, Mapping[str, Any]]]]:
        """Open a fresh SSE subscription; yields parsed frames until close."""


@runtime_checkable
class MonitorEventsClient(Protocol):
    """Subscribes to the monitor ``/events`` SSE stream.

    Nominally distinct from :class:`PipelineEventsClient` so mypy
    catches "wrong client wired into wrong consumer" bugs at type-check
    time.
    """

    def stream(
        self,
    ) -> AbstractAsyncContextManager[AsyncIterator[tuple[str, Mapping[str, Any]]]]:
        """Open a fresh SSE subscription; yields parsed frames until close."""


# ---------------------------------------------------------------------------
# Frame parser (pure)
# ---------------------------------------------------------------------------


def _parse_sse_text(text: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Parse an SSE text stream into ``(event_name, data_dict)`` tuples.

    The parser handles the minimal SSE shape used by the upstream
    schema: one ``event: <name>`` line + one ``data: <json>`` line per
    frame, frames separated by blank lines. Malformed frames (missing
    event / data line, non-JSON ``data:`` content) are logged at WARNING
    and skipped — wire drift surfacing rather than crashing the
    consumer (per ALP-669 scope point 3).
    """
    for raw_frame in text.split(_FRAME_SEPARATOR):
        frame = raw_frame.strip("\r\n")
        if not frame:
            continue
        event_name: str | None = None
        data_text: str | None = None
        for line in frame.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_text = line[len("data:") :].strip()
        if event_name is None or data_text is None:
            log.warning("SSE frame missing event/data line; dropping (frame=%r)", frame[:200])
            continue
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError as exc:
            log.warning(
                "SSE frame data: line is not JSON; dropping (event=%s, err=%s)",
                event_name,
                exc,
            )
            continue
        if not isinstance(data, dict):
            log.warning(
                "SSE frame data: line did not decode to an object; dropping (event=%s, type=%s)",
                event_name,
                type(data).__name__,
            )
            continue
        yield event_name, data


# ---------------------------------------------------------------------------
# httpx-backed impls
# ---------------------------------------------------------------------------


class _HttpxSSEClientBase:
    """Shared httpx implementation for pipeline + monitor SSE clients.

    Holds the base URL and either (a) an ``httpx.AsyncClient`` injected by
    the composition root for production (shared connection pool across
    both upstreams — F10), or (b) an ``httpx.MockTransport`` injected by
    tests, in which case ``stream()`` mints a fresh per-call client off
    the mock transport.

    The ``stream()`` async-context manager opens ``client.stream("GET",
    ...)`` against ``<base_url>/events`` and yields the SSE frames as
    ``(name, dict)`` tuples.

    Connection drop / non-2xx response surfaces as an exception from
    the iterator — the consumer task uses the exception as its
    reconnect-with-backoff trigger.

    Lifetime: when an external ``http_client`` is passed in, this class
    DOES NOT close it on stream exit — the composition root owns the
    client's lifetime via its own ``aclose()`` in the lifespan. When
    only a ``transport`` is passed in (test path), a fresh client is
    minted per ``stream()`` call and closed when the context exits.
    """

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._transport = transport
        # Default timeout: no read timeout (SSE keeps the connection
        # open indefinitely); modest connect timeout so a wedged
        # upstream surfaces quickly. The consumer task's reconnect
        # backoff handles the cadence.
        self._timeout = timeout or httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)

    @asynccontextmanager
    async def stream(
        self,
    ) -> AsyncIterator[AsyncIterator[tuple[str, Mapping[str, Any]]]]:
        """Open the SSE stream; yield a parsed-frames iterator until close.

        If a shared ``http_client`` was passed at construction, reuse it
        for the request (F10) — the composition root owns its lifetime.
        Otherwise mint a fresh client off the constructed transport +
        timeout so the existing test surface (which builds clients with
        a ``MockTransport`` and no shared client) keeps working.
        """
        url = f"{self._base_url}/events"
        async with AsyncExitStack() as stack:
            if self._http_client is not None:
                client = self._http_client
            else:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
                )
            response = await stack.enter_async_context(client.stream("GET", url))
            response.raise_for_status()

            async def frames() -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    # Yield every complete frame; keep the partial tail
                    # in the buffer for the next chunk.
                    while _FRAME_SEPARATOR in buffer:
                        raw_frame, _, buffer = buffer.partition(_FRAME_SEPARATOR)
                        for name, data in _parse_sse_text(raw_frame + _FRAME_SEPARATOR):
                            yield name, data
                # Drain any trailing data after stream close (rare, but
                # possible if the upstream terminates without a final
                # blank line).
                if buffer.strip():
                    for name, data in _parse_sse_text(buffer):
                        yield name, data

            yield frames()


class HttpxPipelineEventsClient(_HttpxSSEClientBase):
    """Production :class:`PipelineEventsClient` against the loopback pipeline."""


class HttpxMonitorEventsClient(_HttpxSSEClientBase):
    """Production :class:`MonitorEventsClient` against the loopback monitor."""


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeEventsClient:
    """Yields canned frames from a frozen list.

    Tests construct one of these and inject it where the consumer task
    expects a Protocol-shaped client. The fake exists to make consumer-
    task tests deterministic without booting a real loopback SSE
    surface.
    """

    frames: Sequence[tuple[str, Mapping[str, Any]]]
    # On_connect callback fires every time stream() is opened; tests use
    # it to count reconnect attempts.
    on_connect: list[int] = field(default_factory=list)

    @asynccontextmanager
    async def stream(
        self,
    ) -> AsyncIterator[AsyncIterator[tuple[str, Mapping[str, Any]]]]:
        self.on_connect.append(1)

        async def gen() -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
            for name, data in self.frames:
                yield name, dict(data)

        yield gen()


class FakePipelineEventsClient(_FakeEventsClient):
    """In-memory :class:`PipelineEventsClient` for tests."""


class FakeMonitorEventsClient(_FakeEventsClient):
    """In-memory :class:`MonitorEventsClient` for tests."""
