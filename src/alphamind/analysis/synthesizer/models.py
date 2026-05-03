"""BriefBundle data model and reference-prefix taxonomy — story 03 (ALP-201).

The synthesizer consumes a uniform shape — :class:`BriefBundle` — for every
upstream brief (sector briefs, the correlation/regime brief, the qualitative
brief, adaptive research findings). The bundle carries a source identifier,
the brief's full text body, the freshness timestamp, and the set of
reference-ID prefixes the source is known to produce.

This module also consolidates the canonical reference-prefix taxonomy as
:class:`ReferencePrefix` and the per-source membership map
:data:`SOURCE_PREFIXES`. The reference-marker extractor (story 04) and the
retrieval-store assembler (story 05a) reuse these values directly. See
``docs/design/03-analysis-layer/synthesizer.md`` § Inputs and § Source
reference mechanism, and
``docs/design/testing/llm-output-validation.md`` § Reference-ID taxonomy.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.models import SECTOR_PREFIX

__all__ = [
    "SOURCE_PREFIXES",
    "BriefBundle",
    "BriefSource",
    "ReferencePrefix",
    "parse_reference_id",
]


class BriefSource(enum.StrEnum):
    """Identifier for the upstream agent that produced a brief.

    Sector members carry the matching :class:`Sector` value verbatim so a
    :class:`BriefSource` round-trips through :class:`Sector` without
    translation — see ``docs/design/03-analysis-layer/synthesizer.md``
    § Inputs.
    """

    SA_TECH = Sector.TECH_SEMIS.value
    SA_FIN = Sector.FINANCIALS.value
    SA_ENERGY = Sector.ENERGY.value
    QR = "qr"
    AR = "ar"
    CR = "cr"


class ReferencePrefix(enum.StrEnum):
    """Closed set of reference-ID prefixes appearing in upstream briefs.

    Mirrors ``docs/design/testing/llm-output-validation.md``
    § Reference-ID taxonomy. The retrieval store keys by full prefixed ID,
    so sub-typed variants (`SA-TECH-ANOM`, `SA-TECH-TC`, `QR-CW`) are
    first-class members rather than encoded as substrings of the base
    prefix.
    """

    SA_TECH = "SA-TECH"
    SA_TECH_ANOM = "SA-TECH-ANOM"
    SA_TECH_TC = "SA-TECH-TC"
    SA_FIN = "SA-FIN"
    SA_FIN_ANOM = "SA-FIN-ANOM"
    SA_FIN_TC = "SA-FIN-TC"
    SA_ENERGY = "SA-ENERGY"
    SA_ENERGY_ANOM = "SA-ENERGY-ANOM"
    SA_ENERGY_TC = "SA-ENERGY-TC"
    QR = "QR"
    QR_CW = "QR-CW"
    AR = "AR"
    CR = "CR"


SOURCE_PREFIXES: dict[BriefSource, frozenset[ReferencePrefix]] = {
    BriefSource.SA_TECH: frozenset(
        {ReferencePrefix.SA_TECH, ReferencePrefix.SA_TECH_ANOM, ReferencePrefix.SA_TECH_TC}
    ),
    BriefSource.SA_FIN: frozenset(
        {ReferencePrefix.SA_FIN, ReferencePrefix.SA_FIN_ANOM, ReferencePrefix.SA_FIN_TC}
    ),
    BriefSource.SA_ENERGY: frozenset(
        {
            ReferencePrefix.SA_ENERGY,
            ReferencePrefix.SA_ENERGY_ANOM,
            ReferencePrefix.SA_ENERGY_TC,
        }
    ),
    BriefSource.QR: frozenset({ReferencePrefix.QR, ReferencePrefix.QR_CW}),
    BriefSource.AR: frozenset({ReferencePrefix.AR}),
    BriefSource.CR: frozenset({ReferencePrefix.CR}),
}


# Compile-time guard: `ReferencePrefix.SA_*` values must match the
# `SECTOR_PREFIX` strings emitted by the domain-researcher brief, otherwise the
# retrieval store would key by a different ID than the producer wrote.
assert ReferencePrefix.SA_TECH.value == SECTOR_PREFIX[Sector.TECH_SEMIS]
assert ReferencePrefix.SA_FIN.value == SECTOR_PREFIX[Sector.FINANCIALS]
assert ReferencePrefix.SA_ENERGY.value == SECTOR_PREFIX[Sector.ENERGY]


# Longest-match parse table: sub-typed prefixes (`SA-TECH-ANOM`) must beat their
# base prefix (`SA-TECH`) so `SA-TECH-ANOM-3` resolves to (SA_TECH_ANOM, 3) and
# never to (SA_TECH, "ANOM-3"). Sorting by descending value length encodes the
# rule once for any future additions.
_PREFIXES_BY_LENGTH: tuple[ReferencePrefix, ...] = tuple(
    sorted(ReferencePrefix, key=lambda p: len(p.value), reverse=True)
)


class BriefBundle(BaseModel, frozen=True):
    """A single upstream brief, normalized for synthesizer consumption.

    ``prefixes`` defaults to the source's canonical prefix set
    (:data:`SOURCE_PREFIXES`). Passing a ``prefixes`` value containing a
    :class:`ReferencePrefix` outside that set fails validation — every
    bundle's prefix set is a subset of what its source can legally
    produce.
    """

    source: BriefSource
    text: str
    freshness: datetime
    # The Field default is a placeholder satisfying mypy/Pydantic; the
    # `_default_prefixes` validator below substitutes the source-canonical
    # set whenever the caller omits `prefixes`, which is the documented
    # default. An explicitly-passed empty frozenset is preserved as-is —
    # legal, since the empty set has no foreign elements.
    prefixes: frozenset[ReferencePrefix] = Field(default_factory=frozenset)

    @model_validator(mode="before")
    @classmethod
    def _default_prefixes(cls, data: Any) -> Any:
        if isinstance(data, dict) and "prefixes" not in data and "source" in data:
            source = BriefSource(data["source"])
            return {**data, "prefixes": SOURCE_PREFIXES[source]}
        return data

    @model_validator(mode="after")
    def _validate_bundle(self) -> BriefBundle:
        if self.freshness.tzinfo is None or self.freshness.utcoffset() is None:
            raise ValueError("freshness must be timezone-aware")
        foreign = self.prefixes - SOURCE_PREFIXES[self.source]
        if foreign:
            raise ValueError(
                f"prefixes {sorted(p.value for p in foreign)} are not produced by "
                f"source {self.source.value}"
            )
        return self


def parse_reference_id(ref_id: str) -> tuple[ReferencePrefix, int] | None:
    """Parse a reference ID like ``SA-TECH-ANOM-3`` into ``(ReferencePrefix, index)``.

    Longest-match: ``SA-TECH-ANOM-3`` resolves to
    ``(ReferencePrefix.SA_TECH_ANOM, 3)``, not
    ``(ReferencePrefix.SA_TECH, "ANOM-3")``. Returns :data:`None` for
    unknown prefixes, missing indexes, non-integer indexes, zero indexes,
    or empty input.
    """
    if not ref_id:
        return None
    for prefix in _PREFIXES_BY_LENGTH:
        head = f"{prefix.value}-"
        if not ref_id.startswith(head):
            continue
        tail = ref_id[len(head) :]
        if not tail or not tail.isdigit():
            return None
        index = int(tail)
        if index <= 0:
            return None
        return (prefix, index)
    return None
