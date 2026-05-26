"""Entry point for ``python -m alphamind.command_center`` (story 02 / ALP-666).

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
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from alphamind.command_center.app import build_app
from alphamind.command_center.config import (
    CommandCenterConfig,
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.command_center.logging_setup import configure_command_center_logging
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
        raise


async def _run(config: CommandCenterConfig) -> None:
    """Run the command-center daemon under the supervisor.

    Records a process_lifetimes row, opens the engine pair, builds the
    FastAPI app + supervisor, registers the Uvicorn task, and runs
    until shutdown.
    """
    archive_root = _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

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
        )

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
