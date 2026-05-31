"""Resolved-config snapshot persistence.

Serializes a ``ResolvedConfig`` to canonical-form JSON, hashes it (SHA-256),
and writes it atomically to the per-invocation provenance directory laid out in
``docs/design/05-execution-layer/state-persistence.md`` § Filesystem snapshot
layout. The hash and reference path are returned for the caller to record on
the invocation record's composition state fields; the database wiring itself
is owned by the not-yet-built state-persistence implementation.

Determinism is the headline requirement: byte-identical inputs produce
byte-identical output across runs and machines so the hash is reproducible
and the feedback loop can join past invocations to the exact composition that
produced their behavior.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from alphamind._kernel.archive_layout import RESOLVED_CONFIG_FILENAME, invocation_archive_dir
from alphamind._kernel.atomic_io import atomic_write_text
from alphamind.config.resolver import ResolvedConfig


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """Return value of :func:`persist_snapshot`.

    The caller writes ``hash`` and ``path`` to the invocation record's
    composition state fields per ``state-persistence.md § Composition state
    fields``; ``feature_flags_snapshot`` populates the inline JSON column on
    the same record.
    """

    hash: str
    path: Path
    feature_flags_snapshot: dict[str, bool]


def _canonical_key(key: Any) -> str:
    """Render a mapping key as the string JSON requires.

    StrEnum/Enum keys (``AgentName.adaptive_researcher``) become their string
    value; everything else passes through ``str``.
    """
    if isinstance(key, Enum):
        return str(key.value)
    return str(key)


def _canonicalize(value: Any) -> Any:
    """Convert a Python value into a JSON-canonical-friendly structure.

    Pydantic models are dumped via ``model_dump_json`` and re-parsed so the
    serializer can apply lexicographic key ordering uniformly through
    ``json.dumps(sort_keys=True)``. StrEnum members convert to their string
    value. Mappings (including ``MappingProxyType`` views from the resolver)
    and sequences recurse so nested structures stay canonical.
    """
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {_canonical_key(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonicalize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _resolved_to_dict(resolved: ResolvedConfig) -> dict[str, Any]:
    """Walk ``ResolvedConfig`` and produce a canonical-friendly dict.

    ``dataclasses.asdict`` is avoided here because it recursively converts
    Pydantic submodels via attribute access, dropping ``model_dump_json``'s
    field-coverage guarantees. Iterating over the dataclass fields directly
    keeps each top-level value processed by :func:`_canonicalize`.
    """
    return {
        field.name: _canonicalize(getattr(resolved, field.name))
        for field in dataclasses.fields(resolved)
    }


def serialize_resolved_config(resolved: ResolvedConfig) -> str:
    """Produce the canonical-form JSON string for a ``ResolvedConfig``.

    The output is byte-identical for byte-identical inputs across runs and
    machines: keys are lexicographically sorted at every level, Pydantic
    submodels round-trip through ``model_dump_json`` so their nested
    field ordering is also lexicographic, and StrEnum members serialize as
    their string ``.value``. Floats use Python's default ``repr()`` precision
    via ``json.dumps`` — no rounding coercion.
    """
    payload = _resolved_to_dict(resolved)
    return json.dumps(payload, sort_keys=True)


def compute_snapshot_hash(serialized: str) -> str:
    """Return the 64-hex-character SHA-256 digest of the canonical JSON."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def feature_flags_snapshot(resolved: ResolvedConfig) -> dict[str, bool]:
    """Extract the flat feature-flag map from ``ResolvedConfig.feature_flags``.

    The result populates the invocation record's inline ``feature_flags_snapshot``
    JSON column per ``state-persistence.md § Composition state fields``. The
    return shape mirrors ``FeatureFlags.model_dump()`` exactly — every field on
    that model is annotated ``bool``, so the projection is value-preserving.
    """
    return {name: bool(value) for name, value in resolved.feature_flags.model_dump().items()}


def persist_snapshot(
    resolved: ResolvedConfig, *, archive_root: Path, invocation_id: str, as_of: datetime
) -> SnapshotResult:
    """Serialize, hash, and atomically write the snapshot for one invocation.

    Writes to ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/<RESOLVED_CONFIG_FILENAME>``
    (date-partitioned canonical layout per ALP-689 followup), creating intermediate
    directories. The caller is responsible for resolving any ``%USERPROFILE%``
    expansions in ``archive_root`` before passing the path in.
    """
    serialized = serialize_resolved_config(resolved)
    digest = compute_snapshot_hash(serialized)
    flags = feature_flags_snapshot(resolved)

    snapshot_path = (
        invocation_archive_dir(archive_root=archive_root, as_of=as_of, invocation_id=invocation_id)
        / RESOLVED_CONFIG_FILENAME
    )
    atomic_write_text(snapshot_path, serialized)

    return SnapshotResult(hash=digest, path=snapshot_path, feature_flags_snapshot=flags)
