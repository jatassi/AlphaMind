"""Writer for UNEVALUABLE counterfactual replay records (ALP-558).

:func:`write_unevaluable_record` constructs an UNEVALUABLE
:class:`~.records.CounterfactualReplayRecord`, inserts it via
:func:`~alphamind.state.repository.counterfactual_replays.insert_counterfactual_replay`,
and returns the record. Used by the engine driver (story 08) for proposals
that fail the Step-1 eligibility check.

The ``(pm_decision_envelope_id, replay_kind)`` unique constraint is enforced
at flush time; a duplicate surfaces as :class:`sqlalchemy.exc.IntegrityError`
rather than a silent overwrite — the caller is responsible for idempotency
checking before invoking this function.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind.execution.counterfactual_replay_engine.enums import (
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
)
from alphamind.state.repository.counterfactual_replays import (
    insert_counterfactual_replay,
)

__all__ = ["write_unevaluable_record"]


def write_unevaluable_record(
    session: Session,
    envelope_id: EnvelopeId,
    replay_kind: ReplayKind,
    reason: UnevaluableReason,
    *,
    replay_engine_version: str = "v2",
) -> CounterfactualReplayRecord:
    """Construct and insert an UNEVALUABLE :class:`CounterfactualReplayRecord`.

    Parameters
    ----------
    session:
        SQLAlchemy session; the row is ``add``-ed to the session but not yet
        flushed / committed.
    envelope_id:
        The PM-decision envelope whose proposal is unevaluable.
    replay_kind:
        Whether this replay is for a rejection or a modification's original form.
    reason:
        Why the proposal could not be evaluated.
    replay_engine_version:
        Engine version tag persisted for provenance. Defaults to ``"v2"``.

    Returns
    -------
    CounterfactualReplayRecord
        The freshly-constructed and queued record. The
        ``(pm_decision_envelope_id, replay_kind)`` unique constraint is
        enforced at flush time — a duplicate raises
        :class:`sqlalchemy.exc.IntegrityError`.

    Notes
    -----
    The data window computed by :func:`check_eligibility` (Rules C / D) is
    intentionally *not* persisted: the record's ``__post_init__`` requires
    ``replay_data_window_*`` to be ``None`` for UNEVALUABLE status.
    """
    replay_id = ReplayId(f"CFR-{envelope_id}-{replay_kind.value}")
    record = CounterfactualReplayRecord(
        replay_id=replay_id,
        pm_decision_envelope_id=envelope_id,
        replay_kind=replay_kind,
        replay_status=ReplayStatus.UNEVALUABLE,
        unevaluable_reason=reason,
        entered=None,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=None,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        confidence=None,
        replay_timestamp=datetime.now(UTC),
        replay_data_window_start=None,
        replay_data_window_end=None,
        replay_engine_version=replay_engine_version,
    )
    insert_counterfactual_replay(session, record)
    return record
