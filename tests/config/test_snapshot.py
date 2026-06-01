"""Tests for resolved-config snapshot persistence (story 07).

These tests exercise ``serialize_resolved_config``, ``compute_snapshot_hash``,
``persist_snapshot``, and ``feature_flags_snapshot`` over a fixture
``ResolvedConfig`` built from the shipped ``config/`` tree.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

# Canonical test invocation timestamp; date partition is "2026-04-27".
_AS_OF = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)


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
    # Pin updated 2026-05-26 (ALP-128 merge into main): canonical bytes shift
    # from the ALP-128 work tree's added scheduler.yaml.control_port (ALP-664)
    # combined with main's PR #200 latency_budget bump (300 -> 500), PR #201/202
    # risk/translator config updates, and the merge-time resolution.
    # Pin updated 2026-05-26 (analyst output_token_budget 6000 -> 100000):
    # --fresh-start debug-e2e run caught the analyst hitting the per-response
    # cap with output_tokens=30400 across 15 turns; the resolved-config
    # snapshot carries the new budget verbatim.
    # Pin updated 2026-05-26 (CTRA removed from energy sector): CTRA was
    # acquired by DVN on 2026-05-07 and delisted; ALP-584/585 closed the
    # data-layer detection, this removes the stale entry from config/assets.yaml.
    # Pin updated 2026-05-27 (researcher latency budgets 600 -> 900s, PR #222):
    # ALP-707/PR #222 raised the equity_researcher/macro_researcher/options_researcher/
    # sentiment_researcher latency_budget_s from 600 to 900 in config/agents.yaml,
    # shifting the resolved-config canonical bytes.
    # Pin updated 2026-05-27 (borrow_accrual_tick_local_time knob, ALP-718):
    # ALP-718 added borrow_accrual_tick_local_time: "16:00" to
    # config/continuous_monitor.yaml and ContinuousMonitorConfig, shifting the
    # resolved-config canonical bytes.
    # Pin updated 2026-05-28 (breach_loop_consecutive_failure_alert_threshold, ALP-732):
    # ALP-732 added breach_loop_consecutive_failure_alert_threshold: 3 to
    # config/continuous_monitor.yaml and ContinuousMonitorConfig (sustained
    # breach-loop-failure escalation), shifting the resolved-config canonical bytes.
    # Pin updated 2026-05-28 (entry_window_evaluation_cadence_seconds knob, ALP-737):
    # ALP-737 added entry_window_evaluation_cadence_seconds: 60.0 to
    # config/continuous_monitor.yaml and ContinuousMonitorConfig (the entry-window
    # expiry watcher's cadence); this value is the combined hash with ALP-732's
    # knob present after the merge into main.
    # Pin updated 2026-05-28 (marketable_entry_bps_through_touch knob, ALP-738):
    # ALP-738 added marketable_entry_bps_through_touch: 5.0 to config/execution.yaml
    # and ExecutionConfig (basis points past the touch when re-pricing an enter-now
    # entry into a marketable limit), shifting the resolved-config canonical bytes.
    # Pin updated 2026-05-28 (entry_window_max_reprices knob, ALP-740):
    # ALP-740 added entry_window_max_reprices: 2 to config/continuous_monitor.yaml
    # and ContinuousMonitorConfig (the reprice/escalate loop bound for patient-retest
    # entries at the entry_window deadline), shifting the resolved-config canonical bytes.
    # Pin updated 2026-05-29 (Tier B schedule, ALP-745): config/scheduler.yaml's
    # triggers block dropped from six cron entries to the four Tier B triggers
    # (pre_open 09:00, market_hours_rolling 13:00, pre_close 15:30, weekend_sunday
    # 18:00), shifting the resolved-config canonical bytes.
    # Pin updated 2026-05-29 (Opus 4.7 -> 4.8 upgrade): the analyst, strategist,
    # and portfolio_manager model in config/agents.yaml moved from claude-opus-4-7
    # to claude-opus-4-8 (AllowedModel.opus_4_8), shifting the resolved-config
    # canonical bytes.
    # Pin updated 2026-06-01 (fill-backfill backstop knobs, ALP-763): ALP-763 added
    # fill_backfill_interval_seconds: 900 and fill_backfill_lookback_seconds: 259200
    # to config/continuous_monitor.yaml and ContinuousMonitorConfig (the periodic
    # fill-backfill backstop), shifting the resolved-config canonical bytes.
    expected = "782912b92a27ba12e2ad168b94196e560a2c4f9f5310732a530934b2aad71028"
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
    result = persist_snapshot(
        resolved, archive_root=tmp_path, invocation_id=invocation_id, as_of=_AS_OF
    )
    expected = tmp_path / "2026-04-27" / invocation_id / "resolved_config.json"
    assert result.path == expected
    assert expected.exists()


def test_persist_snapshot_file_content_equals_serialize_output(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-002"
    persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id, as_of=_AS_OF)
    written = (tmp_path / "2026-04-27" / invocation_id / "resolved_config.json").read_text()
    assert written == serialize_resolved_config(resolved)


def test_persist_snapshot_creates_intermediate_directories(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-003"
    deeply_nested = tmp_path / "does" / "not" / "yet" / "exist" / "provenance"
    persist_snapshot(
        resolved, archive_root=deeply_nested, invocation_id=invocation_id, as_of=_AS_OF
    )
    assert (deeply_nested / "2026-04-27" / invocation_id / "resolved_config.json").exists()


def test_persist_snapshot_leaves_no_tmp_file_on_success(tmp_path: Path) -> None:
    resolved = _fixture_resolved()
    invocation_id = "inv-20260427-004"
    persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id, as_of=_AS_OF)
    invocation_dir = tmp_path / "2026-04-27" / invocation_id
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
    persist_snapshot(resolved, archive_root=tmp_path, invocation_id=invocation_id, as_of=_AS_OF)
    written = (tmp_path / "2026-04-27" / invocation_id / "resolved_config.json").read_text()
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
    result = persist_snapshot(
        resolved, archive_root=tmp_path, invocation_id=invocation_id, as_of=_AS_OF
    )
    assert isinstance(result, SnapshotResult)
    assert result.hash == compute_snapshot_hash(serialize_resolved_config(resolved))
    assert result.path == tmp_path / "2026-04-27" / invocation_id / "resolved_config.json"
    assert result.feature_flags_snapshot == feature_flags_snapshot(resolved)
