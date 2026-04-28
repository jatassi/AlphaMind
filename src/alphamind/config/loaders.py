"""YAML bundle loaders for the configuration cascade (stories 04a-04e).

Each `load_*` helper reads every file expected for one cascade dimension,
validates each via the matching Pydantic model, and returns a frozen mapping
keyed by the bundle's enum so the resolver (story 05) can index without string
gymnastics. Missing files raise — the cascade is closed-set.
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml

from alphamind.config.models.run_types import RunType, RunTypeConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def load_run_types(config_dir: Path) -> Mapping[RunType, RunTypeConfig]:
    """Read every `run_types/<member>.yaml`, validate, and return a frozen mapping.

    Raises `FileNotFoundError` if any expected file is missing — the run-type
    bundle is closed-set per `RunType`.
    """
    run_types_dir = config_dir / "run_types"
    bundle: dict[RunType, RunTypeConfig] = {}
    for member in RunType:
        path = run_types_dir / f"{member.value}.yaml"
        bundle[member] = RunTypeConfig.model_validate(_load_yaml(path))
    return MappingProxyType(bundle)
