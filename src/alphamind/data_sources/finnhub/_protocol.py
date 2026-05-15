"""Protocol declaration for the Finnhub SDK surface used by AlphaMind.

This Protocol declares the methods AlphaMind invokes on
``finnhub.Client``.  The production wrapper :class:`FinnhubClient` exposes
the SDK via the ``sdk`` property; downstream collectors call methods like
``sdk.company_news(...)``, ``sdk.market_status(...)``.  The Protocol lets
tests substitute :class:`FakeFinnhubSDK` (no live network).
"""

from __future__ import annotations

from typing import Any, Protocol


class FinnhubSDK(Protocol):
    """Methods AlphaMind invokes on ``finnhub.Client``."""

    def market_status(self, exchange: str) -> dict[str, Any]: ...
    def company_news(self, symbol: str, _from: str, to: str) -> list[dict[str, Any]]: ...
    def general_news(self, category: str) -> list[dict[str, Any]]: ...
    def company_eps_estimates(self, symbol: str) -> dict[str, Any]: ...
    def company_revenue_estimates(self, symbol: str) -> dict[str, Any]: ...
    def earnings_calendar(
        self, _from: str, to: str, symbol: str, international: bool = False
    ) -> dict[str, Any]: ...
    def calendar_economic(
        self, _from: str | None = None, to: str | None = None
    ) -> dict[str, Any]: ...
    def ipo_calendar(self, _from: str, to: str) -> dict[str, Any]: ...
    def fda_calendar(self) -> list[dict[str, Any]] | dict[str, Any]: ...
