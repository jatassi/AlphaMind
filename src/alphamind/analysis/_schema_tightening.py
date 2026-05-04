"""JSON-Schema tightening for Pydantic-generated schemas with discriminated invariants.

Pydantic's ``model_json_schema()`` renders conditional fields (those typed
``T | None``) as ``anyOf [<T> | null]``. For a model whose ``model_validator``
enforces a discriminated invariant — e.g. AdaptiveBrief's
"for ``assessment == 'signal'``, ``strengthens`` must be a non-None tuple" —
the unmodified schema is too permissive: the API will accept a payload that
emits ``null`` on a branch where the Python invariant requires the field to be
present, leaving the failure to surface only on post-validation Pydantic.

:func:`_tighten_conditional_schema` rewrites the relevant object's schema by
adding a ``oneOf`` over the discriminator's enum values where each branch:

* Sets ``{discriminator_field: {"const": <enum.value>}}``.
* Narrows each required-by-this-branch conditional field to its non-null
  variant.
* Constrains each irrelevant conditional field to ``{"type": "null"}``.

See ``docs/_archive/spikes/alp-288-json-schema-output.md`` § Risk #1 for the
spike output that motivated this.

.. warning::
    The Anthropic API's tool-input-schema validator rejects schemas with
    ``oneOf``/``allOf``/``anyOf`` at the top level (the SDK forwards
    ``output_format`` as a synthetic tool's input schema). This helper
    enforces the constraint by raising :class:`ValueError` if asked to
    tighten the root model: only nested ``$defs`` types are valid targets.
    AdaptiveBrief.InvestigationThread is the canonical fit; QualitativeBrief
    and SectorBrief are NOT — their top-level ``signal_quality`` invariant
    is enforced by the Pydantic ``model_validator`` plus the harness's
    parse-then-retry path instead.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

__all__ = ["_tighten_conditional_schema"]


# Mapping is invariant in its key type — `[EnumT: enum.Enum]` lets the call
# site pass any concrete enum subclass (Assessment, SignalQuality, …) without
# upcasting to enum.Enum.
def _tighten_conditional_schema[EnumT: enum.Enum](
    schema: dict[str, Any],
    model_class: type[BaseModel],
    assessment_field: str,
    required_by_assessment: Mapping[EnumT, Iterable[str]],
) -> dict[str, Any]:
    """Rewrite *schema* to enforce a discriminated conditional-field invariant.

    Parameters
    ----------
    schema:
        The dict produced by ``Model.model_json_schema()`` for the *root* model.
        Mutated in place.
    model_class:
        The Pydantic class whose properties carry *assessment_field*. May be
        the root model itself (e.g. QualitativeBrief, SectorBrief) or a nested
        submodel (e.g. InvestigationThread inside AdaptiveBrief). The class's
        ``__name__`` is matched against ``schema["title"]`` and against
        ``schema["$defs"]`` keys.
    assessment_field:
        Name of the discriminator field on *model_class*. Must reference an
        enum-typed field — the enum values become ``oneOf`` branch constants.
    required_by_assessment:
        Mapping from each discriminator enum value to the set of conditional
        fields that must be non-null on that branch. Conditional fields are
        the union across all values; for each branch, fields not in its
        required set are constrained to ``"type": "null"``. Every value of the
        discriminator enum must appear as a key — a missing branch would let
        the model emit a payload no ``oneOf`` arm matches and trigger an
        API-side schema-validation rejection.

    Returns
    -------
    dict
        The mutated *schema* (same object), for ergonomic chaining.

    Raises
    ------
    ValueError
        If *model_class* cannot be located in *schema*, if *assessment_field*
        is missing or not enum-typed, or if *required_by_assessment* keys do
        not match the discriminator enum values exactly.
    """
    obj_schema = _locate_object_schema(schema, model_class)

    if "properties" not in obj_schema or assessment_field not in obj_schema["properties"]:
        raise ValueError(f"{model_class.__name__!r} schema has no property {assessment_field!r}")

    enum_values = _resolve_enum_values(schema, obj_schema["properties"][assessment_field])
    branch_keys = {value.value for value in required_by_assessment}
    if branch_keys != enum_values:
        missing = enum_values - branch_keys
        extra = branch_keys - enum_values
        parts: list[str] = []
        if missing:
            parts.append(f"missing branches: {sorted(missing)!r}")
        if extra:
            parts.append(f"unknown values: {sorted(extra)!r}")
        raise ValueError(
            f"required_by_assessment must cover every {assessment_field!r} enum value exactly; "
            + "; ".join(parts)
        )

    conditional_fields: frozenset[str] = frozenset().union(
        *(set(fields) for fields in required_by_assessment.values())
    )

    base_properties = obj_schema["properties"]
    branches: list[dict[str, Any]] = []
    for value, required_fields in required_by_assessment.items():
        required_set = frozenset(required_fields)
        forbidden_set = conditional_fields - required_set
        branch_properties: dict[str, Any] = {
            assessment_field: {"const": value.value},
        }
        for name in required_set:
            branch_properties[name] = _strip_null_variant(base_properties[name], name)
        for name in forbidden_set:
            branch_properties[name] = {"type": "null"}
        branch: dict[str, Any] = {"properties": branch_properties}
        if required_set:
            branch["required"] = sorted(required_set)
        branches.append(branch)

    obj_schema["oneOf"] = branches
    return schema


def _locate_object_schema(schema: dict[str, Any], model_class: type[BaseModel]) -> dict[str, Any]:
    """Return the subschema dict in *schema* corresponding to *model_class*.

    Raises :class:`ValueError` when *model_class* is the root of *schema* —
    tightening at root level produces ``oneOf`` at the schema's top level,
    which the Anthropic API tool-input-schema validator rejects (see the
    module docstring's warning). Callers wanting a root-level discriminated
    invariant must rely on the Pydantic ``model_validator`` and the
    harness's parse-then-retry path instead.
    """
    name = model_class.__name__
    if schema.get("title") == name:
        raise ValueError(
            f"{name!r} is the root of the schema; tightening it would write "
            "`oneOf` at the schema root, which the Anthropic API rejects "
            "(`tools.0.custom.input_schema: input_schema does not support "
            "oneOf, allOf, or anyOf at the top level`). Top-level "
            "discriminated invariants must be enforced by the Pydantic "
            "model_validator + the harness's parse-then-retry path; this "
            "helper is only for nested $defs types (e.g. "
            "AdaptiveBrief.InvestigationThread)."
        )
    defs: dict[str, dict[str, Any]] = schema.get("$defs", {})
    if name in defs:
        return defs[name]
    raise ValueError(
        f"Could not locate {name!r} in schema (checked root title and $defs); "
        f"available defs: {sorted(defs)!r}"
    )


def _resolve_enum_values(schema: dict[str, Any], property_schema: dict[str, Any]) -> set[str]:
    """Return the set of enum values for *property_schema*, dereferencing $ref."""
    if "enum" in property_schema:
        return set(property_schema["enum"])
    if "$ref" in property_schema:
        ref: str = property_schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"cannot resolve non-local $ref: {ref!r}")
        def_name = ref.removeprefix("#/$defs/")
        defs = schema.get("$defs", {})
        if def_name not in defs:
            raise ValueError(f"$ref target {ref!r} not in $defs")
        target = defs[def_name]
        if "enum" not in target:
            raise ValueError(f"$ref target {ref!r} is not an enum schema")
        return set(target["enum"])
    raise ValueError(f"property is neither an inline enum nor a $ref: {property_schema!r}")


def _strip_null_variant(property_schema: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Return a copy of *property_schema* with the null arm of ``anyOf`` removed.

    Title/description metadata on the outer schema is carried onto the narrowed
    arm. If *property_schema* has no ``anyOf`` (already non-nullable), it is
    returned as a shallow copy unchanged.
    """
    if "anyOf" not in property_schema:
        return dict(property_schema)

    non_null_arms = [arm for arm in property_schema["anyOf"] if arm.get("type") != "null"]
    if not non_null_arms:
        raise ValueError(f"field {field_name!r} has only null arm(s) in anyOf; cannot strip")
    narrowed: dict[str, Any] = (
        dict(non_null_arms[0]) if len(non_null_arms) == 1 else {"anyOf": non_null_arms}
    )
    for carry in ("title", "description"):
        if carry in property_schema and carry not in narrowed:
            narrowed[carry] = property_schema[carry]
    return narrowed
