"""Reference-ID parsing over persisted agent-call output artifacts (ALP-886 story 06d).

Pure parsing — the file read is the loader's job (``dataset._load_refs``). This
module scans already-read artifact text for bracketed citation markers and
classifies upstream reference IDs onto their producing :class:`CitationSource`.

The grammar is **aligned to the as-built emission**, not the design-doc table
(``docs/design/feedback-loop.md`` § Citation-chain metrics). The authoritative
upstream-ref grammar is the synthesizer's
:data:`alphamind.analysis.synthesizer.models.REF_ID_RE` /
:class:`~alphamind.analysis.synthesizer.models.ReferencePrefix`: every brief
cites upstream findings as bracketed ``[<prefix>-<index>]`` tokens, and the
closed prefix set is the synthesizer taxonomy. Two as-built divergences from the
doc table are folded in here (see :class:`CitationSource`):

* The doc lists ``REC-N`` / ``SA-N`` / ``SA-ORD-N`` / ``ENV-*`` in the same ref
  hierarchy as the upstream sources. They are not upstream *sources* — they are
  the consuming layers' (analyst / strategist / PM) own emitted IDs, absent from
  the synthesizer ``ReferencePrefix`` taxonomy. The citation chain measures the
  flow of *upstream* refs into those consumers, so consumer IDs are not sources.
* The as-built taxonomy carries sub-typed source variants (``SA-TECH-ANOM``,
  ``SA-TECH-TC``, ``QR-CW``) the doc table omits. They are folded into their base
  source (a ``SA-TECH-ANOM-3`` ref is a tech-&-semis-researcher signal), since
  the chain conditions per *source agent*, not per sub-type.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alphamind.analysis.synthesizer.models import REF_ID_RE, parse_reference_id

if TYPE_CHECKING:
    from alphamind.portfolio_state.records.theses import ThesisComponentOutcome

__all__ = [
    "CitationSource",
    "ExtractedRef",
    "RefChain",
    "classify_source",
    "extract_citations",
]


class CitationSource(enum.StrEnum):
    """The upstream agent that produced a reference ID (the chain's conditioning key).

    Mirrors the upstream rows of ``docs/design/feedback-loop.md`` § Citation-chain
    metrics, aligned to the as-built synthesizer
    :class:`~alphamind.analysis.synthesizer.models.ReferencePrefix` taxonomy.
    Sub-typed variants (``SA-TECH-ANOM`` / ``SA-TECH-TC`` / ``QR-CW``) collapse to
    their base source; consumer-layer IDs (``REC`` / ``SA`` / ``SA-ORD`` / ``ENV``)
    are not sources.
    """

    SA_TECH = "sa_tech"
    SA_FIN = "sa_fin"
    SA_ENERGY = "sa_energy"
    QR = "qr"
    AR = "ar"
    CR = "cr"


# Map a synthesizer ``ReferencePrefix`` value to the producing source. Sub-typed
# variants fold to their base; only upstream-producible prefixes appear here.
_PREFIX_TO_SOURCE: dict[str, CitationSource] = {
    "SA-TECH": CitationSource.SA_TECH,
    "SA-TECH-ANOM": CitationSource.SA_TECH,
    "SA-TECH-TC": CitationSource.SA_TECH,
    "SA-FIN": CitationSource.SA_FIN,
    "SA-FIN-ANOM": CitationSource.SA_FIN,
    "SA-FIN-TC": CitationSource.SA_FIN,
    "SA-ENERGY": CitationSource.SA_ENERGY,
    "SA-ENERGY-ANOM": CitationSource.SA_ENERGY,
    "SA-ENERGY-TC": CitationSource.SA_ENERGY,
    "QR": CitationSource.QR,
    "QR-CW": CitationSource.QR,
    "AR": CitationSource.AR,
    "CR": CitationSource.CR,
}


@dataclass(frozen=True, slots=True)
class ExtractedRef:
    """One parsed upstream reference ID and its producing source."""

    ref_id: str
    source: CitationSource


@dataclass(frozen=True, slots=True)
class RefChain:
    """The full citation chain for one upstream reference (the doc's chain made data).

    ``upstream finding → synthesizer brief → decision narrative → thesis
    component → resolution`` collapses to four measurement points:

    * ``cited_in_synthesis`` — the ref appears in the synthesizer's brief.
    * ``cited_in_decision`` — the ref appears in an analyst / strategist / PM
      decision-layer narrative.
    * ``thesis_component_outcome`` — the resolution of the thesis component the
      ref ended in, or ``None`` if it never reached a validated/resolved component.
    """

    ref: ExtractedRef
    cited_in_synthesis: bool
    cited_in_decision: bool
    thesis_component_outcome: ThesisComponentOutcome | None


def classify_source(ref_id: str) -> CitationSource | None:
    """Map *ref_id* (e.g. ``"SA-TECH-3"``) to its producing :class:`CitationSource`.

    Returns ``None`` for consumer-layer IDs (``REC`` / ``SA`` / ``SA-ORD`` /
    ``ENV``), malformed IDs, and any token outside the upstream-source taxonomy.
    Sub-typed prefixes fold to their base source (``SA-TECH-ANOM-3`` →
    :attr:`CitationSource.SA_TECH`).
    """
    parsed = parse_reference_id(ref_id)
    if parsed is None:
        return None
    prefix, _index = parsed
    return _PREFIX_TO_SOURCE.get(prefix.value)


def extract_citations(text: str) -> tuple[str, ...]:
    """Return upstream reference IDs cited as ``[PREFIX-N]`` markers in *text*.

    Scans for bracketed citation markers via the as-built
    :data:`~alphamind.analysis.synthesizer.models.REF_ID_RE`, keeps only IDs that
    resolve to an upstream :class:`CitationSource`, and returns them in document
    order with stable first-occurrence deduplication. Consumer-layer brackets
    (``[REC-3]``) and bare prefixes (``[CR]``) are dropped — only resolvable
    upstream refs survive.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in re.finditer(REF_ID_RE, text):
        ref_id = match.group(1)
        if classify_source(ref_id) is None:
            continue
        if ref_id in seen:
            continue
        seen.add(ref_id)
        out.append(ref_id)
    return tuple(out)
