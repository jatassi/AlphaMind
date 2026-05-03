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
    from alphamind.analysis.tools.earnings_commentary import (
        EarningsCommentaryInput,
        EarningsCommentaryOutput,
        earnings_commentary_factory,
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
    }


TOOLS: dict[str, ToolDefinition] = _build_registry()
