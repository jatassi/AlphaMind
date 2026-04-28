"""Tests for `config/modes/` bundle and the modes Pydantic model (story 04c)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from alphamind.config.loaders import load_modes
from alphamind.config.models import (
    AnalystMode,
    AnalystOutputMode,
    CommandType,
    Mode,
    ModeConfig,
    PendingOrdersDefault,
    PmEmphasis,
    PmMode,
    StrategistAction,
    StrategistMode,
    StrategistOutputMode,
)

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _normal_raw() -> dict[str, Any]:
    return {
        "analyst": {"output_mode": "proposals"},
        "strategist": {
            "output_mode": "normal",
            "allowed_actions": ["hold", "reduce", "close", "adjust-bracket", "add"],
            "pending_orders_default": "maintain",
        },
        "pm": {
            "allowed_command_types": ["OPEN", "CLOSE", "ADJUST", "CANCEL", "ADD"],
            "emphasis": "normal",
        },
    }


def _halt_raw() -> dict[str, Any]:
    return {
        "analyst": {"output_mode": "watchlist"},
        "strategist": {
            "output_mode": "defensive_posture",
            "allowed_actions": ["hold", "reduce", "close", "adjust-bracket"],
            "pending_orders_default": "cancel",
        },
        "pm": {
            "allowed_command_types": ["CLOSE", "ADJUST", "CANCEL"],
            "emphasis": "capital_preservation",
        },
    }


# ---------------------------------------------------------------------------
# Shipped YAML files parse cleanly via load_modes
# ---------------------------------------------------------------------------


def test_load_modes_parses_every_shipped_mode_file() -> None:
    modes = load_modes(CONFIG_DIR)
    assert set(modes.keys()) == set(Mode)
    assert isinstance(modes[Mode.normal], ModeConfig)
    assert isinstance(modes[Mode.halt], ModeConfig)


def test_normal_yaml_carries_passthrough_defaults() -> None:
    modes = load_modes(CONFIG_DIR)
    normal = modes[Mode.normal]
    assert normal.analyst.output_mode is AnalystOutputMode.proposals
    assert normal.strategist.output_mode is StrategistOutputMode.normal
    assert set(normal.strategist.allowed_actions) == set(StrategistAction)
    assert normal.strategist.pending_orders_default is PendingOrdersDefault.maintain
    assert set(normal.pm.allowed_command_types) == set(CommandType)
    assert normal.pm.emphasis is PmEmphasis.normal


def test_halt_yaml_carries_design_doc_worked_example() -> None:
    modes = load_modes(CONFIG_DIR)
    halt = modes[Mode.halt]
    assert halt.analyst.output_mode is AnalystOutputMode.watchlist
    assert halt.strategist.output_mode is StrategistOutputMode.defensive_posture
    assert halt.strategist.allowed_actions == [
        StrategistAction.hold,
        StrategistAction.reduce,
        StrategistAction.close,
        StrategistAction.adjust_bracket,
    ]
    assert halt.strategist.pending_orders_default is PendingOrdersDefault.cancel
    assert halt.pm.allowed_command_types == [
        CommandType.CLOSE,
        CommandType.ADJUST,
        CommandType.CANCEL,
    ]
    assert halt.pm.emphasis is PmEmphasis.capital_preservation


def test_halt_excludes_open_and_add_command_types() -> None:
    modes = load_modes(CONFIG_DIR)
    halt = modes[Mode.halt]
    assert CommandType.OPEN not in halt.pm.allowed_command_types
    assert CommandType.ADD not in halt.pm.allowed_command_types


# ---------------------------------------------------------------------------
# StrategistMode validation
# ---------------------------------------------------------------------------


def test_strategist_mode_rejects_empty_allowed_actions() -> None:
    raw = _halt_raw()["strategist"]
    raw["allowed_actions"] = []
    with pytest.raises(ValidationError):
        StrategistMode.model_validate(raw)


def test_strategist_mode_rejects_duplicate_allowed_actions() -> None:
    raw = _halt_raw()["strategist"]
    raw["allowed_actions"] = ["hold", "hold"]
    with pytest.raises(ValidationError):
        StrategistMode.model_validate(raw)


def test_strategist_mode_rejects_unknown_action() -> None:
    raw = _halt_raw()["strategist"]
    raw["allowed_actions"] = ["pivot"]
    with pytest.raises(ValidationError):
        StrategistMode.model_validate(raw)


def test_strategist_mode_maps_hyphenated_yaml_value_to_underscore_member() -> None:
    raw = _halt_raw()["strategist"]
    raw["allowed_actions"] = ["adjust-bracket"]
    parsed = StrategistMode.model_validate(raw)
    assert parsed.allowed_actions == [StrategistAction.adjust_bracket]
    assert StrategistAction.adjust_bracket.value == "adjust-bracket"


# ---------------------------------------------------------------------------
# PmMode validation
# ---------------------------------------------------------------------------


def test_pm_mode_rejects_empty_allowed_command_types() -> None:
    raw = _halt_raw()["pm"]
    raw["allowed_command_types"] = []
    with pytest.raises(ValidationError):
        PmMode.model_validate(raw)


def test_pm_mode_rejects_duplicate_allowed_command_types() -> None:
    raw = _halt_raw()["pm"]
    raw["allowed_command_types"] = ["CLOSE", "CLOSE"]
    with pytest.raises(ValidationError):
        PmMode.model_validate(raw)


def test_pm_mode_rejects_lowercase_command_type() -> None:
    raw = _halt_raw()["pm"]
    raw["allowed_command_types"] = ["open"]
    with pytest.raises(ValidationError):
        PmMode.model_validate(raw)


# ---------------------------------------------------------------------------
# AnalystMode validation
# ---------------------------------------------------------------------------


def test_analyst_mode_rejects_unknown_output_mode() -> None:
    with pytest.raises(ValidationError):
        AnalystMode.model_validate({"output_mode": "trade"})


# ---------------------------------------------------------------------------
# Loader behavior: missing file
# ---------------------------------------------------------------------------


def test_load_modes_raises_when_file_missing(tmp_path: Path) -> None:
    modes_dir = tmp_path / "modes"
    modes_dir.mkdir()
    (modes_dir / "normal.yaml").write_text(yaml.safe_dump(_normal_raw()))
    # halt.yaml missing on purpose
    with pytest.raises(FileNotFoundError):
        load_modes(tmp_path)


# ---------------------------------------------------------------------------
# Models are frozen
# ---------------------------------------------------------------------------


def test_mode_models_are_frozen() -> None:
    modes = load_modes(CONFIG_DIR)
    halt = modes[Mode.halt]
    with pytest.raises(ValidationError):
        halt.__setattr__("analyst", halt.analyst)
    with pytest.raises(ValidationError):
        halt.analyst.__setattr__("output_mode", AnalystOutputMode.proposals)
    with pytest.raises(ValidationError):
        halt.strategist.__setattr__("pending_orders_default", PendingOrdersDefault.maintain)
    with pytest.raises(ValidationError):
        halt.pm.__setattr__("emphasis", PmEmphasis.normal)
