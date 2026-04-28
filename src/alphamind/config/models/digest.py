"""Pydantic models for digest.yaml — weekly-digest notable-shift thresholds (story 03h).

Each shift block carries the substructure that detector needs — windows and
thresholds for some, just `enabled` for others. The substructures are
intentionally distinct; do not lift common fields into a base class.

Field range constraints implement the semantic-self-test invariants from
docs/design/configuration-management.md § Validation at parse time:
every `baseline_window_weeks` ≥ 1, `multiplier_vs_baseline` > 1.0,
`min_occurrences_this_week` ≥ 0, `median_offset_sigma` > 0,
`delta_pp_threshold` in (0, 100], `days_before_due` ≥ 0.
"""

from pydantic import BaseModel, ConfigDict, Field


class AntiPatternSpike(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_window_weeks: int = Field(ge=1)
    multiplier_vs_baseline: float = Field(gt=1.0)
    min_occurrences_this_week: int = Field(ge=0)


class RegimeChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool


class SectorUnderperform(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_window_weeks: int = Field(ge=1)
    median_offset_sigma: float = Field(gt=0)


class CitationChainShift(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_window_weeks: int = Field(ge=1)
    delta_pp_threshold: float = Field(gt=0, le=100)


class SourceSignalSurvivalDrop(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_window_weeks: int = Field(ge=1)
    delta_pp_threshold: float = Field(gt=0, le=100)


class ValidationWindowEnd(BaseModel):
    model_config = ConfigDict(frozen=True)

    days_before_due: int = Field(ge=0)


class ValidationSuperseded(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool


class DigestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    anti_pattern_spike: AntiPatternSpike
    regime_change: RegimeChange
    sector_underperform: SectorUnderperform
    citation_chain_shift: CitationChainShift
    source_signal_survival_drop: SourceSignalSurvivalDrop
    validation_window_end: ValidationWindowEnd
    validation_superseded: ValidationSuperseded
