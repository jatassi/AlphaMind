"""Forward-only persistence for ``RegimeAdaptationState`` (story 04a).

Mirrors the sibling distillation pattern in
``alphamind.distillation.regime``: an indexed ``order by as_of desc limit 1``
read accessor, a single ``insert`` writer wrapped in the framework's
fail-closed transaction helper, and pure adapters between the typed record
and the ORM row.

The persistence story is intentionally thin: the orchestrator (story 09)
reads the most recent row to reconstruct the loosening interpolation
context (``transition_started_invocation_id`` plus
``transition_origin_regime``) and appends a new row at the end of the
invocation. The schema CHECK constraints land in the migration so a future
direct-SQL writer (e.g. a backfill script) faces the same invariants the
typed-record ``__post_init__`` enforces.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.risk_guardrails.regime_adaptation.types import (
    RegimeAdaptationState,
    overlays_to_strings,
)

if TYPE_CHECKING:
    # ``RegimeAdaptationStateRow`` lives in ``persistence.models``, which
    # imports ``RegimeTransitionState`` (re-exported from
    # ``portfolio_state.records.capital``). Eager loading here would re-enter
    # ``persistence.models`` mid-load through that chain. The annotation-only
    # appearance is safe under ``from __future__ import annotations``;
    # construction sites import lazily.
    from alphamind.persistence.models import RegimeAdaptationStateRow


def state_to_row(
    state: RegimeAdaptationState,
    *,
    ingested_at: str,
) -> RegimeAdaptationStateRow:
    """Adapter — typed record to ORM row. Pure.

    Serializes ``active_overlays`` to an alphabetically-sorted comma-separated
    string. The empty tuple maps to the empty string. The encoding is
    bijective with the typed record because ``row_to_state`` re-sorts on
    read; equality comparisons across persistence cycles are stable.
    """
    # Lazy import — see TYPE_CHECKING block at top of module for the cycle rationale.
    from alphamind.persistence.models import RegimeAdaptationStateRow

    overlays_csv = ",".join(overlays_to_strings(state.active_overlays))
    prior_regime = state.prior_regime.value if state.prior_regime is not None else None
    transition_origin_regime = (
        state.transition_origin_regime.value if state.transition_origin_regime is not None else None
    )
    return RegimeAdaptationStateRow(
        as_of=state.as_of,
        invocation_id=state.invocation_id,
        active_regime=state.active_regime.value,
        prior_regime=prior_regime,
        transition_state=state.transition_state.value,
        transition_invocations_remaining=state.transition_invocations_remaining,
        transition_started_invocation_id=state.transition_started_invocation_id,
        transition_origin_regime=transition_origin_regime,
        active_overlays_csv=overlays_csv,
        distillation_regime_label=state.distillation_regime_label,
        distillation_vix_level=state.distillation_vix_level,
        regime_skip_emergency=1 if state.regime_skip_emergency else 0,
        ingested_at=ingested_at,
    )


def row_to_state(row: RegimeAdaptationStateRow) -> RegimeAdaptationState:
    """Adapter — ORM row to typed record. Pure.

    Validates every vocabulary-bound column resolves to a valid enum
    member and raises ``ValueError`` naming the offending field and value
    on a mismatch. The CHECK constraints landed in the migration prevent
    this on the write path; the read-side check defends against
    direct-SQL inserts that bypass the ORM (e.g. a backfill script).
    """
    active_regime = _parse_enum(Regime, row.active_regime, field="active_regime")
    prior_regime = (
        _parse_enum(Regime, row.prior_regime, field="prior_regime")
        if row.prior_regime is not None
        else None
    )
    transition_origin_regime = (
        _parse_enum(Regime, row.transition_origin_regime, field="transition_origin_regime")
        if row.transition_origin_regime is not None
        else None
    )
    transition_state = _parse_enum(
        RegimeTransitionState, row.transition_state, field="transition_state"
    )
    active_overlays = _parse_overlays_csv(row.active_overlays_csv)
    return RegimeAdaptationState(
        as_of=row.as_of,
        invocation_id=row.invocation_id,
        active_regime=active_regime,
        prior_regime=prior_regime,
        transition_state=transition_state,
        transition_invocations_remaining=row.transition_invocations_remaining,
        transition_started_invocation_id=row.transition_started_invocation_id,
        transition_origin_regime=transition_origin_regime,
        active_overlays=active_overlays,
        distillation_regime_label=row.distillation_regime_label,
        distillation_vix_level=row.distillation_vix_level,
        regime_skip_emergency=bool(row.regime_skip_emergency),
    )


def select_most_recent_state(session: Session) -> RegimeAdaptationState | None:
    """Return the most recently persisted state, or ``None`` on bootstrap.

    Uses the indexed ``order by as_of desc limit 1`` read pattern. The
    bootstrap case (no prior row) returns ``None`` rather than raising —
    the orchestrator distinguishes "first ever invocation" from a
    populated state.
    """
    # Lazy import — see TYPE_CHECKING block at top of module for the cycle rationale.
    from alphamind.persistence.models import RegimeAdaptationStateRow

    row = session.execute(
        select(RegimeAdaptationStateRow).order_by(RegimeAdaptationStateRow.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return row_to_state(row)


def insert_state(
    session: Session,
    state: RegimeAdaptationState,
    *,
    ingested_at: str,
) -> None:
    """Append ``state`` as a new ``regime_adaptation_state`` row.

    Wraps the ``session.add`` + commit cycle in the framework's fail-closed
    transaction helper so a partial write rolls back. Raises
    ``IntegrityError`` if a row with the same ``as_of`` already exists —
    the orchestrator must not double-write within an invocation.
    """
    # Lazy import — ``distillation.baselines`` transitively pulls
    # ``persistence.models``, which back-references this package via
    # ``RegimeTransitionState``. Module-load deferral keeps the chain acyclic.
    from alphamind.distillation.baselines import _refresh_transaction

    row = state_to_row(state, ingested_at=ingested_at)
    with _refresh_transaction(session):
        session.add(row)


# ---------------------------------------------------------------------------
# Vocabulary parsing helpers
# ---------------------------------------------------------------------------


def _parse_enum[E: Enum](enum_cls: type[E], value: str, *, field: str) -> E:
    """Coerce ``value`` to a member of ``enum_cls`` or raise ``ValueError``.

    Defends the read path against direct-SQL inserts that bypass the schema's
    CHECK constraints (e.g. a future backfill script). The error message
    names the offending field and value so the caller can pin the corruption.
    """
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = tuple(m.value for m in enum_cls)
        msg = f"invalid {field}={value!r}; expected one of {valid}"
        raise ValueError(msg) from exc


def _parse_overlays_csv(csv: str) -> tuple[Overlay, ...]:
    """Parse the comma-separated overlay column into a sorted ``Overlay`` tuple.

    Empty string maps to the empty tuple. Each non-empty token must match an
    ``Overlay`` enum value or this raises ``ValueError`` naming the offending
    token. The returned tuple is sorted by enum value to mirror the
    write-side encoding produced by ``overlays_to_strings``.
    """
    if not csv:
        return ()
    overlays: list[Overlay] = []
    for token in csv.split(","):
        try:
            overlays.append(Overlay(token))
        except ValueError as exc:
            msg = (
                f"invalid active_overlays_csv token={token!r}; "
                f"expected one of {tuple(m.value for m in Overlay)}"
            )
            raise ValueError(msg) from exc
    return tuple(sorted(overlays, key=lambda overlay: overlay.value))
