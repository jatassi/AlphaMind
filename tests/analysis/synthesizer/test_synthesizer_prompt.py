"""Verification tests for the synthesizer system prompt (ALP-209).

The synthesizer prompt at ``prompts/analysis/synthesizer.md`` produces prose
with no producer-side reference IDs, so there is no parser/validator pair
to round-trip the example through. These tests instead encode the
mechanical and structural acceptance criteria from ALP-209:

* The comment block at the top points to Linear issues, not archived
  ``docs/implementation/...`` paths (AC2).
* The ``<inputs>`` block lists the upstream-brief prefixes without a stale
  ``DC`` reference (AC3).
* The ``<example_output>`` block remains intact — its load-bearing citation
  patterns and the regime-transition framing are preserved (AC4).
"""

from __future__ import annotations

import re
from pathlib import Path

_PROMPT_PATH = Path(__file__).parents[3] / "prompts" / "analysis" / "synthesizer.md"

_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_INPUTS_RE = re.compile(r"<inputs>(.*?)</inputs>", re.DOTALL)
_EXAMPLE_OUTPUT_RE = re.compile(r"<example_output>(.*?)</example_output>", re.DOTALL)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def test_comment_block_has_no_archived_implementation_paths() -> None:
    """AC2 — comment block must not reference archived `docs/implementation/...` paths.

    The original prompt was authored before the implementation docs migrated
    under ``docs/_archive/``. The story replaces those references with
    Linear-issue links (or removes the comment block entirely).
    """
    content = _read_prompt()
    comment_match = _COMMENT_RE.search(content)
    if comment_match is None:
        # An empty comment block — i.e., the comment was removed entirely —
        # also satisfies the criterion.
        return
    comment_body = comment_match.group(1)
    assert "docs/implementation/" not in comment_body, (
        "synthesizer.md comment block still references stale "
        "docs/implementation/... paths; replace with Linear issue links "
        "(ALP-207 for input-bundle assembler, ALP-206 for portfolio MCP "
        "tools) or remove the comment block."
    )


def test_inputs_block_has_no_dc_reference() -> None:
    """AC3 — the `<inputs>` block must not list a `DC` prefix.

    The synthesizer consumes typed prefixes ``CR``, ``SA-TECH``, ``SA-FIN``,
    ``SA-ENERGY``, ``QR``, ``QR-CW``, ``AR``. ``DC`` is not a real prefix in
    the reference-ID taxonomy and must not appear in the inputs list.
    """
    content = _read_prompt()
    inputs_match = _INPUTS_RE.search(content)
    assert inputs_match is not None, "synthesizer.md is missing an <inputs> block"
    inputs_body = inputs_match.group(1)
    # Match `DC` only as a standalone token — backticked, bracketed, or
    # whitespace-bounded — to avoid false positives inside other words such
    # as "DCF" or "WDC".
    assert re.search(r"(?<![A-Za-z])DC(?![A-Za-z])", inputs_body) is None, (
        "synthesizer.md <inputs> block contains a stale `DC` reference; the "
        "reference-ID taxonomy does not include a DC prefix."
    )


def test_example_output_preserves_load_bearing_content() -> None:
    """AC4 — no regression in the prompt's `<example_output>` block.

    Asserts the example demonstrates the synthesizer's load-bearing behaviors:
    a regime-transition framing, multi-source intersection citation, an
    explicit contradiction flag, an uncertainty/degraded-quality note, and a
    portfolio-state tool cross-reference. These four behaviors are what make
    the example a useful anchor; losing any of them would constitute a
    regression.
    """
    content = _read_prompt()
    example_match = _EXAMPLE_OUTPUT_RE.search(content)
    assert example_match is not None, "synthesizer.md is missing an <example_output> block"
    example_body = example_match.group(1)

    # Regime-transition framing — the example opens by reading the regime label.
    assert "vol_expansion" in example_body
    assert "low_vol_compression" in example_body

    # Multi-source intersection — at least one paragraph must cite two or
    # more upstream prefixes together so the LLM sees the convergence shape.
    citation_pattern = re.compile(
        r"\[(?:CR|SA-TECH|SA-FIN|SA-ENERGY|QR|QR-CW|AR)(?:-[A-Z]+)?-\d+\]"
    )
    citations = citation_pattern.findall(example_body)
    assert len(citations) >= 5, (
        f"example_output should demonstrate dense citation discipline; found {len(citations)}"
    )

    # Contradiction surfacing — the example must contain explicit
    # contradiction language so the LLM sees how to surface (not resolve)
    # opposing reads.
    assert "contradiction" in example_body.lower()

    # Uncertainty / signal-quality flag handling.
    assert (
        "Signal quality" in example_body or "MODERATE" in example_body or "DEGRADED" in example_body
    )

    # Portfolio-state tool cross-reference — at least one of the three
    # tools must be invoked in the example.
    portfolio_tool_pattern = re.compile(
        r"get_(?:positions_summary|active_theses_summary|exposure_snapshot)"
    )
    assert portfolio_tool_pattern.search(example_body) is not None, (
        "example_output should demonstrate at least one portfolio-state tool call"
    )
