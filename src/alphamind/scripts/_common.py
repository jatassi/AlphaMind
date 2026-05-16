"""Shared verification-script primitives (story 13).

The verification scripts intentionally bypass the full
:class:`alphamind.config.load.PipelineConfig` machinery — they are
operator tooling, not pipeline-runtime code, and threading invocation-id
semantics through a one-shot CLI is over-coupling. This module
centralizes the YAML loaders and the per-script result primitives so
each verifier script imports the same building blocks rather than
re-implementing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from alphamind.config.models.distillation import DistillationConfig

_REPO_ROOT = Path(__file__).parents[3]
_DEFAULT_DISTILLATION_PATH = _REPO_ROOT / "config" / "distillation.yaml"
_DEFAULT_ASSETS_PATH = _REPO_ROOT / "config" / "assets.yaml"


@dataclass(frozen=True)
class AssertionFailure:
    """One failed assertion in a verification report.

    ``code`` is a stable, kebab-case identifier (e.g.
    ``"sector-outputs-missing"``) that callers and tests match against;
    ``message`` is the human-readable detail.
    """

    code: str
    message: str


def load_distillation_config(config_path: Path | None = None) -> DistillationConfig:
    """Read ``config/distillation.yaml`` and return the parsed config."""
    path = config_path or _DEFAULT_DISTILLATION_PATH
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return DistillationConfig.model_validate(data)


def load_universe_scope(assets_path: Path | None = None) -> tuple[str, ...]:
    """Return the alphabetical ticker scope from ``config/assets.yaml``.

    Combines every sector list and the benchmark keys per the story-13
    spec ("the universe scope from assets.yaml") so the orchestrator
    sees the full universe the operator validates.
    """
    path = assets_path or _DEFAULT_ASSETS_PATH
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    tickers: set[str] = set()
    for sector_tickers in data.get("sectors", {}).values():
        tickers.update(sector_tickers)
    for ticker in data.get("benchmarks", {}):
        tickers.add(ticker)
    return tuple(sorted(tickers))


__all__ = [
    "AssertionFailure",
    "load_distillation_config",
    "load_universe_scope",
]
