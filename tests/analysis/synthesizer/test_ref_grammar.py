"""Pure reference-ID grammar module (ALP-912 / 06g item A).

``analysis/synthesizer/ref_grammar.py`` is the one source of ``ReferencePrefix`` /
``REF_ID_RE`` / ``parse_reference_id``, importing only ``enum`` + ``re`` so the
feedback-loop citation parser can reuse the canonical producer-side grammar without
pulling the sqlalchemy-tainted transitive path through ``synthesizer/models.py``.
``models.py`` re-exports the three names so its existing consumers are unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

from alphamind.analysis.synthesizer import models, ref_grammar


def test_ref_grammar_is_the_single_source_of_the_three_names() -> None:
    """models.py re-exports the same objects ref_grammar defines (identity, not copies)."""
    assert models.ReferencePrefix is ref_grammar.ReferencePrefix
    assert models.REF_ID_RE is ref_grammar.REF_ID_RE
    assert models.parse_reference_id is ref_grammar.parse_reference_id


def test_ref_grammar_imports_only_enum_and_re() -> None:
    """The pure grammar module imports only the stdlib ``enum`` + ``re`` — no domain deps.

    This is the purity guarantee that lets ``feedback_loop.citation.parser`` import it
    without tripping ``feedback-loop-metric-cores-no-sqlalchemy``.
    """
    source = Path(ref_grammar.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported.add(node.module.split(".")[0])
    imported.discard("__future__")  # compiler directive, not a runtime dependency
    assert imported == {"enum", "re"}


def test_reference_prefix_enum_complete() -> None:
    """ref_grammar.ReferencePrefix exposes the complete 13-member taxonomy."""
    assert {member.value for member in ref_grammar.ReferencePrefix} == {
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


def test_parse_reference_id_longest_match_and_rejection() -> None:
    """Longest-match parse + rejection contract is carried by the pure module."""
    assert ref_grammar.parse_reference_id("SA-TECH-ANOM-3") == (
        ref_grammar.ReferencePrefix.SA_TECH_ANOM,
        3,
    )
    assert ref_grammar.parse_reference_id("QR-CW-1") == (ref_grammar.ReferencePrefix.QR_CW, 1)
    assert ref_grammar.parse_reference_id("SA-TECH-1") == (ref_grammar.ReferencePrefix.SA_TECH, 1)
    # Rejections: unknown prefix, missing index, zero index, empty.
    assert ref_grammar.parse_reference_id("REC-1") is None
    assert ref_grammar.parse_reference_id("SA-TECH") is None
    assert ref_grammar.parse_reference_id("AR-0") is None
    assert ref_grammar.parse_reference_id("") is None


def test_ref_id_re_matches_bracketed_marker() -> None:
    assert ref_grammar.REF_ID_RE.findall("see [SA-TECH-3] and [QR-1]") == ["SA-TECH-3", "QR-1"]
