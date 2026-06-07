"""Pure reference-ID grammar — the canonical producer-side ref taxonomy (ALP-912 / 06g).

The closed set of reference-ID prefixes upstream briefs cite, the bracketed-marker
regex, and the longest-match parser. Extracted from
:mod:`alphamind.analysis.synthesizer.models` so a second consumer — the feedback-loop
citation parser — can import the canonical grammar **without** the sqlalchemy-tainted
transitive path ``models.py`` carries (it pulls ``analysis._shared`` /
``domain_researchers.models``). This module imports only ``enum`` + ``re``; that purity
is what keeps ``feedback_loop.citation.parser → ref_grammar`` clear of the
``feedback-loop-metric-cores-no-sqlalchemy`` import contract.

``models.py`` re-imports :class:`ReferencePrefix`, :data:`REF_ID_RE`, and
:func:`parse_reference_id` from here and re-exports them, so the ALP-531
decision-validator consumers and ``synthesizer/reference_extractor.py`` are unchanged.
"""

from __future__ import annotations

import enum
import re


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


# Bracketed-reference extractor: matches ``[<prefix-segments>-<digits>]``
# anywhere in narrative prose. ``parse_reference_id`` narrows further to the
# canonical synthesizer prefix taxonomy.
REF_ID_RE = re.compile(r"\[([A-Z][A-Z0-9-]*-[0-9]+)\]")


# Longest-match parse table: sub-typed prefixes (`SA-TECH-ANOM`) must beat their
# base prefix (`SA-TECH`) so `SA-TECH-ANOM-3` resolves to (SA_TECH_ANOM, 3) and
# never to (SA_TECH, "ANOM-3"). Sorting by descending value length encodes the
# rule once for any future additions.
_PREFIXES_BY_LENGTH: tuple[ReferencePrefix, ...] = tuple(
    sorted(ReferencePrefix, key=lambda p: len(p.value), reverse=True)
)


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


__all__ = [
    "REF_ID_RE",
    "ReferencePrefix",
    "parse_reference_id",
]
