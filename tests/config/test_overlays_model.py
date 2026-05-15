"""Tests for src/alphamind/config/models/overlays.py — overlay bundle (story 04d)."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"
OVERLAYS_DIR = CONFIG_DIR / "overlays"


def _valid_pre_event_payload() -> dict[str, Any]:
    return {
        "activation": {
            "windows_before_event": 2,
            "events": ["fomc", "cpi", "ppi", "pce", "nfp", "earnings"],
        },
        "multipliers": {"position_max_size_pct": 0.80},
        "final_invocation_before_event": {"block_new_positions": True},
    }


def _valid_stress_payload() -> dict[str, Any]:
    return {
        "activation": {
            "triggers": ["funding_stress_composite", "market_liquidity_score"],
        },
        "multipliers": {
            "sector_concentration_pct": 0.85,
            "net_long_pct": 0.85,
            "net_short_pct": 0.85,
            "gross_exposure_pct": 0.85,
        },
    }


# ---------------------------------------------------------------------------
# Pre-event overlay model
# ---------------------------------------------------------------------------


def test_pre_event_overlay_parses_minimal_payload() -> None:
    from alphamind.config.models import PreEventOverlay

    overlay = PreEventOverlay.model_validate(_valid_pre_event_payload())
    assert overlay.activation.windows_before_event == 2
    assert overlay.multipliers["position_max_size_pct"] == 0.80
    assert overlay.final_invocation_before_event.block_new_positions is True


def test_pre_event_overlay_rejects_unknown_event_type() -> None:
    from alphamind.config.models import PreEventOverlay

    raw = _valid_pre_event_payload()
    raw["activation"]["events"] = ["eclipse"]
    with pytest.raises((ValueError, TypeError)):
        PreEventOverlay.model_validate(raw)


def test_pre_event_overlay_rejects_zero_windows_before_event() -> None:
    from alphamind.config.models import PreEventOverlay

    raw = _valid_pre_event_payload()
    raw["activation"]["windows_before_event"] = 0
    with pytest.raises((ValueError, TypeError)):
        PreEventOverlay.model_validate(raw)


def test_pre_event_overlay_rejects_empty_multipliers() -> None:
    from alphamind.config.models import PreEventOverlay

    raw = _valid_pre_event_payload()
    raw["multipliers"] = {}
    with pytest.raises((ValueError, TypeError)):
        PreEventOverlay.model_validate(raw)


def test_pre_event_overlay_rejects_zero_multiplier() -> None:
    from alphamind.config.models import PreEventOverlay

    raw = _valid_pre_event_payload()
    raw["multipliers"] = {"position_max_size_pct": 0}
    with pytest.raises((ValueError, TypeError)):
        PreEventOverlay.model_validate(raw)


def test_pre_event_overlay_rejects_negative_multiplier() -> None:
    from alphamind.config.models import PreEventOverlay

    raw = _valid_pre_event_payload()
    raw["multipliers"] = {"position_max_size_pct": -0.5}
    with pytest.raises((ValueError, TypeError)):
        PreEventOverlay.model_validate(raw)


def test_pre_event_overlay_rejects_uppercase_multiplier_key() -> None:
    from alphamind.config.models import PreEventOverlay

    raw = _valid_pre_event_payload()
    raw["multipliers"] = {"PositionMaxSize": 0.8}
    with pytest.raises((ValueError, TypeError)):
        PreEventOverlay.model_validate(raw)


def test_pre_event_overlay_is_frozen() -> None:
    from alphamind.config.models import PreEventOverlay

    overlay = PreEventOverlay.model_validate(_valid_pre_event_payload())
    with pytest.raises((ValueError, TypeError)):
        overlay.multipliers = {"position_max_size_pct": 0.5}


# ---------------------------------------------------------------------------
# Stress overlay model
# ---------------------------------------------------------------------------


def test_stress_overlay_parses_minimal_payload() -> None:
    from alphamind.config.models import StressOverlay

    overlay = StressOverlay.model_validate(_valid_stress_payload())
    assert "funding_stress_composite" in {t.value for t in overlay.activation.triggers}
    assert overlay.multipliers["sector_concentration_pct"] == 0.85


def test_stress_overlay_rejects_unknown_trigger() -> None:
    from alphamind.config.models import StressOverlay

    raw = _valid_stress_payload()
    raw["activation"]["triggers"] = ["scary_news"]
    with pytest.raises((ValueError, TypeError)):
        StressOverlay.model_validate(raw)


def test_stress_overlay_rejects_empty_multipliers() -> None:
    from alphamind.config.models import StressOverlay

    raw = _valid_stress_payload()
    raw["multipliers"] = {}
    with pytest.raises((ValueError, TypeError)):
        StressOverlay.model_validate(raw)


def test_stress_overlay_rejects_negative_multiplier() -> None:
    from alphamind.config.models import StressOverlay

    raw = _valid_stress_payload()
    raw["multipliers"] = {"sector_concentration_pct": -0.5}
    with pytest.raises((ValueError, TypeError)):
        StressOverlay.model_validate(raw)


def test_stress_overlay_does_not_carry_final_invocation_before_event() -> None:
    """Stress overlays have no pre-deactivation step; the field is not part of the model."""
    from alphamind.config.models import StressOverlay

    assert "final_invocation_before_event" not in StressOverlay.model_fields


# ---------------------------------------------------------------------------
# Overlay enum
# ---------------------------------------------------------------------------


def test_overlay_enum_member_values_use_underscores() -> None:
    from alphamind.config.models import Overlay

    assert {m.value for m in Overlay} == {"pre_event", "stress"}


# ---------------------------------------------------------------------------
# Shipped YAML files
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def test_pre_event_yaml_parses_cleanly() -> None:
    from alphamind.config.models import PreEventOverlay

    data = _load_yaml(OVERLAYS_DIR / "pre-event.yaml")
    overlay = PreEventOverlay.model_validate(data)
    assert overlay.activation.windows_before_event >= 1
    assert overlay.final_invocation_before_event.block_new_positions is True


def test_stress_yaml_parses_cleanly() -> None:
    from alphamind.config.models import StressOverlay

    data = _load_yaml(OVERLAYS_DIR / "stress.yaml")
    overlay = StressOverlay.model_validate(data)
    # Stress overlay tightens the four exposure rules per regime-adaptation.md
    expected_keys = {
        "sector_concentration_pct",
        "net_long_pct",
        "net_short_pct",
        "gross_exposure_pct",
    }
    assert expected_keys.issubset(overlay.multipliers.keys())
    for key in expected_keys:
        assert overlay.multipliers[key] == 0.85


# ---------------------------------------------------------------------------
# load_overlays
# ---------------------------------------------------------------------------


def test_load_overlays_returns_two_entries() -> None:
    from alphamind.config.loaders import load_overlays
    from alphamind.config.models import Overlay, PreEventOverlay, StressOverlay

    overlays = load_overlays(CONFIG_DIR)
    assert set(overlays.keys()) == {Overlay.pre_event, Overlay.stress}
    assert isinstance(overlays[Overlay.pre_event], PreEventOverlay)
    assert isinstance(overlays[Overlay.stress], StressOverlay)


def test_load_overlays_maps_hyphenated_filename_to_underscored_enum(tmp_path: Path) -> None:
    from alphamind.config.loaders import load_overlays
    from alphamind.config.models import Overlay

    overlays_dir = tmp_path / "overlays"
    overlays_dir.mkdir()
    (overlays_dir / "pre-event.yaml").write_text(yaml.safe_dump(_valid_pre_event_payload()))
    (overlays_dir / "stress.yaml").write_text(yaml.safe_dump(_valid_stress_payload()))

    overlays = load_overlays(tmp_path)
    assert Overlay.pre_event in overlays


def test_load_overlays_raises_when_file_missing(tmp_path: Path) -> None:
    from alphamind.config.loaders import load_overlays

    overlays_dir = tmp_path / "overlays"
    overlays_dir.mkdir()
    # Only ship pre-event.yaml; stress.yaml is missing.
    (overlays_dir / "pre-event.yaml").write_text(yaml.safe_dump(_valid_pre_event_payload()))

    with pytest.raises(FileNotFoundError):
        load_overlays(tmp_path)
