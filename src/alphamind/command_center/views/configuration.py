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

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from alphamind._kernel.atomic_io import atomic_write_text
from alphamind.command_center.config import (
    AlertsConfig,
    CommandCenterConfig,
    ReloadPolicy,
    SecurityConfig,
)

__all__ = [
    "ConfigFile",
    "ConfigUpdateRequest",
    "ConfigUpdateResponse",
    "FormFieldSchema",
    "FormSchema",
    "ReloadPolicy",
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


def build_configuration_router() -> APIRouter:
    """Return a fresh ``APIRouter`` for the config editor framework.

    Mount under ``/api/views/config`` in :mod:`app`. The router carries:

    * ``GET /schema/{config_file}`` — form-schema metadata.
    * ``PUT /{config_file}`` — atomic file write after three-layer
      validation passes.
    """
    router = APIRouter(tags=["views:configuration"])

    @router.get("/path-exists")
    def get_path_exists(path: str) -> dict[str, bool]:
        """Backend probe for the :class:`PathInput` control (story 05i).

        Returns ``{"exists": true|false}``. Used by the frontend
        :class:`PathInput` component to render an inline file-existence
        indicator so the operator catches typos before saving.

        Resolves the path literally (no glob, no symlink follow beyond
        :func:`pathlib.Path.exists`'s default). The probe is read-only so
        it doesn't require CSRF; the surrounding session dependency
        (when wired by 04d's authed-layout route) gates it from
        unauthenticated callers.
        """
        return {"exists": Path(path).exists()}

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

    @router.put("/{config_file}", response_model=ConfigUpdateResponse)
    def put_config(
        config_file: str,
        request: Request,
        body: ConfigUpdateRequest,
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
        """
        entry = _REGISTRY.get(config_file)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config file: {config_file!r}",
            )

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
