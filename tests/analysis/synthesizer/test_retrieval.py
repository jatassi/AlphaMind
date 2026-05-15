"""Tests for the per-invocation retrieval store — story 05a (ALP-203)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.analysis.synthesizer.models import BriefBundle, BriefSource
from alphamind.analysis.synthesizer.retrieval import (
    RetrievalAssemblyError,
    RetrievalStore,
    assemble_retrieval_store,
)


def _bundle(
    source: BriefSource,
    text: str,
    freshness: datetime | None = None,
) -> BriefBundle:
    """Build a :class:`BriefBundle` with a default-freshness fallback."""
    return BriefBundle(
        source=source,
        text=text,
        freshness=freshness or datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )


def test_assemble_empty_returns_empty_store() -> None:
    """Assembly of zero bundles produces a store with no entries."""
    store = assemble_retrieval_store([])
    assert isinstance(store, RetrievalStore)
    assert store.entries == {}
    assert store.freshness_by_source == {}


def test_assemble_single_bundle_indexes_all_refs() -> None:
    """Every reference ID in a single bundle becomes an entry in the store."""
    text = (
        "[SA-TECH-1] First finding.\n"
        "  detail.\n"
        "[SA-TECH-2] Second finding.\n"
        "[SA-TECH-ANOM-1] Anomaly.\n"
    )
    bundle = _bundle(BriefSource.SA_TECH, text)
    store = assemble_retrieval_store([bundle])
    assert set(store.entries) == {"SA-TECH-1", "SA-TECH-2", "SA-TECH-ANOM-1"}
    assert "First finding." in store.entries["SA-TECH-1"]
    assert "Second finding." in store.entries["SA-TECH-2"]
    assert "Anomaly." in store.entries["SA-TECH-ANOM-1"]


def test_assemble_multi_bundle_unions_entries() -> None:
    """Multiple bundles' entries combine into one store keyed by ref ID."""
    tech_bundle = _bundle(BriefSource.SA_TECH, "[SA-TECH-1] tech.\n")
    fin_bundle = _bundle(BriefSource.SA_FIN, "[SA-FIN-1] financials.\n")
    cr_bundle = _bundle(BriefSource.CR, "[CR-1] correlation finding.\n")
    store = assemble_retrieval_store([tech_bundle, fin_bundle, cr_bundle])
    assert set(store.entries) == {"SA-TECH-1", "SA-FIN-1", "CR-1"}


def test_assemble_collision_raises() -> None:
    """Two bundles emitting the same ref ID raise RetrievalAssemblyError."""
    tech_a = _bundle(BriefSource.SA_TECH, "[SA-TECH-1] first source.\n")
    tech_b = _bundle(BriefSource.SA_TECH, "[SA-TECH-1] second source.\n")
    with pytest.raises(RetrievalAssemblyError) as excinfo:
        assemble_retrieval_store([tech_a, tech_b])
    err = excinfo.value
    assert err.ref_id == "SA-TECH-1"
    assert err.first_bundle is tech_a
    assert err.second_bundle is tech_b


def test_assemble_rejects_bundle_with_undeclared_prefix() -> None:
    """A bundle whose text emits a prefix outside ``bundle.prefixes`` raises.

    Catches an upstream-adapter mis-declaration: the bundle declares
    ``BriefSource.AR`` (so its declared prefix set is ``{AR}``) but its
    body emits a ``[SA-TECH-1]`` reference. The retrieval-store assembler
    must surface this as :class:`RetrievalAssemblyError` rather than
    silently indexing the off-source ref.
    """
    bundle = _bundle(BriefSource.AR, "[SA-TECH-1] off-source ref.\n")
    with pytest.raises(RetrievalAssemblyError) as excinfo:
        assemble_retrieval_store([bundle])
    message = str(excinfo.value)
    assert "SA-TECH" in message
    assert BriefSource.AR.value in message


def test_lookup_returns_none_on_unknown() -> None:
    """A ref ID absent from entries returns None, not raises."""
    store = assemble_retrieval_store(
        [_bundle(BriefSource.SA_TECH, "[SA-TECH-1] only entry.\n")],
    )
    section = store.lookup("SA-TECH-1")
    assert section is not None
    assert "only entry." in section
    assert store.lookup("SA-TECH-99") is None
    assert store.lookup("AR-1") is None
    assert store.lookup("") is None


def test_assembly_is_deterministic() -> None:
    """Repeated calls on the same inputs produce identical entries dicts."""
    bundles = [
        _bundle(BriefSource.SA_TECH, "[SA-TECH-1] tech.\n[SA-TECH-2] tech 2.\n"),
        _bundle(BriefSource.QR, "[QR-1] qr.\n[QR-CW-1] qr cw.\n"),
        _bundle(BriefSource.AR, "[AR-1] ar.\n"),
    ]
    first = assemble_retrieval_store(bundles)
    second = assemble_retrieval_store(bundles)
    assert first.entries == second.entries
    assert list(first.entries) == list(second.entries)


def test_freshness_by_source_populated() -> None:
    """freshness_by_source carries one entry per source bundle."""
    tech_freshness = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
    cr_freshness = datetime(2026, 5, 3, 11, 30, tzinfo=UTC)
    store = assemble_retrieval_store(
        [
            _bundle(BriefSource.SA_TECH, "[SA-TECH-1] x.\n", freshness=tech_freshness),
            _bundle(BriefSource.CR, "[CR-1] y.\n", freshness=cr_freshness),
        ],
    )
    assert store.freshness_by_source == {
        BriefSource.SA_TECH: tech_freshness,
        BriefSource.CR: cr_freshness,
    }


def test_retrieval_store_is_frozen() -> None:
    """ALP-474: RetrievalStore is a frozen dataclass — assignment after construction fails."""
    import dataclasses

    store = RetrievalStore(entries={}, freshness_by_source={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        store.entries = {"x": "y"}  # type: ignore[misc]
