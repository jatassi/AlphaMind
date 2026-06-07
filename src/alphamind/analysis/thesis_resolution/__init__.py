"""Thesis-resolution primitives for the analysis layer (ALP-897, ALP-898).

Exposes the programmatic component-outcome assessor used by the analysis
pipeline to resolve falsifiable quantitative thesis components without LLM
tokens (story 04c), and the targeted LLM evaluator (story 04d) that resolves
the ambiguous/qualitative components the assessor leaves ``INCONCLUSIVE``.
"""

from __future__ import annotations

from alphamind.analysis.thesis_resolution.llm_evaluator import (
    ComponentLLMOutcome,
    evaluate_component_llm,
)
from alphamind.analysis.thesis_resolution.programmatic import (
    assess_component_programmatically,
)

__all__ = [
    "ComponentLLMOutcome",
    "assess_component_programmatically",
    "evaluate_component_llm",
]
