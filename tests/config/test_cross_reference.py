"""Tests for the cross-reference validation layer (story 06a).

The validator runs after parse-time Pydantic checks and asserts every name
reference between YAML files resolves: profile/regime/overlay rule keys exist
in guardrails, agent tools are registered, env-var refs exist, etc.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
    read_env_keys,
)
from alphamind.config.models import (
    AgentsConfig,
    AssetsConfig,
    DigestConfig,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Overlay,
    Profile,
    ProfileConfig,
    Regime,
    SchedulerConfig,
    VenueConfig,
)
from alphamind.config.tools import REGISTERED_TOOLS
from alphamind.config.validation.cross_reference import (
    CrossReferenceError,
    validate_cross_references,
)

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


# Shipped tree once at module scope.
_MAIN = MainConfig.model_validate(_read("main.yaml"))
_SCHEDULER = SchedulerConfig.model_validate(_read("scheduler.yaml"))
_VENUE = VenueConfig.model_validate(_read("venue.yaml"))
_EXECUTION = ExecutionConfig.model_validate(_read("execution.yaml"))
_GUARDRAILS = GuardrailsConfig.model_validate(_read("guardrails.yaml"))
_LLM_FAILURE = LLMFailureConfig.model_validate(_read("llm_failure.yaml"))
_DIGEST = DigestConfig.model_validate(_read("digest.yaml"))
_ASSETS = AssetsConfig.model_validate(_read("assets.yaml"))
_AGENTS = AgentsConfig.model_validate(_read("agents.yaml"))
_PROFILES = load_profiles(CONFIG_DIR)
_REGIMES = load_regimes(CONFIG_DIR)
_MODES = load_modes(CONFIG_DIR)
_OVERLAYS = load_overlays(CONFIG_DIR)
_RUN_TYPES = load_run_types(CONFIG_DIR)


def _make_inputs(
    *,
    main: MainConfig | None = None,
    venue: VenueConfig | None = None,
    guardrails: GuardrailsConfig | None = None,
    agents: AgentsConfig | None = None,
    assets: AssetsConfig | None = None,
    profiles: Mapping[Profile, Any] | None = None,
    regimes: Mapping[Regime, Any] | None = None,
    overlays: Mapping[Any, Any] | None = None,
) -> LoadedConfig:
    return LoadedConfig(
        main=main if main is not None else _MAIN,
        scheduler=_SCHEDULER,
        venue=venue if venue is not None else _VENUE,
        execution=_EXECUTION,
        guardrails=guardrails if guardrails is not None else _GUARDRAILS,
        llm_failure=_LLM_FAILURE,
        digest=_DIGEST,
        assets=assets if assets is not None else _ASSETS,
        agents=agents if agents is not None else _AGENTS,
        profiles=dict(profiles) if profiles is not None else dict(_PROFILES),
        regimes=dict(regimes) if regimes is not None else dict(_REGIMES),
        modes=dict(_MODES),
        overlays=dict(overlays) if overlays is not None else dict(_OVERLAYS),
        run_types=dict(_RUN_TYPES),
    )


# Real env keys from the shipped .env.example, augmented with the venue refs the
# shipped venue.yaml carries (paper + live). Tests that assert env-var failures
# build their own narrow set.
_DEFAULT_ENV_KEYS: frozenset[str] = frozenset(
    {
        "ALPACA_PAPER_KEY",
        "ALPACA_PAPER_SECRET",
        "ALPACA_LIVE_KEY",
        "ALPACA_LIVE_SECRET",
    }
)


def test_shipped_yaml_tree_passes_cross_reference_validation() -> None:
    inputs = _make_inputs()
    validate_cross_references(
        inputs,
        env_keys=_DEFAULT_ENV_KEYS,
        registered_tools=REGISTERED_TOOLS,
    )


# ---------------------------------------------------------------------------
# Check 1: main.active_profile names a key in profiles
# ---------------------------------------------------------------------------


def test_active_profile_missing_from_profiles_bundle_raises() -> None:
    # Build profiles bundle without the active profile (medium); validator
    # must surface that the active profile is unloaded.
    pruned: dict[Profile, ProfileConfig] = {
        p: c for p, c in _PROFILES.items() if p is not Profile.medium
    }
    inputs = _make_inputs(profiles=pruned)
    with pytest.raises(CrossReferenceError, match="medium"):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=REGISTERED_TOOLS,
        )


# ---------------------------------------------------------------------------
# Check 2: every active_sectors entry is a key in assets.sectors
# ---------------------------------------------------------------------------


def test_active_sectors_entry_missing_from_assets_raises() -> None:
    bogus_sector = "missing_sector"
    medium_with_bad_sector = _PROFILES[Profile.medium].model_copy(
        update={"active_sectors": [*_PROFILES[Profile.medium].active_sectors, bogus_sector]}
    )
    profiles = dict(_PROFILES)
    profiles[Profile.medium] = medium_with_bad_sector
    inputs = _make_inputs(profiles=profiles)
    with pytest.raises(CrossReferenceError, match=bogus_sector):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=REGISTERED_TOOLS,
        )


# ---------------------------------------------------------------------------
# Check 3: every profile.rule_values key is a registered guardrail rule id
# ---------------------------------------------------------------------------


def test_profile_rule_values_key_missing_from_guardrails_raises() -> None:
    bogus_rule = "bogus_rule_id"
    medium = _PROFILES[Profile.medium]
    rule_values_with_bogus = {**medium.rule_values, bogus_rule: 5.0}
    medium_with_bad_rule = medium.model_copy(update={"rule_values": rule_values_with_bogus})
    profiles = dict(_PROFILES)
    profiles[Profile.medium] = medium_with_bad_rule
    # Add bogus rule to all regimes so the missing-coverage check (#5) does not
    # also fire — the test exercises check 3 in isolation.
    regimes = {
        regime: config.model_copy(update={"multipliers": {**config.multipliers, bogus_rule: 1.0}})
        for regime, config in _REGIMES.items()
    }
    inputs = _make_inputs(profiles=profiles, regimes=regimes)
    with pytest.raises(CrossReferenceError, match=bogus_rule):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=REGISTERED_TOOLS,
        )


# ---------------------------------------------------------------------------
# Check 5: regime multipliers cover every rule used by any profile
# ---------------------------------------------------------------------------


def test_regime_missing_multiplier_for_profile_rule_raises() -> None:
    # Drop pending_order_capital_pct from the normal regime; this is the rule
    # the design doc was originally silent on, so this exercises the "real"
    # drift case the validator catches.
    rule_to_drop = "pending_order_capital_pct"
    normal = _REGIMES[Regime.normal]
    pruned_multipliers = {k: v for k, v in normal.multipliers.items() if k != rule_to_drop}
    broken_normal = normal.model_copy(update={"multipliers": pruned_multipliers})
    regimes = dict(_REGIMES)
    regimes[Regime.normal] = broken_normal
    inputs = _make_inputs(regimes=regimes)
    with pytest.raises(CrossReferenceError, match=rule_to_drop):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=REGISTERED_TOOLS,
        )


# ---------------------------------------------------------------------------
# Check 6: overlay multipliers reference guardrail rule ids
# ---------------------------------------------------------------------------


def test_overlay_multiplier_key_missing_from_guardrails_raises() -> None:
    bogus_rule = "bogus_overlay_rule"
    pre_event = _OVERLAYS[Overlay.pre_event]
    pre_event_with_bad = pre_event.model_copy(
        update={"multipliers": {**pre_event.multipliers, bogus_rule: 0.5}}
    )
    overlays: dict[Overlay, Any] = dict(_OVERLAYS)
    overlays[Overlay.pre_event] = pre_event_with_bad
    inputs = _make_inputs(overlays=overlays)
    with pytest.raises(CrossReferenceError, match=bogus_rule):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=REGISTERED_TOOLS,
        )


# ---------------------------------------------------------------------------
# Check 7: venue.alpaca env-var refs are present in .env keys
# ---------------------------------------------------------------------------


def test_venue_paper_api_key_env_missing_from_env_keys_raises() -> None:
    # Drop ALPACA_PAPER_KEY from env keys so the validator surfaces it.
    env_keys = _DEFAULT_ENV_KEYS - {"ALPACA_PAPER_KEY"}
    inputs = _make_inputs()
    with pytest.raises(CrossReferenceError, match="ALPACA_PAPER_KEY"):
        validate_cross_references(
            inputs,
            env_keys=env_keys,
            registered_tools=REGISTERED_TOOLS,
        )


# ---------------------------------------------------------------------------
# Check 8: every agent tool / tool_caps key is in REGISTERED_TOOLS
# ---------------------------------------------------------------------------


def test_agent_tool_not_in_registered_tools_raises() -> None:
    # Drop "validate_guardrail" from the registered set; the analyst's
    # tools allowlist references it and the validator must surface that.
    smaller_tools = REGISTERED_TOOLS - {"validate_guardrail"}
    inputs = _make_inputs()
    with pytest.raises(CrossReferenceError, match="validate_guardrail"):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=smaller_tools,
        )


def test_adaptive_researcher_tool_caps_key_not_registered_raises() -> None:
    # Drop "macro_data" so the adaptive researcher's tool_caps surface as a
    # broken reference. Drop both the tools list reference and the cap so
    # the test exercises the tool_caps-specific check (not the tools-list
    # check) when there's no corresponding tools-list entry.
    smaller_tools = REGISTERED_TOOLS - {"macro_data"}
    inputs = _make_inputs()
    with pytest.raises(CrossReferenceError, match="macro_data"):
        validate_cross_references(
            inputs,
            env_keys=_DEFAULT_ENV_KEYS,
            registered_tools=smaller_tools,
        )


# ---------------------------------------------------------------------------
# Aggregation: every check runs even after one fails; final exception names
# every broken reference.
# ---------------------------------------------------------------------------


def test_validator_aggregates_multiple_failures_into_one_error() -> None:
    # Stack three independent breakages and assert the raised message
    # mentions all three: a bogus active sector (check 2), a missing venue
    # env key (check 7), and an unregistered agent tool (check 8).
    bogus_sector = "missing_sector"
    medium_with_bad_sector = _PROFILES[Profile.medium].model_copy(
        update={"active_sectors": [*_PROFILES[Profile.medium].active_sectors, bogus_sector]}
    )
    profiles = dict(_PROFILES)
    profiles[Profile.medium] = medium_with_bad_sector

    env_keys = _DEFAULT_ENV_KEYS - {"ALPACA_PAPER_KEY"}
    smaller_tools = REGISTERED_TOOLS - {"validate_guardrail"}

    inputs = _make_inputs(profiles=profiles)
    with pytest.raises(CrossReferenceError) as exc_info:
        validate_cross_references(
            inputs,
            env_keys=env_keys,
            registered_tools=smaller_tools,
        )
    message = str(exc_info.value)
    assert bogus_sector in message
    assert "ALPACA_PAPER_KEY" in message
    assert "validate_guardrail" in message


# ---------------------------------------------------------------------------
# read_env_keys: dotenv reader returns the key set
# ---------------------------------------------------------------------------


def test_read_env_keys_returns_keys_from_fixture_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Header comment\n"
        "POLYGON_API_KEY=value-with-equals=signs\n"
        "FRED_API_KEY=secret\n"
        "\n"
        "# Blank line and comment in the middle\n"
        'ALPACA_PAPER_KEY="quoted-value"\n'
    )
    keys = read_env_keys(env_file)
    assert keys == frozenset({"POLYGON_API_KEY", "FRED_API_KEY", "ALPACA_PAPER_KEY"})


def test_read_env_keys_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_env_keys(tmp_path / "does-not-exist.env")
