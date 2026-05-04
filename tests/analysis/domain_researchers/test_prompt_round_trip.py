"""Round-trip tests for domain-researcher system prompts (ALP-195 / ALP-288).

Post-ALP-288 the prompt examples are JSON. Each test extracts the
``<example_output>`` block from a prompt file, ``json.loads`` it to the dict
shape ``ResultMessage.structured_output`` would deliver, then runs the
production parser + validator. A green suite here means the shipped example
matches the schema and Layer-2/3 invariants.
"""

from __future__ import annotations

import json
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
# ALP-193 / ALP-288 — tech & semis researcher
# ---------------------------------------------------------------------------


def test_tech_semis_prompt_round_trip() -> None:
    """Example output in tech_semis_researcher.md round-trips through parser and validator."""
    prompt_path = _PROMPTS_DIR / "tech_semis_researcher.md"
    payload = json.loads(_extract_example_output(prompt_path))

    brief = parse_brief(payload, Sector.TECH_SEMIS)
    result = validate_brief(brief)

    assert result.is_valid is True, f"Validation errors: {result.errors}"


# ---------------------------------------------------------------------------
# ALP-195 / ALP-288 — financials researcher
# ---------------------------------------------------------------------------


def test_financials_prompt_round_trip() -> None:
    """Example output in financials_researcher.md round-trips through parser and validator."""
    prompt_path = _PROMPTS_DIR / "financials_researcher.md"
    payload = json.loads(_extract_example_output(prompt_path))

    brief = parse_brief(payload, Sector.FINANCIALS)
    result = validate_brief(brief)

    assert result.is_valid is True, f"Validation errors: {result.errors}"


# ---------------------------------------------------------------------------
# ALP-197 / ALP-288 — energy researcher
# ---------------------------------------------------------------------------


def test_energy_prompt_round_trip() -> None:
    """Example output in energy_researcher.md round-trips through parser and validator."""
    prompt_path = _PROMPTS_DIR / "energy_researcher.md"
    payload = json.loads(_extract_example_output(prompt_path))

    brief = parse_brief(payload, Sector.ENERGY)
    result = validate_brief(brief)

    assert result.is_valid is True, f"Validation errors: {result.errors}"
