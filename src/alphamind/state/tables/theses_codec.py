"""Round-trip codec between ``ThesisRecord`` and the parent / child SQL rows.

The persistence schema follows the design's three consumption modes — the
parent ``theses`` row carries metadata cheap to filter on, and the child
``thesis_components`` rows carry per-component bodies loadable selectively.

Some Pydantic fields (``key_catalyst``, ``age_hours``,
``expected_resolution_at``, ``resolution_pnl_usd``, ``entry_fill_gap_usd``,
plus per-component ``linked_bracket_leg_type`` and ``generation_timestamp``)
have no dedicated SQL column; they round-trip via the parent's
``narrative_json`` blob. The codec is the single source of truth for the
parent JSON layout.
"""

from __future__ import annotations

import json
from datetime import datetime

from alphamind._kernel.ids import PositionId, ThesisId
from alphamind.portfolio_state.records.orders import BracketLegType
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.thesis_components import ThesisComponentRow


def _isoformat(timestamp: datetime) -> str:
    return timestamp.isoformat().replace("+00:00", "Z")


def _parse_isoformat(text: str) -> datetime:
    return datetime.fromisoformat(text)


def record_to_rows(
    record: ThesisRecord,
) -> tuple[ThesisRow, tuple[ThesisComponentRow, ...]]:
    """Decompose a ``ThesisRecord`` into one parent row + N component rows.

    Component order is preserved; the child rows appear in the same order
    as ``record.components``.
    """
    component_rows: list[ThesisComponentRow] = []
    component_metadata: dict[str, dict[str, str | None]] = {}
    for comp in record.components:
        component_rows.append(
            ThesisComponentRow(
                component_id=comp.component_id,
                thesis_id=comp.thesis_id,
                component_type=comp.component_type.value,
                linked_bracket_leg=comp.linked_bracket_leg_id,
                instrument_reference=comp.instrument_reference,
                narrative=comp.narrative,
                key_assumptions_json=json.dumps([ka.model_dump() for ka in comp.key_assumptions]),
                supporting_signals_json="[]",
                resolution_outcome=(
                    None if comp.resolution_outcome is None else comp.resolution_outcome.value
                ),
                resolution_notes=comp.resolution_notes,
            )
        )
        component_metadata[comp.component_id] = {
            "linked_bracket_leg_type": (
                None if comp.linked_bracket_leg_type is None else comp.linked_bracket_leg_type.value
            ),
            "generation_timestamp": _isoformat(comp.generation_timestamp),
        }
    narrative_payload = {
        "key_catalyst": record.key_catalyst,
        "age_hours": record.age_hours,
        "expected_resolution_at": _isoformat(record.expected_resolution_at),
        "resolution_pnl_usd": record.resolution_pnl_usd,
        "entry_fill_gap_usd": record.entry_fill_gap_usd,
        "components_metadata": component_metadata,
    }
    thesis_row = ThesisRow(
        thesis_id=record.thesis_id,
        position_id=record.position_id,
        status=record.status.value,
        resolution_timestamp=(
            None if record.resolution_timestamp is None else _isoformat(record.resolution_timestamp)
        ),
        resolution_category=(
            None if record.resolution_category is None else record.resolution_category.value
        ),
        summary=record.summary,
        time_expectation_hours=record.time_expectation_hours,
        position_size_rationale=record.position_size_rationale,
        generation_timestamp=_isoformat(record.generation_timestamp),
        narrative_json=json.dumps(narrative_payload),
    )
    return thesis_row, tuple(component_rows)


def rows_to_record(
    thesis_row: ThesisRow,
    component_rows: tuple[ThesisComponentRow, ...],
) -> ThesisRecord:
    """Reconstruct a ``ThesisRecord`` faithfully from parent + child rows.

    Inverse of :func:`record_to_rows`. The parent ``narrative_json`` payload
    supplies the fields without a dedicated column; per-component metadata
    (linked-bracket-leg type, generation timestamp) is keyed by
    ``component_id`` inside the same payload.
    """
    if thesis_row.time_expectation_hours is None:
        msg = (
            f"thesis row {thesis_row.thesis_id!r} missing time_expectation_hours; "
            "cannot reconstruct ThesisRecord"
        )
        raise ValueError(msg)

    payload = json.loads(thesis_row.narrative_json)
    components_metadata = payload.get("components_metadata", {})
    components = tuple(
        _component_from_row(crow, components_metadata.get(crow.component_id, {}))
        for crow in component_rows
    )

    return ThesisRecord(
        thesis_id=ThesisId(thesis_row.thesis_id),
        position_id=PositionId(thesis_row.position_id),
        summary=thesis_row.summary,
        key_catalyst=payload["key_catalyst"],
        position_size_rationale=thesis_row.position_size_rationale,
        components=components,
        status=ThesisRecordStatus(thesis_row.status),
        generation_timestamp=_parse_isoformat(thesis_row.generation_timestamp),
        time_expectation_hours=thesis_row.time_expectation_hours,
        age_hours=payload["age_hours"],
        expected_resolution_at=_parse_isoformat(payload["expected_resolution_at"]),
        resolution_timestamp=(
            None
            if thesis_row.resolution_timestamp is None
            else _parse_isoformat(thesis_row.resolution_timestamp)
        ),
        resolution_category=(
            None
            if thesis_row.resolution_category is None
            else ThesisResolutionCategory(thesis_row.resolution_category)
        ),
        resolution_pnl_usd=payload["resolution_pnl_usd"],
        entry_fill_gap_usd=payload["entry_fill_gap_usd"],
    )


def _component_from_row(
    row: ThesisComponentRow,
    metadata: dict[str, str | None],
) -> ThesisComponent:
    leg_type_raw = metadata.get("linked_bracket_leg_type")
    leg_type = None if leg_type_raw is None else BracketLegType(leg_type_raw)
    generation_raw = metadata.get("generation_timestamp")
    if generation_raw is None:
        msg = (
            f"thesis_components row {row.component_id!r} missing generation_timestamp "
            "in parent narrative_json; cannot reconstruct ThesisComponent"
        )
        raise ValueError(msg)

    if row.instrument_reference is None:
        msg = (
            f"thesis_components row {row.component_id!r} has NULL instrument_reference; "
            "cannot reconstruct ThesisComponent (typed record requires a string)"
        )
        raise ValueError(msg)

    key_assumptions = tuple(
        KeyAssumption.model_validate(item) for item in json.loads(row.key_assumptions_json)
    )
    return ThesisComponent(
        component_id=row.component_id,
        thesis_id=ThesisId(row.thesis_id),
        component_type=ThesisComponentType(row.component_type),
        linked_bracket_leg_type=leg_type,
        linked_bracket_leg_id=row.linked_bracket_leg,
        instrument_reference=row.instrument_reference,
        narrative=row.narrative,
        key_assumptions=key_assumptions,
        generation_timestamp=_parse_isoformat(generation_raw),
        resolution_outcome=(
            None
            if row.resolution_outcome is None
            else ThesisComponentOutcome(row.resolution_outcome)
        ),
        resolution_notes=row.resolution_notes,
    )
