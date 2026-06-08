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

from alphamind.analysis.synthesizer.ref_grammar import (
    REF_ID_RE,
    ReferencePrefix,
    parse_reference_id,
)

if TYPE_CHECKING:
    from alphamind.portfolio_state.records.theses import ThesisComponentOutcome

__all__ = [
    "CitationSource",
    "ComponentCitation",
    "ExtractedRef",
    "InvocationRefs",
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


# The upstream reference-ID prefix taxonomy, mapped to its producing source.
# Keyed by the canonical synthesizer
# :class:`~alphamind.analysis.synthesizer.ref_grammar.ReferencePrefix` member (not by
# re-listed prefix strings) so a member newly added to that closed taxonomy cannot be
# silently dropped here — the completeness assert below fails the import until it is
# mapped. Sub-typed variants fold to their base source (``SA-TECH-ANOM`` → ``SA_TECH``);
# consumer-layer IDs (``REC`` / ``SA`` / ``SA-ORD`` / ``ENV``) are not ``ReferencePrefix``
# members at all, so they are absent by construction.
_PREFIX_TO_SOURCE: dict[ReferencePrefix, CitationSource] = {
    ReferencePrefix.SA_TECH: CitationSource.SA_TECH,
    ReferencePrefix.SA_TECH_ANOM: CitationSource.SA_TECH,
    ReferencePrefix.SA_TECH_TC: CitationSource.SA_TECH,
    ReferencePrefix.SA_FIN: CitationSource.SA_FIN,
    ReferencePrefix.SA_FIN_ANOM: CitationSource.SA_FIN,
    ReferencePrefix.SA_FIN_TC: CitationSource.SA_FIN,
    ReferencePrefix.SA_ENERGY: CitationSource.SA_ENERGY,
    ReferencePrefix.SA_ENERGY_ANOM: CitationSource.SA_ENERGY,
    ReferencePrefix.SA_ENERGY_TC: CitationSource.SA_ENERGY,
    ReferencePrefix.QR: CitationSource.QR,
    ReferencePrefix.QR_CW: CitationSource.QR,
    ReferencePrefix.AR: CitationSource.AR,
    ReferencePrefix.CR: CitationSource.CR,
}

# Compile-time completeness guard: every ``ReferencePrefix`` must fold to a source, so
# adding a member to the synthesizer taxonomy without mapping it here is a load-time
# failure rather than a silently-dropped citation.
assert set(_PREFIX_TO_SOURCE) == set(ReferencePrefix)


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

    Delegates the grammar to the canonical synthesizer
    :func:`~alphamind.analysis.synthesizer.ref_grammar.parse_reference_id` (longest-match
    prefix, positive-index requirement), then folds the parsed
    :class:`~alphamind.analysis.synthesizer.ref_grammar.ReferencePrefix` to its base
    source. Returns ``None`` for consumer-layer IDs (``REC`` / ``SA`` / ``SA-ORD`` /
    ``ENV``), malformed IDs, and any token the grammar rejects.
    """
    parsed = parse_reference_id(ref_id)
    if parsed is None:
        return None
    prefix, _index = parsed
    return _PREFIX_TO_SOURCE[prefix]


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
    survived into it (cited in its narrative / supporting signals), the
    ``resolution_outcome`` those refs are scored against, and the
    ``invocation_id`` of the pipeline invocation that generated its parent
    thesis. Upstream reference IDs restart per invocation, so the generating
    invocation disambiguates which window ref a component terminates (ALP-917).
    """

    invocation_id: str
    cited_refs: frozenset[str]
    resolution_outcome: ThesisComponentOutcome | None


@dataclass(frozen=True, slots=True)
class InvocationRefs:
    """One invocation's parsed layer signals — the per-invocation chain inputs.

    Upstream reference IDs (``QR-1``, ``SA-TECH-1``, …) restart per invocation,
    so the chain is assembled per invocation and the refs are kept partitioned
    by their emitting ``invocation_id`` rather than pooled flat across the window
    (ALP-917). Each carries that invocation's synthesizer brief body, the
    universe of upstream refs it retrieved, and the upstream refs its
    decision-layer artifacts cite.
    """

    invocation_id: str
    synthesis_text: str
    universe: frozenset[str]
    decision_citations: frozenset[str]


def assemble_chains(
    *,
    invocation_refs: Iterable[InvocationRefs],
    components: Iterable[ComponentCitation],
) -> tuple[RefChain, ...]:
    """Build the per-ref citation chains (pure) from the per-invocation signals.

    Each invocation's refs resolve their terminus only against components
    generated by that *same* invocation: the terminus map is keyed on
    ``(invocation_id, ref_id)``, not bare ``ref_id``, so a window's ``QR-1`` is
    never scored against an unrelated invocation's ``QR-1`` component — including
    a different invocation inside the same window (ALP-917). One
    :class:`RefChain` is emitted per ``(invocation, ref_id)`` pair; on a
    single-invocation window this is one chain per ref, so the per-source rates
    are unchanged.

    Parameters
    ----------
    invocation_refs:
        Per-invocation parsed layer signals. ``universe`` is every upstream ref
        the invocation retrieved (the per-source-rate denominator — the
        synthesizer retrieval-store keys); refs cited downstream but absent from
        it are unioned in so a directly-retrieved ref is never lost.
        ``synthesis_text`` sets ``cited_in_synthesis``; ``decision_citations``
        sets ``cited_in_decision``.
    components:
        Resolved thesis components, each carrying its generating
        ``invocation_id``; a ref appearing in one carries that component's
        ``resolution_outcome`` as its chain terminus only within that invocation.
    """
    # Terminus map keyed on (invocation_id, ref_id): a component disambiguates a
    # ref only within its own generating invocation.
    outcome_by_key: dict[tuple[str, str], ThesisComponentOutcome | None] = {}
    for component in components:
        for ref_id in component.cited_refs:
            if classify_source(ref_id) is None:
                continue
            key = (component.invocation_id, ref_id)
            # First resolved outcome wins; a None never overwrites a real outcome.
            if outcome_by_key.get(key) is None:
                outcome_by_key[key] = component.resolution_outcome

    chains: list[RefChain] = []
    for refs in invocation_refs:
        inv_id = refs.invocation_id
        synth_cited = set(extract_citations(refs.synthesis_text))
        decision_cited = {r for r in refs.decision_citations if classify_source(r) is not None}

        all_refs = {r for r in refs.universe if classify_source(r) is not None}
        all_refs |= synth_cited | decision_cited
        all_refs |= {ref_id for (key_inv, ref_id) in outcome_by_key if key_inv == inv_id}

        for ref_id in sorted(all_refs):
            source = classify_source(ref_id)
            assert source is not None  # filtered above
            chains.append(
                RefChain(
                    ref=ExtractedRef(ref_id=ref_id, source=source),
                    cited_in_synthesis=ref_id in synth_cited,
                    cited_in_decision=ref_id in decision_cited,
                    thesis_component_outcome=outcome_by_key.get((inv_id, ref_id)),
                )
            )
    return tuple(chains)
