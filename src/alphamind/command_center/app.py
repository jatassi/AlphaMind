"""FastAPI app composition for the command center (story 02 / ALP-666).

This is the composition root for the command-center FastAPI app —
the only module that wires routers, lifespan, and dependencies. The
module currently composes:

* ``/healthz`` — a trivial liveness probe.
* ``/auth/*`` — the WebAuthn + sessions + CSRF surface (story 03 /
  ALP-667) via :func:`alphamind.command_center.auth.routes.build_auth_router`.
* ``/api/control/*`` — the operator-action proxy + audit surface
  (story 04a / ALP-668) via
  :func:`alphamind.command_center.control.routes.build_control_router`.
* ``/api/events`` — the SSE multiplexer (story 04b / ALP-669) via
  :func:`alphamind.command_center.events.routes.build_events_router`.
  ``build_events_router`` already bakes the ``/api`` prefix, so no
  ``prefix=`` kwarg is passed at the include site (F9).
* ``/`` static mount — serves the Vite-built SPA bundle from
  ``config.frontend.dist_path`` (story 04c). Mounted LAST so
  ``/api/*`` routes match first; skipped in dev mode (when
  ``COMMAND_CENTER_DEV_MODE`` is truthy) and when the bundle is
  absent.

Documented include points for future stories:

* Story 05a (alerts) — ``app.include_router(alerts_router, prefix="/api/alerts")``.
* View stories (05b-05j, 06a-06c) — included under ``/api/views/...``.

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
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
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
from alphamind.command_center.control.monitor_client import (
    MonitorClient,
    RealMonitorClient,
)
from alphamind.command_center.control.pipeline_client import (
    PipelineClient,
    RealPipelineClient,
)
from alphamind.command_center.control.routes import build_control_router
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

__all__ = ["AuthOverrides", "ControlOverrides", "EventsOverrides", "build_app"]


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


@dataclass(frozen=True, slots=True)
class ControlOverrides:
    """Test-time overrides for the control surface (story 04a / ALP-668).

    Production callers leave every field ``None``; the lifespan
    constructs a shared :class:`httpx.AsyncClient` + the two
    :class:`Real*Client` implementations against the URLs pinned in
    :class:`CommandCenterConfig.pipeline.control_url` /
    ``monitor.control_url``. Tests pass :class:`FakePipelineClient` +
    :class:`FakeMonitorClient` so the route layer is exercised without
    booting the upstream surfaces.
    """

    pipeline_client: PipelineClient | None = None
    monitor_client: MonitorClient | None = None
    process_lifetime_id: str | None = None


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

_DEV_MODE_ENV = "COMMAND_CENTER_DEV_MODE"
"""Env var that, when set to any truthy value, skips the StaticFiles mount.

Dev workflow: the operator runs ``bun run dev`` (Vite at :5173) AND
``python -m alphamind.command_center`` (FastAPI at :8080). Vite proxies
``/api/*``, ``/auth/*``, ``/events``, ``/healthz`` to FastAPI; the SPA
itself is served by Vite. The FastAPI ``StaticFiles`` mount at ``/`` is
inert in this configuration because the browser hits Vite directly — but
we skip the mount anyway so a missing ``dist/`` directory (developer
hasn't built yet) doesn't crash the daemon at startup.

Production: env var is unset; ``bun run build`` produces
``config.frontend.dist_path``; the mount serves the bundle.
"""

log = logging.getLogger(__name__)


def _dev_mode_active() -> bool:
    """Return True if the dev-mode env var is set to a truthy value.

    Truthy values follow the standard set ``{"1", "true", "yes", "on"}``
    (case-insensitive). Anything else (including unset) returns False.
    """
    raw = os.environ.get(_DEV_MODE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    # Construct the loopback HTTP client + Real* control clients only
    # when the build_app caller did NOT pre-inject overrides. The
    # client's lifetime is owned by this lifespan so the connection
    # pool is shared across all /api/control/* dispatches.
    http_client: httpx.AsyncClient | None = None
    if getattr(app.state, "pipeline_client", None) is None:
        if http_client is None:
            http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        app.state.pipeline_client = RealPipelineClient(
            base_url=app.state.command_center_config.pipeline.control_url,
            http_client=http_client,
        )
    if getattr(app.state, "monitor_client", None) is None:
        if http_client is None:
            http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        app.state.monitor_client = RealMonitorClient(
            base_url=app.state.command_center_config.monitor.control_url,
            http_client=http_client,
        )
    app.state.control_http_client = http_client

    try:
        yield
    finally:
        # Dispose engines + the shared httpx clients so SQLite file
        # handles release cleanly and the pool is drained. Each
        # dispose runs in its own try/except so a failure on one does
        # not skip the rest (F6). The production engine is NOT disposed
        # here — its lifetime is owned by the composition root's
        # engine_pair_context.
        events_http_client = getattr(app.state, "events_http_client", None)
        for client_label, client in (
            ("control", http_client),
            ("events", events_http_client),
        ):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception as exc:
                log.warning(
                    "command_center lifespan: %s httpx client close failed: %s",
                    client_label,
                    exc,
                )
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
    process_lifetime_id: str | None = None,
    auth_overrides: AuthOverrides | None = None,
    control_overrides: ControlOverrides | None = None,
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
    process_lifetime_id:
        FK target the control router threads into
        :func:`operator_invocation` (story 04a / ALP-668). Production
        callers thread the value :func:`record_process_lifetime`
        returned at daemon startup; tests may omit it when not
        exercising the control surface (the routes' dependency on
        ``app.state.process_lifetime_id`` is read at request time, not
        at app construction, so a missing value surfaces as an
        ``AttributeError`` only when a control verb is invoked).
    auth_overrides:
        Optional :class:`AuthOverrides` bundle for test / deploy-time
        injection of the auth collaborators (WebAuthn verifier, setup-
        token gate, session signing secret, cookie ``Secure`` flag,
        clock). Production callers leave this ``None``; tests pass an
        :class:`InMemoryWebauthnVerifier` + fixed signing secret +
        frozen clock to make the auth flow deterministic. See
        :class:`AuthOverrides` for the per-field semantics.
    control_overrides:
        Optional :class:`ControlOverrides` bundle for test injection of
        the control-surface clients (story 04a / ALP-668). Production
        callers leave this ``None`` and the lifespan constructs the
        :class:`RealPipelineClient` / :class:`RealMonitorClient` against
        the URLs pinned in :class:`CommandCenterConfig`. Tests pass
        :class:`FakePipelineClient` / :class:`FakeMonitorClient` so the
        proxy + route layer is exercised without booting the upstream
        surfaces.
    events_overrides:
        Optional :class:`EventsOverrides` bundle for test / deploy-time
        injection of the events-surface clients (story 04b / ALP-669).
        Production callers leave this ``None`` and ``build_app``
        constructs the :class:`HttpxPipelineEventsClient` /
        :class:`HttpxMonitorEventsClient` against the URLs pinned in
        :class:`CommandCenterConfig.pipeline.events_url` /
        ``monitor.events_url``, sharing one ``httpx.AsyncClient`` for
        both upstreams (F10) — the shared client's lifetime is owned
        by the lifespan. Tests pass :class:`FakePipelineEventsClient` /
        :class:`FakeMonitorEventsClient` so the consumer-task
        plumbing + the SSE multiplexer + the ``/api/events`` route are
        exercised without booting real loopback SSE surfaces.
    """
    overrides = auth_overrides if auth_overrides is not None else AuthOverrides()
    cc_control = control_overrides if control_overrides is not None else ControlOverrides()
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
    app.state.process_lifetime_id = (
        cc_control.process_lifetime_id
        if cc_control.process_lifetime_id is not None
        else process_lifetime_id
    )

    # Control-surface wiring (story 04a / ALP-668). Test fakes are
    # threaded in via ControlOverrides; production Real* clients are
    # constructed inside the lifespan (the shared httpx client's
    # lifetime belongs there). The lifespan checks app.state for a
    # pre-existing client before constructing its own.
    if cc_control.pipeline_client is not None:
        app.state.pipeline_client = cc_control.pipeline_client
    if cc_control.monitor_client is not None:
        app.state.monitor_client = cc_control.monitor_client

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
    #
    # F10: when no override is provided, mint ONE shared httpx.AsyncClient
    # for both upstream SSE clients. The shared client uses an
    # SSE-compatible timeout (no read timeout — the upstreams keep the
    # connection open) and its lifetime is owned by the lifespan
    # (disposed on shutdown alongside the control http client). When
    # both upstreams share a single client they share a single
    # connection pool, halving the per-process TCP / TLS overhead.
    event_multiplexer = EventMultiplexer()
    app.state.event_multiplexer = event_multiplexer
    events_http_client: httpx.AsyncClient | None = None
    if events.pipeline_events_client is None or events.monitor_events_client is None:
        events_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
        )
    events_pipeline_client = events.pipeline_events_client or HttpxPipelineEventsClient(
        base_url=command_center_config.pipeline.events_url,
        http_client=events_http_client,
    )
    events_monitor_client = events.monitor_events_client or HttpxMonitorEventsClient(
        base_url=command_center_config.monitor.events_url,
        http_client=events_http_client,
    )
    app.state.events_http_client = events_http_client
    app.state.event_consumer_task_factories = {
        "events_pipeline_consumer": _make_pipeline_consumer_factory(
            client=events_pipeline_client, multiplexer=event_multiplexer
        ),
        "events_monitor_consumer": _make_monitor_consumer_factory(
            client=events_monitor_client, multiplexer=event_multiplexer
        ),
    }
    app.include_router(build_events_router())

    # Mount the control router (story 04a / ALP-668) under /api/control.
    app.include_router(build_control_router(), prefix="/api/control")

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

    _maybe_mount_frontend(app, command_center_config=command_center_config)
    return app


def _maybe_mount_frontend(app: FastAPI, *, command_center_config: CommandCenterConfig) -> None:
    """Mount ``StaticFiles`` at ``/`` serving the Vite-built SPA, if appropriate.

    The mount lives LAST in the route table — FastAPI's matching tries
    registered routes first, and the StaticFiles mount at ``/`` with
    ``html=True`` catches every unmatched path and serves ``index.html``
    (TanStack Router's client-side SPA fallback).

    Two skip-paths fail-closed without crashing the daemon:

    * **Dev mode.** ``COMMAND_CENTER_DEV_MODE`` env var is truthy — the
      operator runs Vite alongside FastAPI; the SPA is served by Vite, not
      by FastAPI. Skipping the mount avoids needing a built ``dist/`` for
      dev iteration.
    * **Missing dist directory.** The configured ``dist_path`` doesn't
      resolve to an existing directory (developer hasn't run
      ``bun run build`` yet). Logs a warning and continues — the API
      surface still works; only the SPA fallback is unavailable.
    """
    if _dev_mode_active():
        log.info(
            "command_center: %s set; skipping StaticFiles mount (Vite serves the SPA)",
            _DEV_MODE_ENV,
        )
        return
    dist_path = Path(command_center_config.frontend.dist_path)
    if not dist_path.is_dir():
        log.warning(
            "command_center: frontend dist_path %s is not a directory; skipping "
            "StaticFiles mount. Run `bun run build` in src/alphamind/command_center/"
            "frontend/ to produce the bundle.",
            dist_path,
        )
        return
    app.mount("/", StaticFiles(directory=dist_path, html=True), name="frontend")
