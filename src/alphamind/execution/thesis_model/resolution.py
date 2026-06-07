"""Thesis-level resolution classifier — ALP-334.

The classifier and its _MECHANICAL_EXIT_METHODS constant have been relocated to
``alphamind.portfolio_state.records.thesis_resolution`` (ALP-897) so that the
analysis layer can import them without an upward layer edge.

This module re-exports both symbols for backwards compatibility with any
existing ``execution``-layer consumers that reference the old import path.
"""

from __future__ import annotations

from alphamind.portfolio_state.records.thesis_resolution import (
    _MECHANICAL_EXIT_METHODS,
    classify_thesis_resolution,
)

__all__ = ["_MECHANICAL_EXIT_METHODS", "classify_thesis_resolution"]
