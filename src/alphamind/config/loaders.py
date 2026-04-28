"""Pipeline-side YAML loaders for the cascading bundles (story 04*).

Each ``load_*`` helper reads every file in a bundle subdirectory, parses each
into the matching Pydantic variant, and returns a frozen mapping. Missing files
raise ``FileNotFoundError`` so the resolver fails closed at invocation start
per ``docs/design/configuration-management.md`` § Validation.
"""

from pathlib import Path
from typing import Any, cast

import yaml

from alphamind.config.models.modes import Mode, ModeConfig
from alphamind.config.models.overlays import (
    Overlay,
    PreEventOverlay,
    StressOverlay,
)

# Filename stems use hyphens for operator readability; enum members use
# underscores per Python identifier rules. The mapping bridges the two.
_OVERLAY_FILENAME_STEM: dict[Overlay, str] = {
    Overlay.pre_event: "pre-event",
    Overlay.stress: "stress",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required configuration file not found: {path}")
    return cast(dict[str, Any], yaml.safe_load(path.read_text()) or {})


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
