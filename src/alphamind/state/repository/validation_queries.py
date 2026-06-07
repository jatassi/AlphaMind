"""Read/write helpers for the ``validations`` and ``validation_outcomes``
tables (ALP-874 / story 02b).

Kept in its own module (not ``sql_repository.py``) to avoid wave-collision
with sibling story 02a/02d table work. All helpers use a synchronous
``Session`` (matching the ``counterfactual_replays`` pattern); async helpers
are not needed at this tier.

Helpers:
* :func:`insert_validation` — encode and queue a ``ValidationRecord`` for
  insert.
* :func:`read_validation` — fetch a single ``ValidationRecord`` by ID.
* :func:`read_pending_validations` — return validations that have no outcome
  and are not superseded.
* :func:`mark_validation_superseded` — write the supersession fields on an
  existing row.
* :func:`insert_validation_outcome` — encode and queue a
  ``ValidationOutcomeRecord`` for insert.
* :func:`read_outcomes_by_artifact` — return outcomes for a given
  ``edited_artifact``, ordered most-recent-first.
* :func:`derive_validation_status` — derive the status string for a
  validation row (``pending`` / ``superseded`` / ``evaluated``) without a
  join, given whether an outcome exists.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.feedback_loop.validation.records import (
    SupersededReason,
    ValidationId,
    ValidationOutcomeRecord,
    ValidationRecord,
)
from alphamind.state.tables.validation_outcomes import ValidationOutcomesRow
from alphamind.state.tables.validation_outcomes_codec import (
    record_to_row as outcome_record_to_row,
)
from alphamind.state.tables.validation_outcomes_codec import (
    row_to_record as outcome_row_to_record,
)
from alphamind.state.tables.validations import ValidationsRow
from alphamind.state.tables.validations_codec import (
    record_to_row as validation_record_to_row,
)
from alphamind.state.tables.validations_codec import (
    row_to_record as validation_row_to_record,
)

# ---------------------------------------------------------------------------
# Derived-status helper
# ---------------------------------------------------------------------------


def derive_validation_status(
    row: ValidationsRow,
    *,
    has_outcome: bool,
) -> str:
    """Derive the status string for a ``ValidationsRow``.

    * ``"superseded"`` — ``superseded_at`` is non-null.
    * ``"evaluated"`` — an outcome record exists (``has_outcome=True``).
    * ``"pending"`` — neither condition holds.

    Design note (state-persistence.md § Validations): status is derived at
    read time, not persisted, to avoid a second column that could drift from
    the FK join truth.
    """
    if row.superseded_at is not None:
        return "superseded"
    if has_outcome:
        return "evaluated"
    return "pending"


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def insert_validation(
    session: Session,
    record: ValidationRecord,
) -> None:
    """Encode and queue a ``ValidationRecord`` for insert.

    Adds the row to *session*; the primary-key uniqueness constraint is
    enforced when the unit of work flushes — duplicate ``validation_id``
    surfaces as :class:`sqlalchemy.exc.IntegrityError` at the next
    ``flush`` / ``commit``.
    """
    session.add(validation_record_to_row(record))


def mark_validation_superseded(
    session: Session,
    validation_id: ValidationId,
    reason: SupersededReason,
    at: datetime,
) -> None:
    """Write the supersession fields on an existing ``ValidationsRow``.

    Fetches the row by ``validation_id`` and sets ``superseded_at`` and
    ``superseded_reason`` in-place. The mutation is queued for the next
    flush; the caller owns the commit boundary.

    Raises :class:`ValueError` when no row with the given ID exists.
    """
    row = session.get(ValidationsRow, str(validation_id))
    if row is None:
        msg = f"no validation row found for id={validation_id!r}"
        raise ValueError(msg)
    row.superseded_at = at.isoformat().replace("+00:00", "Z")
    row.superseded_reason = reason.value


def insert_validation_outcome(
    session: Session,
    record: ValidationOutcomeRecord,
) -> None:
    """Encode and queue a ``ValidationOutcomeRecord`` for insert.

    The ``uq_validation_outcomes_validation_id`` unique constraint enforces
    one outcome per validation at the SQL layer; a second insert for the
    same ``validation_id`` surfaces as :class:`sqlalchemy.exc.IntegrityError`
    at the next ``flush`` / ``commit``.
    """
    session.add(outcome_record_to_row(record))


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def read_validation(
    session: Session,
    validation_id: ValidationId,
) -> ValidationRecord | None:
    """Return the ``ValidationRecord`` for *validation_id*, or ``None``."""
    row = session.get(ValidationsRow, str(validation_id))
    if row is None:
        return None
    return validation_row_to_record(row)


def read_pending_validations(
    session: Session,
) -> tuple[ValidationRecord, ...]:
    """Return validations that have no outcome and are not superseded.

    A validation is pending when:
    * ``superseded_at`` is NULL (not superseded), AND
    * no ``ValidationOutcomesRow`` joins to it (not yet evaluated).

    Results are ordered by ``registered_at`` ascending so callers get the
    oldest-first window for evaluation scheduling.
    """
    # Subquery: validation_ids that already have an outcome.
    evaluated_ids_subq = select(ValidationOutcomesRow.validation_id).subquery()

    stmt = (
        select(ValidationsRow)
        .where(
            ValidationsRow.superseded_at.is_(None),
            ValidationsRow.validation_id.not_in(select(evaluated_ids_subq.c.validation_id)),
        )
        .order_by(ValidationsRow.registered_at.asc())
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(validation_row_to_record(row) for row in rows)


def read_outcomes_by_artifact(
    session: Session,
    edited_artifact: str,
) -> tuple[ValidationOutcomeRecord, ...]:
    """Return outcomes for a given ``edited_artifact``, most-recent-first.

    Joins ``validation_outcomes`` → ``validations`` on ``validation_id`` to
    filter by ``edited_artifact``, then orders by ``evaluated_at`` descending
    so the most recent evaluation is first. Used by the validation-discipline
    logic for the rollback-status prior-outcome lookup.
    """
    stmt = (
        select(ValidationOutcomesRow)
        .join(
            ValidationsRow,
            ValidationOutcomesRow.validation_id == ValidationsRow.validation_id,
        )
        .where(ValidationsRow.edited_artifact == edited_artifact)
        .order_by(
            ValidationOutcomesRow.evaluated_at.desc(),
            ValidationOutcomesRow.outcome_id.desc(),
        )
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(outcome_row_to_record(row) for row in rows)


__all__ = [
    "derive_validation_status",
    "insert_validation",
    "insert_validation_outcome",
    "mark_validation_superseded",
    "read_outcomes_by_artifact",
    "read_pending_validations",
    "read_validation",
]
