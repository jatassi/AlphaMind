"""On-demand tool registry for qualitative researcher agents — ALP-247.

Exports ``TOOLS``: a dict keyed by tool name mapping to :class:`ToolDefinition`
instances. Story 04b's harness uses this registry to build the SDK option set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality

__all__ = ["TOOLS", "ToolDefinition", "ToolEnvelope", "ToolQuality"]


@dataclass(frozen=True)
class ToolDefinition:
    """Registry entry for one on-demand tool.

    Story 04b's harness calls ``callable_factory(session)`` to produce a
    typed callable for the SDK option set.
    """

    name: str
    description: str
    input_model: type[Any]
    output_model: type[Any]
    callable_factory: Any


def _build_registry() -> dict[str, ToolDefinition]:
    from alphamind.analysis.tools.earnings_calendar import (
        EarningsCalendarInput,
        EarningsCalendarOutput,
        earnings_calendar_factory,
    )
    from alphamind.analysis.tools.earnings_commentary import (
        EarningsCommentaryInput,
        EarningsCommentaryOutput,
        earnings_commentary_factory,
    )
    from alphamind.analysis.tools.macro_data import (
        MacroDataInput,
        MacroDataOutput,
        macro_data_factory,
    )
    from alphamind.analysis.tools.news_search import (
        NewsSearchInput,
        NewsSearchOutput,
        news_search_factory,
    )
    from alphamind.analysis.tools.prediction_markets import (
        PredictionMarketsInput,
        PredictionMarketsOutput,
        prediction_markets_factory,
    )
    from alphamind.analysis.tools.sec_lending import (
        SecLendingInput,
        SecLendingOutput,
        sec_lending_factory,
    )
    from alphamind.analysis.tools.short_interest import (
        ShortInterestInput,
        ShortInterestOutput,
        short_interest_factory,
    )
    from alphamind.analysis.tools.ticker_deep_pull import (
        TickerDeepPullInput,
        TickerDeepPullOutput,
        ticker_deep_pull_factory,
    )

    return {
        "news_search": ToolDefinition(
            name="news_search",
            description=(
                "Search recent news articles by query text and/or ticker symbols. "
                "Returns ranked articles from the news database filtered by a lookback window."
            ),
            input_model=NewsSearchInput,
            output_model=NewsSearchOutput,
            callable_factory=news_search_factory,
        ),
        "prediction_markets": ToolDefinition(
            name="prediction_markets",
            description=(
                "Query prediction market contracts by keyword or category. "
                "Returns contracts with current probabilities and 24h delta."
            ),
            input_model=PredictionMarketsInput,
            output_model=PredictionMarketsOutput,
            callable_factory=prediction_markets_factory,
        ),
        "earnings_commentary": ToolDefinition(
            name="earnings_commentary",
            description=(
                "Retrieve earnings commentary for a ticker: EPS/revenue results, "
                "price reaction, and post-earnings estimate-revision activity. "
                "Transcript analysis is not yet available (Tier 2 deferred)."
            ),
            input_model=EarningsCommentaryInput,
            output_model=EarningsCommentaryOutput,
            callable_factory=earnings_commentary_factory,
        ),
        "sec_lending": ToolDefinition(
            name="sec_lending",
            description=(
                "Retrieve securities lending market data for specified tickers: "
                "borrow rates, availability, utilization, days-to-cover, and "
                "5-day cost trend."
            ),
            input_model=SecLendingInput,
            output_model=SecLendingOutput,
            callable_factory=sec_lending_factory,
        ),
        "short_interest": ToolDefinition(
            name="short_interest",
            description=(
                "Retrieve short interest, short-volume ratio, days-to-cover, "
                "borrow cost, and a deterministic squeeze composite for "
                "specified tickers."
            ),
            input_model=ShortInterestInput,
            output_model=ShortInterestOutput,
            callable_factory=short_interest_factory,
        ),
        "earnings_calendar": ToolDefinition(
            name="earnings_calendar",
            description=(
                "Retrieve upcoming and most-recent earnings event metadata for "
                "specified tickers: next report date, consensus EPS, revision "
                "trend, last reported date."
            ),
            input_model=EarningsCalendarInput,
            output_model=EarningsCalendarOutput,
            callable_factory=earnings_calendar_factory,
        ),
        "macro_data": ToolDefinition(
            name="macro_data",
            description=(
                "Look up a specific macro indicator series (treasury yields, "
                "credit spreads, etc.) with values, day-over-day and 5-day "
                "changes, and trailing-1y percentile rank."
            ),
            input_model=MacroDataInput,
            output_model=MacroDataOutput,
            callable_factory=macro_data_factory,
        ),
        "ticker_deep_pull": ToolDefinition(
            name="ticker_deep_pull",
            description=(
                "Retrieve a multi-category data snapshot for a single ticker, "
                "selecting from price_volume, short_data, earnings, "
                "macro_context. Use when investigating a specific ticker "
                "anomaly that needs more granular data than routine ingestion "
                "delivers."
            ),
            input_model=TickerDeepPullInput,
            output_model=TickerDeepPullOutput,
            callable_factory=ticker_deep_pull_factory,
        ),
    }


TOOLS: dict[str, ToolDefinition] = _build_registry()
