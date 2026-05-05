"""Round-trip verification tests for the analyst system prompt (ALP-292).

Mirrors the shape of ``tests/analysis/synthesizer/test_synthesizer_prompt.py``.

Acceptance criteria exercised here:

* AC2: The prompt's ``<example_output>`` populates every required field in
  the schema's ``recommendation`` $def (smoke-check that future schema drift
  forces a prompt update).
* AC3: The file exists and pins the prompt-vs-schema invariant; passes under
  ``uv run pytest -n auto``.

The prompt begins with ``<role>`` per the existing envelope convention.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PROMPT_PATH = Path(__file__).parents[3] / "prompts" / "decision" / "analyst.md"
_SCHEMA_PATH = (
    Path(__file__).parents[3] / "docs" / "design" / "04-decision-layer" / "analyst-output-schema.md"
)

# Required fields in the ``recommendation`` $def per the schema doc.
_RECOMMENDATION_REQUIRED = [
    "recommendation_id",
    "instrument",
    "underlying",
    "sector",
    "conviction_level",
    "entry_order",
    "position_size",
    "target",
    "invalidation_legs",
    "time_expectation_hours",
    "guardrail_validation_result",
    "thesis_narrative",
    "target_rationale",
    "invalidation_rationale",
    "position_size_rationale",
    "counterarguments_acknowledged",
]

_EXAMPLE_OUTPUT_RE = re.compile(r"<example_output>(.*?)</example_output>", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```json(.*?)```", re.DOTALL)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _read_schema_md() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test: prompt begins with <role> envelope marker
# ---------------------------------------------------------------------------


def test_prompt_begins_with_role_envelope() -> None:
    """The prompt's first non-comment, non-whitespace content is ``<role>``.

    AC3: asserts the expected envelope marker (``<role>`` per the existing
    prompt) is present at the start of the prompt body after stripping the
    HTML comment header.
    """
    content = _read_prompt()
    # Strip the leading HTML comment block if present.
    stripped = re.sub(r"<!--.*?-->", "", content, count=1, flags=re.DOTALL).lstrip()
    assert stripped.startswith("<role>"), (
        "prompts/decision/analyst.md does not begin with '<role>' after stripping "
        "the comment header.  The expected envelope marker is missing or misplaced."
    )


# ---------------------------------------------------------------------------
# Test: example_output exists and contains a JSON object
# ---------------------------------------------------------------------------


def test_example_output_block_exists_and_contains_json() -> None:
    """The ``<example_output>`` block exists and wraps at least one JSON object.

    AC2: a missing example would mean no field-coverage smoke-check is possible.
    """
    content = _read_prompt()
    match = _EXAMPLE_OUTPUT_RE.search(content)
    assert match is not None, "prompts/decision/analyst.md is missing an <example_output> block."
    example_body = match.group(1)
    # There should be a JSON fence or a raw JSON object inside.
    has_json_fence = _JSON_FENCE_RE.search(example_body) is not None
    has_raw_brace = "{" in example_body
    assert has_json_fence or has_raw_brace, (
        "The <example_output> block does not appear to contain a JSON object.  "
        "Expected either a ```json...``` fence or a raw '{' opening."
    )


# ---------------------------------------------------------------------------
# Test: example_output populates every required recommendation field
# ---------------------------------------------------------------------------


def test_example_output_contains_all_required_recommendation_fields() -> None:
    """Every required field in the schema's ``recommendation`` $def appears as
    a substring in the prompt's ``<example_output>`` block.

    AC2: if a field is added to the schema's required list and this test is not
    updated, it will fail — forcing a prompt update.
    """
    content = _read_prompt()
    match = _EXAMPLE_OUTPUT_RE.search(content)
    assert match is not None, "prompts/decision/analyst.md is missing an <example_output> block."
    example_body = match.group(1)

    missing = [field for field in _RECOMMENDATION_REQUIRED if f'"{field}"' not in example_body]
    assert not missing, (
        f"The <example_output> block is missing the following required "
        f"recommendation fields: {missing}.  "
        f"Add a realistic example value for each missing field so the "
        f"prompt demonstrates full schema coverage."
    )


# ---------------------------------------------------------------------------
# Test: schema's own required list matches _RECOMMENDATION_REQUIRED
# ---------------------------------------------------------------------------


def test_schema_required_list_matches_test_fixture() -> None:
    """The ``required`` list in the schema's ``recommendation`` $def matches
    ``_RECOMMENDATION_REQUIRED`` in this test file.

    This acts as a tripwire: if the schema gains or loses required fields, this
    test fails and the author must update *both* the prompt example and this
    test's fixture — ensuring AC2 stays in sync with the live schema.
    """
    schema_md = _read_schema_md()
    # Extract the JSON block from the schema markdown.
    json_match = _JSON_FENCE_RE.search(schema_md)
    assert json_match is not None, (
        "analyst-output-schema.md does not contain a ```json...``` fenced block."
    )
    schema_json = json.loads(json_match.group(1))

    recommendation_def = schema_json.get("$defs", {}).get("recommendation", {})
    schema_required: list[str] = sorted(recommendation_def.get("required", []))
    fixture_required: list[str] = sorted(_RECOMMENDATION_REQUIRED)

    assert schema_required == fixture_required, (
        f"Mismatch between schema's recommendation required list and the test "
        f"fixture.\n"
        f"  Schema required:  {schema_required}\n"
        f"  Fixture required: {fixture_required}\n"
        f"Update _RECOMMENDATION_REQUIRED in this file to match the schema, "
        f"then confirm the prompt's <example_output> covers all fields."
    )
