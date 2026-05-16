"""Loaders shim for q7 intra_sector_correlation (ALP-486).

The session-bound loading + the ``correlation_divergence`` event writes
live in the whole-category :mod:`._loaders` module. This module
re-exports the relevant entry points so the import-linter contract can
pin them explicitly.
"""

from __future__ import annotations

from alphamind.distillation.q7._loaders import (
    Q7Inputs,
    load_q7_inputs,
)

__all__ = ["Q7Inputs", "load_q7_inputs"]
