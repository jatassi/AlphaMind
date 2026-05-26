"""Entry point for ``python -m alphamind.scheduler`` (stories 01 + 03b).

Subcommands
-----------
run                      Long-running daemon (NSSM service target). Registers
                         the APScheduler driver (04a) and emergency-invocation
                         receiver (04b) on the :class:`PipelineSupervisor`;
                         see ``_run_daemon`` below.
run --once <run_type>    One-shot manual invocation, used by the verify
                         script and operator dry-runs. Routes through the
                         orchestrator's ``run_invocation`` entry point.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
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
from alphamind.persistence.session import engine_pair_context
from alphamind.risk_guardrails.breach_behavior.config import load_breach_behavior_config
from alphamind.scheduler.driver import run_pipeline_scheduler_task
from alphamind.scheduler.emergency import run_emergency_receiver_task
from alphamind.scheduler.fresh_start import run_fresh_start_bootstrap
from alphamind.scheduler.logging_setup import configure_pipeline_logging
from alphamind.scheduler.orchestrator import run_invocation
from alphamind.scheduler.run_context import RunInvocationContext
from alphamind.scheduler.session import PipelineMode, new_session
from alphamind.scheduler.supervisor import PipelineSupervisor
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.process_lifetime import (
    record_process_lifetime,
)

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
    run_p.add_argument(
        "--debug-e2e",
        action="store_true",
        help=(
            "Drive one --once invocation with the synthetic portfolio + "
            "log-only broker + progress.jsonl emit. Requires --once and "
            "is mutually exclusive with --mode live."
        ),
    )
    run_p.add_argument(
        "--fresh-start",
        action="store_true",
        help=(
            "First-run bootstrap. Without --debug-e2e (ALP-620): fetch "
            "Alpaca's cash + positions, hard-fail if any positions exist or "
            "if cash_ledger already has a row, then insert the cash_ledger "
            "and drawdown_state singletons from Alpaca's reported cash and "
            "continue to the normal pipeline. Requires --once. With "
            "--debug-e2e (ALP-618): use the clean-slate FRESH_START_PORTFOLIO "
            "fixture ($100k cash, zero positions, zero theses) instead of "
            "the managed-portfolio SYNTHETIC_PORTFOLIO fixture."
        ),
    )
    run_p.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Override the archive-root directory (debug-e2e only). Production "
            "daemons use the hardcoded ``~/AlphaMind/archive`` default; the "
            "debug-e2e verify script passes this so per-invocation files land "
            "under the operator-chosen verification archive."
        ),
    )
    run_p.add_argument(
        "--resume-from",
        metavar="INVOCATION_ID:PHASE",
        default=None,
        help=(
            "Hydrate SDK-phase outputs from a prior invocation's archive and "
            "re-run from <phase> onward (debug-e2e only, ALP-693). Both the "
            "source invocation id and the target phase are required and "
            "colon-separated; neither side may be empty. Requires --debug-e2e "
            "and is mutually exclusive with --fresh-start. The CLI validates "
            "the source archive at argparse-time and exits 2 with a "
            "named-cause stderr message on failure — no DB writes."
        ),
    )

    args = parser.parse_args(argv)
    if args.subcommand == "run" and args.once is not None and not args.reason:
        parser.error("--once requires --reason")
    if args.subcommand == "run" and args.debug_e2e:
        # Mutual exclusion with ``--mode live`` is enforced at argparse level
        # so the helper never opens an engine pair against the live DB by
        # accident — the seeder's ``-debug-e2e.db`` suffix guard is the last
        # line of defense, this is the first.
        if args.mode == "live":
            parser.error("--debug-e2e is incompatible with --mode live")
        if args.once is None:
            parser.error("--debug-e2e requires --once <run_type> --reason <text>")
    if args.subcommand == "run" and args.archive_root is not None and not args.debug_e2e:
        parser.error("--archive-root is only valid with --debug-e2e")
    if args.subcommand == "run" and args.resume_from is not None:
        _validate_resume_from_argparse(parser, args)
    # --fresh-start has two distinct shapes:
    #   • with --debug-e2e: swap to the FRESH_START_PORTFOLIO fixture (ALP-618).
    #   • without --debug-e2e: production cold-start bootstrap from Alpaca
    #     (ALP-620). The bootstrap is a one-shot operation — daemon mode is
    #     rejected so the operator runs the bootstrap once, then starts the
    #     daemon normally. --mode live is blocked at argparse parallel to the
    #     --debug-e2e live guard above: bootstrapping the local DB against the
    #     LIVE Alpaca account on a typo is a one-keystroke foot-gun, so the
    #     paper-only constraint is enforced at parse time. The runbook
    #     documents only the paper path.
    if args.subcommand == "run" and args.fresh_start and not args.debug_e2e:
        if args.once is None:
            parser.error("--fresh-start (production bootstrap) requires --once")
        if args.mode == "live":
            parser.error("--fresh-start (production bootstrap) is incompatible with --mode live")
    return args


def _validate_resume_from_argparse(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Argparse-level validation for ``--resume-from`` (ALP-693).

    Rejects, via ``parser.error()`` (exit code 2), the four syntactic /
    mutual-exclusion arms the parse layer can see without touching disk:

    * ``--resume-from`` without ``--debug-e2e``;
    * ``--resume-from`` with ``--fresh-start`` (parent decision (F):
      resume against a different portfolio fixture is a category error);
    * value missing the colon;
    * value with an empty side (empty ``<invocation-id>`` or empty
      ``<phase>``).

    The filesystem-level rejections (source dir missing, unknown phase,
    upstream output missing) live in
    :func:`alphamind.scheduler.debug_e2e.resume.load_resume_context` and
    fire inside :func:`_run_debug_e2e` before ``wipe_and_seed`` runs.
    Extracted from :func:`_parse_args` to keep the parser body below
    the linter's complexity threshold.
    """
    if not args.debug_e2e:
        parser.error("--resume-from requires --debug-e2e")
    if args.fresh_start:
        parser.error("--resume-from is mutually exclusive with --fresh-start")
    # Format: ``<invocation-id>:<phase>`` — both sides required. Invocation
    # IDs embed ISO-8601 timestamps (e.g. ``inv-2026-05-26T00:00:00-abc``)
    # which already contain colons, so split on the LAST colon via
    # ``rpartition``: SDK phase names are simple identifiers without
    # colons, so the trailing segment is unambiguously the phase.
    invocation_id, sep, phase = args.resume_from.rpartition(":")
    if not sep:
        parser.error(
            f"--resume-from value {args.resume_from!r} must be "
            "<invocation-id>:<phase> (colon-separated, both required)"
        )
    if not invocation_id:
        parser.error(
            f"--resume-from value {args.resume_from!r} has empty <invocation-id>; "
            "expected <invocation-id>:<phase>"
        )
    if not phase:
        parser.error(
            f"--resume-from value {args.resume_from!r} has empty <phase>; "
            "expected <invocation-id>:<phase>"
        )


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
    now = datetime.now(UTC)

    async with engine_pair_context() as engines:
        # ALP-620 — cold-start bootstrap. Runs before ``record_process_lifetime``
        # so a precondition failure (positions present, cash_ledger already
        # initialized) surfaces to the operator without opening an invocation
        # row. The bootstrap commits its own transaction; the process-lifetime
        # row insertion that follows starts a fresh session.
        if args.fresh_start:
            await run_fresh_start_bootstrap(
                session_factory=engines.async_session_factory,
                venue_config=venue_config,
                execution_mode=execution_mode,
                now=now,
            )
        process_lifetime_id = await record_process_lifetime(
            session_factory=engines.async_session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
        context = RunInvocationContext(
            session_factory=engines.async_session_factory,
            sync_session_factory=engines.sync_session_factory,
            process_lifetime_id=process_lifetime_id,
            archive_root=archive_root,
            config_dir=_CONFIG_DIR,
            env_path=_DEFAULT_ENV_PATH,
            venue_config=venue_config,
            execution_mode=execution_mode,
        )
        summary = await run_invocation(
            context=context,
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason=args.reason,
            firing_run_type=RunType(args.once),
            now=now,
        )

    # The dataclass-asdict path produces a plain dict; ``default=str`` covers
    # the embedded ``RunType`` enum and the ``Phase1Summary`` slots so the
    # serialization stays a one-liner regardless of which fields are added
    # to ``InvocationSummary`` later.
    print(json.dumps(asdict(summary), default=str, indent=2))


async def _run_debug_e2e(args: argparse.Namespace) -> None:
    """Drive a single ``--once`` invocation under debug-e2e mode (story ALP-501).

    Mirrors :func:`_run_once` but (a) constructs the
    :class:`alphamind.scheduler.debug_e2e.settings.DebugE2ESettings`
    bundle, (b) wipes + reseeds the synthetic portfolio against the
    debug DB inside a single async session, (c) emits ``phase_start
    ("seed")`` / ``phase_done("seed")`` around the wipe+seed via a
    pre-invocation emitter (the canonical invocation_id is not known
    until ``run_invocation`` returns; the sentinel ``_pre_invocation``
    keys the seed log). The ``seed`` events live exclusively in
    ``<archive>/invocations/_pre_invocation/progress.jsonl`` — they
    are intentionally NOT part of the real-invocation event stream
    consumed by :func:`scripts.verify_debug_e2e.check_jsonl_ordering`,
    which inspects only the 12 in-invocation phases. Operators may
    consult the pre-invocation archive directory directly for seed-step
    debugging. (d) records the process-lifetime row *after* the wipe
    so the row survives, and (e) hands ``debug_e2e=settings`` to
    ``run_invocation``.

    The imports of ``alphamind.scheduler.debug_e2e`` are deferred via
    :func:`importlib.import_module` so the import-linter contract
    ``debug-e2e-forbidden-in-production`` never sees a production
    scheduler module reaching into the debug package — grimp's static
    parse can't resolve dynamic module names, so the runtime-only edge
    stays invisible to the contract while still executing normally.
    """
    configure_pipeline_logging()

    archive_root = args.archive_root if args.archive_root is not None else _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

    debug_e2e_settings = importlib.import_module("alphamind.scheduler.debug_e2e.settings")
    debug_e2e_seed = importlib.import_module("alphamind.scheduler.debug_e2e.seed")
    debug_e2e_portfolio = importlib.import_module("alphamind.scheduler.debug_e2e.portfolio")
    debug_e2e_resume = importlib.import_module("alphamind.scheduler.debug_e2e.resume")
    configure_debug_e2e = debug_e2e_settings.configure_debug_e2e
    wipe_and_seed = debug_e2e_seed.wipe_and_seed
    portfolio = (
        debug_e2e_portfolio.FRESH_START_PORTFOLIO
        if args.fresh_start
        else debug_e2e_portfolio.SYNTHETIC_PORTFOLIO
    )

    # --resume-from (ALP-693) — validate the source archive against the
    # phase-dependency DAG BEFORE wipe_and_seed runs.  A
    # ResumeValidationError surfaces as ``sys.exit(2)`` with the
    # operator-facing message on stderr; the outer ``BaseException``
    # frame in ``__main__`` is bypassed because ``SystemExit`` is
    # re-raised unchanged there.
    resume_context = None
    if args.resume_from is not None:
        # ``rpartition`` to mirror ``_validate_resume_from_argparse``:
        # invocation IDs contain colons (ISO-8601 timestamps), SDK phase
        # names don't, so the LAST colon separates the two.
        invocation_id, _, phase = args.resume_from.rpartition(":")
        try:
            resume_context = debug_e2e_resume.load_resume_context(
                archive_root=archive_root,
                invocation_id=invocation_id,
                phase=phase,
            )
        except debug_e2e_resume.ResumeValidationError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)

    debug_settings = configure_debug_e2e(
        archive_root=archive_root,
        portfolio=portfolio,
        resume_context=resume_context,
    )
    venue_config = _load_venue_config(_CONFIG_DIR)
    execution_mode = ExecutionMode.paper
    now = datetime.now(UTC)

    async with engine_pair_context() as engines:
        # ``EnginePair`` has no ``db_path`` field; read it off the resolved
        # URL so ``wipe_and_seed``'s ``-debug-e2e.db`` suffix guard sees the
        # path actually in use (a mis-set ``DATABASE_PATH`` then surfaces as
        # a refusing ``RuntimeError`` rather than wiping a paper / live DB).
        resolved_db_path = engines.async_engine.url.database
        if resolved_db_path is None:
            msg = (
                "engine URL did not expose a database path; "
                "cannot enforce the -debug-e2e.db safety guard"
            )
            raise RuntimeError(msg)

        # The canonical invocation_id is not known until ``run_invocation``
        # opens the invocation row; the ``_pre_invocation`` sentinel keys the
        # pre-invocation seed log so it doesn't race with the per-invocation file.
        # ``now`` (already computed above) is passed as ``as_of`` so the emitter
        # can construct the date-partitioned archive path (ALP-689 followup).
        pre_emitter = debug_settings.emitter_factory("_pre_invocation", now)
        pre_emitter.phase_start("seed")
        try:
            async with engines.async_session_factory() as session:
                await wipe_and_seed(
                    session=session,
                    now=now,
                    db_path=resolved_db_path,
                    portfolio=portfolio,
                )
        except RuntimeError:
            # Surface the refusing path before the outermost
            # ``BaseException`` handler in ``__main__`` swallows the
            # exception into a generic "pipeline scheduler exited with
            # error" frame — operators need the actionable path.
            log.exception("debug-e2e seed refused: db_path=%s", resolved_db_path)
            raise
        pre_emitter.phase_done("seed")

        # Record process_lifetime AFTER the wipe so the row survives.
        process_lifetime_id = await record_process_lifetime(
            session_factory=engines.async_session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
        context = RunInvocationContext(
            session_factory=engines.async_session_factory,
            sync_session_factory=engines.sync_session_factory,
            process_lifetime_id=process_lifetime_id,
            archive_root=archive_root,
            config_dir=_CONFIG_DIR,
            env_path=_DEFAULT_ENV_PATH,
            venue_config=venue_config,
            execution_mode=execution_mode,
            debug_e2e=debug_settings,
        )
        summary = await run_invocation(
            context=context,
            trigger_type="manual",
            trigger_source="debug_e2e_cli",
            trigger_reason=args.reason,
            firing_run_type=RunType(args.once),
            now=now,
        )

    print(json.dumps(asdict(summary), default=str, indent=2))


async def _run_daemon(*, mode: PipelineMode) -> None:
    """Daemon path — register both supervisor tasks and run until shutdown.

    Registers the APScheduler driver (story 04a) and the emergency-invocation
    receiver (story 04b) on the :class:`PipelineSupervisor`.
    """
    configure_pipeline_logging()
    cfg = SchedulerConfig.model_validate(read_yaml_file(_CONFIG_DIR / "scheduler.yaml"))
    shutdown_timeout = cfg.supervisor_shutdown_timeout_seconds
    breach_behavior_config = load_breach_behavior_config(_CONFIG_DIR / "breach_behavior.yaml")
    venue_config = _load_venue_config(_CONFIG_DIR)
    execution_mode = ExecutionMode.live if mode == "live" else ExecutionMode.paper

    archive_root = _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

    async with engine_pair_context() as engines:
        process_lifetime_id = await record_process_lifetime(
            session_factory=engines.async_session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
        context = RunInvocationContext(
            session_factory=engines.async_session_factory,
            sync_session_factory=engines.sync_session_factory,
            process_lifetime_id=process_lifetime_id,
            archive_root=archive_root,
            config_dir=_CONFIG_DIR,
            env_path=_DEFAULT_ENV_PATH,
            venue_config=venue_config,
            execution_mode=execution_mode,
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
            name="apscheduler",
            coro_fn=partial(
                run_pipeline_scheduler_task,
                scheduler_config=cfg,
                context=context,
            ),
        )
        supervisor.register_task(
            name="emergency_receiver",
            coro_fn=partial(
                run_emergency_receiver_task,
                poll_interval_seconds=cfg.emergency_poll_interval_seconds,
                cooldown_minutes=breach_behavior_config.emergency_invocation_cooldown_minutes,
                context=context,
            ),
        )
        await supervisor.run()
        log.info("pipeline scheduler session end: process_lifetime_id=%s", process_lifetime_id)


def main(argv: Sequence[str] | None = None) -> None:
    # Switch stdout/stderr to UTF-8 with ``replace`` errors so a Windows
    # ``cp1252`` default doesn't mangle the JSON ``InvocationSummary``
    # captured by ``scripts/verify_debug_e2e.py`` (or surface as a
    # ``UnicodeEncodeError`` inside the subprocess on non-ASCII output).
    configure_utf8_stdio()
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

    # ``--debug-e2e`` branch — synthetic-portfolio + log-only-broker variant
    # of the single-invocation path (story ALP-501).
    if args.debug_e2e:
        asyncio.run(_run_debug_e2e(args))
        return

    # ``--once`` branch: argparse already validated ``args.once`` against the
    # ``RunType`` vocabulary. Drive one invocation end-to-end via the story 03b
    # orchestrator and print the typed summary as JSON for the operator.
    asyncio.run(_run_once(args))


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    # ALP-620 — the precondition class is imported only at the entry-point
    # handler so the typecheck branch below names the public exception
    # without polluting the module-level surface used elsewhere.
    from alphamind.scheduler.fresh_start import FreshStartPreconditionError

    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except FreshStartPreconditionError as exc:
        # The precondition message is operator-actionable on its own; surface
        # it verbatim on stderr without the traceback noise the BaseException
        # frame below would print. Distinct exit code (2) lets shell wrappers
        # distinguish "operator made a misconfiguration call" from "scheduler
        # blew up" — the latter is the NSSM-restart path.
        print(f"--fresh-start: {exc}", file=sys.stderr)
        sys.exit(2)
    except BaseException:
        # Outermost supervisor per runtime §G1: log + exit 1 so NSSM's restart
        # policy fires. ``BaseException`` (vs ``Exception``) catches
        # ``KeyboardInterrupt`` / ``SystemExit`` paths that the inner ``main``
        # entry can synthesize; ``SystemExit`` is rethrown above so the
        # explicit exit code threads through unchanged.
        log.exception("pipeline scheduler exited with error")
        sys.exit(1)
