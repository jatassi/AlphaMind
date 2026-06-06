"""Hydrate originating proposal from PMDecisionDetail (ALP-558).

:func:`hydrate_originating_proposal` reconstructs the typed proposal model
from the ``originating_proposal_json`` field on a ``PMDecisionDetail``,
dispatching on ``source_provenance_json`` keys placed there by ALP-557.

Raises :class:`ProposalHydrationError` on shape failures — a JSON body that
fails Pydantic validation is an upstream contract violation (story 03
guarantees the body was a valid model at write time), not a normal "skip this
replay" condition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from alphamind.decision.analyst.models import Recommendation
from alphamind.decision.strategist.models import PendingOrderAssessment, PositionAssessment

if TYPE_CHECKING:
    from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail

__all__ = [
    "ProposalHydrationError",
    "hydrate_originating_proposal",
]


class ProposalHydrationError(Exception):
    """Raised when ``originating_proposal_json`` cannot be hydrated.

    This is an upstream contract violation — the body was valid at write time
    (story 03) so a failure here means the JSON was corrupted or the dispatch
    key is unexpected. It is not a "skip this replay" condition.
    """


def hydrate_originating_proposal(
    detail: PMDecisionDetail,
) -> Recommendation | PositionAssessment | PendingOrderAssessment:
    """Reconstruct the originating proposal from *detail*.

    Dispatches on ``detail.source_provenance_json["source_provenance"]``:

    * ``"pm_analyst"`` — :class:`~alphamind.decision.analyst.models.Recommendation`
    * ``"pm_strategist"`` — further dispatches on
      ``detail.source_provenance_json["recommendation_type"]``:

      * ``"position_assessment"`` →
        :class:`~alphamind.decision.strategist.models.PositionAssessment`
      * ``"pending_order_assessment"`` →
        :class:`~alphamind.decision.strategist.models.PendingOrderAssessment`

    Raises :class:`ProposalHydrationError` if the ``source_provenance`` or
    ``recommendation_type`` key is missing/unrecognised, or if the JSON body
    fails Pydantic validation.
    """
    provenance = detail.source_provenance_json
    source = provenance.get("source_provenance")

    try:
        if source == "pm_analyst":
            return _hydrate_analyst(detail.originating_proposal_json)
        elif source == "pm_strategist":
            return _hydrate_strategist(provenance, detail.originating_proposal_json)
        else:
            raise ProposalHydrationError(
                f"Unknown source_provenance {source!r} in source_provenance_json; "
                f"expected 'pm_analyst' or 'pm_strategist'."
            )
    except ProposalHydrationError:
        raise
    except Exception as exc:
        raise ProposalHydrationError(
            f"Failed to hydrate originating proposal: {exc}"
        ) from exc


def _hydrate_analyst(body: dict[str, Any]) -> Recommendation:
    try:
        return Recommendation.model_validate(body)
    except ValidationError as exc:
        raise ProposalHydrationError(
            f"pm_analyst originating_proposal_json failed Recommendation validation: {exc}"
        ) from exc


def _hydrate_strategist(
    provenance: dict[str, Any],
    body: dict[str, Any],
) -> PositionAssessment | PendingOrderAssessment:
    rec_type = provenance.get("recommendation_type")
    if rec_type == "position_assessment":
        try:
            return PositionAssessment.model_validate(body)
        except ValidationError as exc:
            raise ProposalHydrationError(
                f"pm_strategist/position_assessment originating_proposal_json "
                f"failed PositionAssessment validation: {exc}"
            ) from exc
    elif rec_type == "pending_order_assessment":
        try:
            return PendingOrderAssessment.model_validate(body)
        except ValidationError as exc:
            raise ProposalHydrationError(
                f"pm_strategist/pending_order_assessment originating_proposal_json "
                f"failed PendingOrderAssessment validation: {exc}"
            ) from exc
    elif rec_type is None:
        raise ProposalHydrationError(
            "source_provenance_json is missing 'recommendation_type' key "
            "for pm_strategist provenance."
        )
    else:
        raise ProposalHydrationError(
            f"Unknown recommendation_type {rec_type!r} for pm_strategist provenance; "
            f"expected 'position_assessment' or 'pending_order_assessment'."
        )
