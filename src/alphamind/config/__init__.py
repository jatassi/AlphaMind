"""Public surface for the ``alphamind.config`` package.

Re-exports the pipeline-facing entry point (story 08) so callers can write
``from alphamind.config import load_full_config, PipelineConfig`` without
walking the submodule tree. Other names live in submodules — import directly
from ``alphamind.config.models``, ``alphamind.config.snapshot``, etc., to keep
the top-level surface focused on the invocation-start contract.
"""

from alphamind.config.load import PipelineConfig, load_full_config

__all__ = [
    "PipelineConfig",
    "load_full_config",
]
