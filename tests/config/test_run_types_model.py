"""Tests for src/alphamind/config/models/run_types.py — run-type bundle model (story 04e)."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"
RUN_TYPES_DIR = CONFIG_DIR / "run_types"


def _full_decision_layer() -> list[str]:
    return ["analyst", "strategist", "portfolio_manager", "synthesizer"]


def _full_roster() -> list[str]:
    return [
        "tech_semis_researcher",
        "financials_researcher",
        "energy_researcher",
        "qualitative_researcher",
        "adaptive_researcher",
        "synthesizer",
        "analyst",
        "strategist",
        "portfolio_manager",
    ]


def _valid_run_type_raw() -> dict[str, Any]:
    return {
        "agents": {
            "enabled": _full_roster(),
            "overrides": {
                "adaptive_researcher": {
                    "cumulative_tool_call_limit": 25,
                    "cumulative_tool_token_budget": 4000,
                    "latency_budget_seconds": 300,
                },
            },
        },
        "qualitative_researcher": {
            "news_digest": {
                "top_n_per_sector": 5,
                "top_n_high_priority": 3,
            },
        },
    }


def test_valid_run_type_parses_cleanly() -> None:
    from alphamind.config.models import RunTypeConfig

    config = RunTypeConfig.model_validate(_valid_run_type_raw())
    assert "adaptive_researcher" in {a.value for a in config.agents.enabled}
    assert config.qualitative_researcher.news_digest.top_n_per_sector == 5


def test_run_type_models_are_frozen() -> None:
    from alphamind.config.models import (
        AgentsSection,
        NewsDigestConfig,
        QualitativeResearcherSection,
        RunTypeConfig,
    )

    config = RunTypeConfig.model_validate(_valid_run_type_raw())

    assert isinstance(config, RunTypeConfig)
    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("agents", None)

    assert isinstance(config.agents, AgentsSection)
    with pytest.raises((ValueError, TypeError)):
        config.agents.__setattr__("enabled", [])

    assert isinstance(config.qualitative_researcher, QualitativeResearcherSection)
    with pytest.raises((ValueError, TypeError)):
        config.qualitative_researcher.__setattr__("news_digest", None)

    assert isinstance(config.qualitative_researcher.news_digest, NewsDigestConfig)
    with pytest.raises((ValueError, TypeError)):
        config.qualitative_researcher.news_digest.__setattr__("top_n_per_sector", 0)


def test_run_type_rejects_missing_analyst_in_enabled() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [a for a in raw["agents"]["enabled"] if a != "analyst"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_missing_strategist_in_enabled() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [a for a in raw["agents"]["enabled"] if a != "strategist"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_missing_portfolio_manager_in_enabled() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [a for a in raw["agents"]["enabled"] if a != "portfolio_manager"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_missing_synthesizer_in_enabled() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [a for a in raw["agents"]["enabled"] if a != "synthesizer"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_empty_enabled_list() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = []
    raw["agents"]["overrides"] = {}
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_duplicate_in_enabled() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [*_full_roster(), "analyst"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_unknown_override_field() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["overrides"]["adaptive_researcher"]["unknown_field"] = 1
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_adaptive_only_field_on_non_adaptive_agent() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["overrides"]["analyst"] = {"cumulative_tool_call_limit": 25}
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_rejects_adaptive_only_token_budget_on_non_adaptive_agent() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["overrides"]["strategist"] = {"cumulative_tool_token_budget": 1000}
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_accepts_latency_budget_seconds_on_adaptive() -> None:
    from alphamind.config.models import AgentName, RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["overrides"]["adaptive_researcher"] = {"latency_budget_seconds": 300}
    config = RunTypeConfig.model_validate(raw)
    assert config.agents.overrides[AgentName.adaptive_researcher]["latency_budget_seconds"] == 300


def test_run_type_accepts_context_token_budget_override() -> None:
    from alphamind.config.models import AgentName, RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["overrides"]["analyst"] = {"context_token_budget": 4000}
    config = RunTypeConfig.model_validate(raw)
    assert config.agents.overrides[AgentName.analyst]["context_token_budget"] == 4000


def test_run_type_accepts_output_token_budget_override() -> None:
    from alphamind.config.models import AgentName, RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["overrides"]["portfolio_manager"] = {"output_token_budget": 2500}
    config = RunTypeConfig.model_validate(raw)
    assert config.agents.overrides[AgentName.portfolio_manager]["output_token_budget"] == 2500


def test_run_type_rejects_override_for_disabled_agent() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [a for a in _full_roster() if a != "adaptive_researcher"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_news_digest_rejects_zero_top_n_per_sector() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["qualitative_researcher"]["news_digest"]["top_n_per_sector"] = 0
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_news_digest_accepts_zero_top_n_high_priority() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["qualitative_researcher"]["news_digest"]["top_n_high_priority"] = 0
    config = RunTypeConfig.model_validate(raw)
    assert config.qualitative_researcher.news_digest.top_n_high_priority == 0


def test_run_type_rejects_unknown_agent_name_in_enabled() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"]["enabled"] = [*_full_roster(), "unknown_agent"]
    with pytest.raises((ValueError, TypeError)):
        RunTypeConfig.model_validate(raw)


def test_run_type_enum_has_seven_members_including_emergency() -> None:
    from alphamind.config.models import RunType

    assert {member.value for member in RunType} == {
        "market_open",
        "market_hours_rolling",
        "pre_close",
        "off_hours_rolling",
        "weekend_saturday",
        "weekend_sunday",
        "emergency",
    }


def test_run_type_emergency_member_value() -> None:
    from alphamind.config.models import RunType

    assert RunType.emergency.value == "emergency"


# --------------------------------------------------------------------------- #
# Loader tests
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def test_every_shipped_run_type_yaml_parses_cleanly() -> None:
    from alphamind.config.loaders import load_run_types
    from alphamind.config.models import RunType

    bundle = load_run_types(CONFIG_DIR)
    assert set(bundle.keys()) == set(RunType)
    assert len(bundle) == 7


def test_load_run_types_returns_immutable_mapping() -> None:
    from alphamind.config.loaders import load_run_types

    bundle = load_run_types(CONFIG_DIR)
    with pytest.raises(TypeError):
        cast(Any, bundle)["market_open"] = None


def test_load_run_types_raises_when_file_missing(tmp_path: Path) -> None:
    from alphamind.config.loaders import load_run_types

    # Build a config dir with only some of the run types present.
    target = tmp_path / "config"
    target.mkdir()
    (target / "run_types").mkdir()
    market_open_raw = _valid_run_type_raw()
    (target / "run_types" / "market_open.yaml").write_text(yaml.safe_dump(market_open_raw))

    with pytest.raises(FileNotFoundError):
        load_run_types(target)


@pytest.mark.parametrize(
    ("filename", "adaptive_present"),
    [
        ("market_open.yaml", True),
        ("market_hours_rolling.yaml", True),
        ("pre_close.yaml", True),
        ("weekend_sunday.yaml", True),
        ("off_hours_rolling.yaml", False),
        ("weekend_saturday.yaml", False),
        ("emergency.yaml", True),
    ],
)
def test_run_type_yaml_adaptive_researcher_membership(
    filename: str, adaptive_present: bool
) -> None:
    raw = _read_yaml(RUN_TYPES_DIR / filename)
    assert ("adaptive_researcher" in raw["agents"]["enabled"]) is adaptive_present


@pytest.mark.parametrize(
    "filename",
    [
        "market_open.yaml",
        "market_hours_rolling.yaml",
        "pre_close.yaml",
        "off_hours_rolling.yaml",
        "weekend_saturday.yaml",
        "weekend_sunday.yaml",
        "emergency.yaml",
    ],
)
def test_every_run_type_carries_decision_layer_and_synthesizer(filename: str) -> None:
    raw = _read_yaml(RUN_TYPES_DIR / filename)
    enabled = set(raw["agents"]["enabled"])
    for required in _full_decision_layer():
        assert required in enabled, f"{required} missing from {filename}"


@pytest.mark.parametrize(
    ("filename", "top_n_per_sector"),
    [
        ("market_open.yaml", 5),
        ("market_hours_rolling.yaml", 5),
        ("pre_close.yaml", 4),
        ("off_hours_rolling.yaml", 3),
        ("weekend_saturday.yaml", 3),
        ("weekend_sunday.yaml", 5),
        ("emergency.yaml", 5),
    ],
)
def test_news_digest_top_n_per_sector_matches_doc_table(
    filename: str, top_n_per_sector: int
) -> None:
    raw = _read_yaml(RUN_TYPES_DIR / filename)
    assert raw["qualitative_researcher"]["news_digest"]["top_n_per_sector"] == top_n_per_sector


@pytest.mark.parametrize(
    ("filename", "tool_call_limit", "tool_token_budget"),
    [
        ("market_open.yaml", 25, 4000),
        ("market_hours_rolling.yaml", 20, 3000),
        ("pre_close.yaml", 15, 2500),
        ("weekend_sunday.yaml", 20, 3000),
        ("emergency.yaml", 25, 4000),
    ],
)
def test_adaptive_overrides_match_doc_table(
    filename: str, tool_call_limit: int, tool_token_budget: int
) -> None:
    raw = _read_yaml(RUN_TYPES_DIR / filename)
    overrides = raw["agents"]["overrides"]["adaptive_researcher"]
    assert overrides["cumulative_tool_call_limit"] == tool_call_limit
    assert overrides["cumulative_tool_token_budget"] == tool_token_budget


def test_run_type_filename_stems_cover_scheduled_triggers_and_unscheduled_run_types() -> None:
    """Every scheduled trigger has an overlay; the spare overlays are the
    valid-but-unscheduled run types plus ``emergency``.

    Under the Tier B schedule (ALP-745) ``scheduler.yaml`` schedules four
    triggers, but all six run-type overlays are retained: ``off_hours_rolling``
    and ``weekend_saturday`` stay valid run types for manual / emergency use
    even though they are no longer on the cron schedule. ``emergency`` has no
    cron entry either — the emergency receiver task (story 04b) dispatches it
    from activity-log events. So every scheduled trigger must have an overlay,
    and the overlay stems beyond the scheduled set are exactly the two
    unscheduled run types plus ``emergency``.
    """
    scheduler = _read_yaml(CONFIG_DIR / "scheduler.yaml")
    trigger_keys = set(scheduler["triggers"].keys())
    yaml_stems = {p.stem for p in RUN_TYPES_DIR.glob("*.yaml")}
    # Every scheduled trigger resolves to an overlay file.
    assert trigger_keys - yaml_stems == set()
    # The overlays not on the schedule are the two retained-but-unscheduled
    # run types plus the non-cron ``emergency`` type.
    assert yaml_stems - trigger_keys == {"emergency", "off_hours_rolling", "weekend_saturday"}


def test_loaded_bundle_market_open_has_news_digest_5_3() -> None:
    from alphamind.config.loaders import load_run_types
    from alphamind.config.models import RunType

    bundle = load_run_types(CONFIG_DIR)
    market_open = bundle[RunType.market_open]
    assert market_open.qualitative_researcher.news_digest.top_n_per_sector == 5
    assert market_open.qualitative_researcher.news_digest.top_n_high_priority == 3


def test_overrides_default_factory_yields_empty_mapping_when_omitted() -> None:
    from alphamind.config.models import RunTypeConfig

    raw = _valid_run_type_raw()
    raw["agents"].pop("overrides")
    config = RunTypeConfig.model_validate(raw)
    assert config.agents.overrides == {}
