"""Reference-marker extractor — story 04 (ALP-202).

Converts a :attr:`BriefBundle.text` body into a per-reference-ID map of
section text. Given a brief whose body begins like ``[SA-TECH-1] {finding}\\n
  ...\\n[SA-TECH-ANOM-1] {finding}\\n  ...``, the extractor returns a dict
keyed by reference ID where each value is the full section text (header
line plus everything until the next reference ID or end-of-string).

The retrieval store (story 05a) consumes this output to build its lookup
map. See ``docs/design/03-analysis-layer/synthesizer.md`` § Inputs and
``docs/design/testing/llm-output-validation.md`` § Reference-ID taxonomy
for the canonical prefix set this primitive relies on.
"""

from __future__ import annotations

import re

from alphamind.analysis.synthesizer.models import parse_reference_id

__all__ = ["ExtractionError", "extract_references"]


# A reference-header line begins with `[`, contains an uppercase prefix
# of one or more `-`-joined segments, ends with `-<digits>]`. The greedy
# `*` over `(?:-[A-Z]+)` segments matches `SA-TECH-ANOM-3` and `QR-CW-2`
# fully (rather than dropping their sub-typed segments) while still
# matching single-segment prefixes like `CR-1`, `QR-1`, `AR-1`.
_HEADER_RE = re.compile(r"^\[([A-Z]+(?:-[A-Z]+)*-\d+)\]")


class ExtractionError(Exception):
    """Raised when the brief text cannot be parsed into well-formed sections.

    Triggered when a line starts with ``[`` but doesn't match a valid
    prefix in :class:`ReferencePrefix`, or when a reference ID appears
    twice in the same body.
    """

    def __init__(self, message: str, offending_line: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.offending_line = offending_line


def extract_references(brief_text: str) -> dict[str, str]:
    """Split ``brief_text`` into a ``{ref_id: section_text}`` map.

    A section spans from a reference-header line (``[<prefix>-<index>]``)
    up to but not including the next reference-header line or end-of-text.
    Newlines and indentation inside a section are preserved verbatim.

    Raises :class:`ExtractionError` when a line opens with ``[`` but does
    not match a valid prefix in :class:`ReferencePrefix`, or when the same
    reference ID appears twice in the body.
    """
    sections: dict[str, list[str]] = {}
    current_id: str | None = None

    for line in brief_text.splitlines(keepends=True):
        match = _HEADER_RE.match(line)
        if match is not None:
            ref_id = match.group(1)
            if parse_reference_id(ref_id) is None:
                raise ExtractionError(
                    f"unknown reference prefix in {ref_id!r}",
                    offending_line=line.rstrip("\n"),
                )
            if ref_id in sections:
                raise ExtractionError(
                    f"duplicate reference ID {ref_id!r}",
                    offending_line=line.rstrip("\n"),
                )
            sections[ref_id] = [line]
            current_id = ref_id
        elif line.startswith("["):
            raise ExtractionError(
                "line opens with '[' but is not a valid reference header",
                offending_line=line.rstrip("\n"),
            )
        elif current_id is not None:
            sections[current_id].append(line)
        # Lines before the first header are dropped — the design doc has
        # no notion of preamble text in a brief body.

    return {ref_id: "".join(parts) for ref_id, parts in sections.items()}
