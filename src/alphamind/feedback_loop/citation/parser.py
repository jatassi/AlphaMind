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
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alphamind.analysis.synthesizer.models import REF_ID_RE, parse_reference_id

if TYPE_CHECKING:
    from alphamind.portfolio_state.records.theses import ThesisComponentOutcome

__all__ = [
    "CitationSource",
    "ComponentCitation",
    "ExtractedRef",
    "RefChain",
    "assemble_chains",
    "classify_source",
    "extract_citations",
    "extract_payload_citations",
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
    return _dedupe_upstream(match.group(1) for match in re.finditer(REF_ID_RE, text))


# Bare (un-bracketed) upstream ref token, e.g. the ``source_references`` array
# entries the analyst/strategist emit as plain ``"QR-1"`` strings rather than
# bracketed prose markers. ``classify_source`` is the authoritative filter.
_BARE_REF_RE = re.compile(r"\b([A-Z][A-Z0-9-]*-[0-9]+)\b")


def extract_payload_citations(payload: object) -> tuple[str, ...]:
    """Return upstream refs cited anywhere in an agent's structured output *payload*.

    An agent's ``output.json`` carries citations in two wire shapes: bracketed
    ``[SA-TECH-1]`` markers in narrative prose (synthesis text, thesis
    narratives) and bare ``"QR-1"`` entries in ``source_references``-style
    arrays. Both reduce to the same question — which upstream refs does this
    artifact cite — so the payload is serialized to text and scanned for every
    upstream ref token (bracketed or bare). :func:`classify_source` discards
    consumer-layer and malformed tokens, so over-matching bare tokens is safe.
    Returns document-ordered, first-occurrence-deduplicated upstream ref IDs.
    """
    text = _payload_to_text(payload)
    return _dedupe_upstream(match.group(1) for match in _BARE_REF_RE.finditer(text))


def _payload_to_text(payload: object) -> str:
    """Flatten a JSON-ish *payload* to a single scannable string.

    ``json.dumps`` would wrap every ref in quotes/escapes but keeps all values
    in one string; the bare-token regex tolerates the surrounding punctuation.
    Falls back to ``str`` for non-serializable inputs so the scan never raises.
    """
    try:
        return json.dumps(payload)
    except (TypeError, ValueError):
        return str(payload)


def _dedupe_upstream(ref_ids: Iterable[str]) -> tuple[str, ...]:
    """Keep upstream-source refs in first-occurrence order, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for ref_id in ref_ids:
        if classify_source(ref_id) is None or ref_id in seen:
            continue
        seen.add(ref_id)
        out.append(ref_id)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ComponentCitation:
    """A resolved thesis component plus the upstream refs its narrative cites.

    The chain terminus: a thesis component carries the upstream refs that
    survived into it (cited in its narrative / supporting signals) and the
    ``resolution_outcome`` those refs are scored against.
    """

    cited_refs: frozenset[str]
    resolution_outcome: ThesisComponentOutcome | None


def assemble_chains(
    *,
    universe: Iterable[str],
    synthesis_text: str,
    decision_citations: Iterable[str],
    components: Iterable[ComponentCitation],
) -> tuple[RefChain, ...]:
    """Build the per-ref citation chains (pure) from the parsed layer signals.

    Parameters
    ----------
    universe:
        Every upstream ref available this window — the denominator for the
        per-source rates. The synthesizer's retrieval-store keys are the
        authoritative universe; refs cited downstream but absent from it are
        unioned in so a directly-retrieved ref is never lost.
    synthesis_text:
        The synthesizer brief body; a ref cited here is ``cited_in_synthesis``.
    decision_citations:
        Upstream refs cited across the decision-layer artifacts (analyst /
        strategist / PM); membership sets ``cited_in_decision``.
    components:
        Resolved thesis components and the refs each cites; a ref appearing in
        one carries that component's ``resolution_outcome`` as its chain terminus.
    """
    synth_cited = set(extract_citations(synthesis_text))
    decision_cited = {r for r in decision_citations if classify_source(r) is not None}
    outcome_by_ref: dict[str, ThesisComponentOutcome | None] = {}
    for component in components:
        for ref_id in component.cited_refs:
            if classify_source(ref_id) is None:
                continue
            # First resolved outcome wins; a None never overwrites a real outcome.
            if outcome_by_ref.get(ref_id) is None:
                outcome_by_ref[ref_id] = component.resolution_outcome

    all_refs = {r for r in universe if classify_source(r) is not None}
    all_refs |= synth_cited | decision_cited | set(outcome_by_ref)

    chains: list[RefChain] = []
    for ref_id in sorted(all_refs):
        source = classify_source(ref_id)
        assert source is not None  # filtered above
        chains.append(
            RefChain(
                ref=ExtractedRef(ref_id=ref_id, source=source),
                cited_in_synthesis=ref_id in synth_cited,
                cited_in_decision=ref_id in decision_cited,
                thesis_component_outcome=outcome_by_ref.get(ref_id),
            )
        )
    return tuple(chains)
