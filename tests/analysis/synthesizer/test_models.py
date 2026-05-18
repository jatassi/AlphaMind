"""Tests for the BriefBundle data model and reference-prefix taxonomy — ALP-201."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.analysis._shared import Sector
from alphamind.analysis.synthesizer.models import (
    SOURCE_PREFIXES,
    BriefBundle,
    BriefSource,
    ReferencePrefix,
    find_bare_prefix_citations,
    parse_reference_id,
)


def test_brief_source_values_match_sector() -> None:
    """Sector members of BriefSource use Sector enum values verbatim."""
    assert BriefSource.SA_TECH.value == Sector.TECH_SEMIS.value
    assert BriefSource.SA_FIN.value == Sector.FINANCIALS.value
    assert BriefSource.SA_ENERGY.value == Sector.ENERGY.value

    # Round-trip property: BriefSource value parses back into a Sector.
    assert Sector(BriefSource.SA_TECH.value) is Sector.TECH_SEMIS
    assert Sector(BriefSource.SA_FIN.value) is Sector.FINANCIALS
    assert Sector(BriefSource.SA_ENERGY.value) is Sector.ENERGY


def test_reference_prefix_enum_complete() -> None:
    """ReferencePrefix exposes the complete prefix set named in the design doc."""
    expected = {
        "SA-TECH",
        "SA-TECH-ANOM",
        "SA-TECH-TC",
        "SA-FIN",
        "SA-FIN-ANOM",
        "SA-FIN-TC",
        "SA-ENERGY",
        "SA-ENERGY-ANOM",
        "SA-ENERGY-TC",
        "QR",
        "QR-CW",
        "AR",
        "CR",
    }
    assert {member.value for member in ReferencePrefix} == expected


def test_source_prefixes_cover_every_source() -> None:
    """Every BriefSource member keys into SOURCE_PREFIXES."""
    assert set(SOURCE_PREFIXES) == set(BriefSource)
    # Sector sources cover their three variants (base, ANOM, TC).
    assert SOURCE_PREFIXES[BriefSource.SA_TECH] == frozenset(
        {ReferencePrefix.SA_TECH, ReferencePrefix.SA_TECH_ANOM, ReferencePrefix.SA_TECH_TC}
    )
    assert SOURCE_PREFIXES[BriefSource.SA_FIN] == frozenset(
        {ReferencePrefix.SA_FIN, ReferencePrefix.SA_FIN_ANOM, ReferencePrefix.SA_FIN_TC}
    )
    assert SOURCE_PREFIXES[BriefSource.SA_ENERGY] == frozenset(
        {ReferencePrefix.SA_ENERGY, ReferencePrefix.SA_ENERGY_ANOM, ReferencePrefix.SA_ENERGY_TC}
    )
    # QR covers QR plus QR_CW.
    assert SOURCE_PREFIXES[BriefSource.QR] == frozenset({ReferencePrefix.QR, ReferencePrefix.QR_CW})
    # AR and CR each cover their single base prefix.
    assert SOURCE_PREFIXES[BriefSource.AR] == frozenset({ReferencePrefix.AR})
    assert SOURCE_PREFIXES[BriefSource.CR] == frozenset({ReferencePrefix.CR})


def test_briefbundle_default_prefixes() -> None:
    """Omitting prefixes defaults to SOURCE_PREFIXES[source]."""
    bundle = BriefBundle(
        source=BriefSource.SA_TECH,
        text="brief body",
        freshness=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )
    assert bundle.prefixes == SOURCE_PREFIXES[BriefSource.SA_TECH]


def test_briefbundle_rejects_foreign_prefixes() -> None:
    """prefixes containing a value outside SOURCE_PREFIXES[source] fails validation."""
    with pytest.raises((ValueError, TypeError)):
        BriefBundle(
            source=BriefSource.SA_TECH,
            text="brief body",
            freshness=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
            # SA_FIN is not in SOURCE_PREFIXES[SA_TECH]
            prefixes=frozenset({ReferencePrefix.SA_FIN}),
        )


def test_briefbundle_rejects_naive_freshness() -> None:
    """A naive datetime (no tzinfo) fails validation."""
    with pytest.raises((ValueError, TypeError)):
        BriefBundle(
            source=BriefSource.QR,
            text="brief body",
            freshness=datetime(2026, 5, 3, 12, 0),  # noqa: DTZ001 — intentional naive in test
        )


def test_briefbundle_hashable() -> None:
    """BriefBundle is frozen and hashable."""
    bundle = BriefBundle(
        source=BriefSource.CR,
        text="cr brief",
        freshness=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )
    assert hash(bundle) == hash(bundle)
    # Usable in a set: deduplication of two equal bundles collapses to one.
    assert len({bundle, bundle}) == 1


def test_parse_reference_id_subtype_longest_match() -> None:
    """SA-TECH-ANOM-3 resolves to (SA_TECH_ANOM, 3), not (SA_TECH, ANOM-3)."""
    assert parse_reference_id("SA-TECH-ANOM-3") == (ReferencePrefix.SA_TECH_ANOM, 3)
    assert parse_reference_id("SA-TECH-TC-7") == (ReferencePrefix.SA_TECH_TC, 7)
    assert parse_reference_id("SA-TECH-1") == (ReferencePrefix.SA_TECH, 1)
    assert parse_reference_id("SA-FIN-ANOM-2") == (ReferencePrefix.SA_FIN_ANOM, 2)
    assert parse_reference_id("SA-ENERGY-TC-4") == (ReferencePrefix.SA_ENERGY_TC, 4)


def test_parse_reference_id_qr_cw() -> None:
    """QR-CW-1 resolves to (QR_CW, 1), not (QR, CW-1)."""
    assert parse_reference_id("QR-CW-1") == (ReferencePrefix.QR_CW, 1)
    assert parse_reference_id("QR-5") == (ReferencePrefix.QR, 5)


def test_parse_reference_id_ar_and_cr() -> None:
    """AR and CR base prefixes parse correctly."""
    assert parse_reference_id("AR-2") == (ReferencePrefix.AR, 2)
    assert parse_reference_id("CR-1") == (ReferencePrefix.CR, 1)


def test_parse_reference_id_returns_none_on_unknown() -> None:
    """Unknown prefix, missing index, non-integer index, zero index, empty input → None."""
    # Unknown prefix.
    assert parse_reference_id("REC-1") is None
    assert parse_reference_id("UNKNOWN-3") is None
    # Missing index.
    assert parse_reference_id("SA-TECH") is None
    assert parse_reference_id("SA-TECH-") is None
    assert parse_reference_id("QR") is None
    # Non-integer index.
    assert parse_reference_id("SA-TECH-foo") is None
    assert parse_reference_id("AR-1.5") is None
    # Zero index.
    assert parse_reference_id("SA-TECH-0") is None
    assert parse_reference_id("AR-0") is None
    # Empty input.
    assert parse_reference_id("") is None


# ---------------------------------------------------------------------------
# find_bare_prefix_citations — ALP-521
# ---------------------------------------------------------------------------


def test_find_bare_prefix_citations_single_bare_prefix() -> None:
    """A single ``[CR]`` token returns its prefix string in a 1-tuple."""
    assert find_bare_prefix_citations("see [CR]") == ("CR",)


def test_find_bare_prefix_citations_preserves_document_order() -> None:
    """Multiple bare prefixes are returned in the order they appear."""
    assert find_bare_prefix_citations("foo [CR] bar [SA-TECH] baz") == ("CR", "SA-TECH")


def test_find_bare_prefix_citations_deduplicates_stably() -> None:
    """Repeated bare prefixes collapse to a single entry, keeping first-occurrence order."""
    assert find_bare_prefix_citations("[CR] and [CR]") == ("CR",)
    # Stable dedup across distinct prefixes: order is first-occurrence.
    assert find_bare_prefix_citations("[SA-TECH] then [CR] then [SA-TECH]") == ("SA-TECH", "CR")


def test_find_bare_prefix_citations_skips_well_formed() -> None:
    """A well-formed ``[CR-3]`` is not bare and must not be flagged."""
    assert find_bare_prefix_citations("[CR-3]") == ()
    # Mixed: only the bare one is flagged.
    assert find_bare_prefix_citations("see [CR] and [SA-TECH-3]") == ("CR",)


def test_find_bare_prefix_citations_skips_unknown_prefixes() -> None:
    """Bracketed tokens not in the ReferencePrefix taxonomy are ignored."""
    # REC, INV, FOO are not ReferencePrefix members. ``[INV-1]`` also has an
    # index suffix, but the detector's job is bare-prefix only — and INV is
    # not in the taxonomy regardless.
    assert find_bare_prefix_citations("[REC] [INV-1] [FOO]") == ()


def test_find_bare_prefix_citations_longest_match_subtype() -> None:
    """``[SA-TECH]`` returns the canonical member; the unknown shorter ``SA`` is never matched."""
    assert find_bare_prefix_citations("[SA-TECH]") == ("SA-TECH",)
    # SA-TECH-ANOM is a longer canonical prefix; bare form should be detected.
    assert find_bare_prefix_citations("[SA-TECH-ANOM]") == ("SA-TECH-ANOM",)
