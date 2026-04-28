"""Loader helpers for `config/` bundle directories (stories 04*).

Each `load_<bundle>` helper reads every file declared by the corresponding
enum, validates each via its Pydantic model, and returns a mapping. Missing
files raise. The loaders are pipeline-side (consumed by the resolver in
story 05); they intentionally do not live in `_common.py`, which is scoped
to the data-sources collector.
"""

from pathlib import Path

import yaml

from alphamind.config.models.modes import Mode, ModeConfig


def load_modes(config_dir: Path) -> dict[Mode, ModeConfig]:
    """Read every `modes/{name}.yaml` for every `Mode` enum member.

    Validates each via `ModeConfig` and returns the mapping. Missing files
    raise `FileNotFoundError` — the mode set is closed and every file must
    exist for the resolver to compose.
    """
    modes_dir = config_dir / "modes"
    result: dict[Mode, ModeConfig] = {}
    for mode in Mode:
        path = modes_dir / f"{mode.value}.yaml"
        if not path.is_file():
            raise FileNotFoundError(
                f"Required mode file {path} missing; every Mode enum member must "
                f"have a corresponding YAML file under {modes_dir}"
            )
        raw = yaml.safe_load(path.read_text())
        result[mode] = ModeConfig.model_validate(raw)
    return result
