"""Tests for the fixture manifest schema and slice loader (story 03).

Verifies the on-disk contract for a regime-stratified replay fixture per
``docs/implementation/02-distillation-layer/replay-harness/03-fixture-manifest-and-slice-loader.md``.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from alphamind.distillation.replay_harness.fixtures import (
    RAW_INPUTS_FILENAME,
    FixtureManifestError,
    FixtureNotFoundError,
    FixtureSlice,
    FixtureStore,
    SliceManifest,
    SliceSource,
    discover_fixture_store,
    load_fixture_slice,
    load_slice_manifest,
)


def _live_archive_manifest_kwargs() -> dict[str, object]:
    return {
        "slice_id": "2024_q1_low_vol_a",
        "regime_label": "low_vol",
        "source": SliceSource.LIVE_ARCHIVE,
        "invocation_timestamps": [
            "2024-01-15T13:30:00Z",
            "2024-01-15T14:00:00Z",
            "2024-01-15T14:30:00Z",
        ],
        "source_table_commit_hashes": {
            "ohlcv_bars": "a" * 64,
            "macro_observations": "b" * 64,
        },
        "original_ingestion_timestamps": None,
        "replaces": [],
        "curation_notes": "VIX between 12 and 14 across the window; baseline regime exemplar.",
        "created_at": "2024-02-01T00:00:00Z",
    }


def _historical_curated_manifest_kwargs() -> dict[str, object]:
    return {
        "slice_id": "2020_q1_crisis",
        "regime_label": "crisis",
        "source": SliceSource.HISTORICAL_CURATED,
        "invocation_timestamps": [
            "2020-03-09T13:30:00Z",
            "2020-03-09T14:00:00Z",
        ],
        "source_table_commit_hashes": None,
        "original_ingestion_timestamps": {
            "ohlcv_bars": "2020-03-09T20:00:00Z",
        },
        "replaces": [],
        "curation_notes": "March 2020 crisis exemplar; hand-curated historical window.",
        "created_at": "2024-02-01T00:00:00Z",
    }


def test_slice_source_enum_has_canonical_members() -> None:
    assert SliceSource.LIVE_ARCHIVE.value == "live_archive"
    assert SliceSource.HISTORICAL_CURATED.value == "historical_curated"


def test_slice_manifest_accepts_live_archive_payload() -> None:
    manifest = SliceManifest.model_validate(_live_archive_manifest_kwargs())
    assert manifest.slice_id == "2024_q1_low_vol_a"
    assert manifest.source is SliceSource.LIVE_ARCHIVE
    assert manifest.source_table_commit_hashes is not None
    assert manifest.original_ingestion_timestamps is None


def test_slice_manifest_is_frozen() -> None:
    manifest = SliceManifest.model_validate(_live_archive_manifest_kwargs())
    with pytest.raises((ValueError, TypeError)):
        manifest.slice_id = "different"


def test_slice_manifest_accepts_historical_curated_payload() -> None:
    manifest = SliceManifest.model_validate(_historical_curated_manifest_kwargs())
    assert manifest.source is SliceSource.HISTORICAL_CURATED
    assert manifest.source_table_commit_hashes is None
    assert manifest.original_ingestion_timestamps is not None


def test_slice_manifest_rejects_live_archive_with_null_commit_hashes() -> None:
    kwargs = _live_archive_manifest_kwargs()
    kwargs["source_table_commit_hashes"] = None
    with pytest.raises((ValueError, TypeError), match="source_table_commit_hashes"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_live_archive_with_ingestion_timestamps() -> None:
    kwargs = _live_archive_manifest_kwargs()
    kwargs["original_ingestion_timestamps"] = {"ohlcv_bars": "2024-01-15T13:30:00Z"}
    with pytest.raises((ValueError, TypeError), match="original_ingestion_timestamps"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_historical_curated_with_commit_hashes() -> None:
    kwargs = _historical_curated_manifest_kwargs()
    kwargs["source_table_commit_hashes"] = {"ohlcv_bars": "a" * 64}
    with pytest.raises((ValueError, TypeError), match="source_table_commit_hashes"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_historical_curated_with_null_ingestion_timestamps() -> None:
    kwargs = _historical_curated_manifest_kwargs()
    kwargs["original_ingestion_timestamps"] = None
    with pytest.raises((ValueError, TypeError), match="original_ingestion_timestamps"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_unknown_regime_label() -> None:
    kwargs = _live_archive_manifest_kwargs()
    kwargs["regime_label"] = "panic"
    with pytest.raises((ValueError, TypeError), match="regime_label"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_empty_invocation_timestamps() -> None:
    kwargs = _live_archive_manifest_kwargs()
    kwargs["invocation_timestamps"] = []
    with pytest.raises((ValueError, TypeError), match="invocation_timestamps"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_non_monotonic_invocation_timestamps() -> None:
    kwargs = _live_archive_manifest_kwargs()
    kwargs["invocation_timestamps"] = [
        "2024-01-15T13:30:00Z",
        "2024-01-15T13:30:00Z",  # duplicate
        "2024-01-15T14:00:00Z",
    ]
    with pytest.raises((ValueError, TypeError), match="strictly ascending"):
        SliceManifest.model_validate(kwargs)


def test_slice_manifest_rejects_descending_invocation_timestamps() -> None:
    kwargs = _live_archive_manifest_kwargs()
    kwargs["invocation_timestamps"] = [
        "2024-01-15T14:30:00Z",
        "2024-01-15T13:30:00Z",
    ]
    with pytest.raises((ValueError, TypeError), match="strictly ascending"):
        SliceManifest.model_validate(kwargs)


def test_raw_inputs_filename_constant() -> None:
    assert RAW_INPUTS_FILENAME == "raw_inputs.sqlite"


def test_fixture_slice_is_frozen_dataclass() -> None:
    manifest = SliceManifest.model_validate(_live_archive_manifest_kwargs())
    slice_dir = pathlib.Path("/tmp/replay_fixtures/low_vol/2024_q1_low_vol_a")
    fixture_slice = FixtureSlice(manifest=manifest, slice_dir=slice_dir)
    assert fixture_slice.manifest is manifest
    assert fixture_slice.slice_dir == slice_dir
    with pytest.raises(dataclasses.FrozenInstanceError):
        fixture_slice.__setattr__("manifest", manifest)


def test_fixture_manifest_error_subclasses_value_error() -> None:
    assert issubclass(FixtureManifestError, ValueError)


def test_fixture_not_found_error_subclasses_file_not_found() -> None:
    assert issubclass(FixtureNotFoundError, FileNotFoundError)


def _write_manifest(slice_dir: pathlib.Path, payload: dict[str, object]) -> pathlib.Path:
    slice_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = slice_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return manifest_path


def test_load_slice_manifest_round_trip_live_archive(tmp_path: pathlib.Path) -> None:
    slice_dir = tmp_path / "low_vol" / "2024_q1_low_vol_a"
    payload = _live_archive_manifest_kwargs()
    _write_manifest(slice_dir, payload)

    manifest = load_slice_manifest(slice_dir)

    assert manifest.slice_id == payload["slice_id"]
    assert manifest.source is SliceSource.LIVE_ARCHIVE
    # Round-trip: re-serializing the loaded model produces a structure that
    # parses back to an equal model.
    reparsed = SliceManifest.model_validate(json.loads(manifest.model_dump_json()))
    assert reparsed == manifest


def test_load_slice_manifest_round_trip_historical_curated(tmp_path: pathlib.Path) -> None:
    slice_dir = tmp_path / "crisis" / "2020_q1_crisis"
    payload = _historical_curated_manifest_kwargs()
    _write_manifest(slice_dir, payload)

    manifest = load_slice_manifest(slice_dir)

    assert manifest.source is SliceSource.HISTORICAL_CURATED
    reparsed = SliceManifest.model_validate(json.loads(manifest.model_dump_json()))
    assert reparsed == manifest


def test_load_slice_manifest_raises_when_manifest_file_missing(tmp_path: pathlib.Path) -> None:
    slice_dir = tmp_path / "low_vol" / "absent"
    slice_dir.mkdir(parents=True)
    with pytest.raises(FixtureNotFoundError):
        load_slice_manifest(slice_dir)


def test_load_slice_manifest_raises_fixture_manifest_error_on_validation_failure(
    tmp_path: pathlib.Path,
) -> None:
    slice_dir = tmp_path / "low_vol" / "broken"
    payload = _live_archive_manifest_kwargs()
    payload["source_table_commit_hashes"] = None  # invalid for LIVE_ARCHIVE
    _write_manifest(slice_dir, payload)

    with pytest.raises(FixtureManifestError, match="source_table_commit_hashes"):
        load_slice_manifest(slice_dir)


def _build_valid_slice_dir(
    root: pathlib.Path,
    *,
    payload: dict[str, object] | None = None,
) -> pathlib.Path:
    """Create a fully-formed slice directory (manifest + empty SQLite snapshot)."""
    payload = payload or _live_archive_manifest_kwargs()
    regime_label = str(payload["regime_label"])
    slice_id = str(payload["slice_id"])
    slice_dir = root / regime_label / slice_id
    _write_manifest(slice_dir, payload)
    (slice_dir / RAW_INPUTS_FILENAME).touch()
    return slice_dir


def test_load_fixture_slice_returns_slice_for_valid_layout(tmp_path: pathlib.Path) -> None:
    slice_dir = _build_valid_slice_dir(tmp_path)
    fixture_slice = load_fixture_slice(slice_dir)
    assert isinstance(fixture_slice, FixtureSlice)
    assert fixture_slice.slice_dir == slice_dir
    assert fixture_slice.manifest.slice_id == "2024_q1_low_vol_a"


def test_load_fixture_slice_raises_when_manifest_missing(tmp_path: pathlib.Path) -> None:
    slice_dir = tmp_path / "low_vol" / "no_manifest"
    slice_dir.mkdir(parents=True)
    (slice_dir / RAW_INPUTS_FILENAME).touch()
    with pytest.raises(FixtureNotFoundError):
        load_fixture_slice(slice_dir)


def test_load_fixture_slice_raises_when_raw_inputs_missing(tmp_path: pathlib.Path) -> None:
    slice_dir = tmp_path / "low_vol" / "2024_q1_low_vol_a"
    _write_manifest(slice_dir, _live_archive_manifest_kwargs())
    # no raw_inputs.sqlite created
    with pytest.raises(FixtureNotFoundError, match=RAW_INPUTS_FILENAME):
        load_fixture_slice(slice_dir)


def test_load_fixture_slice_raises_when_slice_id_mismatches_dirname(
    tmp_path: pathlib.Path,
) -> None:
    payload = _live_archive_manifest_kwargs()
    slice_dir = tmp_path / str(payload["regime_label"]) / "different_basename"
    _write_manifest(slice_dir, payload)
    (slice_dir / RAW_INPUTS_FILENAME).touch()
    with pytest.raises(FixtureManifestError, match="slice_id"):
        load_fixture_slice(slice_dir)


def test_load_fixture_slice_raises_when_regime_label_mismatches_parent(
    tmp_path: pathlib.Path,
) -> None:
    payload = _live_archive_manifest_kwargs()
    slice_dir = tmp_path / "normal" / str(payload["slice_id"])  # parent != regime_label
    _write_manifest(slice_dir, payload)
    (slice_dir / RAW_INPUTS_FILENAME).touch()
    with pytest.raises(FixtureManifestError, match="regime_label"):
        load_fixture_slice(slice_dir)


def _slice_payload(
    *,
    slice_id: str,
    regime_label: str,
    created_at: str,
    replaces: list[str] | None = None,
) -> dict[str, object]:
    payload = _live_archive_manifest_kwargs()
    payload["slice_id"] = slice_id
    payload["regime_label"] = regime_label
    payload["created_at"] = created_at
    payload["replaces"] = replaces or []
    return payload


def test_discover_fixture_store_lists_slices_per_regime_ordered_by_created_at(
    tmp_path: pathlib.Path,
) -> None:
    _build_valid_slice_dir(
        tmp_path,
        payload=_slice_payload(
            slice_id="low_vol_b",
            regime_label="low_vol",
            created_at="2024-03-01T00:00:00Z",
        ),
    )
    _build_valid_slice_dir(
        tmp_path,
        payload=_slice_payload(
            slice_id="low_vol_a",
            regime_label="low_vol",
            created_at="2024-02-01T00:00:00Z",
        ),
    )
    _build_valid_slice_dir(
        tmp_path,
        payload=_slice_payload(
            slice_id="normal_a",
            regime_label="normal",
            created_at="2024-02-15T00:00:00Z",
        ),
    )

    store = discover_fixture_store(tmp_path)

    assert isinstance(store, FixtureStore)
    assert set(store.slices.keys()) == {"low_vol", "normal"}
    low_vol_ids = [s.manifest.slice_id for s in store.slices["low_vol"]]
    assert low_vol_ids == ["low_vol_a", "low_vol_b"]  # ascending by created_at
    normal_ids = [s.manifest.slice_id for s in store.slices["normal"]]
    assert normal_ids == ["normal_a"]
    assert store.warnings == []


def test_discover_fixture_store_filters_out_superseded_slices(
    tmp_path: pathlib.Path,
) -> None:
    _build_valid_slice_dir(
        tmp_path,
        payload=_slice_payload(
            slice_id="low_vol_v1",
            regime_label="low_vol",
            created_at="2024-02-01T00:00:00Z",
        ),
    )
    _build_valid_slice_dir(
        tmp_path,
        payload=_slice_payload(
            slice_id="low_vol_v2",
            regime_label="low_vol",
            created_at="2024-03-01T00:00:00Z",
            replaces=["low_vol_v1"],
        ),
    )

    store = discover_fixture_store(tmp_path)

    ids = [s.manifest.slice_id for s in store.slices["low_vol"]]
    assert ids == ["low_vol_v2"]


def test_discover_fixture_store_skips_malformed_slices_with_warning(
    tmp_path: pathlib.Path,
) -> None:
    _build_valid_slice_dir(
        tmp_path,
        payload=_slice_payload(
            slice_id="low_vol_ok",
            regime_label="low_vol",
            created_at="2024-02-01T00:00:00Z",
        ),
    )
    bad_payload = _slice_payload(
        slice_id="low_vol_bad",
        regime_label="low_vol",
        created_at="2024-02-15T00:00:00Z",
    )
    bad_payload["source_table_commit_hashes"] = None  # invalid for LIVE_ARCHIVE
    bad_dir = tmp_path / "low_vol" / "low_vol_bad"
    _write_manifest(bad_dir, bad_payload)
    (bad_dir / RAW_INPUTS_FILENAME).touch()

    store = discover_fixture_store(tmp_path)

    ids = [s.manifest.slice_id for s in store.slices["low_vol"]]
    assert ids == ["low_vol_ok"]
    assert len(store.warnings) == 1
    assert "low_vol_bad" in store.warnings[0]
