"""Headless retrospective CLI (ALP-890 / story 07c).

The headless data surface ``/feedback-retrospective`` drives. Three subcommands map
one-to-one onto the data layer:

* ``ingest`` — :func:`~alphamind.feedback_loop.retrospective.ingestion.ingest_window`
  over ``[--start, --end)``; prints the in-window counts the skill reads in Phase 1.
* ``save-report`` — :func:`~alphamind.feedback_loop.retrospective.report.save_report`;
  writes the metadata row + the markdown file and prints the new ``report_id``.
* ``capture-decision`` —
  :func:`~alphamind.feedback_loop.retrospective.report.capture_decision`; writes a
  ``retrospective_decisions`` row and prints the new ``decision_id``.

Usage::

    python -m alphamind.feedback_loop.retrospective.cli ingest \\
        --start <ISO-8601> --end <ISO-8601> [--db-path <path>]
    python -m alphamind.feedback_loop.retrospective.cli save-report \\
        --start <ISO-8601> --end <ISO-8601> --markdown-file <path> \\
        [--session-id <id>] [--db-path <path>] [--data-root <path>]
    python -m alphamind.feedback_loop.retrospective.cli capture-decision \\
        --report-id <id> --item-identifier <id> \\
        --decision-type {promotion_candidate,follow_up} \\
        --verdict {accepted,rejected} --rationale <text> \\
        [--linked-validation-id <id>] [--db-path <path>]

Exit codes: ``0`` success; ``1`` argument validation failure (naive datetime,
unreadable markdown file); ``2`` infrastructure error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from alphamind.feedback_loop.retrospective.ingestion import ingest_window
from alphamind.feedback_loop.retrospective.records import (
    DecisionType,
    ReportId,
    Verdict,
)
from alphamind.feedback_loop.retrospective.report import (
    capture_decision,
    save_report,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)

__all__ = ["main"]

log = logging.getLogger(__name__)


def _parse_aware_datetime(value: str, arg_name: str) -> datetime:
    """Parse *value* as ISO-8601 and reject naive datetimes."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: {value!r} is not a valid ISO-8601 datetime. "
            "Provide a tz-aware value, e.g. 2026-01-01T00:00:00+00:00."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: {value!r} is a naive datetime (no timezone offset). "
            "Provide a tz-aware value, e.g. 2026-01-01T00:00:00+00:00."
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.feedback_loop.retrospective.cli",
        description="Retrospective data layer: ingest, save a report, capture a decision.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Pull the Phase-1 retrospective data set.")
    ingest.add_argument("--start", metavar="ISO-8601", required=True)
    ingest.add_argument("--end", metavar="ISO-8601", required=True)
    ingest.add_argument("--db-path", metavar="PATH", default=None, dest="db_path")

    save = subparsers.add_parser("save-report", help="Persist a rendered report.")
    save.add_argument("--start", metavar="ISO-8601", required=True)
    save.add_argument("--end", metavar="ISO-8601", required=True)
    save.add_argument("--markdown-file", metavar="PATH", required=True, dest="markdown_file")
    save.add_argument("--session-id", metavar="ID", default=None, dest="session_id")
    save.add_argument("--db-path", metavar="PATH", default=None, dest="db_path")
    save.add_argument("--data-root", metavar="PATH", default=None, dest="data_root")

    capture = subparsers.add_parser("capture-decision", help="Record a walkthrough decision.")
    capture.add_argument("--report-id", metavar="ID", required=True, dest="report_id")
    capture.add_argument("--item-identifier", metavar="ID", required=True, dest="item_identifier")
    capture.add_argument(
        "--decision-type",
        choices=[t.value for t in DecisionType],
        required=True,
        dest="decision_type",
    )
    capture.add_argument(
        "--verdict",
        choices=[v.value for v in Verdict],
        required=True,
    )
    capture.add_argument("--rationale", metavar="TEXT", required=True)
    capture.add_argument(
        "--linked-validation-id", metavar="ID", default=None, dest="linked_validation_id"
    )
    capture.add_argument("--db-path", metavar="PATH", default=None, dest="db_path")

    return parser


def _warn_default_db_path(db_path: str | None) -> None:
    if db_path is None:
        log.warning(
            "No --db-path provided; falling back to the configured production "
            "database path (DATABASE_PATH env or main.yaml paths.database)."
        )


def _run_ingest(args: argparse.Namespace) -> int:
    try:
        start = _parse_aware_datetime(args.start, "--start")
        end = _parse_aware_datetime(args.end, "--end")
    except argparse.ArgumentTypeError as exc:
        print(str(exc))
        return 1

    _warn_default_db_path(args.db_path)

    async def _ingest() -> int:
        engine = make_async_engine(args.db_path)
        try:
            factory = make_async_session_factory(engine)
            async with factory() as session:
                ingestion = await ingest_window(session, start, end)
        finally:
            await engine.dispose()
        window = ingestion.window
        print(f"agent_calls: {len(window.agent_calls)}")
        print(f"thesis_resolutions: {len(window.outcomes.theses)}")
        print(f"pm_decisions: {len(window.pm_decision_log)}")
        print(f"validations: {len(window.validations)}")
        print(f"replays: {len(window.replays.replays)}")
        print(f"pending_rollbacks: {len(ingestion.pending_rollbacks)}")
        # Emit the canonical identifier + artifact per pending rollback so the
        # skill copies the identifier verbatim into capture-decision (no slug
        # re-derivation on the surfacing side).
        for pending in ingestion.pending_rollbacks:
            print(f"  {pending.item_identifier}\t{pending.edited_artifact}")
        return 0

    try:
        return asyncio.run(_ingest())
    except Exception:
        log.exception("Retrospective ingestion failed")
        return 2


def _run_save_report(args: argparse.Namespace) -> int:
    try:
        start = _parse_aware_datetime(args.start, "--start")
        end = _parse_aware_datetime(args.end, "--end")
    except argparse.ArgumentTypeError as exc:
        print(str(exc))
        return 1

    try:
        markdown = Path(args.markdown_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"--markdown-file: cannot read {args.markdown_file!r}: {exc}")
        return 1

    _warn_default_db_path(args.db_path)
    data_root = Path(args.data_root) if args.data_root is not None else None

    engine = make_engine(args.db_path)
    try:
        with make_session_factory(engine)() as session:
            record = save_report(
                session,
                start,
                end,
                markdown,
                session_id=args.session_id,
                data_root=data_root,
            )
            session.commit()
    except Exception:
        log.exception("Retrospective report save failed")
        return 2
    finally:
        engine.dispose()

    print(record.report_id)
    return 0


def _run_capture_decision(args: argparse.Namespace) -> int:
    _warn_default_db_path(args.db_path)
    engine = make_engine(args.db_path)
    try:
        with make_session_factory(engine)() as session:
            record = capture_decision(
                session,
                ReportId(args.report_id),
                item_identifier=args.item_identifier,
                decision_type=DecisionType(args.decision_type),
                verdict=Verdict(args.verdict),
                rationale=args.rationale,
                linked_validation_id=args.linked_validation_id,
            )
            session.commit()
    except Exception:
        log.exception("Retrospective decision capture failed")
        return 2
    finally:
        engine.dispose()

    print(record.decision_id)
    return 0


_DISPATCH = {
    "ingest": _run_ingest,
    "save-report": _run_save_report,
    "capture-decision": _run_capture_decision,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code (0 success, 1 arg error, 2 runtime)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
