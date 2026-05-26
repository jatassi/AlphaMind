"""FastAPI app composition for the command center (story 02 / ALP-666).

This is the composition root for the command-center FastAPI app —
the only module that wires routers, lifespan, and dependencies. Every
downstream story includes its routers here via ``app.include_router(...)``;
story 03 wires the auth router, with documented include points for the
remaining stories:

* Story 03 (WebAuthn + sessions + CSRF) — auth router included here.
* Story 04a (``/api/control/*`` proxy + audit) —
  ``app.include_router(control_router, prefix="/api/control")``.
* Story 04b (``/api/events`` SSE multiplexer) —
  ``app.include_router(events_router, prefix="/api")``.
* Story 04c (frontend bundling / static mount) —
  ``app.mount("/", StaticFiles(...))`` (mounted last so ``/api/*``
  takes precedence).
* Story 05a (alerts) — ``app.include_router(alerts_router, prefix="/api/alerts")``.
* View stories (05b-05j, 06a-06c) — included under ``/api/views/...``.

This story ships ``/healthz`` (a trivial liveness probe) and the
``/auth/*`` router with five endpoints (register begin/complete, login
begin/complete, logout).

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
import os
import secrets
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center.auth.routes import build_auth_router
from alphamind.command_center.auth.setup_token import SetupTokenGate
from alphamind.command_center.auth.webauthn import (
    RealWebauthnVerifier,
    WebauthnVerifier,
)
from alphamind.command_center.config import (
    AlertsConfig,
    CommandCenterConfig,
    SecurityConfig,
)
from alphamind.command_center.events.clients import (
    HttpxMonitorEventsClient,
    HttpxPipelineEventsClient,
    MonitorEventsClient,
    PipelineEventsClient,
)
from alphamind.command_center.events.multiplexer import (
    EventMultiplexer,
    monitor_consumer_task,
    pipeline_consumer_task,
)
from alphamind.command_center.events.routes import build_events_router
from alphamind.command_center.persistence.session import (
    build_cc_writer_session_factory,
    build_foreign_reader_session_factory,
)
from alphamind.command_center.session import ProcessSession

__all__ = ["AuthOverrides", "EventsOverrides", "build_app"]


@dataclass(frozen=True, slots=True)
class EventsOverrides:
    """Test-time / deploy-time overrides for the events subsystem (story 04b).

    Bundles the optional events collaborators so ``build_app`` keeps a
    small parameter list. Production callers leave every field at its
    default; tests inject in-memory fakes so the consumer tasks can
    yield canned event sequences without booting real loopback SSE
    surfaces.

    Fields:

    * ``pipeline_events_client``: override for the pipeline ``/events``
      upstream client. Defaults to :class:`HttpxPipelineEventsClient`
      constructed against ``command_center_config.pipeline.events_url``.
    * ``monitor_events_client``: override for the monitor ``/events``
      upstream client. Defaults to :class:`HttpxMonitorEventsClient`
      constructed against ``command_center_config.monitor.events_url``.
    """

    pipeline_events_client: PipelineEventsClient | None = None
    monitor_events_client: MonitorEventsClient | None = None


@dataclass(frozen=True, slots=True)
class AuthOverrides:
    """Test-time / deploy-time overrides for the auth surface (story 03).

    Bundles the optional auth collaborators so ``build_app`` keeps a
    small parameter list. Production callers leave every field at its
    default; tests pass ``InMemoryWebauthnVerifier`` + a fixed signing
    secret + a frozen clock to make the auth flow deterministic.

    Fields:

    * ``webauthn_verifier``: override for the WebAuthn relying-party
      surface. Defaults to :class:`RealWebauthnVerifier` constructed
      from :class:`SecurityConfig.webauthn`.
    * ``setup_token_gate``: override for the first-launch enrollment
      gate. Defaults to a fresh :class:`SetupTokenGate`.
    * ``session_signing_secret``: override for the session-cookie
      signing secret. Defaults to
      ``os.environ[COMMAND_CENTER_SESSION_SECRET]`` decoded as UTF-8
      bytes, or a fresh random secret if the env var is absent.
    * ``cookies_secure``: set the ``Secure`` flag on issued cookies.
      Defaults to ``False`` for v1 loopback (HTTPS-only flag).
    * ``clock``: optional callable returning the current UTC datetime.
      Used by auth dependencies for session-expiry checks. Defaults to
      ``lambda: datetime.now(UTC)``.
    """

    webauthn_verifier: WebauthnVerifier | None = None
    setup_token_gate: SetupTokenGate | None = None
    session_signing_secret: bytes | None = None
    cookies_secure: bool = False
    clock: Callable[[], datetime] | None = field(default=None)


_SESSION_SECRET_ENV = "COMMAND_CENTER_SESSION_SECRET"
"""Env var carrying the session-cookie signing secret.

Loaded once at lifespan startup so the per-process secret is stable
for the daemon's lifetime. Production daemons set this in ``.env`` /
NSSM service env vars; tests pass an explicit secret via the
``session_signing_secret_override`` ``build_app`` parameter.

Absent the env var, the daemon mints a fresh random secret per boot —
acceptable for the loopback-only v1 deployment (operators re-authenticate
after each restart, which is consistent with the design's "no remember me
beyond the session lifetime"). The env-var pin is recommended once the
remote-access follow-on lands so a restart doesn't bounce every active
session.
"""

log = logging.getLogger(__name__)


def _resolve_session_secret(overrides: AuthOverrides) -> bytes:
    """Resolve the session-cookie signing secret.

    Precedence: explicit override → ``COMMAND_CENTER_SESSION_SECRET``
    env var → fresh random per-boot. The third path logs a warning
    because operators who want sessions to survive restarts must set
    the env var.
    """
    if overrides.session_signing_secret is not None:
        return overrides.session_signing_secret
    env_secret = os.environ.get(_SESSION_SECRET_ENV)
    if env_secret:
        return env_secret.encode("utf-8")
    log.warning(
        "command_center: %s not set; minted fresh per-boot session secret. "
        "Active sessions will not survive a daemon restart. Set the env "
        "var in production to persist sessions across restarts.",
        _SESSION_SECRET_ENV,
    )
    return secrets.token_bytes(32)


def _resolve_webauthn_verifier(
    overrides: AuthOverrides,
    *,
    command_center_config: CommandCenterConfig,
    security_config: SecurityConfig,
) -> WebauthnVerifier:
    """Resolve the WebAuthn relying-party verifier.

    Falls back to :class:`RealWebauthnVerifier` constructed against the
    loopback origin (host:port from the cc config + RP id from
    security config). The remote-access follow-on switches the origin
    via :class:`SecurityConfig.webauthn` edits.
    """
    if overrides.webauthn_verifier is not None:
        return overrides.webauthn_verifier
    port = command_center_config.bind.port
    expected_origin = f"http://{security_config.webauthn.relying_party_id}:{port}"
    return RealWebauthnVerifier(
        relying_party_id=security_config.webauthn.relying_party_id,
        relying_party_name=security_config.webauthn.relying_party_name,
        expected_origin=expected_origin,
    )


def _make_pipeline_consumer_factory(
    *,
    client: PipelineEventsClient,
    multiplexer: EventMultiplexer,
) -> Callable[[ProcessSession], Coroutine[Any, Any, None]]:
    """Bind the pipeline consumer task to the live client + multiplexer.

    Returns a factory the supervisor calls with its ``ProcessSession``;
    the session is unused by the consumer (the supervisor's signature is
    ``Callable[[ProcessSession], Coroutine[None]]`` and we honor it
    without dragging session-bound state into the SSE consumer).
    """

    async def factory(_session: ProcessSession) -> None:
        await pipeline_consumer_task(client=client, multiplexer=multiplexer)

    return factory


def _make_monitor_consumer_factory(
    *,
    client: MonitorEventsClient,
    multiplexer: EventMultiplexer,
) -> Callable[[ProcessSession], Coroutine[Any, Any, None]]:
    """Bind the monitor consumer task to the live client + multiplexer."""

    async def factory(_session: ProcessSession) -> None:
        await monitor_consumer_task(client=client, multiplexer=multiplexer)

    return factory


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
    auth_overrides: AuthOverrides | None = None,
    events_overrides: EventsOverrides | None = None,
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
    auth_overrides:
        Optional :class:`AuthOverrides` bundle for test / deploy-time
        injection of the auth collaborators (WebAuthn verifier, setup-
        token gate, session signing secret, cookie ``Secure`` flag,
        clock). Production callers leave this ``None``; tests pass an
        :class:`InMemoryWebauthnVerifier` + fixed signing secret +
        frozen clock to make the auth flow deterministic. See
        :class:`AuthOverrides` for the per-field semantics.
    """
    overrides = auth_overrides if auth_overrides is not None else AuthOverrides()
    events = events_overrides if events_overrides is not None else EventsOverrides()
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

    # Auth-state wiring (story 03 / ALP-667). The cc_writer +
    # foreign_reader factories are wired by the lifespan (above); the
    # auth surface needs additional collaborators that don't depend on
    # the engines and so can be wired here.
    app.state.session_signing_secret = _resolve_session_secret(overrides)
    app.state.webauthn_verifier = _resolve_webauthn_verifier(
        overrides, command_center_config=command_center_config, security_config=security_config
    )
    app.state.setup_token_gate = overrides.setup_token_gate or SetupTokenGate()
    app.state.cookies_secure = overrides.cookies_secure
    app.state.clock = (
        overrides.clock if overrides.clock is not None else (lambda: datetime.now(UTC))
    )

    # Mount the auth router (story 03 / ALP-667).
    app.include_router(build_auth_router())

    # Events-subsystem wiring (story 04b / ALP-669). The multiplexer
    # lives on app.state so the route handler + the consumer tasks
    # share the same instance. The consumer-task factories are exposed
    # via app.state.event_consumer_task_factories — the composition
    # root (__main__) reads them and registers each on the
    # CommandCenterSupervisor's TaskGroup. Tests inject fake clients via
    # EventsOverrides.
    event_multiplexer = EventMultiplexer()
    app.state.event_multiplexer = event_multiplexer
    pipeline_client = events.pipeline_events_client or HttpxPipelineEventsClient(
        base_url=command_center_config.pipeline.events_url,
    )
    monitor_client = events.monitor_events_client or HttpxMonitorEventsClient(
        base_url=command_center_config.monitor.events_url,
    )
    app.state.event_consumer_task_factories = {
        "events_pipeline_consumer": _make_pipeline_consumer_factory(
            client=pipeline_client, multiplexer=event_multiplexer
        ),
        "events_monitor_consumer": _make_monitor_consumer_factory(
            client=monitor_client, multiplexer=event_multiplexer
        ),
    }
    app.include_router(build_events_router())

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
