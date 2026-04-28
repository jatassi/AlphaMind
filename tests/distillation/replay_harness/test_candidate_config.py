"""Verify the candidate-config loader for the replay harness (story 04)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from alphamind.distillation.replay_harness.candidate_config import (
    CandidateConfigError,
    LoadedCandidateConfig,
    compute_report_id,
    load_candidate_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_CONFIG_PATH = REPO_ROOT / "config" / "distillation.yaml"


def test_round_trip_happy_path_preserves_field_values_and_hash() -> None:
    loaded = load_candidate_config(CANONICAL_CONFIG_PATH)

    file_bytes = CANONICAL_CONFIG_PATH.read_bytes()
    expected_hash = hashlib.sha256(file_bytes).hexdigest()

    assert isinstance(loaded, LoadedCandidateConfig)
    assert loaded.path == CANONICAL_CONFIG_PATH.resolve()
    assert loaded.content_hash == expected_hash
    assert loaded.content_bytes_size == len(file_bytes)
    # Field values from the canonical YAML pass through unchanged.
    assert loaded.config.anomaly_detection.volume_anomaly_sigma == 2.5
    assert loaded.config.regime_classification.regime_normal_vix_max == 22.0
    assert loaded.config.persistence_windows.volume_baseline_days == 20


def test_load_candidate_config_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_candidate_config(tmp_path / "does_not_exist.yaml")


def test_load_candidate_config_malformed_yaml_raises_candidate_config_error(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(":::\n", encoding="utf-8")
    with pytest.raises(CandidateConfigError):
        load_candidate_config(bad)


def _canonical_yaml_text() -> str:
    return CANONICAL_CONFIG_PATH.read_text(encoding="utf-8")


def test_load_candidate_config_missing_required_field_reports_field_path(
    tmp_path: Path,
) -> None:
    parsed = yaml.safe_load(_canonical_yaml_text())
    del parsed["anomaly_detection"]["volume_anomaly_sigma"]
    target = tmp_path / "missing.yaml"
    target.write_text(yaml.safe_dump(parsed), encoding="utf-8")
    with pytest.raises(CandidateConfigError, match="volume_anomaly_sigma"):
        load_candidate_config(target)


def test_load_candidate_config_invariant_violation_raises_candidate_config_error(
    tmp_path: Path,
) -> None:
    # Break the regime-boundary monotonicity invariant
    # (regime_normal_vix_max must equal regime_elevated_vix_min).
    parsed = yaml.safe_load(_canonical_yaml_text())
    parsed["regime_classification"]["regime_normal_vix_max"] = 22.0
    parsed["regime_classification"]["regime_elevated_vix_min"] = 21.0
    target = tmp_path / "invariant.yaml"
    target.write_text(yaml.safe_dump(parsed), encoding="utf-8")
    with pytest.raises(
        CandidateConfigError, match="regime_normal_vix_max must equal regime_elevated_vix_min"
    ):
        load_candidate_config(target)


def test_content_hash_is_deterministic_across_loads(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.yaml"
    target.write_bytes(CANONICAL_CONFIG_PATH.read_bytes())
    first = load_candidate_config(target)
    second = load_candidate_config(target)
    assert first.content_hash == second.content_hash


def test_content_hash_changes_on_single_byte_difference(tmp_path: Path) -> None:
    base_bytes = CANONICAL_CONFIG_PATH.read_bytes()
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_bytes(base_bytes)
    # Append a single trailing whitespace byte; YAML semantics unchanged but
    # raw-byte hash MUST differ — the operator's edited file IS the artifact
    # under audit.
    b.write_bytes(base_bytes + b" ")
    loaded_a = load_candidate_config(a)
    loaded_b = load_candidate_config(b)
    assert loaded_a.content_hash != loaded_b.content_hash


def test_compute_report_id_single_mode_returns_timestamp_and_truncated_hash() -> None:
    timestamp = datetime(2026, 4, 28, 12, 30, 45, tzinfo=UTC)
    candidate = "abcdef0123456789" + "f" * 48  # 64 hex chars
    report_id = compute_report_id(timestamp, candidate, None)
    assert report_id == "20260428T123045Z_abcdef0123456789"


def test_compute_report_id_diff_mode_returns_both_truncated_hashes() -> None:
    timestamp = datetime(2026, 4, 28, 12, 30, 45, tzinfo=UTC)
    candidate = "abcdef0123456789" + "f" * 48
    baseline = "fedcba9876543210" + "0" * 48
    report_id = compute_report_id(timestamp, candidate, baseline)
    assert report_id == "20260428T123045Z_abcdef0123456789_fedcba9876543210"


def test_compute_report_id_rejects_naive_datetime() -> None:
    naive = datetime(2026, 4, 28, 12, 30, 45, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError, match="tz-aware"):
        compute_report_id(naive, "abc", None)


def test_compute_report_id_rejects_non_utc_tzinfo() -> None:
    eastern = timezone(timedelta(hours=-4))
    not_utc = datetime(2026, 4, 28, 12, 30, 45, tzinfo=eastern)
    with pytest.raises(ValueError, match="UTC"):
        compute_report_id(not_utc, "abc", None)
