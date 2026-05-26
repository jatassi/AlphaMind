"""FastAPI app composition + Uvicorn supervisor task (ALP-664).

Exposes:

* :class:`VerbDispatch` — composition record holding the verb-layer
  dependencies (scheduler + emergency + universe-validator Protocols +
  config_dir + now_factory) the route handlers consume.
* :func:`build_app` — constructs the FastAPI instance, mounts the
  :mod:`alphamind.scheduler.control.routes` router, and registers the
  lifespan ctxmgr that wires the :class:`SSEEventEmitter` onto
  ``app.state``.
* :func:`run_uvicorn_server_task` — coroutine factory registered on
  :class:`PipelineSupervisor` as the third task alongside
  ``apscheduler`` and ``emergency_receiver``.  Binds Uvicorn to
  ``127.0.0.1:control_port`` and runs until cancellation.

Per the ALP-128 architectural invariants:

* **TaskGroup discipline.** The Uvicorn server task is owned by the
  pipeline supervisor's outer ``asyncio.TaskGroup`` — this module never
  calls bare ``asyncio.create_task``.  Cancellation propagates through
  ``Server.should_exit = True`` so Uvicorn drains in-flight connections
  before the task returns.
* **No direct cross-process imports.** Nothing in this module imports
  from ``command_center/`` or ``execution/continuous_monitor/``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from alphamind.scheduler.control import events, routes, verbs

__all__ = ["VerbDispatch", "build_app", "run_uvicorn_server_task"]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verb-layer composition.
# ---------------------------------------------------------------------------


@dataclass
class VerbDispatch:
    """Per-process bundle of verb-layer dependencies.

    Held on :attr:`FastAPI.state.verb_dispatch` so route handlers can
    pull it out via :class:`fastapi.Request`.  The fields are mutable
    in principle — :attr:`scheduler` / :attr:`emergency` / :attr:`universe_validator`
    Protocols may carry internal state — but the dispatch itself is
    constructed once at lifespan and never reassigned.

    The dataclass is intentionally NOT frozen because the concrete
    Protocol implementations may mutate (e.g. the APScheduler-backed
    :class:`SchedulerControl` flips a paused flag); freezing here
    would not propagate immutability and would constrain callers to
    no benefit.
    """

    scheduler: verbs.SchedulerControl
    emergency: verbs.EmergencyTrigger
    universe_validator: verbs.UniverseValidator
    config_dir: Path | None
    now_factory: Callable[[], datetime]


# ---------------------------------------------------------------------------
# Lifespan.
# ---------------------------------------------------------------------------


def _make_lifespan() -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the lifespan ctxmgr that logs surface startup / shutdown.

    The emitter + dispatch are attached to ``app.state`` at
    :func:`build_app` time (synchronous, no asyncio dependency) so
    routes can read them off ``request.app.state`` before the lifespan
    yield point — important for tests using ``TestClient`` and for the
    asgi route resolution path generally.  The lifespan itself owns
    only logging + the start/shutdown sequencing.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        log.info("pipeline /control + /events surface starting")
        try:
            yield
        finally:
            log.info("pipeline /control + /events surface stopping")

    return lifespan


# ---------------------------------------------------------------------------
# App factory.
# ---------------------------------------------------------------------------


def build_app(
    *,
    emitter: events.SSEEventEmitter,
    dispatch: VerbDispatch,
) -> FastAPI:
    """Construct the FastAPI instance and mount the control + events router.

    The same :class:`SSEEventEmitter` instance the supervisor wires
    into the orchestrator's ``ProgressEmitter`` injection is passed
    here so the SSE route observes the events the orchestrator emits.
    """
    app = FastAPI(
        title="AlphaMind pipeline control + events",
        description=(
            "Loopback-bound HTTP surface exposing /control verbs and the "
            "/events SSE stream defined in "
            "docs/design/pipeline-control-and-events-schema.md."
        ),
        lifespan=_make_lifespan(),
    )
    # Attach the shared emitter + dispatch at construction time so
    # request handlers can read them off ``request.app.state`` without
    # depending on the lifespan having yielded yet.
    app.state.event_emitter = emitter
    app.state.verb_dispatch = dispatch
    app.include_router(routes.router)
    return app


# ---------------------------------------------------------------------------
# Uvicorn supervisor task.
# ---------------------------------------------------------------------------


async def run_uvicorn_server_task(*, app: FastAPI, host: str, port: int) -> None:
    """Run Uvicorn against ``app`` until cancellation.

    Designed to be registered on the :class:`PipelineSupervisor` as
    a third coroutine factory alongside ``apscheduler`` and
    ``emergency_receiver``.  The supervisor's outer TaskGroup owns the
    cancellation; on cancel the function sets ``server.should_exit =
    True`` BEFORE re-raising so Uvicorn drains in-flight connections as
    the await unwinds.

    Per the schema's § Scope, ``host`` MUST be a loopback address
    (``127.0.0.1``) in production; the parameter is left explicit so
    tests can bind to ephemeral ports without changing this signature.
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,  # inherit the daemon's logging config
        access_log=False,  # operator console traffic is low-volume; skip noise
    )
    server = uvicorn.Server(config=config)
    # Uvicorn's ``Server.serve()`` is itself a coroutine; awaiting directly
    # keeps this task within its parent TaskGroup's discipline (no bare
    # ``asyncio.create_task`` — see module docstring). On cancellation, the
    # ``BaseException``/``CancelledError`` is delivered to the in-flight
    # ``await``; we catch it, set ``should_exit = True`` so Uvicorn's
    # ``capture_signals`` exit + ``shutdown`` sequence observe the drain
    # request, then re-raise so the supervisor sees the task finish.
    try:
        await server.serve()
    except BaseException:
        server.should_exit = True
        raise
