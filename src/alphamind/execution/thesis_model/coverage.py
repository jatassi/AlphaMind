"""Bracket-thesis coverage cross-validator (ALP-333)."""

from __future__ import annotations

from alphamind.portfolio_state.records.orders import BracketLegType, BracketRecord
from alphamind.portfolio_state.records.theses import ThesisComponentType, ThesisRecord

# Maps each bracket leg type to the thesis component_type required to cover it.
_LEG_TYPE_TO_REQUIRED_COMPONENT_TYPE: dict[BracketLegType, ThesisComponentType] = {
    BracketLegType.TAKE_PROFIT: ThesisComponentType.TARGET_RATIONALE,
    BracketLegType.PRICE_STOP: ThesisComponentType.INVALIDATION_RATIONALE,
    BracketLegType.TIME_EXPIRATION: ThesisComponentType.INVALIDATION_RATIONALE,
    BracketLegType.EVENT_INVALIDATION: ThesisComponentType.INVALIDATION_RATIONALE,
}


def validate_bracket_thesis_coverage(
    bracket: BracketRecord,
    thesis: ThesisRecord,
) -> None:
    """Raise ValueError if any bracket protective leg lacks a coverage-compatible thesis component.

    Coverage rule (per docs/design/05-execution-layer/thesis-model.md § Mandatory coverage):
    every protective leg's leg_id must be referenced by ≥1 thesis component whose
    linked_bracket_leg_id matches it AND whose component_type is compatible with the
    leg's leg_type:

    * TAKE_PROFIT leg requires a component with component_type=TARGET_RATIONALE
    * PRICE_STOP / TIME_EXPIRATION / EVENT_INVALIDATION leg requires a component
      with component_type=INVALIDATION_RATIONALE

    Additionally, the thesis must contain ≥1 ENTRY_RATIONALE component (this mirrors
    the design's "one entry rationale per order leg" rule; the new cross-validator
    enforces presence here, while ThesisRecord._check_mandatory_coverage continues
    enforcing the three-types-exist invariant within the thesis itself).

    Components whose linked_bracket_leg_id is None are ignored by this validator —
    they may be legacy records or components linked to non-bracket entities. The
    validator does not require every component to point at a leg; only that every
    leg is pointed at by ≥1 compatible component.
    """
    errors: list[str] = []
    has_entry = False
    covered: dict[str, set[ThesisComponentType]] = {}

    for component in thesis.components:
        if component.component_type == ThesisComponentType.ENTRY_RATIONALE:
            has_entry = True
        if component.linked_bracket_leg_id is not None:
            covered.setdefault(component.linked_bracket_leg_id, set()).add(component.component_type)

    if not has_entry:
        errors.append("thesis is missing a required ENTRY_RATIONALE component")

    for leg in bracket.protective_legs:
        required_type = _LEG_TYPE_TO_REQUIRED_COMPONENT_TYPE[leg.leg_type]
        if required_type not in covered.get(leg.leg_id, set()):
            errors.append(
                f"leg_id={leg.leg_id} (leg_type={leg.leg_type}) is not covered by any "
                f"thesis component with component_type={required_type}"
            )

    if errors:
        raise ValueError("; ".join(errors))
