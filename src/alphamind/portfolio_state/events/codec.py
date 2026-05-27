"""JSON codec for activity-log detail dataclasses.

The detail payloads are frozen dataclasses; SQLite stores them as JSON in the
``activity_log.detail_json`` TEXT column. This module replaces the prior
Pydantic ``model_dump_json`` / ``model_validate_json`` round-trip with an
encoder + decoder that:

* Serializes ``Decimal`` / ``Money`` / ``Price`` to the canonical string repr
  (``str(Decimal('100.50'))`` = ``"100.50"``) — preserves precision exactly
  the way 05b's ``DecimalText`` column does for first-class columns.
* Serializes ``StrEnum`` to its string value.
* Serializes ``datetime`` to ISO 8601 with ``Z`` suffix for tz-aware UTC.
* Serializes tuples as JSON arrays; the decoder reconstructs tuples from the
  declared field type.
* Serializes nested dataclasses recursively (e.g.,
  ``DistillationConfigChangeDetail.changes`` carries
  ``DistillationConfigChange`` items).

The decoder takes the target dataclass type and walks its declared fields,
parsing each field's stored value through the appropriate constructor:

* ``Money`` / ``Price`` → ``signed_money(...)`` / ``price(...)`` (using the
  signed constructor so previously-stored negative P&L round-trips faithfully).
* ``StrEnum`` → enum member lookup by value.
* ``datetime`` → ``datetime.fromisoformat``.
* ``tuple[T, ...]`` → tuple comprehension over the JSON array.
* Nested dataclass → recursive decode.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from dataclasses import is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from alphamind._kernel.money import Money, Price, price, signed_money


def encode_detail(detail: object) -> str:
    """Serialize a frozen detail dataclass to its JSON string repr.

    The returned string round-trips exactly through :func:`decode_detail`
    given the same target type — including ``Money`` / ``Price`` precision,
    ``StrEnum`` values, and ``datetime`` tz-awareness.

    Output uses compact separators (no spaces) matching Pydantic's
    ``model_dump_json`` default so existing substring assertions in
    consumers keep working across the migration.
    """
    return json.dumps(_encode_value(detail), separators=(",", ":"))


def decode_detail[T](payload: str, target_cls: type[T]) -> T:
    """Reconstruct a detail dataclass instance from a JSON string.

    ``target_cls`` is one of the per-event-type detail classes from
    ``portfolio_state.events`` — the codec uses ``dataclasses.fields()`` on
    it to drive field-by-field reconstruction. The generic parameter lets
    callers narrow the return type without an explicit ``cast`` /
    ``isinstance`` check.
    """
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        msg = f"detail payload must be a JSON object; got {type(raw).__name__}"
        raise TypeError(msg)
    return _decode_dataclass(raw, target_cls)


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


def _encode_value(value: Any) -> Any:
    """Recursively encode a Python value to a JSON-serializable shape."""
    encoded = _try_encode_leaf(value)
    if encoded is not _SENTINEL:
        return encoded
    encoded = _try_encode_container(value)
    if encoded is not _SENTINEL:
        return encoded
    msg = f"Cannot encode value of type {type(value).__name__}: {value!r}"
    raise TypeError(msg)


_SENTINEL = object()


def _try_encode_leaf(value: Any) -> Any:
    """Encode JSON-leaf shapes (None / scalars / Decimal / Enum / datetime / date)."""
    if value is None:
        return None
    # bool is a subclass of int; check before int so True/False survive intact.
    if isinstance(value, bool | str | int | float):
        return value
    if isinstance(value, Decimal):
        # ``str(Decimal('100.50'))`` → ``'100.50'`` — preserves the literal
        # the developer typed (no binary-float drift).
        return str(value)
    if isinstance(value, Enum):
        return value.value
    # datetime check must come before date — datetime is a subclass of date.
    if isinstance(value, datetime):
        return _datetime_to_iso_z(value)
    if isinstance(value, date):
        return value.isoformat()
    return _SENTINEL


def _try_encode_container(value: Any) -> Any:
    """Encode container shapes (list / tuple / dict / dataclass) recursively."""
    if isinstance(value, list | tuple):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _encode_value(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode_value(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return _SENTINEL


def _datetime_to_iso_z(timestamp: datetime) -> str:
    """Render a tz-aware UTC datetime as ISO 8601 with ``Z`` suffix.

    Mirrors the convention used in ``state_persistence.invocation_context``
    so timestamps round-trip identically between dataclass-detail JSON and
    first-class ``entry_at`` columns.
    """
    return timestamp.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


def _decode_dataclass[T](raw: dict[str, Any], target_cls: type[T]) -> T:
    """Decode a dict into ``target_cls``, threading per-field decoders.

    ``target_cls`` must be a dataclass type; the caller (the public
    ``decode_detail`` entry point) only hands frozen-dataclass detail classes
    in via ``EVENT_TYPE_TO_DETAIL_CLASS``. Guard at the boundary so a
    misconfigured registry surfaces a clear error instead of an
    ``AttributeError`` from ``dataclasses.fields``.
    """
    if not is_dataclass(target_cls):
        msg = f"decode_detail target {target_cls!r} is not a dataclass"
        raise TypeError(msg)
    resolved_hints = _resolve_field_hints(target_cls)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(target_cls):
        if field.name not in raw:
            # Allow dataclass defaults to fill in (e.g., config_file default).
            continue
        declared_type = resolved_hints.get(field.name, field.type)
        kwargs[field.name] = _decode_value(raw[field.name], declared_type)
    return target_cls(**kwargs)


def _resolve_field_hints(target_cls: type) -> dict[str, Any]:
    """Resolve ``from __future__ import annotations`` string forwards to real types.

    ``get_type_hints`` evaluates the deferred annotations in the dataclass'
    defining module — the codec needs real type objects (not strings) so the
    decoder can dispatch on ``Money`` / ``Price`` / ``datetime`` / nested
    dataclasses via ``isinstance``-style checks. Lookups against an unknown
    module fall back gracefully to a no-op (the per-field loop carries the
    raw string and the by-name fallback handles common scalar shapes).
    """
    module = sys.modules.get(target_cls.__module__)
    globalns = vars(module) if module is not None else {}
    try:
        return get_type_hints(target_cls, globalns=globalns)
    except (NameError, TypeError, AttributeError):
        # Forward-reference resolution fails when the declaring module's
        # globalns lacks a referenced symbol (``NameError``) or the annotation
        # is malformed (``TypeError``/``AttributeError``). Fall back to the
        # by-name decoder; other exceptions surface naturally so a real bug
        # in the decoder isn't masked.
        return {}


def _decode_value(value: Any, declared_type: Any) -> Any:
    """Decode one field value against its declared type annotation."""
    if value is None:
        return None
    # ``from __future__ import annotations`` turns every annotation into a
    # forward-reference string; the field-hint resolver in
    # ``_resolve_field_hints`` converts those back to real type objects, but
    # the by-name fallback handles the case where resolution fails.
    if isinstance(declared_type, str):
        return _decode_value_by_typename(value, declared_type)
    newtype_decoded = _decode_newtype(value, declared_type)
    if newtype_decoded is not _SENTINEL:
        return newtype_decoded
    return _decode_generic_or_concrete(value, declared_type)


def _decode_newtype(value: Any, declared_type: Any) -> Any:
    """Decode a NewType-annotated field (Money / Price).

    Money / Price are ``NewType`` aliases — they appear in resolved hints as
    the NewType callable itself. Dispatch by ``__name__`` so we don't have
    to import them for an identity comparison.
    """
    new_type_name = getattr(declared_type, "__name__", None)
    if isinstance(declared_type, type):
        return _SENTINEL
    if new_type_name == "Money":
        # signed_money admits negative P&L values that previously stored.
        return signed_money(value)
    if new_type_name == "Price":
        return price(value)
    return _SENTINEL


def _decode_generic_or_concrete(value: Any, declared_type: Any) -> Any:
    """Decode a non-NewType annotation — concrete class or generic container."""
    origin = get_origin(declared_type)
    if origin is None:
        return _decode_concrete(value, declared_type)
    if origin in (Union, types.UnionType):
        return _decode_union(value, declared_type)
    if origin is tuple:
        return _decode_tuple(value, declared_type)
    if origin is list:
        return _decode_list(value, declared_type)
    # dict / other generics: pass through; the values are JSON-native scalars.
    return value


def _decode_concrete(value: Any, target: type) -> Any:
    """Decode a value whose declared type is a concrete class (not a generic)."""
    if target is type(None):
        return None
    if isinstance(target, type) and issubclass(target, Enum):
        return target(value)
    # datetime check must come before date — datetime is a subclass of date.
    if isinstance(target, type) and issubclass(target, datetime):
        return datetime.fromisoformat(value)
    if isinstance(target, type) and issubclass(target, date):
        return date.fromisoformat(value)
    if is_dataclass(target):
        return _decode_dataclass(value, target)
    return value


_STRING_TYPENAME_DECODERS: dict[str, Any] = {
    # Money may carry a sign in storage (realized P&L can be negative); decode
    # via signed_money so previously-saved negative values round-trip.
    "Money": signed_money,
    "Price": price,
    "Decimal": Decimal,
    "datetime": datetime.fromisoformat,
    "date": date.fromisoformat,
}


def _decode_value_by_typename(value: Any, typename: str) -> Any:
    """Decode a value whose declared type is the *string* repr of a type.

    With ``from __future__ import annotations``, every field annotation in the
    detail dataclasses comes through as a string like ``"Money"`` or
    ``"datetime"``. The codec interprets the known leaf type names; everything
    else passes through unchanged (JSON-native scalars, plain ``str`` /
    ``int`` / ``float``).
    """
    stripped = typename.replace(" ", "")
    # Strip Optional / None alternatives.
    if stripped.endswith("|None") or stripped.startswith("None|"):
        if value is None:
            return None
        alts = [t for t in stripped.split("|") if t and t != "None"]
        if len(alts) == 1:
            return _decode_value_by_typename(value, alts[0])
        return value
    decoder = _STRING_TYPENAME_DECODERS.get(stripped)
    if decoder is not None:
        return decoder(value)
    # tuple[...], list[...], dict[...] inside string annotations are not
    # used at the leaf level inside detail dataclasses; bail to the
    # JSON-native value.
    return value


def _decode_union(value: Any, declared_type: Any) -> Any:
    """Decode a Union-typed field — typically ``T | None`` or a ``Literal[...]``.

    For ``T | None``: if the value is ``None``, return it; otherwise decode
    against the non-None alternative.
    """
    args = [a for a in get_args(declared_type) if a is not type(None)]
    if value is None:
        return None
    if len(args) == 1:
        return _decode_value(value, args[0])
    # Multiple non-None alternatives (e.g., ``str | int``) — pass through; the
    # detail dataclasses don't currently use these shapes for monetary fields.
    return value


def _decode_tuple(value: Any, declared_type: Any) -> tuple[Any, ...]:
    """Decode a JSON array back into a tuple, honoring the element type."""
    args = get_args(declared_type)
    if not args or len(args) == 1:
        # tuple[T] is invalid; the only zero/one-arg form is bare ``tuple``.
        return tuple(value)
    # Either ``tuple[T, ...]`` (variadic) or ``tuple[T, U, V]`` (positional).
    if len(args) == 2 and args[1] is Ellipsis:
        elem_type = args[0]
        return tuple(_decode_value(item, elem_type) for item in value)
    return tuple(_decode_value(item, t) for item, t in zip(value, args, strict=False))


def _decode_list(value: Any, declared_type: Any) -> list[Any]:
    """Decode a JSON array back into a list with per-element decoding."""
    args = get_args(declared_type)
    if not args:
        return list(value)
    elem_type = args[0]
    return [_decode_value(item, elem_type) for item in value]


__all__ = [
    "Money",
    "Price",
    "decode_detail",
    "encode_detail",
]
