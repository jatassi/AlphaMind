"""FastAPI app composition + Uvicorn bind helper for the monitor /control surface (ALP-665).

:func:`build_app` constructs a FastAPI application with the routes from
``routes.py`` mounted, plus the validation-error handler that maps Pydantic
422s to the schema's 400 envelope.

:func:`build_uvicorn_config` builds a :class:`uvicorn.Config` bound to
``127.0.0.1:{port}`` — loopback isolation is the trust boundary per the
parent issue's pre-resolved decision (B).

The Uvicorn server task lifecycle is owned by the supervisor's
:class:`asyncio.TaskGroup` via the closure :func:`make_control_surface_task`
returns — bare ``asyncio.create_task`` is forbidden by the parent issue's
TaskGroup-discipline invariant.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.control.routes import (
    ControlSurfaceDependencies,
    build_router,
    install_validation_error_handler,
)
from alphamind.execution.continuous_monitor.session import MonitorSession

# Re-exported for callers who only import ``app``.
__all__ = [
    "ControlSurfaceDependencies",
    "build_app",
    "build_uvicorn_config",
    "make_control_surface_task",
]

log = logging.getLogger(__name__)


def build_app(
    deps: ControlSurfaceDependencies,
    *,
    heartbeat_interval_seconds: float = 15.0,
) -> FastAPI:
    """Construct the FastAPI app with /control + /events mounted."""
    app = FastAPI(
        title="AlphaMind continuous monitor — control surface",
        description=(
            "Loopback-bound HTTP surface for the continuous monitor process. "
            "Three POST /control/* verbs (cancel_order, force_close_position, "
            "set_halt_mode) plus one GET /events SSE stream. See "
            "docs/design/monitor-control-and-events-schema.md."
        ),
        docs_url=None,  # diagnostic surface is loopback-only; no Swagger UI exposure
        redoc_url=None,
        openapi_url=None,
    )
    install_validation_error_handler(app)
    app.include_router(
        build_router(deps, heartbeat_interval_seconds=heartbeat_interval_seconds)
    )
    return app


def build_uvicorn_config(*, app: FastAPI, port: int) -> uvicorn.Config:
    """Build the Uvicorn :class:`Config` bound to ``127.0.0.1:{port}``.

    Loopback isolation is the trust boundary per the parent issue. Logging
    is left to the existing ``configure_monitor_logging()`` setup — Uvicorn's
    access log is disabled because every legitimate request comes from the
    command-center backend (single client; its own audit log captures the
    call).
    """
    return uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        # Lifespan events flow through FastAPI's default lifespan support.
        lifespan="on",
    )


def make_control_surface_task(
    *,
    deps: ControlSurfaceDependencies,
    port: int,
    heartbeat_interval_seconds: float = 15.0,
) -> Callable[[MonitorSession, ContinuousMonitorConfig], Awaitable[None]]:
    """Return the supervisor-compatible task that runs the Uvicorn server.

    The supervisor's :class:`asyncio.TaskGroup` owns the resulting coroutine.
    On supervisor shutdown (``CancelledError``), Uvicorn's
    :meth:`Server.shutdown` runs in the ``finally`` block so the bound port
    releases cleanly.
    """

    async def _task(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        del session, config  # signature carries them for symmetry; not consumed here
        app = build_app(deps, heartbeat_interval_seconds=heartbeat_interval_seconds)
        ucfg = build_uvicorn_config(app=app, port=port)
        server = uvicorn.Server(config=ucfg)
        log.info("control surface server starting on 127.0.0.1:%d", port)
        try:
            await server.serve()
        finally:
            # Server.serve() returns when the cancel handler fires; the
            # finally block runs on the supervisor's cascade-cancel path.
            log.info("control surface server stopped")

    return _task
