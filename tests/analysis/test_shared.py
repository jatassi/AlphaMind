"""Tests for the shared analysis-layer types module — story ALP-133."""

from __future__ import annotations

import pytest


def test_sector_has_three_members_with_documented_values() -> None:
    """Sector exposes exactly tech_semis, financials, energy."""
    from alphamind.analysis._shared import Sector

    assert {member.value for member in Sector} == {"tech_semis", "financials", "energy"}


def test_sector_audience_map_covers_every_sector_with_distinct_audiences() -> None:
    """Every Sector member resolves to a distinct OutputAudience."""
    from alphamind.analysis._shared import _SECTOR_AUDIENCE_MAP, Sector

    assert set(_SECTOR_AUDIENCE_MAP.keys()) == set(Sector)
    audiences = list(_SECTOR_AUDIENCE_MAP.values())
    assert len(set(audiences)) == len(audiences)


def test_tokens_used_rejects_negative_values() -> None:
    """Each TokensUsed integer field rejects negatives at construction."""
    from alphamind.analysis._shared import TokensUsed

    base_kwargs = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    for field in base_kwargs:
        kwargs = dict(base_kwargs)
        kwargs[field] = -1
        with pytest.raises((ValueError, TypeError)):
            TokensUsed(**kwargs)


def test_anomaly_severity_re_export_is_the_same_object() -> None:
    """`from alphamind.analysis._shared import AnomalySeverity` returns the
    same object as the distillation module's literal."""
    from alphamind.analysis import _shared as shared_module
    from alphamind.distillation import output as distillation_output

    assert shared_module.AnomalySeverity is distillation_output.AnomalySeverity


def test_sector_values_match_domain_researcher_by_audience_strings() -> None:
    """Sector values round-trip through the distillation audience mapping
    without translation — guards against silent drift."""
    from alphamind.analysis._shared import _SECTOR_AUDIENCE_MAP, Sector
    from alphamind.distillation.sector_assembly import DOMAIN_RESEARCHER_BY_AUDIENCE

    resolved = {DOMAIN_RESEARCHER_BY_AUDIENCE[aud] for aud in _SECTOR_AUDIENCE_MAP.values()}
    assert resolved == {sector.value for sector in Sector}


def test_signal_quality_is_importable_from_shared() -> None:
    """SignalQuality is promoted to _shared so the qualitative brief reuses it."""
    from alphamind.analysis._shared import SignalQuality

    assert {member.value for member in SignalQuality} == {"high", "moderate", "low", "degraded"}


def test_signal_quality_re_export_from_domain_researchers_models_is_same_object() -> None:
    """domain_researchers.models.SignalQuality re-exports the _shared enum;
    existing call sites keep working without redefinition."""
    from alphamind.analysis._shared import SignalQuality as SharedSignalQuality
    from alphamind.analysis.domain_researchers.models import (
        SignalQuality as DomainSignalQuality,
    )

    assert SharedSignalQuality is DomainSignalQuality
