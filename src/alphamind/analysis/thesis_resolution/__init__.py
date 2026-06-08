"""Thesis-resolution primitives for the analysis layer (ALP-897, ALP-898, ALP-899).

Exposes the programmatic component-outcome assessor used by the analysis
pipeline to resolve falsifiable quantitative thesis components without LLM
tokens (story 04c), the targeted LLM evaluator (story 04d) that resolves the
ambiguous/qualitative components the assessor leaves ``INCONCLUSIVE``, and the
closed-position resolver (story 04e) that composes both — plus the relocated
classifier — to author the ``ACTIVE → RESOLVED`` transition.
"""

from __future__ import annotations

from alphamind.analysis.thesis_resolution.llm_evaluator import (
    ComponentLLMOutcome,
    evaluate_component_llm,
)
from alphamind.analysis.thesis_resolution.programmatic import (
    assess_component_programmatically,
)
from alphamind.analysis.thesis_resolution.resolver import (
    PreparedResolution,
    ResolvedThesis,
    persist_thesis_resolutions,
    prepare_closed_position_resolutions,
    resolve_closed_position_theses,
)

__all__ = [
    "ComponentLLMOutcome",
    "PreparedResolution",
    "ResolvedThesis",
    "assess_component_programmatically",
    "evaluate_component_llm",
    "persist_thesis_resolutions",
    "prepare_closed_position_resolutions",
    "resolve_closed_position_theses",
]
