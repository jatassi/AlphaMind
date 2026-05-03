"""Round-trip test for the qualitative-researcher system prompt (ALP-250).

Extracts the ``<example_output>`` block from
``prompts/analysis/qualitative_researcher.md``, parses each ``<output>``
entry with ``parse_qualitative_brief``, and validates it with
``validate_qualitative_brief``.  A green suite confirms the shipped example
output conforms to the contract enforced by the production parser and
validator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alphamind.analysis.qualitative_research.parser import parse_qualitative_brief
from alphamind.analysis.qualitative_research.validation import validate_qualitative_brief

_PROMPTS_DIR = Path(__file__).parents[3] / "prompts" / "analysis"

_OUTPUT_RE = re.compile(r"<output>\s*(.*?)\s*</output>", re.DOTALL)
_EXAMPLE_OUTPUT_RE = re.compile(r"<example_output>(.*?)</example_output>", re.DOTALL)

# Tickers that appear in the example output's catalyst-watch and sentiment-
# snapshot entries.  The validator requires every CatalystWatch.ticker to be
# a member of the universe when a universe is supplied.
_TEST_UNIVERSE: frozenset[str] = frozenset({"JPM", "NVDA", "AMD", "AVGO", "MU"})


def _extract_example_outputs(prompt_path: Path) -> list[str]:
    """Return every ``<output>`` block nested inside ``<example_output>`` in *prompt_path*."""
    content = prompt_path.read_text(encoding="utf-8")
    example_output_match = _EXAMPLE_OUTPUT_RE.search(content)
    if example_output_match is None:
        pytest.fail(f"No <example_output> block found in {prompt_path}")
    example_output_section = example_output_match.group(1)
    outputs = _OUTPUT_RE.findall(example_output_section)
    if not outputs:
        pytest.fail(f"No <output> blocks found inside <example_output> in {prompt_path}")
    return outputs


# ---------------------------------------------------------------------------
# ALP-250 — qualitative researcher
# ---------------------------------------------------------------------------


def test_qualitative_researcher_prompt_round_trip() -> None:
    """Example output in qualitative_researcher.md round-trips through parser and validator."""
    prompt_path = _PROMPTS_DIR / "qualitative_researcher.md"
    examples = _extract_example_outputs(prompt_path)

    for i, example in enumerate(examples):
        brief = parse_qualitative_brief(example, invocation_id=f"inv-round-trip-{i + 1}")
        result = validate_qualitative_brief(brief, universe=_TEST_UNIVERSE)
        assert result.is_valid is True, f"Example {i + 1} failed validation: {result.errors}"
