"""Entry point for ``python -m alphamind.scheduler`` (story 01).

Subcommands
-----------
run                      Long-running daemon (NSSM service target).
run --once <run_type>    One-shot manual invocation (story 05 verify
                         script + operator dry-runs). Argument parsing
                         lands here in story 01; the actual dispatch to
                         ``run_invocation`` ships in story 03b, at which
                         point the ``NotImplementedError`` below is
                         replaced with the call.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.execution.state_persistence.process_lifetime import (
    record_process_lifetime,
)
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.scheduler.logging_setup import configure_pipeline_logging
from alphamind.scheduler.session import PipelineMode, new_session
from alphamind.scheduler.supervisor import PipelineSupervisor

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parents[3] / "config"
_DEFAULT_ARCHIVE_ROOT = Path.home() / "AlphaMind" / "archive"

# Story 03b replaces the placeholder ``run --once`` body with a real
# ``run_invocation`` call. The CLI surface itself is committed here so
# story 05's verify script can target it.
_RUN_INVOCATION_NOT_YET_WIRED = "run_invocation not yet wired; ships in story 03b"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.scheduler",
        description="AlphaMind pipeline scheduler — long-running runner process.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser("run", help="Start the long-running scheduler (blocks).")
    run_p.add_argument(
        "--once",
        metavar="RUN_TYPE",
        choices=[rt.value for rt in RunType],
        default=None,
        help=(
            "Fire exactly one invocation of the given run type and exit "
            "instead of installing the APScheduler. trigger_type is "
            "recorded as 'manual'."
        ),
    )
    run_p.add_argument(
        "--reason",
        metavar="TEXT",
        default=None,
        help="Free-form reason recorded on the invocation row (required with --once).",
    )
    run_p.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode for the session (default: paper).",
    )

    args = parser.parse_args(argv)
    if args.subcommand == "run" and args.once is not None and not args.reason:
        parser.error("--once requires --reason")
    return args


async def _run_daemon(*, mode: PipelineMode) -> None:
    """Daemon path — empty registry in this story; later stories register tasks."""
    configure_pipeline_logging()
    cfg = SchedulerConfig.model_validate(read_yaml_file(_CONFIG_DIR / "scheduler.yaml"))
    shutdown_timeout = cfg.supervisor_shutdown_timeout_seconds

    archive_root = _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    try:
        process_lifetime_id = await record_process_lifetime(
            session_factory=session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
        session = new_session(process_lifetime_id=process_lifetime_id, mode=mode)
        log.info(
            "pipeline scheduler session start: process_lifetime_id=%s mode=%s",
            process_lifetime_id,
            mode,
        )
        supervisor = PipelineSupervisor(
            session=session,
            shutdown_timeout_seconds=shutdown_timeout,
        )
        # Story 04a / 04b call ``supervisor.register_task(...)`` here; in
        # story 01 the registry is empty by design.
        await supervisor.run()
        log.info("pipeline scheduler session end: process_lifetime_id=%s", process_lifetime_id)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    if args.subcommand != "run":
        # ``required=True`` on the subparser makes this unreachable, but be
        # defensive so a future subcommand addition doesn't silently fall
        # through.
        msg = f"unknown subcommand: {args.subcommand!r}"
        raise RuntimeError(msg)

    if args.once is None:
        # argparse ``choices=["paper", "live"]`` already narrowed the value;
        # ``cast`` informs mypy without an additional runtime check.
        asyncio.run(_run_daemon(mode=cast(PipelineMode, args.mode)))
        return

    # ``--once`` branch: argparse already validated ``args.once`` against the
    # ``RunType`` vocabulary. Story 03b replaces this with a real
    # ``run_invocation`` dispatch.
    raise NotImplementedError(_RUN_INVOCATION_NOT_YET_WIRED)


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        log.exception("pipeline scheduler exited with error")
        sys.exit(1)
