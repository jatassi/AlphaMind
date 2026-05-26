"""Config editor framework (story 05i / ALP-679).

Two surfaces:

* ``GET /api/views/config/schema/{config_file}`` — form-schema metadata
  derived from the file's Pydantic model + per-field reload-policy
  annotations. The frontend consumes this to build the form dynamically.
* ``PUT /api/views/config/{config_file}`` — atomic file write via
  :func:`alphamind._kernel.atomic_io.atomic_write_text` after all three
  validation layers (parse / cross-ref / semantic) pass.

The framework is generic; per-file editor pages (06a profiles+regimes,
06b alerts+security+other, 06c resolved viewer + diff) instantiate it
against their respective Pydantic models.

Per ``docs/design/command-center.md`` § Config editor:
* Each leaf renders as a type-matched control (number, boolean, enum,
  string, string-array, object-array, cron, path).
* Inline validation surfaces the three layers from
  ``docs/design/configuration-management.md`` § Validation.
* Each setting carries a reload-policy badge — invocation-time-reload
  (most settings) or deploy-time-only.

This story decorates :class:`CommandCenterConfig` / :class:`SecurityConfig`
/ :class:`AlertsConfig`; the other YAML files inherit the same framework
via additional registry entries in stories 06a/06b.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin, get_type_hints

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from alphamind.command_center.config import (
    AlertsConfig,
    CommandCenterConfig,
    ReloadPolicy,
    SecurityConfig,
)

__all__ = [
    "ConfigFile",
    "FormFieldSchema",
    "FormSchema",
    "ReloadPolicy",
    "build_configuration_router",
    "reload_policy_of",
]


# ----------------------------------------------------------------------------
# ConfigFile registry
# ----------------------------------------------------------------------------


ControlType = Literal[
    "number",
    "boolean",
    "enum",
    "string",
    "string-array",
    "object-array",
    "cron",
    "path",
]
"""Closed set of control types per ``docs/design/command-center.md`` §
Config editor. The frontend's :func:`FormComposer` dispatches on this
string to pick the per-control React component.
"""


@dataclass(frozen=True, slots=True)
class ConfigFile:
    """A registered Pydantic model + the YAML file it parses.

    Keeps the (model, filename) pairing in one place so the route layer
    consults a small registry rather than threading per-file conditionals
    through the endpoint handlers.

    * ``slug``: the URL token (``command-center``, ``security``, ...).
    * ``model``: the Pydantic v2 model parsed at load time.
    * ``filename``: the on-disk file relative to ``config/``.
    """

    slug: str
    model: type[BaseModel]
    filename: str


_REGISTRY: dict[str, ConfigFile] = {
    "command-center": ConfigFile(
        slug="command-center",
        model=CommandCenterConfig,
        filename="command-center.yaml",
    ),
    "security": ConfigFile(
        slug="security",
        model=SecurityConfig,
        filename="security.yaml",
    ),
    "alerts": ConfigFile(
        slug="alerts",
        model=AlertsConfig,
        filename="alerts.yaml",
    ),
}


# ----------------------------------------------------------------------------
# Form schema response models (Pydantic at the HTTP boundary)
# ----------------------------------------------------------------------------


class FormFieldSchema(BaseModel):
    """Per-leaf form-schema entry the frontend consumes.

    Carries the dotted path (e.g. ``bind.host``), the control type the
    frontend renders, the reload-policy badge string, and a flat
    constraints dict for client-side parse-layer enforcement (minimum,
    maximum, pattern, enum_choices).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    control_type: ControlType
    reload_policy: str
    constraints: dict[str, Any]


class FormSchema(BaseModel):
    """Full form-schema for one config file.

    Returned by ``GET /api/views/config/schema/{config_file}``. The
    frontend renders one form, one field per :class:`FormFieldSchema`
    entry, dispatching on ``control_type``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    filename: str
    fields: list[FormFieldSchema]


# ----------------------------------------------------------------------------
# Reload-policy extraction
# ----------------------------------------------------------------------------


def reload_policy_of(model: type[BaseModel], field_name: str) -> ReloadPolicy:
    """Extract the :class:`ReloadPolicy` from a Pydantic field's annotation.

    Walks ``typing.Annotated`` metadata on the field's type hint. When no
    :class:`ReloadPolicy` is present the default is
    :attr:`ReloadPolicy.INVOCATION_TIME` — the design's "most settings"
    bucket — so model authors only annotate the deploy-time exceptions.
    """
    hints = get_type_hints(model, include_extras=True)
    if field_name not in hints:
        return ReloadPolicy.INVOCATION_TIME
    hint = hints[field_name]
    # Annotated[T, *metadata] has __metadata__ when using typing.Annotated.
    metadata: tuple[Any, ...] = getattr(hint, "__metadata__", ())
    for item in metadata:
        if isinstance(item, ReloadPolicy):
            return item
    # Walk one level into generic containers (e.g. list[Annotated[...]]) in
    # case the annotation lives on the element type rather than the field
    # type itself.
    origin = get_origin(hint)
    if origin is not None:
        for arg in get_args(hint):
            inner_meta: tuple[Any, ...] = getattr(arg, "__metadata__", ())
            for item in inner_meta:
                if isinstance(item, ReloadPolicy):
                    return item
    return ReloadPolicy.INVOCATION_TIME


# ----------------------------------------------------------------------------
# Form-schema derivation
# ----------------------------------------------------------------------------


def _strip_annotated(tp: Any) -> Any:
    """Return the underlying type of a ``typing.Annotated[T, ...]``."""
    origin = get_origin(tp)
    if origin is None:
        return tp
    if get_origin(tp) is not None and hasattr(tp, "__metadata__"):
        return get_args(tp)[0]
    return tp


def _classify_list_element(elem: Any) -> ControlType:
    """Inner dispatch for a ``list[T]`` element type."""
    elem_origin = get_origin(elem)
    if isinstance(elem, type) and issubclass(elem, BaseModel):
        return "object-array"
    if elem is dict or elem_origin is dict:
        return "object-array"
    # str-element falls through to string-array (the default for lists).
    return "string-array"


def _classify_control_type(field_type: Any) -> ControlType:
    """Pick a control type for a leaf field's Python type.

    Closed dispatch order: bool → boolean, int/float → number, list[…] →
    one of {string-array, object-array}, fallback → string. Enum + cron +
    path live in ``json_schema_extra`` overrides; this classifier handles
    the structural types only and is intentionally narrow — per-file
    editors in 06a/06b extend the override surface as their YAML shapes
    demand.
    """
    inner = _strip_annotated(field_type)
    if inner is bool:
        return "boolean"
    if inner in (int, float):
        return "number"
    if get_origin(inner) is list:
        args = get_args(inner)
        if not args:
            return "string-array"
        return _classify_list_element(_strip_annotated(args[0]))
    return "string"


def _extract_constraints(field_info: Any) -> dict[str, Any]:
    """Read parse-time constraints from a field for client-side enforcement.

    Pulls min/max from numeric metadata and pattern from string metadata.
    The minimum / maximum keys mirror the JSON Schema vocabulary the
    frontend already understands.
    """
    constraints: dict[str, Any] = {}
    metadata = getattr(field_info, "metadata", []) or []
    for item in metadata:
        if hasattr(item, "ge") and item.ge is not None:
            constraints["minimum"] = item.ge
        if hasattr(item, "le") and item.le is not None:
            constraints["maximum"] = item.le
        if hasattr(item, "gt") and item.gt is not None:
            # exclusiveMinimum mirrors the JSON Schema vocabulary.
            constraints["exclusiveMinimum"] = item.gt
        if hasattr(item, "lt") and item.lt is not None:
            constraints["exclusiveMaximum"] = item.lt
        if hasattr(item, "min_length") and item.min_length is not None:
            constraints["minLength"] = item.min_length
        if hasattr(item, "max_length") and item.max_length is not None:
            constraints["maxLength"] = item.max_length
        if hasattr(item, "pattern") and item.pattern is not None:
            constraints["pattern"] = item.pattern
    return constraints


def _walk_fields(model: type[BaseModel], prefix: str = "") -> list[FormFieldSchema]:
    """Walk a Pydantic model recursively into dotted-path leaf entries.

    Nested :class:`BaseModel` subclasses recurse with the dotted prefix
    extended; leaf scalar / list fields produce one :class:`FormFieldSchema`
    each. The reload policy for a nested-model field propagates to every
    leaf below it (an explicit annotation on a leaf still wins).
    """
    fields: list[FormFieldSchema] = []
    for name, field_info in model.model_fields.items():
        annotation = field_info.annotation
        if annotation is None:
            continue
        inner = _strip_annotated(annotation)
        path = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
        own_policy = reload_policy_of(model, name)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            # Recurse into the sub-model; if the parent field carries an
            # explicit DEPLOY_TIME annotation, propagate it to leaves
            # that have no override of their own.
            child_fields = _walk_fields(inner, prefix=path)
            if own_policy is ReloadPolicy.DEPLOY_TIME:
                child_fields = [
                    FormFieldSchema(
                        path=child.path,
                        control_type=child.control_type,
                        reload_policy=ReloadPolicy.DEPLOY_TIME.value,
                        constraints=child.constraints,
                    )
                    for child in child_fields
                ]
            fields.extend(child_fields)
            continue
        constraints = _extract_constraints(field_info)
        control_type = _classify_control_type(annotation)
        fields.append(
            FormFieldSchema(
                path=path,
                control_type=control_type,
                reload_policy=own_policy.value,
                constraints=constraints,
            )
        )
    return fields


def derive_form_schema(config_file: ConfigFile) -> FormSchema:
    """Build a :class:`FormSchema` from a registered :class:`ConfigFile`.

    Pure function — no I/O. Tests can call it directly without booting
    the route layer.
    """
    return FormSchema(
        slug=config_file.slug,
        filename=config_file.filename,
        fields=_walk_fields(config_file.model),
    )


# ----------------------------------------------------------------------------
# FastAPI router
# ----------------------------------------------------------------------------


def build_configuration_router() -> APIRouter:
    """Return a fresh ``APIRouter`` for the config editor framework.

    Mount under ``/api/views/config`` in :mod:`app`. The router carries:

    * ``GET /schema/{config_file}`` — form-schema metadata.
    * ``PUT /{config_file}`` — atomic file write (added in a follow-up
      commit).
    """
    router = APIRouter(tags=["views:configuration"])

    @router.get("/schema/{config_file}", response_model=FormSchema)
    def get_schema(config_file: str) -> FormSchema:
        """Return the form-schema metadata for the named config file.

        404 when the slug is not registered — the operator chose a config
        file the framework doesn't yet support.
        """
        entry = _REGISTRY.get(config_file)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config file: {config_file!r}",
            )
        return derive_form_schema(entry)

    return router
