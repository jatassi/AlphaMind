"""Thesis-resolution primitives for the analysis layer (ALP-897).

Exposes the programmatic component-outcome assessor used by the analysis
pipeline to resolve falsifiable quantitative thesis components without LLM
tokens. The LLM-based qualitative evaluator (story 04d) lives alongside and
handles INCONCLUSIVE components.
"""

from __future__ import annotations

from alphamind.analysis.thesis_resolution.programmatic import (
    assess_component_programmatically,
)

__all__ = ["assess_component_programmatically"]
