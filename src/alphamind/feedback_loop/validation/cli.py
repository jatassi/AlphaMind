"""Headless validation CLI — the surface ``/feedback-validate`` drives (ALP-889).

Three subcommands, each emitting JSON on stdout and (for ``register`` /
``evaluate``) accepting a JSON payload on ``--input`` (a path, or ``-`` for
stdin):

* ``register`` — snapshot provenance, freeze the criteria, persist the
  validation; emits ``{"validation_id": ...}``.
* ``evaluate`` — compute the pre/post metric comparison, derive the verdict +
  rollback status, write the outcome (or short-circuit on supersession); emits
  the outcome (or supersession state).
* ``list`` — emit the pending validations (point-in-time, not window-bounded).

Datetimes cross the boundary as ISO-8601 (tz-aware) strings; the CLI parses
them once at ingest into aware ``datetime``s (parse-don't-validate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import TYPE_CHECKING

from alphamind.feedback_loop.metrics.types import MetricId
from alphamind.feedback_loop.validation.evaluate import (
    EvaluationJudgments,
    EvaluationResult,
    evaluate_validation,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    OutcomeId,
    ValidationId,
    ValidationOutcomeRecord,
    ValidationRecord,
)
from alphamind.feedback_loop.validation.register import register_validation
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.validation_queries import read_pending_validations

if TYPE_CHECKING:
    from collections.abc import Mapping

EXIT_OK = 0
EXIT_ERROR = 1


def _read_payload(path: str) -> Mapping[str, object]:
    """Read a JSON object from *path* (``-`` reads stdin)."""
    raw = sys.stdin.read() if path == "-" else _read_file(path)
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        msg = "input payload must be a JSON object"
        raise ValueError(msg)
    return parsed


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:  # noqa: PTH123 — explicit text read
        return handle.read()


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 string into a tz-aware datetime, rejecting naive input."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = f"{value!r} is a naive datetime; provide a tz-aware ISO-8601 value"
        raise ValueError(msg)
    return parsed


def _validation_to_dict(record: ValidationRecord) -> dict[str, object]:
    return {
        "validation_id": str(record.validation_id),
        "registered_at": record.registered_at.isoformat(),
        "registered_by_session_id": record.registered_by_session_id,
        "edited_artifact": record.edited_artifact,
        "pre_edit_version": record.pre_edit_version,
        "post_edit_version": record.post_edit_version,
        "registered_regime": record.registered_regime,
        "registered_model_id": record.registered_model_id,
        "watched_metric_ids": [str(m) for m in record.watched_metric_ids],
        "window_length_days": record.window_length_days,
        "expected_direction": record.expected_direction.value,
        "expected_magnitude": record.expected_magnitude,
        "success_criterion": record.success_criterion,
        "failure_criterion": record.failure_criterion,
        "evaluation_due_at": record.evaluation_due_at.isoformat(),
    }


def _outcome_to_dict(outcome: ValidationOutcomeRecord) -> dict[str, object]:
    return {
        "outcome_id": str(outcome.outcome_id),
        "validation_id": str(outcome.validation_id),
        "evaluated_at": outcome.evaluated_at.isoformat(),
        "evaluated_by_session_id": outcome.evaluated_by_session_id,
        "verdict": outcome.verdict.value,
        "posterior_summary": outcome.posterior_summary,
        "confounder_notes": outcome.confounder_notes,
        "narrative": outcome.narrative,
        "rollback_status": outcome.rollback_status.value,
    }


def _result_to_dict(result: EvaluationResult) -> dict[str, object]:
    return {
        "superseded": result.superseded,
        "superseded_reason": (
            None if result.superseded_reason is None else result.superseded_reason.value
        ),
        "outcome": None if result.outcome is None else _outcome_to_dict(result.outcome),
    }


def _emit(payload: object) -> None:
    print(json.dumps(payload))


def _cmd_register(args: argparse.Namespace) -> int:
    payload = _read_payload(args.input)
    engine = make_engine(args.db_path)
    try:
        with make_session_factory(engine)() as session:
            validation_id = register_validation(
                session,
                validation_id=str(payload["validation_id"]),
                registering_invocation_id=_opt_str(payload.get("registering_invocation_id")),
                registered_at=_parse_dt(str(payload["registered_at"])),
                edited_artifact=str(payload["edited_artifact"]),
                pre_edit_version=str(payload["pre_edit_version"]),
                post_edit_version=str(payload["post_edit_version"]),
                watched_metric_ids=tuple(
                    MetricId(str(m)) for m in payload["watched_metric_ids"]  # type: ignore[union-attr]
                ),
                window_length_days=int(payload["window_length_days"]),  # type: ignore[call-overload]
                expected_direction=ExpectedDirection(str(payload["expected_direction"])),
                expected_magnitude=str(payload["expected_magnitude"]),
                success_criterion=str(payload["success_criterion"]),
                failure_criterion=str(payload["failure_criterion"]),
                session_id=_opt_str(payload.get("session_id")),
                registered_regime=_opt_str(payload.get("registered_regime")),
                registered_model_id=_opt_str(payload.get("registered_model_id")),
            )
            session.commit()
    finally:
        engine.dispose()
    _emit({"validation_id": str(validation_id)})
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    engine = make_engine(args.db_path)
    try:
        with make_session_factory(engine)() as session:
            pending = read_pending_validations(session)
    finally:
        engine.dispose()
    _emit({"pending": [_validation_to_dict(v) for v in pending]})
    return EXIT_OK


def _cmd_evaluate(args: argparse.Namespace) -> int:
    payload = _read_payload(args.input)
    judgments = EvaluationJudgments(
        confounder_flagged=bool(payload["confounder_flagged"]),
        failure_criterion_crossed=bool(payload["failure_criterion_crossed"]),
        confounder_notes=_opt_str(payload.get("confounder_notes")),
    )
    result = asyncio.run(
        _run_evaluate(
            db_path=args.db_path,
            validation_id=ValidationId(args.validation_id),
            outcome_id=OutcomeId(args.outcome_id),
            evaluated_at=_parse_dt(args.evaluated_at),
            judgments=judgments,
            narrative=str(payload["narrative"]),
            evaluated_by_session_id=_opt_str(payload.get("evaluated_by_session_id")),
        )
    )
    _emit(_result_to_dict(result))
    return EXIT_OK


async def _run_evaluate(
    *,
    db_path: str | None,
    validation_id: ValidationId,
    outcome_id: OutcomeId,
    evaluated_at: datetime,
    judgments: EvaluationJudgments,
    narrative: str,
    evaluated_by_session_id: str | None,
) -> EvaluationResult:
    engine = make_async_engine(db_path)
    try:
        async with make_async_session_factory(engine)() as session:
            result = await evaluate_validation(
                session,
                validation_id=validation_id,
                outcome_id=outcome_id,
                evaluated_at=evaluated_at,
                judgments=judgments,
                narrative=narrative,
                evaluated_by_session_id=evaluated_by_session_id,
            )
            await session.commit()
    finally:
        await engine.dispose()
    return result


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.feedback_loop.validation.cli",
        description="Headless register / evaluate / list surface for validation discipline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="Register a pre-registered validation.")
    register.add_argument("--db-path", default=None, metavar="PATH")
    register.add_argument(
        "--input",
        default="-",
        metavar="PATH",
        help="JSON registration payload (path, or '-' for stdin).",
    )
    register.set_defaults(handler=_cmd_register)

    evaluate = sub.add_parser("evaluate", help="Evaluate a registered validation.")
    evaluate.add_argument("--db-path", default=None, metavar="PATH")
    evaluate.add_argument("--validation-id", required=True, metavar="ID")
    evaluate.add_argument("--outcome-id", required=True, metavar="ID")
    evaluate.add_argument("--evaluated-at", required=True, metavar="ISO-8601")
    evaluate.add_argument(
        "--input",
        default="-",
        metavar="PATH",
        help="JSON judgments payload (path, or '-' for stdin).",
    )
    evaluate.set_defaults(handler=_cmd_evaluate)

    list_cmd = sub.add_parser("list", help="List pending (unevaluated, un-superseded) validations.")
    list_cmd.add_argument("--db-path", default=None, metavar="PATH")
    list_cmd.set_defaults(handler=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the validation CLI and return the operator-facing exit code."""
    args = _build_parser().parse_args(argv)
    try:
        handler = args.handler
        return int(handler(args))
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
