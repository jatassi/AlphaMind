"""Validation REGISTER shell — provenance snapshot + criteria freeze (ALP-889).

``register_validation`` is the imperative shell that captures the pre-registered
operator contract at REGISTER time:

* snapshots ``registered_regime`` from the registering invocation's
  ``active_regime`` and ``registered_model_id`` from that invocation's
  ``agent_calls`` provenance,
* computes ``evaluation_due_at = registered_at + window_length_days``,
* freezes the success/failure criteria + ``watched_metric_ids``,
* writes the :class:`~alphamind.feedback_loop.validation.records.ValidationRecord`
  via the 02b repository helper.

The paired post-rollback path (EVALUATE step 10) seeds ``registered_regime`` /
``registered_model_id`` directly rather than looking them up, so a fresh
registration following a ``mandatory_clean_failure`` does not require a live
registering invocation. ``session_id`` is nullable — ALP-686 supplies it later.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    ValidationId,
    ValidationRecord,
)
from alphamind.state.repository.validation_queries import insert_validation
from alphamind.state.tables.agent_calls import AgentCallsRow
from alphamind.state.tables.invocations import InvocationRow


class ProvenanceLookupError(Exception):
    """Raised when the registering invocation's provenance cannot be resolved.

    Either the invocation row is absent, or it has no ``agent_calls`` from which
    to snapshot the model id. Registration cannot freeze a faithful conditioning
    context without both, so it fails closed rather than persisting a partial
    snapshot.
    """


def _snapshot_provenance(
    session: Session,
    registering_invocation_id: str,
) -> tuple[str, str]:
    """Return ``(active_regime, model_id)`` snapshotted from the invocation.

    Reads ``invocations.active_regime`` and the earliest ``agent_calls.model_id``
    (by attempt then call id) for the registering invocation. Raises
    :class:`ProvenanceLookupError` when the invocation is missing or has no
    agent calls.
    """
    invocation = session.get(InvocationRow, registering_invocation_id)
    if invocation is None:
        msg = f"no invocation row found for id={registering_invocation_id!r}"
        raise ProvenanceLookupError(msg)
    model_id = session.execute(
        select(AgentCallsRow.model_id)
        .where(AgentCallsRow.invocation_id == registering_invocation_id)
        .order_by(AgentCallsRow.attempt_number.asc(), AgentCallsRow.agent_call_id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if model_id is None:
        msg = f"invocation id={registering_invocation_id!r} has no agent_calls to snapshot a model id from"
        raise ProvenanceLookupError(msg)
    return invocation.active_regime, model_id


def register_validation(
    session: Session,
    *,
    validation_id: str,
    registering_invocation_id: str | None,
    registered_at: datetime,
    edited_artifact: str,
    pre_edit_version: str,
    post_edit_version: str,
    watched_metric_ids: tuple[MetricId, ...],
    window_length_days: int,
    expected_direction: ExpectedDirection,
    expected_magnitude: str,
    success_criterion: str,
    failure_criterion: str,
    session_id: str | None = None,
    registered_regime: str | None = None,
    registered_model_id: str | None = None,
) -> ValidationId:
    """Snapshot provenance, freeze the contract, and persist the validation.

    Provenance: when ``registered_regime`` / ``registered_model_id`` are both
    supplied (the seeded post-rollback path) they are used verbatim; otherwise
    they are snapshotted from ``registering_invocation_id`` — which must then be
    non-``None`` and resolvable, or :class:`ProvenanceLookupError` is raised.

    The validation is queued on *session* for insert; the caller owns the commit
    boundary. Returns the ``ValidationId``.
    """
    if registered_regime is None or registered_model_id is None:
        if registering_invocation_id is None:
            msg = (
                "register_validation requires either a registering_invocation_id "
                "to snapshot provenance from, or both registered_regime and "
                "registered_model_id seeded directly"
            )
            raise ProvenanceLookupError(msg)
        registered_regime, registered_model_id = _snapshot_provenance(
            session, registering_invocation_id
        )

    record = ValidationRecord(
        validation_id=ValidationId(validation_id),
        registered_at=registered_at,
        registered_by_session_id=session_id,
        edited_artifact=edited_artifact,
        pre_edit_version=pre_edit_version,
        post_edit_version=post_edit_version,
        registered_regime=registered_regime,
        registered_model_id=registered_model_id,
        watched_metric_ids=watched_metric_ids,
        window_length_days=window_length_days,
        expected_direction=expected_direction,
        expected_magnitude=expected_magnitude,
        success_criterion=success_criterion,
        failure_criterion=failure_criterion,
        evaluation_due_at=registered_at + timedelta(days=window_length_days),
        superseded_at=None,
        superseded_reason=None,
    )
    insert_validation(session, record)
    return record.validation_id


__all__ = [
    "ProvenanceLookupError",
    "register_validation",
]
