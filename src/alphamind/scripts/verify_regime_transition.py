"""Regime state-machine integrity verifier (story 13).

Reads ``distillation_regime_state`` for the last
``--lookback-days N`` (default 7) and asserts the four invariants from
story 09's transition state machine:

1. Every consecutive label change records ``prior_label``.
2. Every ``confirmed`` row was preceded by at least
   ``regime_transition_confirmed_invocations - 1`` rows at the same label
   (the row count needed to cross the confirmation threshold per
   :func:`alphamind.distillation.regime.compute_transition_state`).
3. Every ``early-strong`` row carries
   ``indicator_agreement_count >= regime_transition_indicator_agreement_min``.
4. Every ``early-weak`` row carries
   ``indicator_agreement_count < regime_transition_indicator_agreement_min``.

The config thresholds are loaded from ``config/distillation.yaml`` so
operator customizations propagate without re-editing this script.

Exit codes:
- 0 — every invariant holds (or the lookback window is empty)
- 1 — at least one row violates an invariant
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.regime import RegimeLabel, TransitionState
from alphamind.persistence.models import DistillationRegimeState
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._common import AssertionFailure, load_distillation_config

DEFAULT_LOOKBACK_DAYS = 7
"""Lookback window in days. Default per story 13."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeRow:
    """In-memory snapshot of one ``distillation_regime_state`` row."""

    as_of: str
    regime_label: str
    transition_state: str
    prior_label: str
    indicator_agreement_count: int
    invocations_held: int


@dataclass(frozen=True)
class RegimeTransitionReport:
    passed: bool
    failures: tuple[AssertionFailure, ...]
    rows: tuple[RegimeRow, ...]
    lookback_days: int
    confirmed_invocations_threshold: int
    indicator_agreement_min: int


# ---------------------------------------------------------------------------
# Pure assertion logic
# ---------------------------------------------------------------------------


_VALID_STATES = frozenset(member.value for member in TransitionState)
_VALID_LABELS = frozenset(member.value for member in RegimeLabel)


def _vocabulary_failures(row: RegimeRow) -> list[AssertionFailure]:
    """Surface schema-level vocabulary violations (unknown label / state)."""
    failures: list[AssertionFailure] = []
    if row.regime_label not in _VALID_LABELS:
        failures.append(
            AssertionFailure(
                code="unknown-regime-label",
                message=(
                    f"row as_of={row.as_of} regime_label={row.regime_label!r} is "
                    f"not one of the four documented labels"
                ),
            )
        )
    if row.transition_state not in _VALID_STATES:
        failures.append(
            AssertionFailure(
                code="unknown-transition-state",
                message=(
                    f"row as_of={row.as_of} transition_state="
                    f"{row.transition_state!r} is not one of the four "
                    f"documented states"
                ),
            )
        )
    return failures


def _label_change_invariants(
    row: RegimeRow, *, previous: RegimeRow | None
) -> list[AssertionFailure]:
    """Invariant 1 — label change carries non-empty prior_label."""
    if previous is None or row.regime_label == previous.regime_label or row.prior_label:
        return []
    return [
        AssertionFailure(
            code="missing-prior-label-on-change",
            message=(
                f"row as_of={row.as_of}: label changed from "
                f"{previous.regime_label!r} to {row.regime_label!r} but "
                f"prior_label is empty"
            ),
        )
    ]


def _confirmed_invariants(
    rows: Sequence[RegimeRow],
    index: int,
    *,
    confirmed_invocations_threshold: int,
) -> list[AssertionFailure]:
    """Invariant 2 — confirmed requires the run length to cross the threshold."""
    row = rows[index]
    if row.transition_state != TransitionState.CONFIRMED.value:
        return []
    run_length = 1
    for back in range(index - 1, -1, -1):
        if rows[back].regime_label == row.regime_label:
            run_length += 1
        else:
            break
    if run_length >= confirmed_invocations_threshold:
        return []
    return [
        AssertionFailure(
            code="confirmed-without-prior-runlength",
            message=(
                f"row as_of={row.as_of}: confirmed at label "
                f"{row.regime_label!r} but run_length={run_length} < "
                f"required {confirmed_invocations_threshold}"
            ),
        )
    ]


def _early_state_invariants(
    row: RegimeRow, *, indicator_agreement_min: int
) -> list[AssertionFailure]:
    """Invariants 3 / 4 — early-strong vs. early-weak agreement bands."""
    if (
        row.transition_state == TransitionState.EARLY_STRONG.value
        and row.indicator_agreement_count < indicator_agreement_min
    ):
        return [
            AssertionFailure(
                code="early-strong-low-agreement",
                message=(
                    f"row as_of={row.as_of}: early-strong with "
                    f"indicator_agreement_count={row.indicator_agreement_count} "
                    f"< required {indicator_agreement_min}"
                ),
            )
        ]
    if (
        row.transition_state == TransitionState.EARLY_WEAK.value
        and row.indicator_agreement_count >= indicator_agreement_min
    ):
        return [
            AssertionFailure(
                code="early-weak-high-agreement",
                message=(
                    f"row as_of={row.as_of}: early-weak with "
                    f"indicator_agreement_count={row.indicator_agreement_count} "
                    f">= required {indicator_agreement_min}"
                ),
            )
        ]
    return []


def _check_invariants(
    rows: Sequence[RegimeRow],
    *,
    confirmed_invocations_threshold: int,
    indicator_agreement_min: int,
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    for index, row in enumerate(rows):
        previous = rows[index - 1] if index > 0 else None
        failures.extend(_vocabulary_failures(row))
        failures.extend(_label_change_invariants(row, previous=previous))
        failures.extend(
            _confirmed_invariants(
                rows,
                index,
                confirmed_invocations_threshold=confirmed_invocations_threshold,
            )
        )
        failures.extend(
            _early_state_invariants(row, indicator_agreement_min=indicator_agreement_min)
        )
    return failures


def _load_regime_rows(
    session: Session,
    *,
    now: datetime,
    lookback_days: int,
) -> tuple[RegimeRow, ...]:
    """Read regime-state rows from the lookback window, ordered oldest first.

    The lookback filter parses each row's ``as_of`` and compares it as a
    proper datetime rather than a lexicographic prefix string. SQL
    string comparison only works correctly when every writer emits the
    same suffix (``Z`` vs. ``+00:00``); parsing post-fetch keeps the
    filter robust to mixed timezone-suffix conventions across writers.
    """
    cutoff = now - timedelta(days=lookback_days)
    stmt = (
        select(
            DistillationRegimeState.as_of,
            DistillationRegimeState.regime_label,
            DistillationRegimeState.transition_state,
            DistillationRegimeState.prior_label,
            DistillationRegimeState.indicator_agreement_count,
            DistillationRegimeState.invocations_held,
        )
        .order_by(DistillationRegimeState.as_of.asc())
    )
    rows = session.execute(stmt).all()
    selected: list[RegimeRow] = []
    for as_of, label, state, prior, agreement, held in rows:
        if _parse_as_of(as_of) < cutoff:
            continue
        selected.append(
            RegimeRow(
                as_of=as_of,
                regime_label=label,
                transition_state=state,
                prior_label=prior or "",
                indicator_agreement_count=agreement,
                invocations_held=held,
            )
        )
    return tuple(selected)


def _parse_as_of(as_of: str) -> datetime:
    """Parse a regime-row ``as_of`` ISO 8601 string into a tz-aware datetime.

    Accepts the canonical ``Z`` suffix as well as offset suffixes
    (``+00:00``, ``-05:00``) so the verification script does not silently
    drop rows when a writer's serialization changes.
    """
    text = as_of[:-1] + "+00:00" if as_of.endswith("Z") else as_of
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def compute_regime_transition_report(
    *,
    session: Session,
    now: datetime,
    lookback_days: int,
    config: DistillationConfig | None = None,
) -> RegimeTransitionReport:
    """Build the regime-transition report.

    The ``config`` parameter lets tests inject custom thresholds; when
    omitted the script reads ``config/distillation.yaml``.
    """
    if config is None:
        config = load_distillation_config()
    confirmed_invocations_threshold = (
        config.regime_transition.regime_transition_confirmed_invocations
    )
    indicator_agreement_min = config.regime_transition.regime_transition_indicator_agreement_min
    rows = _load_regime_rows(session, now=now, lookback_days=lookback_days)
    failures = _check_invariants(
        rows,
        confirmed_invocations_threshold=confirmed_invocations_threshold,
        indicator_agreement_min=indicator_agreement_min,
    )
    return RegimeTransitionReport(
        passed=not failures,
        failures=tuple(failures),
        rows=rows,
        lookback_days=lookback_days,
        confirmed_invocations_threshold=confirmed_invocations_threshold,
        indicator_agreement_min=indicator_agreement_min,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def format_regime_transition_report(report: RegimeTransitionReport) -> str:
    lines: list[str] = []
    banner = "=" * 70
    lines.append(banner)
    lines.append("AlphaMind Regime Transition Verification")
    lines.append(banner)
    lines.append("")
    lines.append(
        f"Lookback: {report.lookback_days} days  "
        f"confirmed_threshold={report.confirmed_invocations_threshold}  "
        f"agreement_min={report.indicator_agreement_min}"
    )

    lines.append("")
    lines.append("[ Trajectory ]")
    if not report.rows:
        lines.append("  (empty lookback window — no rows to verify)")
    else:
        for row in report.rows:
            lines.append(
                f"  {row.as_of}  {row.regime_label:<22} "
                f"{row.transition_state:<14} "
                f"prior={row.prior_label or '(none)':<22} "
                f"agreement={row.indicator_agreement_count} "
                f"held={row.invocations_held}"
            )

    transition_events = [
        row for row in report.rows if row.transition_state != TransitionState.STABLE.value
    ]
    if transition_events:
        lines.append("")
        lines.append("[ Transition events ]")
        for row in transition_events:
            lines.append(
                f"  {row.as_of}  {row.transition_state:<14} -> "
                f"{row.regime_label}  (agreement={row.indicator_agreement_count})"
            )

    if report.failures:
        lines.append("")
        lines.append("[ Failures ]")
        for failure in report.failures:
            lines.append(f"  {failure.code}: {failure.message}")

    lines.append("")
    lines.append(banner)
    if report.passed:
        lines.append("RESULT: PASS — every regime invariant holds.")
    else:
        lines.append(f"RESULT: FAIL — {len(report.failures)} regime invariant violation(s).")
    lines.append(banner)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify regime-state-machine invariants over the most recent "
            "lookback window (story 13)."
        )
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to the AlphaMind SQLite database.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback window in days (default {DEFAULT_LOOKBACK_DAYS}).",
    )
    args = parser.parse_args(argv)

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)
    now = datetime.now(tz=UTC)
    with factory() as session:
        report = compute_regime_transition_report(
            session=session,
            now=now,
            lookback_days=args.lookback_days,
        )
    print(format_regime_transition_report(report))
    return 0 if report.passed else 1


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "AssertionFailure",
    "RegimeRow",
    "RegimeTransitionReport",
    "compute_regime_transition_report",
    "format_regime_transition_report",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
