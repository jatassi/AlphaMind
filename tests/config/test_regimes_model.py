"""Tests for src/alphamind/config/models/regimes.py — regimes/*.yaml model (story 04b)."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def _valid_regime_raw() -> dict[str, Any]:
    return {
        "vix_range": [14, 22],
        "multipliers": {"position_max_size_pct": 1.0},
        "transition": {
            "tighten_on_entry": "immediate",
            "loosen_on_exit": "linear_over_invocations_3",
        },
    }


def test_regime_config_parses_minimal_valid_dict() -> None:
    from alphamind.config.models import RegimeConfig

    config = RegimeConfig.model_validate(_valid_regime_raw())
    assert config.vix_range == (14.0, 22.0)
    assert config.multipliers == {"position_max_size_pct": 1.0}


def test_regime_rejects_inverted_vix_range() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["vix_range"] = [22, 14]
    with pytest.raises((ValueError, TypeError), match="vix_range"):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_negative_vix_range() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["vix_range"] = [-1, 22]
    with pytest.raises((ValueError, TypeError), match="vix_range"):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_zero_multiplier() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["multipliers"] = {"position_max_size_pct": 0}
    with pytest.raises((ValueError, TypeError), match="multiplier"):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_negative_multiplier() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["multipliers"] = {"position_max_size_pct": -0.5}
    with pytest.raises((ValueError, TypeError), match="multiplier"):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_empty_multipliers() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["multipliers"] = {}
    with pytest.raises((ValueError, TypeError), match="multipliers"):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_capitalized_multiplier_key() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["multipliers"] = {"Position_Max_Size_Pct": 1.0}
    with pytest.raises((ValueError, TypeError), match="does not match"):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_unknown_tighten_on_entry() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["transition"]["tighten_on_entry"] = "tightening"
    with pytest.raises((ValueError, TypeError)):
        RegimeConfig.model_validate(raw)


def test_regime_rejects_unknown_loosen_on_exit() -> None:
    from alphamind.config.models import RegimeConfig

    raw = _valid_regime_raw()
    raw["transition"]["loosen_on_exit"] = "linear_over_invocations_5"
    with pytest.raises((ValueError, TypeError)):
        RegimeConfig.model_validate(raw)


def test_regime_models_are_frozen() -> None:
    from alphamind.config.models import RegimeConfig, TransitionPolicy

    config = RegimeConfig.model_validate(_valid_regime_raw())

    assert isinstance(config, RegimeConfig)
    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("vix_range", (0.0, 0.0))

    assert isinstance(config.transition, TransitionPolicy)
    with pytest.raises((ValueError, TypeError)):
        config.transition.__setattr__("tighten_on_entry", "immediate")


def test_load_regimes_parses_all_four_shipped_files() -> None:
    from alphamind.config.loaders import load_regimes
    from alphamind.config.models import Regime

    regimes = load_regimes(CONFIG_DIR)
    assert set(regimes) == {Regime.low_vol, Regime.normal, Regime.elevated, Regime.crisis}
    assert len(regimes) == 4


def test_normal_regime_multipliers_are_all_one() -> None:
    from alphamind.config.loaders import load_regimes
    from alphamind.config.models import Regime

    regimes = load_regimes(CONFIG_DIR)
    normal = regimes[Regime.normal]
    assert all(value == 1.0 for value in normal.multipliers.values()), normal.multipliers


def test_loader_maps_hyphenated_filename_to_underscored_enum_member(tmp_path: Path) -> None:
    """`low-vol.yaml` (hyphen) must resolve to `Regime.low_vol` (underscore)."""
    from alphamind.config.loaders import load_regimes
    from alphamind.config.models import Regime

    regimes_dir = tmp_path / "regimes"
    regimes_dir.mkdir()
    payload = (
        "vix_range: [0, 14]\n"
        "multipliers:\n"
        "  position_max_size_pct: 1.20\n"
        "transition:\n"
        "  tighten_on_entry: immediate\n"
        "  loosen_on_exit: linear_over_invocations_3\n"
    )
    for stem in ("low-vol", "normal", "elevated", "crisis"):
        (regimes_dir / f"{stem}.yaml").write_text(payload)

    regimes = load_regimes(tmp_path)
    assert Regime.low_vol in regimes
    assert regimes[Regime.low_vol].multipliers == {"position_max_size_pct": 1.20}


def test_loader_raises_when_a_regime_file_is_missing(tmp_path: Path) -> None:
    from alphamind.config.loaders import load_regimes

    regimes_dir = tmp_path / "regimes"
    regimes_dir.mkdir()
    payload = (
        "vix_range: [0, 14]\n"
        "multipliers:\n"
        "  position_max_size_pct: 1.0\n"
        "transition:\n"
        "  tighten_on_entry: immediate\n"
        "  loosen_on_exit: linear_over_invocations_3\n"
    )
    # Ship three of four — `crisis.yaml` is intentionally absent.
    for stem in ("low-vol", "normal", "elevated"):
        (regimes_dir / f"{stem}.yaml").write_text(payload)

    with pytest.raises(FileNotFoundError, match="crisis"):
        load_regimes(tmp_path)
