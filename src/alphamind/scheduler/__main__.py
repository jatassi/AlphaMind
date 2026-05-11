"""Entry point for ``python -m alphamind.scheduler`` (stories 01 + 03b).

Subcommands
-----------
run                      Long-running daemon (NSSM service target).
run --once <run_type>    One-shot manual invocation (story 05 verify
                         script + operator dry-runs). Story 03b wires
                         the ``--once`` branch into the orchestrator
                         (``run_invocation``); the daemon-loop body
                         remains the empty registry shipped in story 01
                         until stories 04a / 04b register tasks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.state_persistence.process_lifetime import (
    record_process_lifetime,
)
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.risk_guardrails.breach_behavior.config import load_breach_behavior_config
from alphamind.scheduler.emergency import run_emergency_receiver_task
from alphamind.scheduler.logging_setup import configure_pipeline_logging
from alphamind.scheduler.orchestrator import run_invocation
from alphamind.scheduler.session import PipelineMode, new_session
from alphamind.scheduler.supervisor import PipelineSupervisor

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parents[3] / "config"
_DEFAULT_ARCHIVE_ROOT = Path.home() / "AlphaMind" / "archive"
_DEFAULT_ENV_PATH = Path(__file__).parents[3] / ".env"


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


def _load_venue_config(config_dir: Path) -> VenueConfig:
    """Read ``venue.yaml`` and return a validated :class:`VenueConfig`.

    Extracted as a module-level seam so the CLI tests can monkey-patch the
    filesystem read without inventing a fake YAML on disk.
    """
    return VenueConfig.model_validate(read_yaml_file(config_dir / "venue.yaml"))


async def _run_once(args: argparse.Namespace) -> None:
    """Drive a single ``run_invocation`` and print its summary as JSON.

    Loads the venue + execution-mode config, records a process-lifetime
    row (so the per-invocation row's FK is satisfiable), then dispatches
    one orchestrator pass for the requested run-type.
    """
    configure_pipeline_logging()

    archive_root = _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

    venue_config = _load_venue_config(_CONFIG_DIR)
    execution_mode = ExecutionMode.live if args.mode == "live" else ExecutionMode.paper

    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    try:
        process_lifetime_id = await record_process_lifetime(
            session_factory=session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
        summary = await run_invocation(
            session_factory=session_factory,
            process_lifetime_id=process_lifetime_id,
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason=args.reason,
            firing_run_type=RunType(args.once),
            archive_root=archive_root,
            config_dir=_CONFIG_DIR,
            env_path=_DEFAULT_ENV_PATH,
            venue_config=venue_config,
            execution_mode=execution_mode,
            now=datetime.now(UTC),
        )
    finally:
        await engine.dispose()

    # The dataclass-asdict path produces a plain dict; ``default=str`` covers
    # the embedded ``RunType`` enum and the ``Phase1Summary`` slots so the
    # serialization stays a one-liner regardless of which fields are added
    # to ``InvocationSummary`` later.
    print(json.dumps(asdict(summary), default=str, indent=2))


async def _run_daemon(*, mode: PipelineMode) -> None:
    """Daemon path — registers the emergency-invocation receiver (story 04b).

    Story 04a will register the APScheduler driver alongside this task.
    """
    configure_pipeline_logging()
    cfg = SchedulerConfig.model_validate(read_yaml_file(_CONFIG_DIR / "scheduler.yaml"))
    shutdown_timeout = cfg.supervisor_shutdown_timeout_seconds
    breach_behavior_config = load_breach_behavior_config(_CONFIG_DIR / "breach_behavior.yaml")
    venue_config = _load_venue_config(_CONFIG_DIR)
    execution_mode = ExecutionMode.live if mode == "live" else ExecutionMode.paper

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
        supervisor.register_task(
            name="emergency_receiver",
            coro_fn=partial(
                run_emergency_receiver_task,
                poll_interval_seconds=cfg.emergency_poll_interval_seconds,
                cooldown_minutes=breach_behavior_config.emergency_invocation_cooldown_minutes,
                session_factory=session_factory,
                archive_root=archive_root,
                config_dir=_CONFIG_DIR,
                env_path=_DEFAULT_ENV_PATH,
                venue_config=venue_config,
                execution_mode=execution_mode,
            ),
        )
        # Story 04a will register the APScheduler driver here.
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
    # ``RunType`` vocabulary. Drive one invocation end-to-end via the story 03b
    # orchestrator and print the typed summary as JSON for the operator.
    asyncio.run(_run_once(args))


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        log.exception("pipeline scheduler exited with error")
        sys.exit(1)
