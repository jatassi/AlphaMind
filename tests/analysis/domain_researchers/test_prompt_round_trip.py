"""Round-trip tests for domain-researcher system prompts (ALP-195 et al.).

Each test extracts the ``<example_output>`` block from a prompt file, parses
it with ``parse_brief``, and validates it with ``validate_brief``.  A green
suite here means the shipped example output conforms to the contract enforced
by the production parser and validator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.parser import parse_brief
from alphamind.analysis.domain_researchers.validation import validate_brief

_PROMPTS_DIR = Path(__file__).parents[3] / "prompts" / "analysis"

_OUTPUT_RE = re.compile(r"<output>\s*(.*?)\s*</output>", re.DOTALL)


def _extract_example_output(prompt_path: Path) -> str:
    """Return the text inside the first ``<output>`` block of *prompt_path*."""
    content = prompt_path.read_text(encoding="utf-8")
    m = _OUTPUT_RE.search(content)
    if m is None:
        pytest.fail(f"No <output> block found in {prompt_path}")
    return m.group(1)


# ---------------------------------------------------------------------------
# ALP-195 — financials researcher
# ---------------------------------------------------------------------------


def test_financials_prompt_round_trip() -> None:
    """Example output in financials_researcher.md round-trips through parser and validator."""
    prompt_path = _PROMPTS_DIR / "financials_researcher.md"
    example = _extract_example_output(prompt_path)

    brief = parse_brief(example, Sector.FINANCIALS)
    result = validate_brief(brief)

    assert result.is_valid is True, f"Validation errors: {result.errors}"
