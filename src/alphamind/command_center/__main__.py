"""Entry point for ``python -m alphamind.command_center``.

Boots the FastAPI app under Uvicorn inside the supervised
:class:`CommandCenterSupervisor` task tree, records a
``process_lifetimes`` row at startup (so per-operator-action invocation
rows have a valid FK target), and shuts down cleanly on SIGTERM /
SIGINT.

Wiring sequence:

1. Configure UTF-8 stdio so a Windows ``cp1252`` default doesn't mangle
   JSON output the future verify script ingests.
2. Load ``.env`` so secrets named by ``config/alerts.yaml`` (e.g.
   ``ALPHAMIND_DISCORD_WEBHOOK``) resolve.
3. Configure command-center logging (``%USERPROFILE%/AlphaMind/logs/
   command_center.log``).
4. Load the three config models (``command-center.yaml`` /
   ``security.yaml`` / ``alerts.yaml``).
5. Open the engine pair against ``alphamind.db`` so the
   ``record_process_lifetime`` write has a session factory.
6. Insert the ``process_lifetimes`` row.
7. Build the FastAPI app + the supervisor; register the Uvicorn task
   onto the supervisor's TaskGroup.
8. ``await supervisor.run()`` — blocks until SIGTERM/SIGINT or a
   task crash.

The Uvicorn task is the supervisor's first registered task; future
stories (04b SSE consumers, 05a alert engine) add more.

Per the parent-issue architectural invariants:

* **TaskGroup discipline.** Uvicorn runs as a supervised task — never
  via ``asyncio.create_task(server.serve())`` outside the TaskGroup.
* **No direct cross-process imports.** Nothing here imports from
  ``alphamind.scheduler`` or ``alphamind.execution.continuous_monitor``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center.app import build_app
from alphamind.command_center.auth.setup_token import SetupTokenGate
from alphamind.command_center.config import (
    CommandCenterConfig,
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.command_center.logging_setup import configure_command_center_logging
from alphamind.command_center.persistence.session import build_cc_writer_session_factory
from alphamind.command_center.session import ProcessSession, new_session
from alphamind.command_center.supervisor import CommandCenterSupervisor
from alphamind.persistence.session import engine_pair_context
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.process_lifetime import record_process_lifetime

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parents[3] / "config"
_DEFAULT_ARCHIVE_ROOT = Path.home() / "AlphaMind" / "archive"
_SHUTDOWN_TIMEOUT_SECONDS = 30


async def _run_uvicorn_task(
    _session: ProcessSession, *, app: FastAPI, host: str, port: int
) -> None:
    """Run Uvicorn against the FastAPI app until cancellation.

    Mirrors :func:`alphamind.scheduler.control.app.run_uvicorn_server_task`:
    awaits ``Server.serve()`` directly inside the supervisor's
    TaskGroup; on cancellation, sets ``server.should_exit = True``
    BEFORE re-raising so Uvicorn drains in-flight connections cleanly.

    ``log_config=None`` inherits the daemon's logging config (configured
    by :func:`configure_command_center_logging` at startup);
    ``access_log=False`` skips the per-request log noise since the
    operator-console traffic is low-volume and the audit trail lives in
    the ``activity_log`` table anyway.
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config=config)
    try:
        await server.serve()
    except BaseException:
        server.should_exit = True
        # Yield once so Uvicorn's main serve loop observes
        # ``should_exit`` before the cancellation propagates further.
        # Without this yield the cancellation can race past the flag
        # check and Uvicorn skips its drain step (F7).
        await asyncio.sleep(0)
        raise


async def _maybe_emit_setup_token(
    *,
    gate: SetupTokenGate,
    cc_writer_factory: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
    bind_host: str | None = None,  # ALP-727: for LAN suggestion (non-local + zero creds)
    bind_port: int = 8080,  # ALP-727 review follow-up: use the actual port in the hint
) -> str | None:
    """Mint (and log) the one-time setup token iff this is a fresh install (zero credentials).

    Called from the production ``_run`` path after ``build_app`` so the
    gate is present on ``app.state`` and we can resolve the DB path from
    the loaded command-center config. A transient factory is used only for
    the count; the lifespan owns the real engine + canonical factory.

    ALP-727 (03a): when ``existing == 0`` (mint path) *and* ``bind_host`` is
    non-local, emit the exact multi-line LAN hostname suggestion block (with
    socket.gethostname()-derived suggestion, copy-paste YAML for access: +
    webauthn.relying_party_id using the actual ``bind_port``, and RUNBOOK
    pointer) immediately after the "command_center setup token: ..." line.

    Returns the minted token (for tests to assert against ``gate._token``)
    or None when credentials already exist.
    """
    from alphamind.command_center.auth.repository import count_credentials

    existing = await count_credentials(cc_writer_factory)
    if existing == 0:
        token = gate.mint()
        logger.info("command_center setup token: %s", token)
        # ALP-727: first-boot LAN suggestion only on non-local bind (zero creds already true here)
        if bind_host is not None and not _is_loopbackish(bind_host):
            hn = socket.gethostname()
            suggested = f"{hn}.local"
            block = dedent(
                f"""\

LAN / hostname suggestion for passkey support:
  Edit config/command-center.yaml:
    access:
      scheme: "http"
      host: "{suggested}"     # or .local/hosts alias (socket.gethostname() == '{hn}')
      port: {bind_port}
  Edit config/security.yaml:
    webauthn:
      relying_party_id: "{suggested}"
      ...
  Then restart. You will need to re-enroll passkeys (rpId change).
  See RUNBOOK_command_center.md § LAN access for the full recipe.
"""
            ).strip()
            logger.info(block)
        return token
    logger.info(
        "command_center setup token: not minted (%d existing credentials)",
        existing,
    )
    return None


def _is_loopbackish(host: str | None) -> bool:
    """True for the common loopback forms (including bare 'localhost').

    Used by both the mixed-bind warning (ALP-726) and the first-boot LAN
    suggestion (ALP-727) so the set of 'local' addresses stays in one place.
    """
    if not host:
        return False
    h = host.lower().strip()
    return h in {"127.0.0.1", "::1", "localhost"} or h.startswith("127.0.0.")


def _warn_if_mixed_lan_bind_and_access(config: CommandCenterConfig) -> None:
    """ALP-726: emit one-time startup WARNING when bind widened but access host still localhost-ish.

    This is the validation warning from the approved LAN plan (§8 story 02).
    Emitted early in the _run boot path (near first-boot setup-token logging)
    so operators see it on `python -m alphamind.command_center` before Uvicorn binds.
    Directs to the access: block (and RUNBOOK) rather than silently producing
    broken WebAuthn origins / cookie flags.

    Tests exercise via direct call + caplog (mirrors _maybe_emit_setup_token pattern).
    """
    bind_host = config.bind.host
    access = config.access
    if not _is_loopbackish(bind_host):
        ah = access.host.lower().strip()
        is_localhostish = _is_loopbackish(ah) or ah.startswith("127.0.0.")
        if is_localhostish:
            log.warning(
                "command_center: bind.host=%s is not loopback but access.host=%s "
                "still looks localhost-ish. Edit the access: block (scheme + host + port) "
                "in command-center.yaml and align webauthn.relying_party_id in security.yaml; "
                "see RUNBOOK_command_center.md for LAN setup.",
                bind_host,
                access.host,
            )

    # Review follow-up (ALP-724): if someone has an old-style YAML (no access key)
    # that also customizes bind.port away from 8080, the default_factory will
    # still produce :8080 in the origin. Warn once so they know to supply the block.
    if access.host == "localhost" and access.port == 8080 and config.bind.port != 8080:
        log.warning(
            "command_center: bind.port=%s but no explicit access: block was supplied. "
            "The default origin will be http://localhost:8080 (not port %s). "
            "Supply an explicit access: block with a matching port to keep WebAuthn "
            "and cookies correct. See RUNBOOK_command_center.md § LAN access.",
            config.bind.port,
            config.bind.port,
        )


async def _run(config: CommandCenterConfig) -> None:
    """Run the command-center daemon under the supervisor.

    Records a process_lifetimes row, opens the engine pair, builds the
    FastAPI app + supervisor, registers the Uvicorn task, and runs
    until shutdown.
    """
    archive_root = _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

    # ALP-726: mixed bind/access validation warning (early, before engines or build_app).
    _warn_if_mixed_lan_bind_and_access(config)

    async with engine_pair_context() as engines:
        process_lifetime_id = await record_process_lifetime(
            session_factory=engines.async_session_factory,
            # The 'monitor' role is used because the cc daemon is an
            # always-on observer process (not a 'pipeline' invocation
            # producer). The ProcessRole vocabulary is closed —
            # ('pipeline', 'monitor') — and 'monitor' best matches the
            # cc's runtime shape. A future schema bump may add a
            # dedicated 'command_center' role; story 02 stays inside
            # the existing vocabulary.
            process_role="monitor",
            archive_root=archive_root,
        )
        session = new_session(process_lifetime_id=process_lifetime_id)
        log.info(
            "command center session start: process_lifetime_id=%s",
            process_lifetime_id,
        )

        # Load the auxiliary configs now so a validation error fails
        # the daemon before the FastAPI app is built (rather than
        # surfacing during lifespan when a request already arrived).
        security_config = load_security_config(_CONFIG_DIR)
        alerts_config = load_alerts_config(_CONFIG_DIR)

        app = build_app(
            command_center_config=config,
            security_config=security_config,
            alerts_config=alerts_config,
            production_session_factory=engines.async_session_factory,
            process_lifetime_id=process_lifetime_id,
            config_dir=_CONFIG_DIR,
        )

        # ALP-721: first-launch setup token mint + log (only when zero
        # webauthn_credentials). Uses a transient cc_writer factory for the
        # count decision; the canonical one (with owned engine lifetime) is
        # created inside the FastAPI lifespan. The else branch logs explicitly
        # on re-boots of credentialed installs so operators aren't confused
        # by a "minted" line that can never be consumed.
        # ALP-727: pass bind_host so _maybe can emit the LAN hostname suggestion
        # block (after the token line) exactly on non-local first-boot case.
        db_path = app.state.command_center_config.db.alphamind_db_path
        cc_writer_boot = build_cc_writer_session_factory(db_path)
        try:
            await _maybe_emit_setup_token(
                gate=app.state.setup_token_gate,
                cc_writer_factory=cc_writer_boot,
                logger=log,
                bind_host=config.bind.host,
                bind_port=config.bind.port,
            )
        except Exception:
            # Token mint is a first-launch UX convenience (per RUNBOOKs).
            # Do not hard-fail the entire daemon if the count or mint path
            # encounters a transient error (e.g. brief DB lock). The operator
            # can still use the verify script or a manual token if needed.
            log.exception("ALP-721: setup token mint path failed; continuing boot")
        finally:
            # Hygiene: match the explicit dispose discipline used by the
            # lifespan for all engines it constructs (F6). The boot factory
            # is used only for the one-time count decision.
            boot_engine = cc_writer_boot.kw["bind"]
            with contextlib.suppress(Exception):
                await boot_engine.dispose()

        supervisor = CommandCenterSupervisor(
            session=session,
            shutdown_timeout_seconds=_SHUTDOWN_TIMEOUT_SECONDS,
        )

        async def uvicorn_task(s: ProcessSession) -> None:
            await _run_uvicorn_task(
                s,
                app=app,
                host=config.bind.host,
                port=config.bind.port,
            )

        supervisor.register_task(name="uvicorn", coro_fn=uvicorn_task)
        # Story 04b / ALP-669: register the two upstream-events consumer
        # tasks alongside Uvicorn. The factories are built by ``build_app``
        # against the live ``HttpxPipelineEventsClient`` / ``HttpxMonitor...``;
        # they read off ``app.state.event_consumer_task_factories``.
        for task_name, factory in app.state.event_consumer_task_factories.items():
            supervisor.register_task(name=task_name, coro_fn=factory)
        # Story 05a / ALP-671: the alert engine factory is constructed
        # inside the lifespan (it needs the cc_writer + foreign_reader
        # factories the lifespan builds against the configured DB). The
        # factory may be None if alerts are explicitly disabled in tests;
        # production always wires it.
        alerts_factory = getattr(app.state, "alert_engine_task_factory", None)
        if alerts_factory is not None:
            supervisor.register_task(name="alerts_engine", coro_fn=alerts_factory)
        await supervisor.run()
        log.info(
            "command center session end: process_lifetime_id=%s",
            process_lifetime_id,
        )


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point — argparse-less for story 02.

    Future stories may add subcommands (e.g. ``register-credential``)
    that justify an argparse shell mirroring
    :func:`alphamind.scheduler.__main__.main`. Story 02 ships only the
    long-running daemon path: ``python -m alphamind.command_center``.

    ``argv`` is accepted for parity with the scheduler entry but
    unused in this story — argparse comes in when subcommands do.
    """
    # Mirror scheduler/__main__ UTF-8 setup.
    configure_utf8_stdio()
    if argv is not None and argv:
        log.warning(
            "command_center entrypoint ignored unexpected argv: %r (story 02 has no subcommands)",
            list(argv),
        )
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    configure_command_center_logging()

    config = load_command_center_config(_CONFIG_DIR)
    asyncio.run(_run(config))


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        # Outermost supervisor per runtime §G1: log + exit 1 so NSSM's
        # restart policy fires. ``BaseException`` catches
        # ``KeyboardInterrupt`` / ``SystemExit`` paths from the inner
        # main; ``SystemExit`` is rethrown above so the explicit exit
        # code threads through unchanged.
        log.exception("command center exited with error")
        sys.exit(1)
