"""On-demand CLI entry point for the counterfactual replay engine (ALP-565, story 09).

Usage::

    python -m alphamind.execution.counterfactual_replay_engine.cli \\
        [--since <ISO-8601>] [--as-of <ISO-8601>] \\
        [--config-dir <path>] [--db-path <path>]

Opens a SQLAlchemy session, loads config, calls
:func:`~alphamind.execution.counterfactual_replay_engine.engine.replay_pending_proposals`,
and prints a structured summary to stdout.

Exit codes:

* ``0`` — success (including empty queue, which is a no-op, not an error)
* ``1`` — argument validation failure (naive datetime, missing config, etc.)
* ``2`` — infrastructure error raised by the batch driver
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["main"]

log = logging.getLogger(__name__)

# Ordered as they must appear in the summary output.  ``unsupported_bracket_type``
# is reserved and normally 0; it appears for enum completeness per the design.
_UNEVALUABLE_REASON_ORDER = [
    "unsupported_instrument",
    "unsupported_bracket_type",
    "data_missing",
    "corporate_action_in_window",
    "strategist_position_action_not_supported",
]

# Column position where the count digit starts (0-indexed from line start).
# Top-level lines: "  evaluated:                 12" → col 29.
# Reason sub-lines are indented 4 spaces; longest label is
# "    strategist_position_action_not_supported:" (46 chars), which gets 1 space.
_TOP_COUNT_COL = 29
_REASON_COUNT_COL = 32  # aligns short/medium reason names; long ones get 1 space


def _parse_aware_datetime(value: str, arg_name: str) -> datetime:
    """Parse *value* as an ISO-8601 datetime and reject naive datetimes.

    Returns a tz-aware :class:`datetime`. Raises :class:`argparse.ArgumentTypeError`
    with a clear message on any parse failure or on a naive (tz-less) input so
    argparse can surface it before we touch the database.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: {value!r} is not a valid ISO-8601 datetime. "
            "Provide a tz-aware value, e.g. 2026-06-01T14:00:00+00:00 or "
            "2026-06-01T14:00:00Z (replace Z with +00:00 for fromisoformat)."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: {value!r} is a naive datetime (no timezone offset). "
            "Provide a tz-aware value, e.g. 2026-06-01T14:00:00+00:00."
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.execution.counterfactual_replay_engine.cli",
        description=(
            "Replay pending PM-decision proposals whose evaluation horizon has elapsed "
            "and write one counterfactual record per (envelope_id, replay_kind) pair."
        ),
    )
    parser.add_argument(
        "--since",
        metavar="ISO-8601",
        default=None,
        help=(
            "Only replay PM decisions whose timestamp >= SINCE.  "
            "Must be a tz-aware ISO-8601 datetime (e.g. 2026-06-01T00:00:00+00:00)."
        ),
    )
    parser.add_argument(
        "--as-of",
        metavar="ISO-8601",
        default=None,
        dest="as_of",
        help=(
            "The 'now' timestamp used to gate evaluation-horizon-elapsed. "
            "Defaults to UTC now when omitted.  "
            "Must be a tz-aware ISO-8601 datetime."
        ),
    )
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        default="config/",
        dest="config_dir",
        help="Root directory for the config cascade (default: config/).",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        dest="db_path",
        help=(
            "Explicit SQLite database path.  When omitted the configured "
            "production path is resolved via make_engine's priority chain "
            "(DATABASE_PATH env → main.yaml paths.database) and a WARNING is "
            "logged."
        ),
    )
    return parser


def _format_summary(
    *,
    evaluated: int,
    unevaluable_by_reason: dict[str, int],
    skipped_not_due: int,
    skipped_idempotent: int,
    error_count: int,
) -> str:
    """Render the structured summary block.

    Column layout: label left-padded to a fixed width, count right-aligned
    at a fixed column so all digits align.

    Example::

        counterfactual replay summary
          evaluated:                 12
          unevaluable:               4
            unsupported_instrument:    1
            unsupported_bracket_type:  0
            data_missing:              2
            corporate_action_in_window: 1
            strategist_position_action_not_supported: 0
          skipped_not_due:           3
          skipped_idempotent:        8
          errors:                    0
          total processed:           24
    """
    unevaluable_total = sum(unevaluable_by_reason.get(r, 0) for r in _UNEVALUABLE_REASON_ORDER)
    total = evaluated + unevaluable_total + skipped_not_due + skipped_idempotent + error_count

    def _top_line(label: str, count: int) -> str:
        full_label = f"  {label}:"
        pad = max(1, _TOP_COUNT_COL - len(full_label))
        return f"{full_label}{' ' * pad}{count}"

    def _reason_line(label: str, count: int) -> str:
        full_label = f"    {label}:"
        pad = max(1, _REASON_COUNT_COL - len(full_label))
        return f"{full_label}{' ' * pad}{count}"

    lines: list[str] = ["counterfactual replay summary"]
    lines.append(_top_line("evaluated", evaluated))
    lines.append(_top_line("unevaluable", unevaluable_total))
    for reason in _UNEVALUABLE_REASON_ORDER:
        count = unevaluable_by_reason.get(reason, 0)
        lines.append(_reason_line(reason, count))
    lines.append(_top_line("skipped_not_due", skipped_not_due))
    lines.append(_top_line("skipped_idempotent", skipped_idempotent))
    lines.append(_top_line("errors", error_count))
    lines.append(_top_line("total processed", total))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns the exit code (0 = success, 1 = arg error, 2 = runtime)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = _build_parser()
    args = parser.parse_args(argv)

    # --- Validate datetime args ---
    since: datetime | None = None
    if args.since is not None:
        try:
            since = _parse_aware_datetime(args.since, "--since")
        except argparse.ArgumentTypeError as exc:
            print(str(exc))
            return 1

    as_of: datetime
    if args.as_of is not None:
        try:
            as_of = _parse_aware_datetime(args.as_of, "--as-of")
        except argparse.ArgumentTypeError as exc:
            print(str(exc))
            return 1
    else:
        as_of = datetime.now(UTC)

    # --- Warn on default DB path ---
    if args.db_path is None:
        log.warning(
            "No --db-path provided; falling back to the configured production "
            "database path (DATABASE_PATH env or main.yaml paths.database)."
        )

    # --- Load configs ---
    config_dir = Path(args.config_dir)
    try:
        from alphamind.config.loaders import read_yaml_file
        from alphamind.config.models.execution import ExecutionConfig
        from alphamind.execution.counterfactual_replay_engine.config import (
            load_replay_engine_config,
        )

        replay_config = load_replay_engine_config(config_dir)
        execution_config = ExecutionConfig.model_validate(
            read_yaml_file(config_dir / "execution.yaml")
        )
        paper_harness_config = execution_config.paper_harness
    except Exception:
        log.exception("Configuration load failed")
        return 2

    # --- Risk-free rate: read from macro_observations; fall back to a
    #     conservative default (matching the scheduler's _DEFAULT_RISK_FREE_RATE).
    risk_free_rate = _load_risk_free_rate(args.db_path)

    # --- Open session and run ---
    from alphamind.execution.counterfactual_replay_engine.engine import (
        replay_pending_proposals,
    )
    from alphamind.persistence.session import make_engine, make_session_factory

    try:
        engine = make_engine(args.db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            result = replay_pending_proposals(
                session,
                config=replay_config,
                paper_harness_config=paper_harness_config,
                risk_free_rate=risk_free_rate,
                as_of=as_of,
                since=since,
            )
    except Exception:
        log.exception("Replay engine failed")
        return 2

    # --- Print summary ---
    summary = _format_summary(
        evaluated=result.evaluated,
        unevaluable_by_reason=result.unevaluable_by_reason,
        skipped_not_due=result.skipped_not_due,
        skipped_idempotent=result.skipped_idempotent,
        error_count=result.error_count,
    )
    print(summary)

    if result.first_error:
        log.warning("First per-proposal error: %s", result.first_error)

    return 0


def _load_risk_free_rate(db_path: str | None) -> float:
    """Read the latest DTB3 rate from ``macro_observations``; fall back to 0.045.

    Mirrors the scheduler's :func:`_read_latest_risk_free_rate` but synchronous
    (the CLI uses the sync session). On any failure (empty table, missing table,
    connection error) the conservative mid-cycle scalar 0.045 is used.
    """
    default_rfr = 0.045
    dtb3_series_id = "DTB3"
    try:
        from sqlalchemy import select

        from alphamind.persistence.models import MacroObservations
        from alphamind.persistence.session import make_engine, make_session_factory

        engine = make_engine(db_path)
        factory = make_session_factory(engine)
        with factory() as session:
            stmt = (
                select(MacroObservations.value)
                .where(
                    MacroObservations.series_id == dtb3_series_id,
                    MacroObservations.value.is_not(None),
                )
                .order_by(MacroObservations.observation_date.desc())
                .limit(1)
            )
            value = session.execute(stmt).scalar_one_or_none()
        engine.dispose()
        if value is not None:
            return float(value) / 100.0
    except Exception as exc:
        log.warning(
            "Could not read risk-free rate from DB (%s); using default %.4f",
            exc,
            default_rfr,
        )
    return default_rfr


if __name__ == "__main__":
    sys.exit(main())
