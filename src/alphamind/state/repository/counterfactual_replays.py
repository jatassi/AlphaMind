"""Repository helpers for the ``counterfactual_replays`` table (ALP-556).

Two helpers expose the read / write surface consumed by the replay engine driver
(story 08):

* :func:`insert_counterfactual_replay` — encode + insert one record; raises
  ``IntegrityError`` on a duplicate ``(pm_decision_envelope_id, replay_kind)``
  pair so the engine's idempotency check is the caller's responsibility.

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
    """Encode and insert a ``CounterfactualReplayRecord``.

    Raises :class:`sqlalchemy.exc.IntegrityError` on a
    ``(pm_decision_envelope_id, replay_kind)`` collision — the table
    enforces one-record-per-(envelope, kind). The engine driver is
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
