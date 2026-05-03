"""Per-invocation retrieval store — story 05a (ALP-203).

Folds the upstream :class:`BriefBundle` set into a single ref-ID-keyed
lookup the synthesizer's ``retrieve_brief`` MCP tool (story 06a) and the
decision-layer Layer-3 referential-integrity check both consume. See
``docs/design/03-analysis-layer/synthesizer.md`` § Retrieval store
side-effect for the contract and ``docs/design/testing/llm-output-validation.md``
§ Layer 3 — Referential integrity for the consumer-side resolution rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel

from alphamind.analysis.synthesizer.models import (
    BriefBundle,
    BriefSource,
    ReferencePrefix,
    parse_reference_id,
)
from alphamind.analysis.synthesizer.reference_extractor import extract_references

__all__ = [
    "RetrievalAssemblyError",
    "RetrievalStore",
    "assemble_retrieval_store",
]


class RetrievalAssemblyError(Exception):
    """Raised when assembling the retrieval store detects an inconsistency.

    Two distinct cases:

    - Collision: two source bundles emit the same reference ID. Carries
      the colliding ``ref_id`` and both source bundles for diagnostics.
    - Undeclared prefix: a bundle's body emits a reference whose prefix
      lies outside that bundle's declared :attr:`BriefBundle.prefixes`
      set. The bundle attribute carries the offending source bundle.
    """

    def __init__(
        self,
        message: str,
        *,
        ref_id: str | None = None,
        first_bundle: BriefBundle | None = None,
        second_bundle: BriefBundle | None = None,
    ) -> None:
        super().__init__(message)
        self.ref_id = ref_id
        self.first_bundle = first_bundle
        self.second_bundle = second_bundle

    @classmethod
    def collision(
        cls,
        ref_id: str,
        first_bundle: BriefBundle,
        second_bundle: BriefBundle,
    ) -> RetrievalAssemblyError:
        """Build the two-bundles-share-a-ref-ID variant."""
        return cls(
            f"reference ID {ref_id!r} emitted by both "
            f"{first_bundle.source.value} and {second_bundle.source.value}",
            ref_id=ref_id,
            first_bundle=first_bundle,
            second_bundle=second_bundle,
        )

    @classmethod
    def undeclared_prefix(
        cls,
        bundle: BriefBundle,
        offending: frozenset[ReferencePrefix],
    ) -> RetrievalAssemblyError:
        """Build the bundle-emits-an-undeclared-prefix variant."""
        prefixes_render = ", ".join(sorted(p.value for p in offending))
        return cls(
            f"source {bundle.source.value!r} emitted reference prefix(es) "
            f"{{{prefixes_render}}} outside its declared prefix set",
            first_bundle=bundle,
        )


class RetrievalStore(BaseModel, frozen=True):
    """The synthesizer's per-invocation lookup store.

    ``entries`` is keyed by full prefixed reference ID
    (``SA-TECH-3``, ``QR-CW-1``, ``CR-1``, ...) and holds the section
    text the originating bundle's body carried for that ID.
    ``freshness_by_source`` carries one timestamp per contributing
    source for diagnostics (e.g., a stale-input check at the Layer-3
    seam).
    """

    entries: dict[str, str]
    freshness_by_source: dict[BriefSource, datetime]

    def lookup(self, ref_id: str) -> str | None:
        """Return the section text for ``ref_id`` or :data:`None` if absent."""
        return self.entries.get(ref_id)


def assemble_retrieval_store(bundles: Iterable[BriefBundle]) -> RetrievalStore:
    """Fold ``bundles`` into a :class:`RetrievalStore`.

    Each bundle's body is split via :func:`extract_references`; results
    union into a single entries map. A reference ID emitted by two
    bundles raises :class:`RetrievalAssemblyError` carrying both source
    bundles so the caller can surface the collision.
    """
    entries: dict[str, str] = {}
    bundle_by_ref: dict[str, BriefBundle] = {}
    freshness_by_source: dict[BriefSource, datetime] = {}

    for bundle in bundles:
        sections = extract_references(bundle.text)
        emitted_prefixes: set[ReferencePrefix] = set()
        for ref_id, section_text in sections.items():
            if ref_id in entries:
                raise RetrievalAssemblyError.collision(
                    ref_id=ref_id,
                    first_bundle=bundle_by_ref[ref_id],
                    second_bundle=bundle,
                )
            entries[ref_id] = section_text
            bundle_by_ref[ref_id] = bundle
            # Record the prefix the bundle's body actually emitted so we can
            # cross-check against `bundle.prefixes` once every section is in.
            # `parse_reference_id` cannot return None here because
            # `extract_references` already validated each ref-ID's prefix.
            parsed = parse_reference_id(ref_id)
            assert parsed is not None
            emitted_prefixes.add(parsed[0])
        offending = emitted_prefixes - bundle.prefixes
        if offending:
            raise RetrievalAssemblyError.undeclared_prefix(
                bundle=bundle,
                offending=frozenset(offending),
            )
        freshness_by_source[bundle.source] = bundle.freshness

    return RetrievalStore(entries=entries, freshness_by_source=freshness_by_source)
