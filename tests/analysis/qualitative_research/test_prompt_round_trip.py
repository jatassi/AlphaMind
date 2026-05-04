"""Round-trip test for the qualitative-researcher system prompt (ALP-250 / ALP-288).

Post-ALP-288 the prompt example is JSON. This test extracts each ``<output>``
block from ``prompts/analysis/qualitative_researcher.md``, ``json.loads`` it
to the dict shape ``ResultMessage.structured_output`` would deliver, then
runs the production parser + validator. A green suite confirms the shipped
example matches the schema and Layer-2/3 invariants.
"""

from __future__ import annotations

import json
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
# ALP-250 / ALP-288 — qualitative researcher
# ---------------------------------------------------------------------------


def test_qualitative_researcher_prompt_round_trip() -> None:
    """Example output in qualitative_researcher.md round-trips through parser and validator."""
    prompt_path = _PROMPTS_DIR / "qualitative_researcher.md"
    examples = _extract_example_outputs(prompt_path)

    for i, example in enumerate(examples):
        payload = json.loads(example)
        brief = parse_qualitative_brief(payload, invocation_id=f"inv-round-trip-{i + 1}")
        result = validate_qualitative_brief(brief, universe=_TEST_UNIVERSE)
        assert result.is_valid is True, f"Example {i + 1} failed validation: {result.errors}"
