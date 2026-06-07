"""Reference-ID parser — extraction + source classification (ALP-886 story 06d).

The parser is pure over already-read artifact text: it scans for bracketed
``[PREFIX-N]`` citation markers (the as-built synthesizer grammar — see
``analysis/synthesizer/models.py`` ``REF_ID_RE`` / ``ReferencePrefix``) and
classifies each upstream ref ID onto its producing :class:`CitationSource`.
"""

from __future__ import annotations

from alphamind.feedback_loop.citation.parser import (
    CitationSource,
    classify_source,
    extract_citations,
)


def test_classify_source_maps_base_prefixes() -> None:
    assert classify_source("SA-TECH-3") is CitationSource.SA_TECH
    assert classify_source("SA-FIN-1") is CitationSource.SA_FIN
    assert classify_source("SA-ENERGY-12") is CitationSource.SA_ENERGY
    assert classify_source("QR-2") is CitationSource.QR
    assert classify_source("AR-7") is CitationSource.AR
    assert classify_source("CR-1") is CitationSource.CR


def test_classify_source_folds_subtyped_variants_to_base() -> None:
    # As-built sub-typed prefixes fold to the base source agent.
    assert classify_source("SA-TECH-ANOM-3") is CitationSource.SA_TECH
    assert classify_source("SA-FIN-TC-1") is CitationSource.SA_FIN
    assert classify_source("SA-ENERGY-ANOM-2") is CitationSource.SA_ENERGY
    assert classify_source("QR-CW-4") is CitationSource.QR


def test_classify_source_rejects_consumer_layer_and_malformed_ids() -> None:
    # Consumer-layer IDs are not upstream sources.
    assert classify_source("REC-1") is None
    assert classify_source("SA-ORD-2") is None
    assert classify_source("ENV-REC-3") is None
    # Malformed / unknown.
    assert classify_source("CR") is None  # bare prefix, no index
    assert classify_source("CR-0") is None  # zero index rejected by the grammar
    assert classify_source("FOO-1") is None
    assert classify_source("") is None


def test_extract_citations_pulls_bracketed_upstream_refs_in_order_deduped() -> None:
    text = (
        "Tech momentum confirmed [SA-TECH-3] and again [SA-TECH-3]; "
        "macro regime per [CR-1]. The analyst's own [REC-2] is not a source, "
        "nor is the bare [CR] prefix or unbracketed SA-FIN-1."
    )
    assert extract_citations(text) == ("SA-TECH-3", "CR-1")


def test_extract_citations_empty_for_no_markers() -> None:
    assert extract_citations("no citations here at all") == ()
