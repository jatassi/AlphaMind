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
"""

from __future__ import annotations

from typing import Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from alphamind.command_center.config import ReloadPolicy

__all__ = [
    "ReloadPolicy",
    "reload_policy_of",
]


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
