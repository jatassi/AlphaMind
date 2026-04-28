"""Pipeline-side configuration loaders.

These helpers read individual YAML files from the `config/` tree and return
validated, frozen Pydantic models. They are pipeline-side (consumed by the
composition resolver, story 05) and intentionally separate from `_common.py`
in `data_sources/`, which is collector-side.
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml

from alphamind.config.models.main import Profile
from alphamind.config.models.profiles import ProfileConfig


def load_profiles(config_dir: Path) -> Mapping[Profile, ProfileConfig]:
    """Load every `profiles/<name>.yaml` for every `Profile` enum member.

    Returns a frozen mapping from Profile to ProfileConfig. Raises if any
    profile file is missing, unreadable, or fails Pydantic validation.
    """
    profiles_dir = config_dir / "profiles"
    loaded: dict[Profile, ProfileConfig] = {}
    for profile in Profile:
        path = profiles_dir / f"{profile.value}.yaml"
        if not path.is_file():
            raise FileNotFoundError(f"Profile file for {profile.value!r} not found at {path}")
        raw = cast(dict[str, Any], yaml.safe_load(path.read_text()))
        loaded[profile] = ProfileConfig.model_validate(raw)
    return MappingProxyType(loaded)
