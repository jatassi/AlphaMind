"""Fixture manifest schema and slice loader for the replay harness (story 03).

Defines the on-disk contract for a regime-stratified replay fixture under
``data/replay_fixtures/{regime_label}/{slice_id}/`` plus the loaders the
engine (story 05) consumes. See
``docs/design/02-distillation-layer/replay-harness.md`` § Fixture store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from alphamind.config.models.regimes import Regime

_LOGGER = logging.getLogger("alphamind")

MANIFEST_FILENAME = "manifest.json"
"""Filename of the per-slice manifest the loader parses."""

RAW_INPUTS_FILENAME = "raw_inputs.sqlite"
"""Filename of the per-slice SQLite snapshot the engine (story 05) connects to."""

_SLICE_ID_PATTERN = r"^[a-z0-9_-]+$"


class FixtureManifestError(ValueError):
    """Raised when a fixture manifest fails schema or cross-field validation."""


class FixtureNotFoundError(FileNotFoundError):
    """Raised when a fixture's manifest or raw-input snapshot is missing."""


class SliceSource(StrEnum):
    """Curation provenance for a replay fixture slice."""

    LIVE_ARCHIVE = "live_archive"
    HISTORICAL_CURATED = "historical_curated"


class SliceManifest(BaseModel):
    """Pydantic model for a slice's ``manifest.json`` contract.

    Per ``docs/design/02-distillation-layer/replay-harness.md`` § Fixture
    store. Cross-field validators enforce the mutual exclusion between
    ``source_table_commit_hashes`` (LIVE_ARCHIVE) and
    ``original_ingestion_timestamps`` (HISTORICAL_CURATED).
    """

    model_config = ConfigDict(frozen=True)

    slice_id: str = Field(min_length=1, pattern=_SLICE_ID_PATTERN)
    regime_label: Regime
    source: SliceSource
    invocation_timestamps: list[str] = Field(min_length=1)
    source_table_commit_hashes: dict[str, str] | None
    original_ingestion_timestamps: dict[str, str] | None
    replaces: list[str] = Field(default_factory=list)
    curation_notes: str = Field(min_length=1)
    created_at: str

    @model_validator(mode="after")
    def invocation_timestamps_are_strictly_ascending(self) -> SliceManifest:
        for previous, current in zip(
            self.invocation_timestamps, self.invocation_timestamps[1:], strict=False
        ):
            if previous >= current:
                raise ValueError(
                    "invocation_timestamps must be strictly ascending "
                    f"(got {previous!r} followed by {current!r})"
                )
        return self

    @model_validator(mode="after")
    def source_provenance_fields_match_source(self) -> SliceManifest:
        if self.source is SliceSource.LIVE_ARCHIVE:
            if self.source_table_commit_hashes is None:
                raise ValueError(
                    "source_table_commit_hashes is required when source is LIVE_ARCHIVE"
                )
            if self.original_ingestion_timestamps is not None:
                raise ValueError(
                    "original_ingestion_timestamps must be null when source is LIVE_ARCHIVE"
                )
        else:
            if self.original_ingestion_timestamps is None:
                raise ValueError(
                    "original_ingestion_timestamps is required when source is HISTORICAL_CURATED"
                )
            if self.source_table_commit_hashes is not None:
                raise ValueError(
                    "source_table_commit_hashes must be null when source is HISTORICAL_CURATED"
                )
        return self


@dataclass(frozen=True, slots=True)
class FixtureSlice:
    """A loaded fixture slice — manifest plus the directory it came from.

    Does not pre-load the raw-input table contents. The engine (story 05)
    reads from ``slice_dir / RAW_INPUTS_FILENAME`` lazily into an isolated
    SQLAlchemy session.
    """

    manifest: SliceManifest
    slice_dir: Path


def load_slice_manifest(slice_dir: Path) -> SliceManifest:
    """Read ``slice_dir / manifest.json`` and validate against ``SliceManifest``.

    Raises ``FixtureNotFoundError`` if the manifest file is missing.
    Raises ``FixtureManifestError`` (a ``ValueError`` subclass) when the JSON
    parses but fails schema or cross-field validation; the exception message
    names the offending field via the underlying ``pydantic.ValidationError``.
    """
    manifest_path = slice_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FixtureNotFoundError(f"manifest file not found at {manifest_path}")
    try:
        return SliceManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise FixtureManifestError(f"invalid manifest at {manifest_path}: {exc}") from exc


def load_fixture_slice(slice_dir: Path) -> FixtureSlice:
    """Load and validate a fixture slice directory.

    Validates the manifest, asserts the SQLite snapshot file is present, and
    confirms the directory layout matches the manifest's ``slice_id`` and
    ``regime_label``. Returns a :class:`FixtureSlice`. Does not open the
    SQLite file.
    """
    manifest = load_slice_manifest(slice_dir)
    raw_inputs_path = slice_dir / RAW_INPUTS_FILENAME
    if not raw_inputs_path.is_file():
        raise FixtureNotFoundError(f"{RAW_INPUTS_FILENAME} not found at {raw_inputs_path}")
    if slice_dir.name != manifest.slice_id:
        raise FixtureManifestError(
            f"slice directory basename {slice_dir.name!r} does not match "
            f"manifest slice_id {manifest.slice_id!r}"
        )
    if slice_dir.parent.name != manifest.regime_label:
        raise FixtureManifestError(
            f"slice parent directory {slice_dir.parent.name!r} does not match "
            f"manifest regime_label {manifest.regime_label!r}"
        )
    return FixtureSlice(manifest=manifest, slice_dir=slice_dir)


@dataclass(frozen=True, slots=True)
class FixtureStore:
    """Discovered fixture-store contents.

    ``slices`` maps each canonical regime label to its currently-authoritative
    slices ordered by ``manifest.created_at`` ascending. ``warnings`` carries
    one human-readable line per directory that failed validation and was
    skipped — story 08's CLI surfaces these to the operator.
    """

    slices: dict[str, list[FixtureSlice]]
    warnings: list[str] = field(default_factory=list)


def discover_fixture_store(root: Path) -> FixtureStore:
    """Walk ``root`` and load every fixture slice it contains.

    Slice directories live two levels deep
    (``{root}/{regime_label}/{slice_id}/``). Directories whose manifest fails
    validation are skipped with a warning rather than aborting the walk —
    operator-driven curation should not be blocked by a single malformed
    slice. Superseded slices (those whose ``slice_id`` appears in another
    slice's ``replaces``) are filtered out so only the
    currently-authoritative slice per chain is returned.
    """
    discovered: list[FixtureSlice] = []
    warnings: list[str] = []
    for candidate in sorted(root.glob("*/*")):
        if not candidate.is_dir():
            continue
        if not (candidate / MANIFEST_FILENAME).is_file():
            continue
        try:
            fixture_slice = load_fixture_slice(candidate)
        except (FixtureManifestError, FixtureNotFoundError) as exc:
            warning = f"skipped fixture slice at {candidate}: {exc}"
            warnings.append(warning)
            _LOGGER.warning(warning)
            continue
        _LOGGER.info(
            "discovered fixture slice %s (regime=%s)",
            fixture_slice.manifest.slice_id,
            fixture_slice.manifest.regime_label,
        )
        discovered.append(fixture_slice)

    superseded: set[str] = {
        replaced for fixture_slice in discovered for replaced in fixture_slice.manifest.replaces
    }

    grouped: dict[str, list[FixtureSlice]] = {}
    for fixture_slice in discovered:
        if fixture_slice.manifest.slice_id in superseded:
            continue
        grouped.setdefault(fixture_slice.manifest.regime_label, []).append(fixture_slice)
    for regime_slices in grouped.values():
        regime_slices.sort(key=lambda s: s.manifest.created_at)
    return FixtureStore(slices=grouped, warnings=warnings)
