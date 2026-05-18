"""Brief-store read helper (story ALP-518).

Cross-process consumers (replay harness, command-center diagnostic) call
:func:`load_brief` to hydrate a brief written to the persistent store by
Phase 7 of the distillation orchestrator. The hot path inside one
process still reads ``DistillationOutputs.correlation_regime_brief`` —
this helper exists for the cross-process case the in-process passthrough
cannot serve.

The persisted shape (per ``docs/architecture/data-and-state.md`` § Brief
store) is one row per ``(invocation_id, brief_kind)``; the rendered body
travels in ``text`` and the ``CR-N`` → block_id map serializes as JSON
in ``reference_index_json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.persistence.models import Brief


@dataclass(frozen=True, slots=True)
class StoredBrief:
    """A brief reconstructed from the ``briefs`` table.

    Mirrors the columns of :class:`alphamind.persistence.models.Brief`
    with ``reference_index_json`` already deserialized into the
    ``CR-N`` → block_id mapping.
    """

    invocation_id: str
    brief_kind: str
    text: str
    reference_index: dict[str, str]
    created_at: str


def load_brief(session: Session, *, invocation_id: str, brief_kind: str) -> StoredBrief | None:
    """Fetch the brief at ``(invocation_id, brief_kind)`` if present.

    Returns ``None`` when no row matches; callers decide whether that is
    an error (replay harness against an invocation that predates the
    brief-store schema) or expected (diagnostic on an invocation that
    skipped distillation).
    """
    row = session.execute(
        select(Brief).where(
            Brief.invocation_id == invocation_id,
            Brief.brief_kind == brief_kind,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return StoredBrief(
        invocation_id=row.invocation_id,
        brief_kind=row.brief_kind,
        text=row.text,
        reference_index=json.loads(row.reference_index_json),
        created_at=row.created_at,
    )


__all__ = ["StoredBrief", "load_brief"]
