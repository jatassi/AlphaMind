"""Pipeline-side YAML loaders for the cascading bundles (story 04*).

Each ``load_*`` helper reads every file in a bundle subdirectory, parses each
into the matching Pydantic variant, and returns a frozen mapping. Missing files
raise ``FileNotFoundError`` so the resolver fails closed at invocation start
per ``docs/design/configuration-management.md`` § Validation.
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml

from alphamind.config.models.main import Profile
from alphamind.config.models.modes import Mode, ModeConfig
from alphamind.config.models.overlays import (
    Overlay,
    PreEventOverlay,
    StressOverlay,
)
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import Regime, RegimeConfig
from alphamind.config.models.run_types import RunType, RunTypeConfig

# Filename stems use hyphens for operator readability; enum members use
# underscores per Python identifier rules. The mapping bridges the two.
_OVERLAY_FILENAME_STEM: dict[Overlay, str] = {
    Overlay.pre_event: "pre-event",
    Overlay.stress: "stress",
}

_REGIME_FILENAME_STEM: dict[Regime, str] = {
    Regime.low_vol: "low-vol",
    Regime.normal: "normal",
    Regime.elevated: "elevated",
    Regime.crisis: "crisis",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required configuration file not found: {path}")
    return cast(dict[str, Any], yaml.safe_load(path.read_text()) or {})


def load_profiles(config_dir: Path) -> Mapping[Profile, ProfileConfig]:
    """Load every ``profiles/<name>.yaml`` for every ``Profile`` enum member.

    Returns a frozen mapping. Raises if any profile file is missing,
    unreadable, or fails Pydantic validation.
    """
    profiles_dir = config_dir / "profiles"
    loaded: dict[Profile, ProfileConfig] = {
        profile: ProfileConfig.model_validate(_read_yaml(profiles_dir / f"{profile.value}.yaml"))
        for profile in Profile
    }
    return MappingProxyType(loaded)


def load_overlays(config_dir: Path) -> dict[Overlay, PreEventOverlay | StressOverlay]:
    """Load every overlay file under ``config_dir / 'overlays'``.

    Returns a mapping keyed by ``Overlay`` enum member. The two overlay shapes
    are different — ``PreEventOverlay`` carries a ``final_invocation_before_event``
    block, ``StressOverlay`` does not — so the value type is a discriminated
    union dispatched on the enum key.
    """
    overlays_dir = config_dir / "overlays"
    resolved: dict[Overlay, PreEventOverlay | StressOverlay] = {}
    for overlay, stem in _OVERLAY_FILENAME_STEM.items():
        payload = _read_yaml(overlays_dir / f"{stem}.yaml")
        if overlay is Overlay.pre_event:
            resolved[overlay] = PreEventOverlay.model_validate(payload)
        else:
            resolved[overlay] = StressOverlay.model_validate(payload)
    return resolved


def load_modes(config_dir: Path) -> dict[Mode, ModeConfig]:
    """Load every mode file under ``config_dir / 'modes'``.

    The mode set is closed; every ``Mode`` enum member must have a matching
    YAML file or the resolver fails closed.
    """
    modes_dir = config_dir / "modes"
    return {
        mode: ModeConfig.model_validate(_read_yaml(modes_dir / f"{mode.value}.yaml"))
        for mode in Mode
    }


def load_regimes(config_dir: Path) -> Mapping[Regime, RegimeConfig]:
    """Load every regime file under ``config_dir / 'regimes'``.

    Returns a frozen mapping keyed by the ``Regime`` enum member. The four
    members are a closed set; every member must have a matching YAML file or
    the loader raises ``FileNotFoundError`` so the resolver fails closed.
    Filenames use hyphens (``low-vol.yaml``) while enum members use
    underscores (``Regime.low_vol``); the ``_REGIME_FILENAME_STEM`` mapping
    bridges the two.
    """
    regimes_dir = config_dir / "regimes"
    loaded: dict[Regime, RegimeConfig] = {
        regime: RegimeConfig.model_validate(_read_yaml(regimes_dir / f"{stem}.yaml"))
        for regime, stem in _REGIME_FILENAME_STEM.items()
    }
    return MappingProxyType(loaded)


def load_run_types(config_dir: Path) -> Mapping[RunType, RunTypeConfig]:
    """Load every ``run_types/<member>.yaml`` for every ``RunType`` enum member.

    Returns a frozen mapping. The run-type bundle is closed-set; missing files
    raise ``FileNotFoundError``.
    """
    run_types_dir = config_dir / "run_types"
    bundle: dict[RunType, RunTypeConfig] = {
        member: RunTypeConfig.model_validate(_read_yaml(run_types_dir / f"{member.value}.yaml"))
        for member in RunType
    }
    return MappingProxyType(bundle)
