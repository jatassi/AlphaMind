"""FastAPI app composition for the command center (story 02 / ALP-666).

This is the composition root for the command-center FastAPI app —
the only module that wires routers, lifespan, and dependencies. Every
downstream story includes its routers here via ``app.include_router(...)``;
this story ships an empty router list with documented include points
for the future stories:

* Story 03 (WebAuthn + sessions + CSRF) — ``app.include_router(auth_router)``.
* Story 04a (``/api/control/*`` proxy + audit) —
  ``app.include_router(control_router, prefix="/api/control")``.
* Story 04b (``/api/events`` SSE multiplexer) —
  ``app.include_router(events_router, prefix="/api")``.
* Story 04c (frontend bundling / static mount) —
  ``app.mount("/", StaticFiles(...))`` (mounted last so ``/api/*``
  takes precedence).
* Story 05a (alerts) — ``app.include_router(alerts_router, prefix="/api/alerts")``.
* View stories (05b-05j, 06a-06c) — included under ``/api/views/...``.

This story ships only the ``/healthz`` route — a trivial liveness probe
the operator script + NSSM service-restart logic poll against.

Per the parent-issue architectural invariants:

* **Pydantic at boundaries only.** Future FastAPI request / response
  models live next to their respective routers (story 03 / 04a / 04b
  / 05a). Internal logic operates on the frozen-dataclass mirrors
  defined in :mod:`alphamind.command_center._kernel` and
  :mod:`alphamind.command_center.persistence.codecs`.
* **TaskGroup discipline.** The FastAPI lifespan does NOT spawn its
  own background tasks. The Uvicorn server is the supervisor's first
  task (see :mod:`alphamind.command_center.__main__`); future
  long-running tasks (SSE consumers, alert engine) register on the
  same supervisor's outer ``asyncio.TaskGroup``.
* **No direct cross-process imports.** This module imports nothing
  from ``alphamind.scheduler`` or
  ``alphamind.execution.continuous_monitor`` — all cross-process
  communication routes through the loopback HTTP surfaces (stories
  01b + 01c).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center.config import (
    AlertsConfig,
    CommandCenterConfig,
    SecurityConfig,
)
from alphamind.command_center.persistence.session import (
    build_cc_writer_session_factory,
    build_foreign_reader_session_factory,
)

__all__ = ["build_app"]

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — wires session factories onto ``app.state``.

    The cc_writer + foreign_reader factories are constructed once at
    daemon startup; downstream route handlers pull them off
    ``request.app.state`` rather than re-constructing per request (a
    fresh factory means a fresh engine + fresh connection pool — that
    would defeat WAL-mode reads sharing the same connection pool).

    The third factory exposed on ``app.state`` is the
    ``production_session_factory`` — the
    :data:`alphamind.persistence.models.Base`-backed async factory the
    composition root threads in via :func:`build_app` (constructed under
    :func:`alphamind.persistence.session.engine_pair_context` in
    ``__main__``). It serves the
    :func:`alphamind.command_center._kernel.operator_invocation.operator_invocation`
    helper, which writes :class:`InvocationRow` (on the production
    ``Base``, not on :class:`CommandCenterBase`) and would be rejected by
    the cc writer's ``before_flush`` guard. The production factory's
    engine lifetime is owned by the composition root's
    ``engine_pair_context`` context manager; this lifespan only holds a
    reference — it does NOT dispose the production engine on shutdown.

    On shutdown, the lifespan disposes the cc_writer + foreign_reader
    engines (constructed inside the lifespan, so it owns their lifetime)
    so the SQLite file handles release cleanly — important on Windows
    where a lingering handle blocks process restart. Each ``dispose()``
    runs inside its own ``try/except`` so a failure on one engine still
    permits disposal of the others (F6).
    """
    db_path = app.state.command_center_config.db.alphamind_db_path
    cc_writer = build_cc_writer_session_factory(db_path)
    foreign_reader = build_foreign_reader_session_factory(db_path)
    app.state.cc_writer_session_factory = cc_writer
    app.state.foreign_reader_session_factory = foreign_reader
    try:
        yield
    finally:
        # Dispose engines so SQLite file handles release cleanly. Each
        # dispose runs in its own try/except so a failure on one does
        # not skip the rest (F6). The production engine is NOT disposed
        # here — its lifetime is owned by the composition root's
        # engine_pair_context.
        for engine_name, engine in (
            ("cc_writer", cc_writer.kw["bind"]),
            ("foreign_reader", foreign_reader.kw["bind"]),
        ):
            try:
                await engine.dispose()
            except Exception as exc:
                log.warning(
                    "command_center lifespan: %s engine dispose failed: %s",
                    engine_name,
                    exc,
                )


def build_app(
    *,
    command_center_config: CommandCenterConfig,
    security_config: SecurityConfig,
    alerts_config: AlertsConfig,
    production_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """Construct the FastAPI app composition root.

    The three loaded configs are stashed on ``app.state`` so downstream
    routers can pull them off without re-loading the YAML. Downstream
    stories include their routers via ``app.include_router(...)``; this
    story ships only the ``/healthz`` route.

    Parameters
    ----------
    command_center_config:
        Loaded :class:`CommandCenterConfig` — drives the lifespan's
        engine construction (DB path).
    security_config:
        Loaded :class:`SecurityConfig` — drives the future auth router's
        cookie + WebAuthn settings (story 03).
    alerts_config:
        Loaded :class:`AlertsConfig` — drives the future alert engine's
        rule list (story 05a).
    production_session_factory:
        The :data:`alphamind.persistence.models.Base`-backed async
        session factory the composition root builds via
        :func:`alphamind.persistence.session.engine_pair_context`. Stashed
        on ``app.state.production_session_factory`` so the
        :func:`alphamind.command_center._kernel.operator_invocation.operator_invocation`
        helper (used by story 04a's control proxy to bridge an operator
        action into an :class:`InvocationRow`) can reach it without
        re-opening an engine. Optional in tests that do not exercise the
        operator-action bridge; production callers (``__main__``) always
        thread it in. The lifespan does NOT own the engine's lifetime;
        ``engine_pair_context`` in the composition root does.
    """
    app = FastAPI(
        title="AlphaMind command center",
        description=(
            "Operator-facing web application running on the trading machine. "
            "Backend composition root — see "
            "docs/design/command-center.md for the full design surface."
        ),
        lifespan=_lifespan,
    )
    app.state.command_center_config = command_center_config
    app.state.security_config = security_config
    app.state.alerts_config = alerts_config
    app.state.production_session_factory = production_session_factory

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Trivial liveness probe.

        Returns ``{"status": "ok"}`` while the app is serving. Operator
        scripts + NSSM service-restart logic poll this endpoint to
        confirm the daemon is up. No DB / engine traffic — a hung
        engine should still let ``/healthz`` respond so the operator
        can distinguish "daemon dead" from "DB stuck".
        """
        return {"status": "ok"}

    return app
