"""Tests for resolved-config snapshot persistence (story 07).

These tests exercise ``serialize_resolved_config``, ``compute_snapshot_hash``,
``persist_snapshot``, and ``feature_flags_snapshot`` over a fixture
``ResolvedConfig`` built from the shipped ``config/`` tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
)
from alphamind.config.models import (
    AgentsConfig,
    AssetsConfig,
    ContinuousMonitorConfig,
    DigestConfig,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Mode,
    Profile,
    Regime,
    RegimeConfig,
    ResolvedConfig,
    RuntimeDimensions,
    RunType,
    SchedulerConfig,
    VenueConfig,
    compose_config,
)
from alphamind.config.snapshot import (
    SnapshotResult,
    compute_snapshot_hash,
    feature_flags_snapshot,
    persist_snapshot,
    serialize_resolved_config,
)

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


# Shipped tree once — every test composes from these.
_MAIN = MainConfig.model_validate(_read("main.yaml"))
_SCHEDULER = SchedulerConfig.model_validate(_read("scheduler.yaml"))
_VENUE = VenueConfig.model_validate(_read("venue.yaml"))
_EXECUTION = ExecutionConfig.model_validate(_read("execution.yaml"))
_GUARDRAILS = GuardrailsConfig.model_validate(_read("guardrails.yaml"))
_LLM_FAILURE = LLMFailureConfig.model_validate(_read("llm_failure.yaml"))
_DIGEST = DigestConfig.model_validate(_read("digest.yaml"))
_ASSETS = AssetsConfig.model_validate(_read("assets.yaml"))
_AGENTS = AgentsConfig.model_validate(_read("agents.yaml"))
_CONTINUOUS_MONITOR = ContinuousMonitorConfig.model_validate(_read("continuous_monitor.yaml"))
_PROFILES = load_profiles(CONFIG_DIR)
_RAW_REGIMES = load_regimes(CONFIG_DIR)
_MODES = load_modes(CONFIG_DIR)
_OVERLAYS = load_overlays(CONFIG_DIR)
_RUN_TYPES = load_run_types(CONFIG_DIR)


def _aligned_regimes() -> dict[Regime, RegimeConfig]:
    """Patch shipped regime YAMLs to cover every profile rule.

    Same alignment helper used in ``test_resolver.py`` — the shipped regimes
    predate the ``pending_order_capital_pct`` rule landing in profiles, so the
    resolver would raise ``KeyError``. Aligning here keeps these tests focused
    on serialization rather than entangling them with the doc-vs-config drift
    story 06a will resolve.
    """
    aligned: dict[Regime, RegimeConfig] = {}
    for regime, config in _RAW_REGIMES.items():
        multipliers = dict(config.multipliers)
        multipliers.setdefault("pending_order_capital_pct", 1.0)
        aligned[regime] = config.model_copy(update={"multipliers": multipliers})
    return aligned


_REGIMES = _aligned_regimes()


def _fixture_resolved() -> ResolvedConfig:
    """Compose a deterministic fixture ``ResolvedConfig`` for snapshot tests."""
    inputs = LoadedConfig(
        main=_MAIN.model_copy(update={"active_profile": Profile.medium}),
        scheduler=_SCHEDULER,
        venue=_VENUE,
        execution=_EXECUTION,
        guardrails=_GUARDRAILS,
        llm_failure=_LLM_FAILURE,
        digest=_DIGEST,
        assets=_ASSETS,
        agents=_AGENTS,
        continuous_monitor=_CONTINUOUS_MONITOR,
        profiles=dict(_PROFILES),
        regimes=dict(_REGIMES),
        modes=dict(_MODES),
        overlays=dict(_OVERLAYS),
        run_types=dict(_RUN_TYPES),
    )
    runtime = RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.pre_open,
    )
    return compose_config(inputs, runtime)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_serialize_resolved_config_is_byte_identical_for_repeated_calls() -> None:
    resolved = _fixture_resolved()
    first = serialize_resolved_config(resolved)
    second = serialize_resolved_config(resolved)
    assert first == second


# ---------------------------------------------------------------------------
# Hash shape and pinning
# ---------------------------------------------------------------------------


def test_compute_snapshot_hash_returns_64_hex_chars() -> None:
    resolved = _fixture_resolved()
    digest = compute_snapshot_hash(serialize_resolved_config(resolved))
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_pinned_fixture_hash_matches_known_value() -> None:
    """Regression-fixture pin per AC line 67.

    A change to the canonical-form bytes (key ordering, enum serialization,
    float formatting) forces a deliberate update of this expected value. If
    the fixture inputs change (config YAMLs evolve), the new expected value
    is whatever ``compute_snapshot_hash(serialize_resolved_config(_fixture_resolved()))``
    returns — pin it once and treat future drift as a signal to investigate.
    """
    resolved = _fixture_resolved()
    digest = compute_snapshot_hash(serialize_resolved_config(resolved))
    # Pin updated 2026-05-26 (ALP-664): scheduler.yaml gained the
    # ``control_port`` field (default 8765) so the canonical bytes
    # shifted; the YAML default preserves the historical behaviour but
    # the serialised form now carries the extra key.
    expected = "dcae35c62ba30b82f5f08614c9ff5640676bc6f4221865e65f048146663d0002"
    assert digest == expected, (
        f"Snapshot hash drift detected. Got {digest}; expected {expected}. "
        f"If the inputs intentionally changed, update the pinned value."
    )


# ---------------------------------------------------------------------------
# Persist contract
# ---------------------------------------------------------------------------


def test_persist_snapshot_writes_to_documented_path_layout(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-001"
    result = persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id)
    expected = tmp_path / "invocations" / invocation_id / "resolved_config.json"
    assert result.path == expected
    assert expected.exists()


def test_persist_snapshot_file_content_equals_serialize_output(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-002"
    persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id)
    written = (tmp_path / "invocations" / invocation_id / "resolved_config.json").read_text()
    assert written == serialize_resolved_config(resolved)


def test_persist_snapshot_creates_intermediate_directories(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-003"
    deeply_nested = tmp_path / "does" / "not" / "yet" / "exist" / "provenance"
    persist_snapshot(resolved, archive_root=deeply_nested, invocation_id=invocation_id)
    assert (deeply_nested / "invocations" / invocation_id / "resolved_config.json").exists()


def test_persist_snapshot_leaves_no_tmp_file_on_success(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-004"
    persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id)
    invocation_dir = tmp_path / "invocations" / invocation_id
    tmp_files = list(invocation_dir.glob("*.tmp"))
    assert tmp_files == [], f"Expected no .tmp files; found {tmp_files}"


# ---------------------------------------------------------------------------
# StrEnum serialization contract
# ---------------------------------------------------------------------------


def test_strenum_fields_serialize_as_string_value_not_member_name() -> None:
    """``analyst_output_mode`` is a StrEnum; output must use ``.value``."""
    resolved = _fixture_resolved()
    serialized = serialize_resolved_config(resolved)
    payload = json.loads(serialized)
    # AnalystOutputMode.proposals → "proposals", not "AnalystOutputMode.proposals"
    assert payload["analyst_output_mode"] == "proposals"
    # ExecutionMode is a StrEnum on MainConfig — pass-through scalar on the dataclass
    assert payload["execution_mode"] == _MAIN.execution_mode.value


# ---------------------------------------------------------------------------
# feature_flags_snapshot contract
# ---------------------------------------------------------------------------


def test_feature_flags_snapshot_returns_flat_dict_equal_to_model_dump() -> None:
    resolved = _fixture_resolved()
    snapshot = feature_flags_snapshot(resolved)
    assert snapshot == resolved.feature_flags.model_dump(mode="json")
    # Type contract: dict[str, bool]
    for key, value in snapshot.items():
        assert isinstance(key, str)
        assert isinstance(value, bool)


# ---------------------------------------------------------------------------
# Round-trippability
# ---------------------------------------------------------------------------


def test_persisted_json_is_round_trippable(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-005"
    persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id)
    written = (tmp_path / "invocations" / invocation_id / "resolved_config.json").read_text()
    payload = json.loads(written)
    assert isinstance(payload, dict)
    # Spot-check a few expected top-level keys
    assert "rule_values" in payload
    assert "feature_flags" in payload
    assert "active_overlays" in payload


# ---------------------------------------------------------------------------
# SnapshotResult contract
# ---------------------------------------------------------------------------


def test_persist_snapshot_returns_snapshot_result_with_hash_path_and_flags(
    tmp_path: Path,
) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-006"
    result = persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id)
    assert isinstance(result, SnapshotResult)
    assert result.hash == compute_snapshot_hash(serialize_resolved_config(resolved))
    assert result.path == tmp_path / "invocations" / invocation_id / "resolved_config.json"
    assert result.feature_flags_snapshot == feature_flags_snapshot(resolved)
