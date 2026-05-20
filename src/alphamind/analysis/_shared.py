"""Shared types across the analysis layer.

Re-exports the canonical identifiers the analysis sub-packages
(domain_researchers, qualitative_research, adaptive_research, synthesizer)
import. Centralizing here avoids per-package redefinition of names that
already exist in distillation or elsewhere in the codebase.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from alphamind.distillation.output import AnomalySeverity, OutputAudience
from alphamind.distillation.sector_assembly import DOMAIN_RESEARCHER_BY_AUDIENCE

__all__ = [
    "_SECTOR_AUDIENCE_MAP",
    "AnomalySeverity",
    "Sector",
    "SignalQuality",
    "TokensUsed",
    "render_regime_payload_body",
]


def render_regime_payload_body(regime_label: dict[str, Any]) -> str:
    """Render the universal-broadcast regime payload as deterministic body text.

    Emits every key in the payload as a ``key: value`` line in sorted order
    so the body is payload-agnostic — additions to the regime block (per
    :func:`alphamind.distillation.regime.assemble_regime_block`) propagate
    without renderer edits. Returns an empty string for an empty payload;
    callers prepend their own section header.
    """
    return "\n".join(f"{key}: {regime_label[key]}" for key in sorted(regime_label))


class Sector(StrEnum):
    """Analysis-layer sector identifier.

    Values match `DOMAIN_RESEARCHER_BY_AUDIENCE` so a `Sector` round-trips
    through the distillation audience mapping without translation.
    """

    TECH_SEMIS = "tech_semis"
    FINANCIALS = "financials"
    ENERGY = "energy"


class SignalQuality(StrEnum):
    """Overall signal quality for an analysis-layer brief.

    Shared across the domain-researcher :class:`SectorBrief` and the
    qualitative-research brief. ``DEGRADED`` carries an accompanying reason
    string; the other levels do not.
    """

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    DEGRADED = "degraded"


class TokensUsed(BaseModel, frozen=True):
    """Token-accounting record returned alongside any LLM-agent invocation.

    Mirrors what the Claude Agent SDK exposes in its response metadata.
    Fields are non-negative integers; the SDK reports zero for absent
    cache reads/writes rather than `None`.
    """

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)


_SECTOR_AUDIENCE_MAP: dict[Sector, OutputAudience] = {
    Sector.TECH_SEMIS: OutputAudience.SECTOR_TECH_SEMIS,
    Sector.FINANCIALS: OutputAudience.SECTOR_FINANCIALS,
    Sector.ENERGY: OutputAudience.SECTOR_ENERGY,
}

# Compile-time guard: a future drift between Sector and DOMAIN_RESEARCHER_BY_AUDIENCE
# fails import rather than silently producing a wrong sector lookup downstream.
assert {DOMAIN_RESEARCHER_BY_AUDIENCE[aud] for aud in _SECTOR_AUDIENCE_MAP.values()} == {
    s.value for s in Sector
}
