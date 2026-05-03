"""Tests for on-demand tool registry — ALP-247, ALP-261.

Verifies the TOOLS registry shape and that each entry carries the required
ToolDefinition fields. No database required for registry-level tests.

ALP-261 widens the registry from 3 to 8 entries by adding the four simpler
adaptive-research tools (sec_lending, short_interest, earnings_calendar,
macro_data) plus ticker_deep_pull.
"""

from __future__ import annotations

from alphamind.analysis.tools import TOOLS, ToolDefinition
from alphamind.analysis.tools.earnings_calendar import EarningsCalendarInput
from alphamind.analysis.tools.macro_data import MacroDataInput
from alphamind.analysis.tools.sec_lending import SecLendingInput
from alphamind.analysis.tools.short_interest import ShortInterestInput
from alphamind.analysis.tools.ticker_deep_pull import TickerDeepPullInput


def test_tools_registry_has_exactly_eight_entries() -> None:
    assert set(TOOLS.keys()) == {
        "news_search",
        "prediction_markets",
        "earnings_commentary",
        "sec_lending",
        "short_interest",
        "earnings_calendar",
        "macro_data",
        "ticker_deep_pull",
    }


def test_tools_registry_excludes_unregistered_adaptive_tools() -> None:
    """Per parent issue resolutions (A) and (B): ``social_sentiment`` and
    ``options_flow`` are not registered."""
    assert "social_sentiment" not in TOOLS
    assert "options_flow" not in TOOLS


def test_new_adaptive_tools_carry_their_input_models() -> None:
    """Each new entry's ``input_model`` is the Pydantic class declared in
    its source module — guards against silent misalignment when wiring."""
    assert TOOLS["sec_lending"].input_model is SecLendingInput
    assert TOOLS["short_interest"].input_model is ShortInterestInput
    assert TOOLS["earnings_calendar"].input_model is EarningsCalendarInput
    assert TOOLS["macro_data"].input_model is MacroDataInput
    assert TOOLS["ticker_deep_pull"].input_model is TickerDeepPullInput


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
