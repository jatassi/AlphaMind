"""Config editor framework (05i / ALP-679) + profiles/regimes editors
(06a / ALP-682) + resolved-config / git-history diagnostic views
(06c / ALP-684).

Editor surfaces (05i + 06a):

* ``GET /api/views/config/schema/{config_file}`` — form-schema metadata
  derived from the file's Pydantic model + per-field reload-policy
  annotations. The frontend consumes this to build the form dynamically.
* ``PUT /api/views/config/{config_file}`` — atomic file write via
  :func:`alphamind._kernel.atomic_io.atomic_write_text` after all three
  validation layers (parse / cross-ref / semantic) pass.
* ``GET /api/views/config/files`` — list of config-file slugs for a given
  ``family`` query parameter (``profiles`` or ``regimes``). The frontend
  uses this to populate the file-picker sidebar on each editor page.

The framework is generic; per-file editor pages (06a profiles+regimes,
06b alerts+security+other, 06c resolved viewer + diff) instantiate it
against their respective Pydantic models.

Read-only diagnostic views (06c):

* ``GET /api/views/config/resolved`` — per-invocation resolved-config
  bundle + source-file side-panel.  Reads the snapshot written by the
  composition resolver at invocation start.
* ``GET /api/views/config/resolved/diff`` — structured diff between two
  invocations' resolved configs.
* ``GET /api/views/config/git/history`` — git log for a tracked config
  file.
* ``GET /api/views/config/git/diff`` — text diff between two SHAs for a
  tracked config file.
* ``GET /api/views/config/git/status`` — tracked / untracked + uncommitted
  state for a config file.

Per ``docs/design/command-center.md`` § Config editor:
* Each leaf renders as a type-matched control (number, boolean, enum,
  string, string-array, object-array, cron, path).
* Inline validation surfaces the three layers from
  ``docs/design/configuration-management.md`` § Validation.
* Each setting carries a reload-policy badge — invocation-time-reload
  (most settings) or deploy-time-only.

Story 05i decorates :class:`CommandCenterConfig` / :class:`SecurityConfig`
/ :class:`AlertsConfig`. Story 06a registers one slug per profile YAML
(``profiles/small``, ``profiles/medium``, etc.) and one per regime YAML
(``regimes/normal``, ``regimes/crisis``, etc.) using
:class:`alphamind.config.models.profiles.ProfileConfig` and
:class:`alphamind.config.models.regimes.RegimeConfig`.

Security notes for git endpoints (06c):

* All ``file=`` query params are validated against an allowlist: the
  value must resolve to a path inside ``config_dir`` (same sandbox as
  ``/path-exists``).  Path traversal (``../``, absolute paths outside
  config/) returns 400.
* Git invocations use ``asyncio.create_subprocess_exec`` (no
  ``shell=True``); ``cwd`` is the resolved repo root (``config_dir``
  parent on the POSIX/Windows layout used in production).  No write
  operations are issued — every git command is read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin, get_type_hints

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.atomic_io import atomic_write_text
from alphamind._kernel.invocations import INVOCATIONS_DIRNAME, RESOLVED_CONFIG_FILENAME
from alphamind.command_center._kernel.ids import OperatorSessionId
from alphamind.command_center.auth.dependencies import csrf_required, current_session
from alphamind.command_center.config import (
    AlertsConfig,
    CommandCenterConfig,
    ControlHint,
    ReloadPolicy,
    SecurityConfig,
)
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import RegimeConfig
from alphamind.state.tables.invocations import InvocationRow

__all__ = [
    "ConfigContentResponse",
    "ConfigFile",
    "ConfigFileListResponse",
    "ConfigUpdateRequest",
    "ConfigUpdateResponse",
    "FormFieldSchema",
    "FormSchema",
    "GitCommitEntry",
    "GitDiffResponse",
    "GitHistoryResponse",
    "GitStatusResponse",
    "ReloadPolicy",
    "ResolvedConfigBundle",
    "ResolvedConfigDiff",
    "ValidationLayerError",
    "ValidationReport",
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


CrossReferenceHook = Callable[[BaseModel], list[str]]
"""Per-file cross-reference validator. Returns a list of error messages.

The framework story's three registered files (``command-center``,
``security``, ``alerts``) have no cross-references against the broader
``config/`` tree, so they ship with a no-op hook. Per-file editors in
06a/06b/06c wire the existing
:mod:`alphamind.config.validation.cross_reference` chain.
"""

SemanticHook = Callable[[BaseModel], list[str]]
"""Per-file semantic invariant validator. Returns a list of error
messages.

Same shape as :data:`CrossReferenceHook`; the framework story ships
no-op hooks for the three registered files.
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
    * ``cross_reference_hook``: per-file cross-reference validator.
      Default: no-op (returns ``[]``). Per-file editors in 06a/06b/06c
      wire the existing ``config/validation/cross_reference.py`` chain.
    * ``semantic_hook``: per-file semantic invariant validator. Default:
      no-op. Same handoff as ``cross_reference_hook``.
    """

    slug: str
    model: type[BaseModel]
    filename: str
    cross_reference_hook: CrossReferenceHook = field(default=lambda _model: [])
    semantic_hook: SemanticHook = field(default=lambda _model: [])


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
    # Story 06b — notable-shift thresholds for the weekly digest. All
    # fields are INVOCATION_TIME by default (no explicit annotation
    # needed — the next weekly digest pick up edits at composition
    # time).
    "digest": ConfigFile(
        slug="digest",
        model=DigestConfig,
        filename="digest.yaml",
    ),
    # Story 06a: profiles — one slug per YAML file in config/profiles/.
    "profiles/large": ConfigFile(
        slug="profiles/large",
        model=ProfileConfig,
        filename="profiles/large.yaml",
    ),
    "profiles/medium": ConfigFile(
        slug="profiles/medium",
        model=ProfileConfig,
        filename="profiles/medium.yaml",
    ),
    "profiles/micro": ConfigFile(
        slug="profiles/micro",
        model=ProfileConfig,
        filename="profiles/micro.yaml",
    ),
    "profiles/small": ConfigFile(
        slug="profiles/small",
        model=ProfileConfig,
        filename="profiles/small.yaml",
    ),
    # Story 06a: regimes — one slug per YAML file in config/regimes/.
    "regimes/crisis": ConfigFile(
        slug="regimes/crisis",
        model=RegimeConfig,
        filename="regimes/crisis.yaml",
    ),
    "regimes/elevated": ConfigFile(
        slug="regimes/elevated",
        model=RegimeConfig,
        filename="regimes/elevated.yaml",
    ),
    "regimes/low-vol": ConfigFile(
        slug="regimes/low-vol",
        model=RegimeConfig,
        filename="regimes/low-vol.yaml",
    ),
    "regimes/normal": ConfigFile(
        slug="regimes/normal",
        model=RegimeConfig,
        filename="regimes/normal.yaml",
    ),
}

# Ordered slug lists per family — consumed by GET /api/views/config/files.
_FAMILY_SLUGS: dict[str, list[str]] = {
    "profiles": ["profiles/large", "profiles/medium", "profiles/micro", "profiles/small"],
    "regimes": ["regimes/crisis", "regimes/elevated", "regimes/low-vol", "regimes/normal"],
}


# ----------------------------------------------------------------------------
# Form schema response models (Pydantic at the HTTP boundary)
# ----------------------------------------------------------------------------


class FormFieldSchema(BaseModel):
    """Per-leaf form-schema entry the frontend consumes.

    Carries the dotted path (e.g. ``bind.host``), the control type the
    frontend renders, the reload-policy badge string, a flat constraints
    dict for client-side parse-layer enforcement (minimum, maximum,
    pattern, enum_choices), and an optional ``columns`` list for
    ``object-array`` controls (one entry per typed sub-field of the
    array element's Pydantic model, so the table editor knows what
    cells to render). For non-array controls ``columns`` is ``None``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    control_type: ControlType
    reload_policy: str
    constraints: dict[str, Any]
    columns: list[FormFieldSchema] | None = None


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
# PUT request / response models + layered validation envelope
# ----------------------------------------------------------------------------


class ConfigUpdateRequest(BaseModel):
    """Request body for ``PUT /api/views/config/{config_file}``.

    Carries the proposed YAML payload as a single string. The frontend
    composes the YAML from form state via ``js-yaml`` before posting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    yaml: str


class ValidationLayerError(BaseModel):
    """One per-field error entry inside the layered envelope.

    * ``path``: dotted path of the offending field (or empty string
      for whole-document errors like cross-file mismatches).
    * ``message``: human-readable error message the frontend surfaces.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    message: str


class ValidationReport(BaseModel):
    """Three-layer validation envelope returned on PUT rejection.

    Each layer carries its own error list so the frontend can paint
    parse-time errors next to the offending field (the dotted path
    matches the form's field identifier) and surface cross-reference
    and semantic errors as page-level banners.

    Layers run in order — parse failures short-circuit before
    cross-reference fires; cross-reference failures short-circuit before
    semantic. The non-failing layers' lists are always empty (not
    ``null``) so the frontend renders the three buckets unconditionally.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parse: list[ValidationLayerError]
    cross_reference: list[ValidationLayerError]
    semantic: list[ValidationLayerError]


class ConfigUpdateResponse(BaseModel):
    """Successful ``PUT /api/views/config/{config_file}`` response.

    ``deploy_time_fields_changed`` is true when any field carrying a
    :attr:`ReloadPolicy.DEPLOY_TIME` annotation was modified by the
    update; the frontend surfaces a restart-needed reminder when set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    filename: str
    deploy_time_fields_changed: bool


class ConfigFileListResponse(BaseModel):
    """Response for ``GET /api/views/config/files?family=<family>``.

    Returns the ordered list of registered slugs for the requested
    config-file family. The frontend's file-picker sidebar iterates
    this list to build navigation links to the per-file editor pages.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: str
    slugs: list[str]


class ConfigContentResponse(BaseModel):
    """Current on-disk contents of a registered config file.

    Returned by ``GET /api/views/config/{config_file}`` (story 06b /
    ALP-683). The editor page seeds the :class:`FormComposer`'s value
    map from :attr:`values`; the raw YAML round-trips through
    :attr:`yaml` so the operator can switch to the raw editor without
    a round-trip through the form's flatten step.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    filename: str
    yaml: str
    values: dict[str, Any]


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


def _control_hint_of(field_type: Any) -> ControlHint | None:
    """Return the :class:`ControlHint` annotation on a field, if any.

    Walks ``typing.Annotated`` metadata in the same shape as
    :func:`reload_policy_of`. Pydantic strips Enum metadata items from
    the field's annotation at model-construction time *if they're not
    subclasses of FieldInfo*, so we read directly from the model's
    ``__annotations__`` rather than from the resolved hint when needed.
    """
    metadata: tuple[Any, ...] = getattr(field_type, "__metadata__", ())
    for item in metadata:
        if isinstance(item, ControlHint):
            return item
    return None


def _classify_list_element(elem: Any) -> ControlType:
    """Inner dispatch for a ``list[T]`` element type."""
    elem_origin = get_origin(elem)
    if isinstance(elem, type) and issubclass(elem, BaseModel):
        return "object-array"
    if elem is dict or elem_origin is dict:
        return "object-array"
    # str-element falls through to string-array (the default for lists).
    return "string-array"


def _is_enum_class(tp: Any) -> bool:
    """``True`` iff *tp* is a ``type`` subclass of :class:`enum.Enum`."""
    return isinstance(tp, type) and issubclass(tp, Enum)


_HINT_TO_CONTROL: dict[ControlHint, ControlType] = {
    ControlHint.PATH: "path",
    ControlHint.CRON: "cron",
}


def _classify_scalar(inner: Any) -> ControlType | None:
    """Map a non-list inner type to its control variant, or ``None``."""
    if _is_enum_class(inner) or get_origin(inner) is Literal:
        return "enum"
    if inner is bool:
        return "boolean"
    if inner in (int, float):
        return "number"
    return None


def _classify_control_type(field_type: Any) -> ControlType:
    """Pick a control type for a leaf field's Python type.

    Dispatch order:

    1. ``ControlHint.PATH`` / ``ControlHint.CRON`` annotation wins
       (semantic refinement of plain ``str``).
    2. Enum subclass or ``Literal[...]`` with string args → ``enum``.
    3. Structural: bool → boolean, int/float → number,
       list[BaseModel | dict] → object-array, list[anything else] →
       string-array, dict[str, T] → object-array (key/value table).
    4. Fallback → ``string``.

    ``dict[str, T]`` (e.g. ``rule_values: dict[str, float]``) renders as
    an object-array table with ``key`` and ``value`` columns — the same
    :class:`ObjectArrayTableEditor` component the frontend uses for lists.
    """
    hint = _control_hint_of(field_type)
    if hint is not None:
        return _HINT_TO_CONTROL[hint]
    inner = _strip_annotated(field_type)
    scalar = _classify_scalar(inner)
    if scalar is not None:
        return scalar
    if get_origin(inner) is list:
        args = get_args(inner)
        if not args:
            return "string-array"
        return _classify_list_element(_strip_annotated(args[0]))
    if get_origin(inner) is dict:
        return "object-array"
    return "string"


def _enum_choices(field_type: Any) -> list[str] | None:
    """Extract the closed set of enum / literal values from a field type.

    Returns ``None`` when the field isn't an enum-like type so callers
    can omit the ``enum_choices`` constraint entry.
    """
    inner = _strip_annotated(field_type)
    if _is_enum_class(inner):
        return [member.value for member in inner]
    if get_origin(inner) is Literal:
        return [str(arg) for arg in get_args(inner)]
    return None


def _element_columns(field_type: Any) -> list[FormFieldSchema] | None:
    """Derive per-column FormFieldSchema entries for a ``list[BaseModel]``.

    Returns ``None`` for non-typed list elements (``list[dict[str, Any]]``
    et al). The framework's three registered files include alerts.yaml,
    whose ``rules`` field is currently untyped at the loader (story 02
    ships ``list[dict[str, object]]`` so the per-rule schema isn't fixed
    here); the table editor on the frontend will fall back to inferring
    columns from row data in that case.
    """
    inner = _strip_annotated(field_type)
    if get_origin(inner) is not list:
        return None
    args = get_args(inner)
    if not args:
        return None
    elem = _strip_annotated(args[0])
    if isinstance(elem, type) and issubclass(elem, BaseModel):
        # Recurse with no prefix — the column path is the sub-field
        # name relative to the row object, not relative to the
        # surrounding form payload.
        return _walk_fields(elem, prefix="")
    return None


def _key_column() -> FormFieldSchema:
    """Build the ``key`` column shared by every ``dict[str, T]`` editor."""
    return FormFieldSchema(
        path="key",
        control_type="string",
        reload_policy=ReloadPolicy.INVOCATION_TIME.value,
        constraints={},
    )


def _dict_columns(field_type: Any) -> list[FormFieldSchema] | None:
    """Derive ``key`` + ``value`` columns for a ``dict[str, T]`` field.

    Three branches keyed off the dict's value type ``T``:

    * Scalar (``int`` / ``float`` / ``str``): one ``value`` column
      typed accordingly.  This covers ``rule_values: dict[str, float]``
      (profiles) and ``multipliers: dict[str, float]`` (regimes).
    * Pydantic :class:`BaseModel`: walk the model's leaf fields and
      emit one column per scalar field with a dotted path
      (``value.<field>``).  Frontend's :class:`ObjectArrayTableEditor`
      reads ``row[col.key]``; the dotted path supports nested values
      via the small helper added in this fix — ``row.value.<field>``.
      Wave-6 finding #12: pre-fix the BaseModel branch fell through to
      a single ``value`` string column, so ``agent_token_budgets:
      dict[str, TokenBudgetRange]`` could not be edited.
    * Anything else (``tuple``, ``list``, etc.): fall back to a single
      ``value`` string column.  Non-scalar leaves don't surface in the
      table cells but the row is still editable as raw JSON / YAML in
      a future iteration.

    Columns carry no ``reload_policy`` or ``constraints`` of their own —
    those live on the enclosing field.  Returns ``None`` for non-dict
    types.
    """
    inner = _strip_annotated(field_type)
    if get_origin(inner) is not dict:
        return None
    args = get_args(inner)
    value_type = _strip_annotated(args[1]) if len(args) >= 2 else None
    # BaseModel value → recurse into per-field sub-columns.
    if isinstance(value_type, type) and issubclass(value_type, BaseModel):
        sub_fields = _walk_fields(value_type, prefix="value")
        return [_key_column(), *sub_fields]
    # Scalar value → single typed column.
    value_control: ControlType = (
        "number" if value_type is not None and value_type in (int, float) else "string"
    )
    return [
        _key_column(),
        FormFieldSchema(
            path="value",
            control_type=value_control,
            reload_policy=ReloadPolicy.INVOCATION_TIME.value,
            constraints={},
        ),
    ]


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
    # Pull the resolved annotations via ``get_type_hints`` so that
    # ``Annotated[...]`` metadata (including :class:`ControlHint`)
    # survives the ``from __future__ import annotations`` deferral. The
    # raw ``__annotations__`` mapping contains string forms when the
    # module uses PEP 563, so the classifier wouldn't see the metadata.
    type_hints = get_type_hints(model, include_extras=True)
    fields: list[FormFieldSchema] = []
    for name, field_info in model.model_fields.items():
        annotation = type_hints.get(name, field_info.annotation)
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
                        columns=child.columns,
                    )
                    for child in child_fields
                ]
            fields.extend(child_fields)
            continue
        constraints = _extract_constraints(field_info)
        control_type = _classify_control_type(annotation)
        # Enum / Literal fields surface their closed value set so the
        # frontend's EnumDropdown can render choices without a second
        # round-trip.
        choices = _enum_choices(annotation)
        if choices is not None:
            constraints["enum_choices"] = choices
        # Object-array fields surface per-column FormFieldSchema entries
        # so the table editor knows what cells to render (#12).
        # list[BaseModel] → typed element columns; dict[str, T] → key/value
        # columns; list[dict[str, Any]] (untyped) → None (table editor
        # infers from first-row keys).
        columns: list[FormFieldSchema] | None = None
        if control_type == "object-array":
            columns = _element_columns(annotation) or _dict_columns(annotation)
        fields.append(
            FormFieldSchema(
                path=path,
                control_type=control_type,
                reload_policy=own_policy.value,
                constraints=constraints,
                columns=columns,
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
# Three-layer validation
# ----------------------------------------------------------------------------


def _empty_report() -> ValidationReport:
    return ValidationReport(parse=[], cross_reference=[], semantic=[])


def _parse_yaml(yaml_text: str) -> tuple[Any, list[ValidationLayerError]]:
    """Parse YAML text; return ``(payload, [])`` or ``(None, errors)``.

    A YAML parse failure surfaces as a single ``parse`` layer error with
    an empty path (whole-document failure) and the YAML library's
    diagnostic message.
    """
    try:
        return yaml.safe_load(yaml_text), []
    except yaml.YAMLError as exc:
        return None, [ValidationLayerError(path="", message=f"YAML parse error: {exc}")]


def _pydantic_errors_to_layer(exc: ValidationError) -> list[ValidationLayerError]:
    """Convert a Pydantic :class:`ValidationError` into per-field layer rows.

    Each Pydantic error carries a ``loc`` tuple (the dotted path) and a
    ``msg`` string; the converter joins ``loc`` with dots so it matches
    the form-schema's ``path`` identifier the frontend uses to highlight
    the offending control.
    """
    rows: list[ValidationLayerError] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        # ``loc`` items are ints (list index) or strings (field name); the
        # form-schema's path uses strings, so cast each item.
        path = ".".join(str(item) for item in loc)
        message = str(err.get("msg", ""))
        rows.append(ValidationLayerError(path=path, message=message))
    return rows


def run_validation(
    config_file: ConfigFile, yaml_text: str
) -> tuple[BaseModel | None, ValidationReport]:
    """Run the three validation layers in order; return ``(model, report)``.

    On parse failure: ``model`` is ``None`` and the report's ``parse``
    layer carries the error rows. On cross-reference or semantic failure:
    ``model`` is the parsed (parse-clean) model but the report's
    corresponding layer carries the error rows. On success: ``model`` is
    the validated model and the report is empty.

    Per ``docs/design/configuration-management.md`` § Validation, the
    layers fail-stop: parse failure short-circuits before cross-reference,
    cross-reference failure short-circuits before semantic.
    """
    payload, parse_errors = _parse_yaml(yaml_text)
    if parse_errors:
        return None, ValidationReport(parse=parse_errors, cross_reference=[], semantic=[])

    try:
        model = config_file.model.model_validate(payload)
    except ValidationError as exc:
        return None, ValidationReport(
            parse=_pydantic_errors_to_layer(exc),
            cross_reference=[],
            semantic=[],
        )

    cross_ref = [
        ValidationLayerError(path="", message=msg)
        for msg in config_file.cross_reference_hook(model)
    ]
    if cross_ref:
        return model, ValidationReport(parse=[], cross_reference=cross_ref, semantic=[])

    semantic = [
        ValidationLayerError(path="", message=msg) for msg in config_file.semantic_hook(model)
    ]
    if semantic:
        return model, ValidationReport(parse=[], cross_reference=[], semantic=semantic)

    return model, _empty_report()


# ----------------------------------------------------------------------------
# Deploy-time-only field tracker
# ----------------------------------------------------------------------------


def _deploy_time_fields(config_file: ConfigFile) -> list[str]:
    """Return the dotted paths of every deploy-time-only field for a model."""
    return [
        f.path
        for f in _walk_fields(config_file.model)
        if f.reload_policy == ReloadPolicy.DEPLOY_TIME.value
    ]


def _value_at_path(payload: Any, path: str) -> Any:
    """Drill into a nested dict/list payload by dotted path.

    Returns ``None`` when any segment is missing — used for the
    deploy-time-vs-existing comparison so missing-on-either-side is a
    no-change signal rather than a crash.
    """
    cur: Any = payload
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _deploy_time_field_changed(
    config_file: ConfigFile, *, existing_text: str | None, proposed_text: str
) -> bool:
    """Return True when any deploy-time field differs between texts."""
    if existing_text is None:
        # No existing file — every value is "new", so by definition any
        # deploy-time field is "changed".
        return bool(_deploy_time_fields(config_file))
    try:
        existing = yaml.safe_load(existing_text)
        proposed = yaml.safe_load(proposed_text)
    except yaml.YAMLError:
        return False
    for path in _deploy_time_fields(config_file):
        if _value_at_path(existing, path) != _value_at_path(proposed, path):
            return True
    return False


# ----------------------------------------------------------------------------
# 06c — Resolved-config and git-history models
# ----------------------------------------------------------------------------


class ResolvedConfigBundle(BaseModel):
    """Per-invocation resolved-config response (``GET /resolved``).

    * ``invocation_id``: the invocation whose snapshot was read.
    * ``bundle``: the full JSON payload from ``resolved_config.json``.
    * ``source_files``: per-source-file content side-panel (profile,
      regime, active overlays, mode files from ``config/``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str
    bundle: dict[str, Any]
    source_files: dict[str, str]


class ResolvedConfigDiff(BaseModel):
    """Structured diff between two invocations' resolved configs
    (``GET /resolved/diff``).

    * ``from_invocation_id`` / ``to_invocation_id``: the two snapshots
      compared.
    * ``diff_lines``: unified-diff text lines (``+``/``-``/`` ``
      prefixed).  Empty when the two snapshots are byte-identical.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_invocation_id: str
    to_invocation_id: str
    diff_lines: list[str]


class GitCommitEntry(BaseModel):
    """One ``git log`` entry for a config file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str
    short_sha: str
    author: str
    date: str
    subject: str


class GitHistoryResponse(BaseModel):
    """Response for ``GET /git/history?file=``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    commits: list[GitCommitEntry]


class GitDiffResponse(BaseModel):
    """Response for ``GET /git/diff?file=&from_sha=&to_sha=``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    from_sha: str
    to_sha: str
    diff_text: str


class GitStatusResponse(BaseModel):
    """Response for ``GET /git/status?file=``.

    * ``tracked``: ``True`` when the file appears in git's index (i.e.
      has been ``git add``-ed at least once).
    * ``has_uncommitted_changes``: ``True`` when ``git status --porcelain``
      shows the file as modified/added/deleted compared to HEAD.
    * ``untracked``: ``True`` when the file exists on disk but has never
      been added to git.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    tracked: bool
    has_uncommitted_changes: bool
    untracked: bool


# ----------------------------------------------------------------------------
# 06c — Git subprocess helpers
# ----------------------------------------------------------------------------

_GIT_TIMEOUT: int = 10
"""Seconds to wait for a git subprocess before raising ``HTTPException(503)``."""

_GIT_TIMEOUT_REAP: float = 1.0
"""Seconds to wait for the timed-out subprocess to reap after ``proc.kill()``.

Without this bound the killed git child becomes a zombie if it exits between
``kill()`` and the next event-loop iteration that would normally collect it —
under repeated 503 paths the FD/process table leaks one entry per failure.
"""

# Concrete SHA shape: 4-40 hex chars (covers short SHAs and full 40-char
# IDs). HEAD-relative aliases (``HEAD~1``, ``main~2``, branch names with
# ``~`` / ``^``) deliberately fail this regex — the API only accepts
# concrete SHAs so an attacker can't smuggle ``--output=`` or other
# option-shaped arguments through the ``from_sha`` / ``to_sha`` query
# parameters. Operators who need symbolic refs resolve them client-side
# (e.g. via the ``/git/history`` endpoint) before calling ``/git/diff``.
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _repo_root_from_config_dir(config_dir: Path) -> Path:
    """Return the git repo root from the config directory.

    In production the config dir is ``<repo_root>/config``, so the repo
    root is one level up.  We verify the parent contains a ``.git`` entry
    before returning; if not we fall back to using ``config_dir`` itself
    so tests that point ``config_dir`` at the repo root still work.
    """
    parent = config_dir.resolve().parent
    if (parent / ".git").exists():
        return parent
    # Fallback: config_dir is already a git root (e.g. in tests).
    return config_dir.resolve()


def _validate_config_file_param(file_param: str, config_dir: Path) -> Path:
    """Validate that *file_param* names a file inside *config_dir*.

    Returns the resolved :class:`Path`; raises :class:`HTTPException`
    (400) on path-traversal attempts or if the value is absolute.
    """
    if Path(file_param).is_absolute():
        raise HTTPException(status_code=400, detail="file parameter must be a relative path")
    resolved_config_dir = config_dir.resolve()
    candidate = (resolved_config_dir / file_param).resolve()
    if not candidate.is_relative_to(resolved_config_dir):
        raise HTTPException(
            status_code=400,
            detail="file parameter must resolve to a path inside config/",
        )
    return candidate


def _validate_sha(sha: str, *, field_name: str) -> str:
    """Reject any value that is not a concrete git SHA before passing to git.

    Treats the SHA-shape as the security boundary: ``git diff <X>..<Y>``
    treats argv elements starting with ``-`` or ``--`` as options, so an
    attacker who can supply ``from_sha=--output=/tmp/owned`` to
    ``/git/diff`` would otherwise gain an arbitrary file-write primitive
    via ``--output=``.  Concrete SHAs (4-40 hex chars) carry no leading
    dash and cannot collide with any git option name, so the regex
    validation is sufficient.

    Symbolic refs (``HEAD~1``, branch names) are intentionally rejected;
    callers needing those resolve them client-side first.
    """
    if not _SHA_PATTERN.match(sha):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a concrete git SHA (4-40 hex chars)",
        )
    return sha


async def _run_git(
    *args: str,
    cwd: Path,
) -> tuple[int, str, str]:
    """Run a read-only git command via ``asyncio.create_subprocess_exec``.

    Returns ``(returncode, stdout, stderr)``.  Raises
    :class:`HTTPException(503)` on timeout — reaps the child so the
    timed-out process does not leak as a zombie under repeated failure.

    Explicitly not ``shell=True`` — security requirement.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),  # already resolved by callers
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT
        )
    except TimeoutError as exc:
        proc.kill()
        # Wait briefly so the kernel reaps the child — otherwise the
        # subprocess stays as a zombie until the parent exits and the FD
        # table leaks one slot per 503. ``asyncio.wait_for`` raises if
        # the wait itself times out; we swallow that case because the
        # operator already saw a 503 and the eventual exit cleanup
        # belongs to the OS.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_GIT_TIMEOUT_REAP)
        raise HTTPException(status_code=503, detail="git operation timed out") from exc
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


# ----------------------------------------------------------------------------
# 06c — Resolved-config helpers
# ----------------------------------------------------------------------------


def _archive_base_dir() -> Path:
    """Return ``%USERPROFILE%/AlphaMind/archive`` (cross-platform)."""
    profile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    return Path(profile) / "AlphaMind" / "archive"


def _find_resolved_config_path(invocation_id: str) -> Path:
    """Return the canonical resolved-config snapshot path for *invocation_id*.

    Resolves to ``<archive_root>/invocations/<invocation_id>/resolved_config.json``
    per :mod:`alphamind.config.snapshot` and
    :mod:`alphamind._kernel.invocations` — the writer's pinned layout.
    The path is returned regardless of whether the file exists on disk;
    caller checks :py:meth:`Path.is_file` before reading.

    Previously this routed through
    :func:`alphamind._kernel.archive_layout.find_invocation_archive_dir`,
    which globbed ``<archive_root>/*/<invocation_id>`` and matched BOTH
    the resolved-config writer's ``invocations/`` partition AND the
    distillation orchestrator's ``<YYYY-MM-DD>/`` date partition.  Any
    invocation that produced both directories caused the glob to return
    two matches → the helper returned ``None`` → the endpoint 404'd in
    production.
    """
    return (
        _archive_base_dir()
        / INVOCATIONS_DIRNAME
        / invocation_id
        / RESOLVED_CONFIG_FILENAME
    )


def _load_resolved_config_for_invocation(invocation_id: str) -> dict[str, Any] | None:
    """Load ``resolved_config.json`` for *invocation_id*; return ``None`` if absent.

    Returns ``None`` for the three "no readable snapshot" branches:

    * Snapshot file missing.
    * I/O / JSON decode failure.
    * Top-level JSON is not an object — the downstream consumers
      (``_source_files_for_bundle`` / ``_diff_bundles``) treat the
      payload as a mapping and a non-dict would crash the route.
    """
    snapshot_path = _find_resolved_config_path(invocation_id)
    if not snapshot_path.is_file():
        return None
    try:
        parsed = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _safe_config_subpath(config_dir: Path, rel: str) -> Path | None:
    """Resolve *rel* under *config_dir* and reject path-traversal attempts.

    Returns the resolved :class:`Path` only when it falls inside the
    resolved ``config_dir``; returns ``None`` otherwise.  Callers treat
    ``None`` like a missing file (the bundle entry serialises to the
    empty string).

    The bundle's ``profile`` / ``regime`` / ``mode`` / ``active_overlays``
    fields are read from a per-invocation snapshot on disk, which is
    operator-controlled but may carry stale or hand-edited values.  A
    snapshot whose ``profile`` field reads ``../../etc/passwd`` would
    otherwise let an authenticated operator read arbitrary files under
    the daemon's UID via the ``/resolved`` endpoint.
    """
    resolved_root = config_dir.resolve()
    candidate = (resolved_root / rel).resolve()
    if not candidate.is_relative_to(resolved_root):
        return None
    return candidate


def _source_files_for_bundle(bundle: dict[str, Any], config_dir: Path) -> dict[str, str]:
    """Read the source YAML files referenced by the resolved-config bundle.

    Reads profile base, active regime, active overlays, and active mode
    files from ``config_dir``.  Missing or unreadable files produce an
    empty string placeholder.  Files that would resolve outside the
    sandboxed ``config_dir`` (path traversal) are also returned as
    empty strings.
    """
    sources: dict[str, str] = {}

    def _read(rel: str) -> str:
        safe_path = _safe_config_subpath(config_dir, rel)
        if safe_path is None:
            return ""
        try:
            return safe_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # Profile base.
    profile = bundle.get("profile")
    if isinstance(profile, str) and profile:
        sources[f"profiles/{profile}.yaml"] = _read(f"profiles/{profile}.yaml")

    # Active regime.
    regime = bundle.get("regime")
    if isinstance(regime, str) and regime:
        sources[f"regimes/{regime}.yaml"] = _read(f"regimes/{regime}.yaml")

    # Active overlays.
    overlays = bundle.get("active_overlays")
    if isinstance(overlays, list):
        for overlay in overlays:
            if isinstance(overlay, str) and overlay:
                sources[f"overlays/{overlay}.yaml"] = _read(f"overlays/{overlay}.yaml")

    # Active mode.
    mode = bundle.get("mode")
    if isinstance(mode, str) and mode:
        sources[f"modes/{mode}.yaml"] = _read(f"modes/{mode}.yaml")

    return sources


def _diff_bundles(
    from_id: str,
    to_id: str,
    from_bundle: dict[str, Any],
    to_bundle: dict[str, Any],
) -> list[str]:
    """Produce unified-diff lines between two resolved-config dicts."""
    from_text = json.dumps(from_bundle, sort_keys=True, indent=2)
    to_text = json.dumps(to_bundle, sort_keys=True, indent=2)
    return list(
        difflib.unified_diff(
            from_text.splitlines(keepends=True),
            to_text.splitlines(keepends=True),
            fromfile=f"{from_id}/resolved_config.json",
            tofile=f"{to_id}/resolved_config.json",
        )
    )


# ----------------------------------------------------------------------------
# 06c — DB session helper
# ----------------------------------------------------------------------------


def _reader_factory_config(request: Request) -> async_sessionmaker[AsyncSession]:
    """Pull the foreign-reader session factory off ``app.state``."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.foreign_reader_session_factory
    return factory


async def _most_recent_invocation_id(
    session: AsyncSession,
) -> str | None:
    """Return the invocation_id of the most recently started invocation."""
    result = await session.execute(
        select(InvocationRow.invocation_id).order_by(InvocationRow.start_at.desc()).limit(1)
    )
    return result.scalars().first()


# ----------------------------------------------------------------------------
# FastAPI router
# ----------------------------------------------------------------------------


def _resolve_config_dir(request: Request) -> Path:
    """Pull the config directory off ``app.state``; default to repo ``config/``.

    Tests set ``app.state.config_dir = tmp_path`` to redirect writes to a
    sandbox; production wires the daemon's resolved ``config/`` directory
    at lifespan startup.
    """
    cfg_dir = getattr(request.app.state, "config_dir", None)
    if cfg_dir is None:
        raise HTTPException(
            status_code=500,
            detail="config_dir not configured on app.state",
        )
    return Path(cfg_dir)


def _require_registered(config_file: str) -> ConfigFile:
    """Return the registered :class:`ConfigFile` or raise 404.

    Centralizes the registry-miss branch shared by the three route
    handlers so :func:`build_configuration_router` stays under the
    lint chain's complexity ceiling.
    """
    entry = _REGISTRY.get(config_file)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown config file: {config_file!r}",
        )
    return entry


def _load_config_contents(entry: ConfigFile, config_dir: Path) -> ConfigContentResponse:
    """Read + parse the YAML for a registered :class:`ConfigFile`.

    Hoisted out of :func:`build_configuration_router`'s GET handler so the
    handler stays under the lint chain's complexity ceiling. Raises
    :class:`HTTPException` for the same four failure modes the handler
    used to surface inline.
    """
    target_path = config_dir / entry.filename
    if not target_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Config file not found on disk: {entry.filename}",
        )
    yaml_text = target_path.read_text(encoding="utf-8")
    try:
        values = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse {entry.filename}: {exc}",
        ) from exc
    if not isinstance(values, dict):
        raise HTTPException(
            status_code=500,
            detail=f"Top-level YAML in {entry.filename} must be a mapping",
        )
    return ConfigContentResponse(
        slug=entry.slug,
        filename=entry.filename,
        yaml=yaml_text,
        values=values,
    )


def build_configuration_router() -> APIRouter:  # noqa: C901, PLR0915
    """Return a fresh ``APIRouter`` for the config editor framework.

    Mount under /api/views/config in :mod:`app`. The router carries:

    * GET /files — ordered slug list for a config-file family.
    * GET /schema/{config_file} — form-schema metadata.
    * PUT /{config_file} — atomic file write after three-layer
      validation passes.
    * ``GET /resolved`` — per-invocation resolved-config bundle (06c).
    * ``GET /resolved/diff`` — diff between two invocations (06c).
    * ``GET /git/history`` — git log for a config file (06c).
    * ``GET /git/diff`` — git diff between two SHAs (06c).
    * ``GET /git/status`` — tracked/untracked + uncommitted state (06c).

    The noqa suppression (C901 complexity + PLR0915 statements) is
    warranted: this is the composition root for all config-view routes;
    each inner ``@router.get/put`` closure is a distinct HTTP surface,
    not a logical branch in a single algorithm.
    """
    router = APIRouter(tags=["views:configuration"])

    @router.get("/files", response_model=ConfigFileListResponse)
    def get_config_files(
        family: str,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
    ) -> ConfigFileListResponse:
        """Return the ordered list of registered slugs for a config-file family.

        ``family`` must be one of the keys in :data:`_FAMILY_SLUGS`
        (``profiles`` or ``regimes``). 404 for unknown families.

        Requires a valid session cookie — the slug list reveals the
        on-disk file inventory, which is operator-only information.
        """
        del _session
        slugs = _FAMILY_SLUGS.get(family)
        if slugs is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config file family: {family!r}",
            )
        return ConfigFileListResponse(family=family, slugs=slugs)

    @router.get("/path-exists")
    def get_path_exists(
        path: str,
        request: Request,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
    ) -> dict[str, bool]:
        """Backend probe for the :class:`PathInput` control (story 05i).

        Returns ``{"exists": true|false}``. Used by the frontend
        :class:`PathInput` component to render an inline file-existence
        indicator so the operator catches typos before saving.

        Two security layers (findings #1 and #2 from Wave-5 review):

        * Authentication: the route requires a valid session cookie
          (``current_session`` dependency). Without it the endpoint was
          a filesystem oracle that any unauthenticated curl could use
          to probe arbitrary paths on the host.

        * Path sandboxing: the requested path is resolved and checked
          against the configured ``config_dir`` ancestor. Paths outside
          (including ``..``-traversal escapes) return 400. The probe is
          only useful for PathInput controls editing config-tree values
          anyway; the sandbox closes the broader filesystem-oracle
          surface without losing functionality.
        """
        del _session
        config_dir = _resolve_config_dir(request).resolve()
        candidate = Path(path)
        # ``Path.resolve`` is on a non-existent path returns a normalized
        # absolute path on POSIX but on Windows can still surface the
        # original — normalize via ``absolute().resolve(strict=False)``
        # so the ``is_relative_to`` check works uniformly.
        resolved = candidate.absolute().resolve(strict=False)
        if not resolved.is_relative_to(config_dir):
            raise HTTPException(
                status_code=400,
                detail="path must be inside the configured config directory",
            )
        return {"exists": resolved.exists()}

    # -----------------------------------------------------------------------
    # 06c: Resolved-config viewer (registered before the path-converter
    # routes below so ``/resolved`` and ``/resolved/diff`` are matched as
    # concrete paths rather than being captured by ``/{config_file:path}``).
    # -----------------------------------------------------------------------

    @router.get("/resolved", response_model=ResolvedConfigBundle)
    async def get_resolved_config(
        request: Request,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
        invocation_id: Annotated[str | None, Query()] = None,
        reader: Annotated[
            async_sessionmaker[AsyncSession],
            Depends(_reader_factory_config),
        ] = ...,  # type: ignore[assignment]
    ) -> ResolvedConfigBundle:
        """Return the resolved-config bundle for *invocation_id*.

        When ``invocation_id`` is omitted, the most-recent invocation row
        is used (latest ``start_at``).  Returns 404 when no invocation is
        found or the snapshot file is absent.
        """
        del _session
        config_dir = _resolve_config_dir(request)

        resolved_id = invocation_id
        if resolved_id is None:
            async with reader() as db:
                resolved_id = await _most_recent_invocation_id(db)
            if resolved_id is None:
                raise HTTPException(
                    status_code=404,
                    detail=("No invocation_id specified and no most-recent invocation found."),
                )

        bundle = _load_resolved_config_for_invocation(resolved_id)
        if bundle is None:
            raise HTTPException(
                status_code=404,
                detail=(f"Resolved-config snapshot not found for invocation {resolved_id!r}."),
            )

        source_files = _source_files_for_bundle(bundle, config_dir)
        return ResolvedConfigBundle(
            invocation_id=resolved_id,
            bundle=bundle,
            source_files=source_files,
        )

    @router.get("/resolved/diff", response_model=ResolvedConfigDiff)
    async def get_resolved_config_diff(
        _session: Annotated[OperatorSessionId, Depends(current_session)],
        from_invocation_id: Annotated[str, Query()],
        to_invocation_id: Annotated[str, Query()],
    ) -> ResolvedConfigDiff:
        """Return a unified diff between two invocations' resolved configs.

        Both ``from_invocation_id`` and ``to_invocation_id`` are required.
        Returns 404 when either snapshot is absent.
        """
        del _session

        from_bundle = _load_resolved_config_for_invocation(from_invocation_id)
        if from_bundle is None:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot not found for from_invocation_id={from_invocation_id!r}",
            )
        to_bundle = _load_resolved_config_for_invocation(to_invocation_id)
        if to_bundle is None:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot not found for to_invocation_id={to_invocation_id!r}",
            )

        diff_lines = _diff_bundles(from_invocation_id, to_invocation_id, from_bundle, to_bundle)
        return ResolvedConfigDiff(
            from_invocation_id=from_invocation_id,
            to_invocation_id=to_invocation_id,
            diff_lines=diff_lines,
        )

    # -----------------------------------------------------------------------
    # 06c: Git history / diff / status (concrete paths — registered before
    # the path-converter routes below).
    # -----------------------------------------------------------------------

    @router.get("/git/history", response_model=GitHistoryResponse)
    async def get_git_history(
        request: Request,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
        file: Annotated[str, Query(description="Config file relative to config/")],
    ) -> GitHistoryResponse:
        """Return the git commit history for a tracked config file.

        ``file`` must be a path relative to ``config/`` (e.g.
        ``guardrails.yaml`` or ``profiles/default.yaml``).  Path
        traversal returns 400.

        Returns an empty commits list when the file is untracked or git
        returns a non-zero exit code.
        """
        del _session
        config_dir = _resolve_config_dir(request)
        _validate_config_file_param(file, config_dir)
        repo_root = _repo_root_from_config_dir(config_dir)

        # Relative path from repo root so git log output is clean.
        rel_path = f"config/{file}"
        rc, stdout, _stderr = await _run_git(
            "log",
            "--format=%H\x1f%h\x1f%an\x1f%ai\x1f%s",
            "--",
            rel_path,
            cwd=repo_root,
        )
        commits: list[GitCommitEntry] = []
        if rc == 0:
            for line in stdout.splitlines():
                parts = line.split("\x1f", 4)
                if len(parts) == 5:
                    commits.append(
                        GitCommitEntry(
                            sha=parts[0],
                            short_sha=parts[1],
                            author=parts[2],
                            date=parts[3],
                            subject=parts[4],
                        )
                    )
        return GitHistoryResponse(file=file, commits=commits)

    @router.get("/git/diff", response_model=GitDiffResponse)
    async def get_git_diff(
        request: Request,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
        file: Annotated[str, Query(description="Config file relative to config/")],
        from_sha: Annotated[str, Query(description="Base commit SHA (4-40 hex)")],
        to_sha: Annotated[str, Query(description="Target commit SHA (4-40 hex)")],
    ) -> GitDiffResponse:
        """Return the unified text diff for a config file between two SHAs.

        Uses ``git diff <from_sha>..<to_sha> -- config/<file>``.  Returns
        an empty ``diff_text`` when git produces no output (identical
        contents) or on a non-zero exit code (e.g. invalid SHAs — caller
        should check the response).

        ``from_sha`` and ``to_sha`` MUST be concrete git SHAs (4-40 hex
        chars).  Symbolic refs (``HEAD~1``, branch names) are rejected
        with 400 — otherwise an attacker could smuggle option-shaped
        arguments (``--output=/path``) through the SHA params and turn
        the read-only diff endpoint into an arbitrary file-write
        primitive.
        """
        del _session
        config_dir = _resolve_config_dir(request)
        _validate_config_file_param(file, config_dir)
        validated_from = _validate_sha(from_sha, field_name="from_sha")
        validated_to = _validate_sha(to_sha, field_name="to_sha")
        repo_root = _repo_root_from_config_dir(config_dir)

        rel_path = f"config/{file}"
        rc, stdout, _stderr = await _run_git(
            "diff",
            f"{validated_from}..{validated_to}",
            "--",
            rel_path,
            cwd=repo_root,
        )
        return GitDiffResponse(
            file=file,
            from_sha=validated_from,
            to_sha=validated_to,
            diff_text=stdout if rc == 0 else "",
        )

    @router.get("/git/status", response_model=GitStatusResponse)
    async def get_git_status(
        request: Request,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
        file: Annotated[str, Query(description="Config file relative to config/")],
    ) -> GitStatusResponse:
        """Return the git tracking state for a config file.

        * ``tracked``: the file appears in git's index.
        * ``has_uncommitted_changes``: the file has local modifications
          not yet committed.
        * ``untracked``: the file exists on disk but has never been added
          to git.

        The three flags are derived from a single ``git status --porcelain``
        invocation so the cost is O(1) subprocess call regardless of the
        repo size (the ``-- <path>`` suffix scopes git's output).
        """
        del _session
        config_dir = _resolve_config_dir(request)
        validated_path = _validate_config_file_param(file, config_dir)
        repo_root = _repo_root_from_config_dir(config_dir)

        rel_path = f"config/{file}"

        # ``git ls-files --error-unmatch`` exits 1 when the file is not
        # tracked, 0 when it is.
        rc_tracked, _out, _err = await _run_git(
            "ls-files",
            "--error-unmatch",
            "--",
            rel_path,
            cwd=repo_root,
        )
        tracked = rc_tracked == 0

        # ``git status --porcelain -- <path>`` produces one or two-char
        # status codes followed by the path.  Non-empty output means
        # either modifications or an untracked file.
        _rc_status, status_out, _serr = await _run_git(
            "status",
            "--porcelain",
            "--",
            rel_path,
            cwd=repo_root,
        )
        status_lines = [ln for ln in status_out.splitlines() if ln.strip()]
        has_uncommitted = bool(status_lines) and tracked
        untracked_flag = bool(status_lines) and not tracked and validated_path.exists()

        return GitStatusResponse(
            file=file,
            tracked=tracked,
            has_uncommitted_changes=has_uncommitted,
            untracked=untracked_flag,
        )

    # ``{config_file:path}`` captures slashes so slugs like ``profiles/small``
    # (story 06a) are routed correctly. ``/files``, ``/path-exists``,
    # ``/resolved``, ``/resolved/diff``, and ``/git/*`` are registered as
    # concrete paths above, so the path-converter routes remain unambiguous.
    @router.get("/schema/{config_file:path}", response_model=FormSchema)
    def get_schema(
        config_file: str,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
    ) -> FormSchema:
        """Return the form-schema metadata for the named config file.

        404 when the slug is not registered — the operator chose a config
        file the framework doesn't yet support.

        Requires a valid session cookie (finding #1, Wave-5 review) —
        the schema reveals the config tree's shape, which is not
        operator-secret material but is still operator-only.
        """
        del _session
        return derive_form_schema(_require_registered(config_file))

    @router.get("/{config_file:path}", response_model=ConfigContentResponse)
    def get_config_contents(
        config_file: str,
        request: Request,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
    ) -> ConfigContentResponse:
        """Return the current on-disk YAML + parsed values (story 06b).

        Editor pages seed their form state from this endpoint at mount.
        Returns 404 when the slug is not registered + 404 when the YAML
        file does not exist on disk (the daemon was started against a
        config tree missing the file).

        Authenticated reads only (the editor reveals the config tree's
        contents; operator-only material).
        """
        del _session
        return _load_config_contents(
            _require_registered(config_file),
            _resolve_config_dir(request),
        )

    @router.put("/{config_file:path}", response_model=ConfigUpdateResponse)
    def put_config(
        config_file: str,
        request: Request,
        body: ConfigUpdateRequest,
        _session: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ConfigUpdateResponse:
        """Atomically replace the named YAML file after layered validation.

        Validation order: parse → cross-reference → semantic. The first
        failing layer's errors are surfaced in the layered envelope
        (``detail`` carries the :class:`ValidationReport` shape); the
        atomic write only fires after all three pass.

        Atomic write semantics inherited from
        :func:`alphamind._kernel.atomic_io.atomic_write_text`: writes
        ``{path}.tmp``, fsyncs, renames onto ``{path}``, fsyncs parent
        directory on non-Windows hosts.

        Gated by both ``current_session`` (finding #1, Wave-5 review)
        and ``csrf_required`` per the project's auth contract for
        mutating verbs (see ``alerts/routes.py:225`` and
        ``control/routes.py:332`` for the same pattern).
        """
        del _session, _csrf
        entry = _require_registered(config_file)
        config_dir = _resolve_config_dir(request)
        target_path = config_dir / entry.filename

        _model, report = run_validation(entry, body.yaml)
        if report.parse or report.cross_reference or report.semantic:
            raise HTTPException(status_code=422, detail=report.model_dump())

        existing_text: str | None = None
        if target_path.exists():
            existing_text = target_path.read_text(encoding="utf-8")

        atomic_write_text(target_path, body.yaml)

        deploy_changed = _deploy_time_field_changed(
            entry,
            existing_text=existing_text,
            proposed_text=body.yaml,
        )

        return ConfigUpdateResponse(
            slug=entry.slug,
            filename=entry.filename,
            deploy_time_fields_changed=deploy_changed,
        )

    return router
