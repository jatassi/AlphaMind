"""One-shot YAML + ``.env`` config loader for the data-sources library.

Loads every data-layer YAML config file plus the ``.env`` file and returns
an immutable :class:`AlphaMindConfig` aggregate. Validates that every
provider's ``api_key_env`` reference resolves to a name present in
``.env``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from alphamind.config.models import (
    CollectorScheduleConfig,
    DataSourcesConfig,
    DistillationConfig,
    NewsOutletsConfig,
)

__all__ = ["AlphaMindConfig", "load_config"]


@dataclass(frozen=True)
class AlphaMindConfig:
    """Immutable aggregate of the YAML config sections.

    Sections are listed alphabetically — every load source the data layer
    consumes appears once, the order matches the ``load_config`` reads, and
    new sections are added by extending the alphabetic chain.
    """

    collector_schedule: CollectorScheduleConfig
    data_sources: DataSourcesConfig
    distillation: DistillationConfig
    news_outlets: NewsOutletsConfig


def load_config(
    config_dir: str | None = None,
    env_file: str | None = None,
) -> AlphaMindConfig:
    """Load and validate every data-layer YAML config file plus the ``.env`` file.

    Parameters
    ----------
    config_dir:
        Directory containing ``collector_schedule.yaml``, ``data_sources.yaml``,
        ``distillation.yaml``, and ``news_outlets.yaml``. Defaults to
        ``<repo-root>/config/``.
    env_file:
        Path to the ``.env`` file.  Defaults to ``<repo-root>/.env``.

    Raises
    ------
    EnvironmentError
        If a provider's ``api_key_env`` reference names an environment variable
        that is not present in the resolved ``.env``.
    """
    import yaml

    root = Path(__file__).parents[4]
    cfg_dir = Path(config_dir) if config_dir is not None else root / "config"
    dot_env = Path(env_file) if env_file is not None else root / ".env"

    # Load env vars from .env (does not affect os.environ — keeps it pure)
    env_values: dict[str, str | None] = dotenv_values(str(dot_env))

    def _read(name: str) -> dict[str, Any]:
        path = cfg_dir / name
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    collector_schedule = CollectorScheduleConfig.model_validate(_read("collector_schedule.yaml"))
    data_sources = DataSourcesConfig.model_validate(_read("data_sources.yaml"))
    distillation = DistillationConfig.model_validate(_read("distillation.yaml"))
    news_outlets = NewsOutletsConfig.model_validate(_read("news_outlets.yaml"))

    # Validate that every api_key_env reference resolves
    for name, provider in data_sources.providers.items():
        if provider.api_key_env is not None and (
            provider.api_key_env not in env_values or env_values[provider.api_key_env] is None
        ):
            raise OSError(
                f"Provider {name!r} requires environment variable "
                f"{provider.api_key_env!r} but it is not set in {dot_env}"
            )

    return AlphaMindConfig(
        collector_schedule=collector_schedule,
        data_sources=data_sources,
        distillation=distillation,
        news_outlets=news_outlets,
    )
