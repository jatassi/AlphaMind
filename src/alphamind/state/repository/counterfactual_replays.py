"""Repository helpers for the ``counterfactual_replays`` table (ALP-556).

Two helpers expose the read / write surface consumed by the replay engine driver
(story 08):

* :func:`insert_counterfactual_replay` — encode + queue one record for insert.
  The ``(pm_decision_envelope_id, replay_kind)`` UniqueConstraint raises
  ``IntegrityError`` at flush / commit (not at the ``add`` call) on a
  duplicate, so the engine's idempotency check is the caller's responsibility.

* :func:`load_counterfactual_replays_for_envelope` — return all replay records
  for a given envelope, ordered by ``replay_kind``. Used by story 08 to detect
  whether a ``(envelope, kind)`` pair has already been computed before running
  the replay.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.ids import EnvelopeId
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
)
from alphamind.state.tables.counterfactual_replays import CounterfactualReplays
from alphamind.state.tables.counterfactual_replays_codec import (
    decode_counterfactual_replay,
    encode_counterfactual_replay,
)


def insert_counterfactual_replay(
    session: Session,
    record: CounterfactualReplayRecord,
) -> None:
    """Encode and queue a ``CounterfactualReplayRecord`` for insert.

    Adds the row to *session*; the ``(pm_decision_envelope_id, replay_kind)``
    UniqueConstraint is enforced when the unit of work flushes, so a duplicate
    surfaces as :class:`sqlalchemy.exc.IntegrityError` at the next ``flush`` /
    ``commit`` (or autoflush) — not at this call. The engine driver is
    responsible for idempotency checking before calling this helper.
    """
    row = CounterfactualReplays(**encode_counterfactual_replay(record))
    session.add(row)


def load_counterfactual_replays_for_envelope(
    session: Session,
    envelope_id: EnvelopeId,
) -> tuple[CounterfactualReplayRecord, ...]:
    """Return all replay records for *envelope_id*, ordered by ``replay_kind``.

    Returns an empty tuple when no records exist for the envelope. Used by
    story 08 to determine which ``(envelope, kind)`` pairs are already
    persisted before computing new replays.
    """
    stmt = (
        select(CounterfactualReplays)
        .where(CounterfactualReplays.pm_decision_envelope_id == str(envelope_id))
        .order_by(CounterfactualReplays.replay_kind)
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(decode_counterfactual_replay(row) for row in rows)


__all__ = [
    "insert_counterfactual_replay",
    "load_counterfactual_replays_for_envelope",
]
