"""Tests for on-demand tool registry — ALP-247.

Verifies the TOOLS registry shape and that each entry carries the required
ToolDefinition fields. No database required for registry-level tests.
"""

from __future__ import annotations

from alphamind.analysis.tools import TOOLS, ToolDefinition


def test_tools_registry_has_exactly_three_entries() -> None:
    assert set(TOOLS.keys()) == {"news_search", "prediction_markets", "earnings_commentary"}


def test_tools_registry_no_social_sentiment() -> None:
    assert "social_sentiment" not in TOOLS


def test_each_tool_is_tool_definition() -> None:
    for name, defn in TOOLS.items():
        assert isinstance(defn, ToolDefinition), f"{name} is not a ToolDefinition"


def test_each_tool_definition_has_required_fields() -> None:
    for name, defn in TOOLS.items():
        assert defn.name == name, f"{name}.name mismatch"
        assert defn.description, f"{name} has no description"
        assert defn.input_model is not None, f"{name} has no input_model"
        assert defn.output_model is not None, f"{name} has no output_model"
        assert callable(defn.callable_factory), f"{name} callable_factory not callable"
